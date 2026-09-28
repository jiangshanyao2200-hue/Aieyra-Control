"""Bounded project views over local messages and one-page legacy center reads."""

from __future__ import annotations

import hashlib
import json
import re

MAX_SEQ = 9223372036854775807
PROJECT = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")
EPOCH = re.compile(r"[a-f0-9]{64}\Z")


class PageError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def parse(route, query):
    cursor = "after" if route == "inbox" else "before"
    if set(query) - {cursor, "limit", "project", "include_coordination", "stream_epoch"}:
        raise PageError("invalid_center_filter_query")

    def single(key, default):
        raw = query.get(key, [default])
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str):
            raise PageError("invalid_center_filter_query")
        return raw[0]

    project = single("project", "")
    include = single("include_coordination", "0")
    if not PROJECT.fullmatch(project) or include not in ("0", "1"):
        raise PageError("invalid_center_filter_query")
    epoch = single("stream_epoch", "")
    if "stream_epoch" in query and not EPOCH.fullmatch(epoch):
        raise PageError("invalid_center_stream_epoch")
    values = {}
    for key, default, minimum, maximum in (
        (cursor, "0" if route == "inbox" else str(MAX_SEQ), 0 if route == "inbox" else 1, MAX_SEQ),
        ("limit", "50" if route == "inbox" else "20", 1, 100),
    ):
        raw = single(key, default)
        if not re.fullmatch(r"[0-9]{1,19}", raw) or not minimum <= int(raw) <= maximum:
            raise PageError("invalid_center_filter_query")
        values[key] = int(raw)
    return {
        **values,
        "project": project,
        "include_coordination": include == "1",
        "stream_epoch": epoch or None,
    }


def metadata(route, value, mode):
    return {
        "filter_mode": mode,
        "filter": {k: value[k] for k in ("project", "include_coordination")},
        "order": "ascending" if route == "inbox" else "descending",
        "limit": value["limit"],
        "reading_does_not_acknowledge": True,
    }


def projects(value):
    return tuple(
        dict.fromkeys(
            [value["project"], *(["coordination"] if value["include_coordination"] else [])]
        )
    )


def local_page(client, peer, route, value):
    # Resolve the enrolled identity and check revocation inside the same snapshot
    # as the project, head cursor and rows. This never reads as the local owner.
    with client.store.db() as db:
        db.execute("BEGIN")
        actor = db.execute(
            "SELECT a.id FROM local_identities i JOIN actors a ON a.id=i.actor_id "
            "WHERE i.alias=? AND a.id=? AND a.revoked=0",
            (peer["identity"], peer["actor_id"]),
        ).fetchone()
        if not actor:
            raise PageError("unauthorized", 401)
        if not db.execute(
            "SELECT 1 FROM registered_projects WHERE id=?", (value["project"],)
        ).fetchone():
            raise PageError("unknown_project")
        head = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0]
        selected = projects(value)
        seed = db.execute("SELECT epoch FROM control_message_stream WHERE id=1").fetchone()
        if not seed or not EPOCH.fullmatch(seed[0]):
            raise PageError("center_stream_epoch_unavailable", 503)
        epoch = hashlib.sha256(
            json.dumps(["center-view/1", seed[0], sorted(selected)], separators=(",", ":")).encode()
        ).hexdigest()
        expected = value.get("stream_epoch")
        if expected is not None and expected != epoch:
            raise PageError("center_stream_epoch_mismatch", 409)
        if expected is not None and route == "inbox" and value["after"] > head:
            raise PageError("center_cursor_ahead_of_stream", 409)
        descending = route == "history"
        cursor = "before" if descending else "after"
        where = (
            "m.project IN ("
            + ",".join("?" for _ in selected)
            + ") AND m.seq"
            + ("<?" if descending else ">?")
        )
        rows = client.store.messages(
            db, dict(actor), where, (*selected, value[cursor]), descending, value["limit"] + 1
        )
    page = rows[: value["limit"]]
    more = len(rows) > len(page)
    result = {
        **metadata(route, value, "server"),
        "messages": page,
        "has_more": more,
        "snapshot_cursor": head,
        "stream_epoch": epoch,
        "epoch_checked": expected is not None,
        "scanned_count": len(page),
    }
    if descending:
        result["next_before"] = page[-1]["seq"] if more else None
    else:
        result["next_cursor"] = page[-1]["seq"] if more else max(value["after"], head)
    return result


def legacy_page(remote, peer, route, value):
    # A legacy deployment is explicit configuration. Never silently scan multiple
    # message pages or pretend a client-side selection is database filtering.
    registry = remote(peer["identity"], "registry")
    registered = registry.get("projects")
    if not isinstance(registered, list) or any(not isinstance(p, dict) for p in registered):
        raise PageError("invalid_center_filter_response", 502)
    if not any(p.get("id") == value["project"] for p in registered):
        raise PageError("unknown_project")
    if value.get("stream_epoch") is not None:
        raise PageError("center_stream_epoch_unavailable", 501)
    descending = route == "history"
    cursor = "before" if descending else "after"
    query = {cursor: [str(value[cursor])]}
    if not descending:
        query["limit"] = [str(min(value["limit"] + 1, 100))]
    raw = remote(peer["identity"], route, query=query)
    rows = raw.get("messages")
    if (
        not isinstance(rows, list)
        or len(rows) > 100
        or any(
            not isinstance(row, dict)
            or type(row.get("seq")) is not int
            or not 1 <= row["seq"] <= MAX_SEQ
            or not isinstance(row.get("project"), str)
            for row in rows
        )
    ):
        raise PageError("invalid_center_filter_response", 502)
    seqs = [value[cursor], *[r["seq"] for r in rows]]
    if any(not (a > b if descending else a < b) for a, b in zip(seqs, seqs[1:])):
        raise PageError("invalid_center_filter_response", 502)
    if "has_more" in raw and type(raw["has_more"]) is not bool:
        raise PageError("invalid_center_filter_response", 502)
    scanned = rows[: value["limit"]]
    more = len(rows) > len(scanned) or raw.get("has_more", len(rows) == 100)
    if more and not scanned:
        raise PageError("invalid_center_filter_response", 502)
    result = {
        **metadata(route, value, "legacy_scan"),
        "messages": [r for r in scanned if r["project"] in projects(value)],
        "scanned_count": len(scanned),
        "has_more": more,
        "snapshot_cursor": None,
        "stream_epoch": None,
        "epoch_checked": False,
    }
    if descending:
        result["next_before"] = scanned[-1]["seq"] if more else None
    else:
        end = scanned[-1]["seq"] if scanned else value["after"]
        original = raw.get("next_cursor")
        if (
            type(original) is not int
            or not (rows[-1]["seq"] if rows else value["after"]) <= original <= MAX_SEQ
        ):
            raise PageError("invalid_center_filter_response", 502)
        result["next_cursor"] = end if len(rows) > len(scanned) else original
    return result
