"""Bounded receive pass using the center's existing ProductHost executor/ledger."""

import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

from matrix_client import canonical, require

TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted", "persistence_failed"}


def poll(bridge, host, call, factory):
    require(host.get("enable_poll") is True, "product_job_poll_disabled")
    require(
        host.get("runtime_ref") == bridge.client.binding["session_id"],
        "product_job_runtime_mismatch",
    )
    require(
        isinstance(host.get("host_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", host["host_id"]),
        "invalid_product_host",
    )
    relay = factory(host, bridge, call)
    cursor_key = "product_job_cursor:" + host["host_id"]
    with bridge.transaction() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (cursor_key,)).fetchone()
        cursor = row[0] if row else ""
    page = call("/v1/product-job-outbox?" + urlencode(dict(host_id=host["host_id"], after=cursor)))
    require(
        page.get("host_id") == host["host_id"]
        and isinstance(page.get("jobs"), list)
        and len(page["jobs"]) <= 100
        and type(page.get("has_more")) is bool,
        "invalid_product_job_page",
    )
    previous = cursor
    for item in page["jobs"]:
        require(
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", item["id"])
            and item["id"] > previous,
            "invalid_product_job_cursor",
        )
        previous = item["id"]
    require(
        page.get("next_cursor") == previous and (bool(page["jobs"]) or not page["has_more"]),
        "invalid_product_job_cursor",
    )
    # No new claim while this page reports an existing unfinished seat task.
    # Center claim/begin remains authoritative even if state changes after this read.
    busy = any(
        x.get("runtime_ref") == host["runtime_ref"]
        and x.get("ledger_instance")
        and x.get("state") not in TERMINAL
        for x in page["jobs"]
    )
    items, errors = [], []
    scanned, called, claimed = cursor, 0, False
    for job in page["jobs"]:
        if called >= 4:
            break
        scanned = job["id"]
        if any(
            job.get(key) != host.get(key)
            for key in ("host_id", "adapter_id", "adapter_version", "runtime_ref")
        ):
            continue  # Old runtime history is never adopted by this receiver.
        if not job.get("ledger_instance"):
            if not job.get("can_claim") or busy or claimed:
                continue
            claimed = True  # At most one new execution intent in a receive pass.
        elif job["ledger_instance"] != relay.instance:
            continue
        try:
            called += 1
            result = relay.run_once(job["id"])
            require(
                isinstance(result, dict) and result.get("id", result.get("job_id")) == job["id"],
                "invalid_product_host_result",
            )
            items.append(
                dict(
                    job_id=job["id"],
                    **{
                        key: result[key]
                        for key in (
                            "state",
                            "last_fact",
                            "review_state",
                            "product_acceptance",
                            "cancel_requested",
                            "cancel_supported",
                            "receipt_pending",
                            "receipt_sync",
                        )
                        if key in result
                    },
                )
            )
        except Exception as error:
            code = str(error) if type(error).__name__ in ("BridgeError", "RelayError") else ""
            errors.append(
                {
                    "job_id": job["id"],
                    "error": code
                    if re.fullmatch(r"[a-z0-9_]{1,100}", code)
                    else "product_job_unavailable_query_original_id",
                }
            )
    next_cursor = scanned if scanned != previous or page["has_more"] else ""
    with bridge.transaction() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (cursor_key, next_cursor))
    return dict(
        items=items,
        errors=errors,
        next_cursor=next_cursor,
        has_more=bool(next_cursor),
        source="original_center_product_host",
        new_intent_limit=1,
    )


def run(bridge, reference):
    path = Path(reference)
    require(
        path.is_absolute() and path.is_file() and path.stat().st_size <= 131072,
        "explicit_product_host_required",
    )
    host = json.loads(path.read_text(encoding="utf-8-sig"))
    require(
        isinstance(host.get("ledger_file"), str) and Path(host["ledger_file"]).is_absolute(),
        "explicit_product_ledger_required",
    )
    require(
        isinstance(host.get("inputs"), dict)
        and len(host["inputs"]) <= 256
        and all(isinstance(x, str) and Path(x).is_absolute() for x in host["inputs"].values()),
        "explicit_product_inputs_required",
    )
    client = Path(host["center_client_dir"])
    require(
        client.is_absolute() and (client / "product_host.py").is_file(),
        "explicit_center_product_host_required",
    )
    require(
        Path(host["bridge_module_dir"]).resolve() == Path(__file__).resolve().parent,
        "product_bridge_module_mismatch",
    )
    config = Path(host["bridge_config_ref"])
    require(
        config.is_absolute() and config.is_file() and config.stat().st_size <= 32768,
        "explicit_bridge_config_required",
    )
    require(
        canonical(json.loads(config.read_text(encoding="utf-8-sig"))) == canonical(bridge.config),
        "product_bridge_policy_changed",
    )
    sys.path.insert(0, str(client))
    import coord
    from product_host import ProductHost
    from human_relay import HumanRelay

    def call(route, body=None):
        return coord.call(host["center_identity"], route, body)

    require(host.get("enable_poll") is True, "product_job_poll_disabled")
    # Same runtime observation as human callbacks, even if no human project is enabled.
    # Registry idle means the receiver remains available; waiting_user is a task state.
    HumanRelay(bridge, host, call).observe_runtime()
    return poll(bridge, host, call, ProductHost)
