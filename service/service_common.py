"""Small protocol primitives shared by the local service modules."""

import datetime as dt
import re

REGISTRY_ACTIONS = frozenset(
    {
        "project-register",
        "governance-grant",
        "host-register",
        "adapter-register",
        "seat-create",
        "seat-update",
        "seat-control",
        "operation-receipt",
    }
)

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
MARKER = re.compile(
    r"^\[control-delivery:([A-Za-z0-9_.:-]{1,100})\]\n【用户通过协作管理客户端发给本工位的消息】\n"
)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def delivery_message(delivery):
    return (
        "[control-delivery:"
        + delivery["id"]
        + "]\n【用户通过协作管理客户端发给本工位的消息】\n"
        + delivery["body"]
    )


class Problem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
