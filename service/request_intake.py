"""Read-only facade for the center's request-intake contract.

Routes and query fields are fixed locally; no credential or arbitrary URL comes
from the browser. Reading a requirement never schedules its task.
"""

import copy
from datetime import datetime, timezone
import re

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
ROUTES = {
    "requirements": {"limit", "after", "q", "project", "state"},
    "requirement": {"id", "history", "before_version"},
    "tasks": {"limit", "after", "status", "project"},
    "task": {"id"},
    "requirement-receipts": {"id", "after", "limit"},
    "intake-receipt": {"request_id"},
    "intake-events": {"after", "stream_epoch"},
}


class IntakeError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def query_fields(route, query):
    if route not in ROUTES or not isinstance(query, dict) or set(query) - ROUTES[route]:
        raise IntakeError("invalid_intake_query")
    fields = {}
    for key, values in query.items():
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
            or len(values[0]) > 4000
        ):
            raise IntakeError("invalid_intake_query")
        fields[key] = values[0]
    required = (
        "id"
        if route in ("requirement", "task", "requirement-receipts")
        else "request_id"
        if route == "intake-receipt"
        else None
    )
    if required and not ID.fullmatch(fields.get(required, "")):
        raise IntakeError("invalid_intake_identifier")
    for key in ("id", "request_id", "project"):
        if fields.get(key) and not ID.fullmatch(fields[key]):
            raise IntakeError("invalid_intake_identifier")
    for key in ("limit", "before_version", "history"):
        if key in fields:
            value = fields[key]
            maximum = 100 if key == "limit" else 1 if key == "history" else 2147483647
            minimum = 0 if key == "history" else 1
            if not value.isascii() or not value.isdecimal() or not minimum <= int(value) <= maximum:
                raise IntakeError("invalid_intake_pagination")
    if "after" in fields:
        if route in ("requirements", "tasks"):
            if fields["after"] and not ID.fullmatch(fields["after"]):
                raise IntakeError("invalid_intake_cursor")
        elif (
            not fields["after"].isascii()
            or not fields["after"].isdecimal()
            or not 0 <= int(fields["after"]) <= 9223372036854775807
        ):
            raise IntakeError("invalid_intake_cursor")
    if fields.get("state") and fields["state"] not in ("active", "cancelled", "superseded"):
        raise IntakeError("invalid_requirement_state")
    return fields


class RequestIntake:
    def __init__(self, hub):
        self.hub = hub

    def read(self, route, query):
        fields = query_fields(route, query)
        if not hasattr(self.hub, "intake_read"):
            raise IntakeError("intake_not_connected", 503)
        try:
            result = self.hub.intake_read(route, fields)
        except Exception as error:
            match = re.fullmatch(r"([1-5][0-9]{2}):([A-Za-z0-9_.-]{1,100})", str(error))
            if match:
                status, code = int(match[1]), match[2]
                if status == 404 and code == "not_found":
                    raise IntakeError("intake_not_connected", 503) from None
                if status in (400, 401, 403, 404, 409, 413, 429):
                    raise IntakeError(code, status) from None
            raise IntakeError("intake_unavailable", 503) from None
        if not isinstance(result, dict):
            raise IntakeError("invalid_intake_response", 502)
        value = copy.deepcopy(result)
        # Center cursor/epoch/history are kept exactly. No cross-page snapshot
        # or execution receipt is inferred here; the client must reconcile events.
        value["control"] = {
            "source": "coordination_request_intake_v1",
            "available": True,
            "stale": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        return value
