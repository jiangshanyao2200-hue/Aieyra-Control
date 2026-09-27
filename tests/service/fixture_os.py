"""Synthetic OS contract fixture. No model, shell tool, UI, or product config."""

from collections import Counter
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service/product_bridge"))
from bridge import now, read_artifact
from matrix_client import BridgeError, canonical


class FakeOS:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alpha-product-os-fixture-")
        self.root = Path(self.temp.name)
        self.records = {}
        self.exchanges = {}
        self.submissions = {}
        self.calls = Counter()
        self.activity = []
        self.archive = b""
        self.receipts = {}
        self.drop_after = Counter()
        self.drop_before = Counter()
        self.reject = set()
        self.redirect = set()
        self.malformed = set()
        self.lock = threading.RLock()
        self._token = secrets.token_hex(32)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if (
                    self.headers.get("Authorization") != "Bearer " + fixture._token
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or self.headers.get("Origin")
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                ):
                    self.reply(403, {"ok": False, "error": "unauthorized"})
                    return
                try:
                    if (
                        self.path != "/v1/rpc"
                        or int(self.headers.get("Content-Length", "0")) > 1 << 20
                    ):
                        raise BridgeError("invalid_request")
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if set(request) != {"action", "arguments"}:
                        raise BridgeError("invalid_request")
                    action = request["action"]
                    with fixture.lock:
                        fixture.calls[action] += 1
                        if fixture.drop_before[action]:
                            fixture.drop_before[action] -= 1
                            self.drop()
                            return
                        if action in fixture.redirect:
                            self.send_response(307)
                            self.send_header("Location", "http://127.0.0.1:1/leak")
                            self.end_headers()
                            return
                        if action in fixture.reject:
                            self.reply(200, {"ok": False, "error": fixture._token})
                            return
                        if action in fixture.malformed:
                            self.reply(200, {"ok": True, "data": "wrong shape"})
                            return
                        result = fixture.call(action, request["arguments"])
                        if fixture.drop_after[action]:
                            fixture.drop_after[action] -= 1
                            self.drop()
                            return
                    self.reply(200, {"ok": True, "data": result})
                except (BridgeError, ValueError, KeyError, TypeError) as exc:
                    self.reply(400, {"ok": False, "error": str(exc)})

            def drop(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()

            def reply(self, status, value):
                body = canonical(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.discovery = self.root / "fixture-discovery.json"
        self.discovery.write_text(
            canonical(
                {
                    "version": 1,
                    "session_id": "fixture-matrix",
                    "pid": os.getpid(),
                    "executable": str(Path(sys.executable).resolve()),
                    "url": f"http://127.0.0.1:{self.server.server_port}/v1/rpc",
                    "created_at": now(),
                    "token": self._token,
                    "fixture": True,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "work").mkdir()
        self.config = {
            "version": 1,
            "mode": "fixture",
            "discovery_ref": str(self.discovery),
            "expected_session": "fixture-matrix",
            "expected_pid": os.getpid(),
            "expected_executable": str(Path(sys.executable).resolve()),
            "work_root": str(self.root / "work"),
            "state_dir": str(self.root / "state"),
            "allowed_models": ["fixture-model"],
            "allowed_tools": ["exec_command", "apply_patch"],
            "timeout_seconds": 2,
        }
        self.config_path = self.root / "client.json"
        self.config_path.write_text(canonical(self.config), encoding="utf-8")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, action, args):
        if action == "describe":
            return {
                "version": 1,
                "session_id": "fixture-matrix",
                "fixture": True,
                "generation_guarded_commands": True,
                "actions": [
                    "matrix.status",
                    "matrix.send",
                    "matrix.cancel",
                    "agents.create",
                    "agents.list",
                    "agents.status",
                    "agents.activity",
                    "agents.send",
                    "agents.cancel",
                ],
            }
        if action == "matrix.status":
            if args.get("request_id"):
                return deepcopy(self.exchanges[args["request_id"]])
            return {
                "busy": any(r["state"] == "running" for r in self.exchanges.values()),
                "requests": deepcopy(list(self.exchanges.values())),
            }
        if action == "matrix.send":
            rid, message = args["request_id"], args["message"]
            sha = hashlib.sha256(message.encode()).hexdigest()
            if rid in self.exchanges:
                if self.exchanges[rid]["message_hash"] != sha:
                    raise BridgeError("request_id_conflict")
                return deepcopy(self.exchanges[rid])
            if any(r["state"] == "running" for r in self.exchanges.values()):
                raise BridgeError("matrix_busy")
            self.exchanges[rid] = {
                "request_id": rid,
                "message_hash": sha,
                "state": "running",
                "started_at": now(),
                "updated_at": now(),
                "model": "fixture-model",
            }
            return deepcopy(self.exchanges[rid])
        if action == "matrix.cancel":
            record = self.exchanges[args["request_id"]]
            if record["state"] != "running":
                raise BridgeError("not_active")
            record.update(state="cancelled", updated_at=now())
            return deepcopy(record)
        if action == "agents.models":
            return [{"id": "fixture-model", "model": "synthetic-no-provider"}]
        if action == "agents.create":
            for r in self.records.values():
                if r["spec"]["request_id"] == args["request_id"]:
                    if self.submissions[args["request_id"]] != args:
                        raise BridgeError("request_id_conflict")
                    return deepcopy(r)
            agent_id = f"fixture-agent-{len(self.records) + 1}"
            spec = deepcopy(args)
            if not spec["tools"]:
                spec["tools"] = None  # Real Go AgentSpec marshals a nil slice as null.
            if not spec["title"]:
                del spec["title"]
            for field in ("constraints", "deliverables"):
                if not spec["contract"][field]:
                    del spec["contract"][field]
            r = {
                "id": agent_id,
                "parent": "fixture-matrix",
                "depth": 1,
                "spec": spec,
                "state": "queued",
                "created": now(),
                "updated": now(),
                "acceptance": "pending",
                "generation": 1,
                "usage": [],
                "reviews": [],
                "turns": {args["request_id"]: args["task"]},
            }
            self.records[agent_id] = r
            self.submissions[args["request_id"]] = deepcopy(args)
            return deepcopy(r)
        if action == "agents.list":
            # Real OS summaries deliberately omit request_id and spec.
            return [
                {k: r[k] for k in ("id", "parent", "state", "acceptance")}
                for r in self.records.values()
            ]
        record = self.records[args["id"]]
        if action == "agents.send":
            if args["request_id"] in record["turns"]:
                if record["turns"][args["request_id"]] != args["message"]:
                    raise BridgeError("request_id_conflict")
                return deepcopy(record)
            if args.get("generation") != record["generation"] or record["state"] in {
                "queued",
                "running",
                "cancelling",
            }:
                raise BridgeError("busy_or_generation_changed")
            record["turns"][args["request_id"]] = args["message"]
            record.update(
                generation=record["generation"] + 1,
                state="queued",
                acceptance="pending",
                updated=now(),
                delivery=None,
            )
            return deepcopy(record)
        if action == "agents.cancel":
            if args.get("generation") != record["generation"]:
                raise BridgeError("generation_changed")
            if record["state"] in {"queued", "running", "cancelling"}:
                record.update(state="cancelled", updated=now())
            return {"id": args["id"], "requested": True}
        if action in {"agents.status", "agents.wait"}:
            return deepcopy(record)
        if action == "agents.activity":
            since = args.get("since", 0)
            items = [a for a in self.activity if a["revision"] > since]
            return {
                "items": deepcopy(items),
                "cursor": max([since, *[a["revision"] for a in items]]),
                "has_more": False,
                "page": "delta" if since else "latest",
                "state": record["state"],
            }
        if action == "agents.events":
            cursor = args.get("cursor", 0)
            if not 0 <= cursor <= len(self.archive) or cursor and self.archive[cursor - 1] != 10:
                raise BridgeError("invalid_cursor")
            rows = []
            for line in self.archive[cursor:].splitlines(keepends=True):
                if not line.endswith(b"\n") or len(rows) >= args.get("limit", 20):
                    break
                rows.append(json.loads(line))
                cursor += len(line)
            return {"events": rows, "next_cursor": cursor, "has_more": cursor < len(self.archive)}
        if action == "agents.review":
            if args["generation"] != record["generation"] or record["state"] != "completed":
                raise BridgeError("stale_or_unfinished")
            delivery = record["delivery"]
            if len(args["checks"]) != len(record["spec"]["contract"]["acceptance"]):
                raise BridgeError("missing_checks")
            for c in args["checks"]:
                if c["verdict"] != "passed" or not c["receipts"]:
                    raise BridgeError("missing_receipts")
                for ref in c["receipts"]:
                    source = ref.get("agent_id", "fixture-matrix")
                    receipt = self.receipts.get((source, ref["session_id"]))
                    if (
                        source != "fixture-matrix"
                        or not receipt
                        or receipt["started_at"] < delivery["captured_at"]
                        or receipt["state"] != "exited"
                        or receipt["exit_code"] != ref["expected_exit"]
                    ):
                        raise BridgeError("independent_receipt_required")
            for artifact in delivery["artifacts"]:
                read_artifact(record["spec"]["workdir"], artifact)
            record["reviews"].append(
                {k: v for k, v in args.items() if k != "id"} | {"created_at": now()}
            )
            record.update(acceptance="accepted", updated=now())
            return deepcopy(record)
        raise BridgeError("unknown_fixture_action")

    def running(self, agent_id):
        with self.lock:
            self.records[agent_id].update(state="running", updated=now())

    def complete(self, agent_id, contents=b"alpha fixture output\n"):
        with self.lock:
            r = self.records[agent_id]
            artifacts = []
            for relative in r["spec"]["contract"]["deliverables"]:
                path = Path(r["spec"]["workdir"]) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
                artifacts.append(
                    {
                        "path": relative,
                        "bytes": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                    }
                )
            r.update(
                state="completed",
                result="Synthetic fixture task completed; no model called",
                updated=now(),
                delivery={
                    "generation": r["generation"],
                    "captured_at": now(),
                    "artifacts": artifacts,
                },
            )

    def independent_receipt(self, session_id=7001, **overrides):
        # Only a fixture control method. No real shell receipt files are created.
        self.receipts[("fixture-matrix", session_id)] = {
            "synthetic": True,
            "started_at": now(),
            "state": "exited",
            "exit_code": 0,
            **overrides,
        }
        return {"agent_id": "fixture-matrix", "session_id": session_id, "expected_exit": 0}


def sample_task(tid="alpha-fixture-01"):
    return {
        "version": 1,
        "task_id": tid,
        "model_id": "fixture-model",
        "task": "Produce the isolated fixture deliverable.",
        "prompt": "Fixture only. No provider or window.",
        "tools": [],
        "contract": {
            "acceptance": ["The output exactly matches the independent reference bytes."],
            "deliverables": ["result.txt"],
            "constraints": ["Do not invoke a model, shell tool, or UI."],
        },
    }
