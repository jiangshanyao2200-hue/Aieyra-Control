"""Server-owner publication of an existing signed release. No HTTP write route."""

import argparse
import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from matrix_store import canonical, safe


def release_payload(envelope, public_pem):
    payload = base64.b64decode(envelope["payload"], validate=True)
    load_pem_public_key(public_pem).verify(
        base64.b64decode(envelope["signature"], validate=True), payload
    )
    manifest = json.loads(payload)
    if (
        manifest != envelope["manifest"]
        or manifest.get("schema") != 1
        or manifest.get("product") != "aieyra-control"
        or type(manifest.get("sequence")) is not int
        or manifest["sequence"] < 1
        or not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", ""))
    ):
        raise ValueError("invalid_signed_release")
    version = manifest["version"]
    notes = safe(manifest.get("notes", ""), 10, 10000)
    return {
        "sequence": manifest["sequence"],
        "digest": hashlib.sha256(payload).hexdigest(),
        "title": "Aieyra Control " + version + " 已发布",
        "summary": notes[:500],
        "content": notes
        + "\n\n前往官网下载页获取 Windows 版本。已有安装由领导 Agent 验签、审阅本地改动后更新。欢迎在本话题回复使用体验，或在功能反馈板块提出问题。",
        "version": version,
    }


def publish(app, value):
    """Caller is the server-shell owner; both signature and source were verified."""
    topic = str(uuid.uuid5(uuid.NAMESPACE_URL, "aieyra-control-release:" + str(value["sequence"])))
    subject, now = "system:verified-release", time.time()
    with app.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS matrix_release_publications(sequence INTEGER PRIMARY KEY,digest TEXT NOT NULL,topic TEXT NOT NULL UNIQUE)"
        )
        prior = db.execute(
            "SELECT * FROM matrix_release_publications WHERE sequence=?", (value["sequence"],)
        ).fetchone()
        if prior:
            if prior["digest"] != value["digest"]:
                raise ValueError("release_sequence_conflict")
            return {"topic": prior["topic"], "replayed": True}
        app.matrix_author(db, subject)
        db.execute(
            "INSERT INTO matrix_topics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
            (
                topic,
                subject,
                "Aieyra Control",
                "update",
                value["title"],
                value["summary"],
                value["content"],
                "https://ctrl.aieyra.cn/download",
                "open",
                1,
                "release",
                value["version"],
                "",
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO matrix_release_publications VALUES(?,?,?)",
            (value["sequence"], value["digest"], topic),
        )
        db.execute(
            "INSERT INTO matrix_events(topic,kind,revision,created) VALUES(?,'created',1,?)",
            (topic, now),
        )
        db.execute(
            "INSERT INTO matrix_audit VALUES(?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                subject,
                topic,
                "signed_release_published",
                canonical({"sequence": value["sequence"], "payload_sha256": value["digest"]}),
                now,
            ),
        )
    return {"topic": topic, "replayed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    key = root / "release-public.pem"
    if not key.exists():
        key = root.parent / "config/release-public.pem"
    value = release_payload(
        json.loads((args.data / "releases/stable.json").read_text(encoding="utf-8")),
        key.read_bytes(),
    )
    if args.publish:
        from server import Cloud

        result = publish(Cloud(args.data), value)
        print(json.dumps({**result, "version": value["version"], "signatureVerified": True}))
    else:
        print(json.dumps({"preview": value, "published": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
