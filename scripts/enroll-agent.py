#!/usr/bin/env python3
"""Enroll an Agent on this computer. Writes its credential to a new private file."""

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, default=17910)
    p.add_argument("--request-id", default=None)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Retry exactly the saved enrollment after a lost response",
    )
    a = p.parse_args()
    origin = "http://127.0.0.1:" + str(a.port)
    opener = build_opener(ProxyHandler({}))
    if a.resume:
        config = json.loads(a.output.read_text(encoding="utf-8-sig"))
        if (
            config.get("url") != origin
            or config.get("enrollment") != {"name": a.name, "project": a.project}
            or (a.request_id and a.request_id != config["request_id"])
        ):
            raise ValueError("Resume must match the saved enrollment exactly")
    elif a.output.exists():
        raise ValueError(
            "Refusing to overwrite an existing credential; use --resume for the exact original request"
        )
    else:
        config = {
            "protocol": "aieyra-agent/1",
            "url": origin,
            "token": secrets.token_urlsafe(32),
            "request_id": a.request_id or "enroll-" + secrets.token_hex(16),
            "enrollment": {"name": a.name, "project": a.project},
        }
        a.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(a.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    csrf = json.load(opener.open(origin + "/api/session", timeout=10))["csrf"]
    body = {
        "request_id": config["request_id"],
        "name": a.name,
        "project": a.project,
        "token": config["token"],
    }
    request = Request(
        origin + "/api/agent-access/enroll",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Origin": origin, "X-Control-CSRF": csrf},
    )
    result = json.load(opener.open(request, timeout=30))
    print(
        json.dumps(
            {
                "credential": result["credential"],
                "config": str(a.output),
                "state": result["state"],
                "project_memory": result.get("project_memory"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
