"""Server-shell-only moderation. This module is not imported by the web server."""

import argparse
import json
import sqlite3
import time
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))
from feedback_contract import redact

p = argparse.ArgumentParser()
p.add_argument("--data", type=Path, default=Path("/srv/aieyra-control/data"))
s = p.add_subparsers(dest="action", required=True)
a = s.add_parser("hide")
a.add_argument("seq", type=int)
a = s.add_parser("block")
a.add_argument("subject")
a.add_argument("--reason", required=True)
a = s.add_parser("unblock")
a.add_argument("subject")
a = s.add_parser("feedback-list")
a.add_argument("--status", choices=["received", "triaged", "in_progress", "resolved", "rejected"])
a = s.add_parser("feedback-update")
a.add_argument("ticket")
a.add_argument("--revision", type=int, required=True)
a.add_argument(
    "--status", required=True, choices=["triaged", "in_progress", "resolved", "rejected"]
)
a.add_argument("--note", required=True)
s.add_parser("status")
args = p.parse_args()
with sqlite3.connect(args.data / "cloud.sqlite") as db:
    db.row_factory = sqlite3.Row
    if args.action == "feedback-list":
        rows = db.execute(
            "SELECT id,product,version,report,status,revision,note,created,updated FROM feedback "
            + ("WHERE status=? " if args.status else "")
            + "ORDER BY created DESC LIMIT 50",
            (args.status,) if args.status else (),
        ).fetchall()
        print(json.dumps({"tickets": [dict(r) for r in rows]}, ensure_ascii=False))
        sys.exit(0)
    if args.action == "feedback-update":
        if not re.fullmatch("ACF-[a-f0-9]{24}", args.ticket) or not 1 <= len(args.note) <= 1000:
            raise SystemExit("Invalid ticket/note")
        note = redact(args.note)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "UPDATE feedback SET status=?,note=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
            (args.status, note, time.time(), args.ticket, args.revision),
        )
        if row.rowcount != 1:
            raise SystemExit("Revision conflict or missing ticket")
        db.execute(
            "INSERT INTO feedback_audit(ticket,revision,status,note,created) VALUES(?,?,?,?,?)",
            (args.ticket, args.revision + 1, args.status, note, time.time()),
        )
        db.commit()
        print(json.dumps({"id": args.ticket, "status": args.status, "revision": args.revision + 1}))
        sys.exit(0)
    if args.action == "hide":
        db.execute("UPDATE posts SET hidden=1 WHERE seq=?", (args.seq,))
    elif args.action == "block":
        db.execute("INSERT OR REPLACE INTO blocked VALUES(?,?)", (args.subject, args.reason))
        db.execute("UPDATE sessions SET revoked=1 WHERE subject=?", (args.subject,))
    elif args.action == "unblock":
        db.execute("DELETE FROM blocked WHERE subject=?", (args.subject,))
    print(
        json.dumps(
            {
                "action": args.action,
                "posts": db.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                "blocked": db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0],
            }
        )
    )
