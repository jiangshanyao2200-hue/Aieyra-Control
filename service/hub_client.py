"""Legacy center transport adapter; local mode supplies its own hub."""

import hashlib
import importlib
import json
import sys
import time
from human_requests import HumanError
from request_intake import ROUTES as INTAKE_READS
from service_common import REGISTRY_ACTIONS
from requirement_delivery import canonical
from service_common import ID, Problem


class Hub:
    def __init__(self, config):
        self.config = config
        sys.path.insert(0, config["hub_client_dir"])
        self.client = importlib.import_module("coord")

    def status(self):
        return self.client.call(self.config["hub_read_identity"], "/v1/status")

    def chat_history(self, before=None):
        if getattr(self, "is_local", False):
            return self.client.chat_history(before)
        result = self.client.call(
            self.config["hub_read_identity"],
            "/v1/history" + ("?before=" + str(before) if before else ""),
        )
        end = time.time()
        start = end - 86400
        raw = result.get("messages")
        if not isinstance(raw, list):
            raise ValueError("invalid_chat_history")
        rows = [
            m
            for m in raw
            if isinstance(m, dict)
            and isinstance(m.get("created"), (int, float))
            and start <= m["created"] <= end
        ]
        more = bool(raw and result.get("has_more") and raw[-1].get("created", 0) >= start)
        return {
            "messages": list(reversed(rows)),
            "has_more": more,
            "next_before": raw[-1]["seq"] if more else None,
            "window_start": start,
            "window_end": end,
            "retention_hours": 24,
        }

    def agent_call(self, identity, path, body=None):
        return self.client.call(identity, path, body)

    def agent_provision(self, document):
        """Keep center credentials in the existing OS-user-bound vault."""
        identity = document["local_identity"]
        if identity:
            actor = self.client.call(identity, "/v1/status")["actor"]
            return {"identity": identity, "actor_id": actor["id"]}
        seed = hashlib.sha256(document["request_id"].encode()).hexdigest()[:24]
        identity = "control-agent-" + seed
        owner = self.config.get("hub_user_identity")
        if not owner:
            raise ValueError("503:agent_owner_identity_not_configured")
        # Registration replay intentionally omits the token. Reuse the vault first.
        try:
            result = self.client.load(identity)
        except FileNotFoundError:
            result = None
        if result is None:
            for generation in range(4):
                result = self.client.call(
                    owner,
                    "/v1/register",
                    {
                        "request_id": "control-enroll-" + seed + "-" + str(generation),
                        "name": document["name"],
                        "project": document["project"],
                    },
                )
                if result.get("token"):
                    self.client.save(identity, result)
                    break
                # First registration response was lost before DPAPI persistence.
                # Retire that undelivered identity before allocating its replacement.
                self.client.call(
                    owner,
                    "/v1/revoke",
                    {
                        "request_id": "control-orphan-" + seed + "-" + str(generation),
                        "id": result["id"],
                    },
                )
            else:
                raise ValueError("503:agent_registration_recovery_exhausted")
        actor = self.client.call(identity, "/v1/status")["actor"]
        if actor["id"] != result["id"] or actor["role"] != "agent":
            raise ValueError("403:dedicated_agent_identity_required")
        host, adapter, seat = "ext-host-" + seed, "ext-adapter-" + seed, "ext-seat-" + seed
        operations = [
            (
                "host-register",
                {
                    "id": host,
                    "version": 0,
                    "name": document["name"],
                    "actor_id": actor["id"],
                    "projects": [document["project"]],
                    "max_active": 1,
                    "max_seats": 8,
                },
            ),
            (
                "adapter-register",
                {
                    "id": adapter,
                    "version": 0,
                    "host_id": host,
                    "name": document["name"],
                    "kind": "generic-agent",
                    "version_label": "aieyra-agent/1",
                    "actions": ["inspect", "dispatch"],
                    "availability": "available",
                },
            ),
            (
                "seat-create",
                {
                    "id": seat,
                    "version": 0,
                    "name": document["name"],
                    "project": document["project"],
                    "scope": "external/" + seed,
                    "actor_id": actor["id"],
                    "host_id": host,
                    "adapter_id": adapter,
                    "ownership": "external",
                },
            ),
        ]
        for action, payload in operations:
            self.client.call(
                owner, "/v1/" + action, {**payload, "request_id": "control-" + seed + "-" + action}
            )
        return {"identity": identity, "actor_id": actor["id"], "seat_id": seat}

    def intake_read(self, route, fields):
        from urllib.parse import urlencode

        if route not in INTAKE_READS:
            raise ValueError("invalid_intake_route")
        query = urlencode(fields)
        return self.client.call(
            self.config["hub_read_identity"], "/v1/" + route + ("?" + query if query else "")
        )

    def intake_record(self, payload):
        return self.client.call(
            self.config["hub_read_identity"], "/v1/requirement-receipt", payload
        )

    def intake_receipt(self, request_id):
        return self.intake_read("intake-receipt", {"request_id": request_id})

    def human_snapshot(self, project, after=""):
        identity = self.config.get("hub_user_identity", self.config["hub_read_identity"])
        return self.client.call(
            identity,
            "/v1/collaboration/snapshot?project="
            + project
            + ("&after_human=" + after if after else ""),
        )

    def human_get(self, ident):
        identity = self.config.get("hub_user_identity", self.config["hub_read_identity"])
        return self.client.call(identity, "/v1/human-request?id=" + ident)

    def human_decide(self, payload):
        identity = self.config.get("hub_user_identity")
        if not identity:
            raise HumanError("human_owner_identity_not_configured", 503)
        return self.client.call(identity, "/v1/human-request-decide", payload)

    def registry(self):
        # The registry includes capability flags and host-management visibility.
        # Keep the identity explicit and backend-only; default to the management
        # identity so the browser does not see a read-only projection while the
        # same local console is authorized to manage seats.
        identity = self.config.get(
            "hub_registry_identity",
            self.config.get("hub_management_identity", self.config["hub_read_identity"]),
        )
        return self.client.call(identity, "/v1/registry")

    def host_operations(self, host_id):
        if not isinstance(host_id, str) or not ID.fullmatch(host_id):
            raise Problem("invalid_host_id", "工位主机编号无效。")
        identity = self.config.get(
            "hub_operations_identity",
            self.config.get("hub_management_identity", self.config.get("hub_read_identity")),
        )
        return self.client.call(identity, "/v1/host-operations?host_id=" + host_id)

    def management(self, action, payload):
        # The browser never chooses an arbitrary cloud path. The center remains the
        # authority for role, scope, capacity, epoch and receipt validation.
        if action not in REGISTRY_ACTIONS:
            raise Problem("unsupported_management_action", "管理动作不在白名单内。", 400)
        if not isinstance(payload, dict):
            raise Problem("invalid_management_payload", "管理动作需为对象。", 400)
        identity = self.config.get("hub_management_identity", self.config.get("hub_user_identity"))
        return self.client.call(identity, "/v1/" + action, payload)

    def reconnect(self):
        # Reuse the center's authenticated tunnel recovery, including its existing fallback.
        return self.client.start()

    def message(self, delivery):
        # 此入口由本机用户主动发送，身份沿已有站长控制台；Agent后台汇报使用各自身份。
        if delivery.get("intake_context"):
            # Requirement delivery keeps its private body in the original local
            # ledger/native thread. The maintenance actor reports metadata only.
            return self.client.call(
                self.config["hub_read_identity"],
                "/v1/message",
                {
                    "project": "coordination",
                    "kind": "progress",
                    "request_id": "control:" + delivery["id"],
                    "body": canonical(
                        {
                            "control_delivery": delivery["id"],
                            "context": json.loads(delivery["intake_context"]),
                            "body_sha256": delivery["body_sha256"],
                            "state": "dispatch_intent",
                        }
                    ),
                },
            )
        return self.client.call(
            self.config["hub_user_identity"],
            "/v1/message",
            {
                "project": "coordination",
                "kind": "discussion",
                "request_id": "control:" + delivery["id"],
                "body": (
                    "[发送给 " + delivery["target"] + "]\n" if delivery["target"] != "all" else ""
                )
                + delivery["body"],
            },
        )
