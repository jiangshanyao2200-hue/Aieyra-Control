"""Local project handoff revisions. Center identities authorize, Control retains content."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

SECTIONS = ("blueprint", "timeline", "checkpoint", "recovery", "index")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class MemoryError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise MemoryError("invalid_memory_identifier")
    return value


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class ProjectMemory:
    def __init__(self, store, projects):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_projects(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,root TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_memory_revisions(
                    project TEXT NOT NULL, version INTEGER NOT NULL, sections TEXT NOT NULL,
                    sha256 TEXT NOT NULL, summary TEXT NOT NULL, actor TEXT NOT NULL,
                    session_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(project,version));
                CREATE TABLE IF NOT EXISTS project_memory_requests(
                    actor TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    project TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(actor,request_id));
            """)
        for project in projects:
            self.register(project)

    def register(self, project):
        """Register explicit metadata only; never inspect or upload the project root."""
        key = identifier(project["id"])
        with self.store.db() as db:
            db.execute(
                """INSERT INTO memory_projects VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                name=CASE WHEN ? THEN excluded.name ELSE memory_projects.name END,
                root=CASE WHEN ? THEN excluded.root ELSE memory_projects.root END""",
                (
                    key,
                    str(project.get("name", key))[:100],
                    str(project.get("root", ""))[:500],
                    "name" in project,
                    "root" in project,
                ),
            )

    def project(self, project):
        identifier(project)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM memory_projects WHERE id=?", (project,)).fetchone()
        if not row:
            raise MemoryError("memory_project_not_configured", 404)
        return dict(row)

    @staticmethod
    def document(row):
        value = dict(row)
        value["sections"] = json.loads(value["sections"])
        value["storage"] = "local_control"
        return value

    def listing(self):
        with self.store.db() as db:
            projects = [dict(p) for p in db.execute("SELECT * FROM memory_projects ORDER BY id")]
            rows = {
                r["project"]: dict(r)
                for r in db.execute("""SELECT project,MAX(version) version,
                    created_at,summary FROM project_memory_revisions GROUP BY project""")
            }
        return {
            "projects": [
                {**p, **rows.get(p["id"], {}), "version": rows.get(p["id"], {}).get("version", 0)}
                for p in projects
            ],
            "storage": "local_control",
        }

    def read(self, project, version=None):
        meta = self.project(project)
        if version is not None and (type(version) is not int or not 1 <= version <= 2147483647):
            raise MemoryError("invalid_memory_version")
        with self.store.db() as db:
            latest = (
                db.execute(
                    "SELECT MAX(version) FROM project_memory_revisions WHERE project=?", (project,)
                ).fetchone()[0]
                or 0
            )
            row = db.execute(
                "SELECT * FROM project_memory_revisions WHERE project=? AND version=?",
                (project, latest if version is None else version),
            ).fetchone()
        if row is None and version is not None:
            raise MemoryError("memory_revision_missing", 404)
        doc = (
            self.document(row)
            if row
            else {
                "project": project,
                "version": 0,
                "sections": {k: "" for k in SECTIONS},
                "sha256": None,
                "summary": "",
                "actor": None,
                "session_id": None,
                "created_at": None,
                "storage": "local_control",
            }
        )
        return {
            "memory": doc,
            "project": meta,
            "current_version": latest,
            "missing_sections": [k for k in SECTIONS if not doc["sections"][k].strip()],
            "read_order": list(SECTIONS),
            "content_is_project_data": True,
        }

    def history(self, project, before=None):
        self.project(project)
        if before is not None and (type(before) is not int or not 1 <= before <= 2147483647):
            raise MemoryError("invalid_memory_cursor")
        with self.store.db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    """SELECT version,summary,actor,session_id,created_at,sha256
                FROM project_memory_revisions WHERE project=? AND version<? ORDER BY version DESC LIMIT 21""",
                    (project, before if before is not None else 9223372036854775807),
                )
            ]
        return {
            "project": project,
            "revisions": rows[:20],
            "next_before": rows[19]["version"] if len(rows) > 20 else None,
        }

    def save(self, value, actor, session_id="", authorize=None):
        if set(value) != {"request_id", "project", "version", "sections", "summary"}:
            raise MemoryError("invalid_memory_fields")
        rid = identifier(value["request_id"])
        project = value["project"]
        self.project(project)
        version, sections, summary = value["version"], value["sections"], value["summary"]
        if type(version) is not int or not 0 <= version < 2147483647:
            raise MemoryError("invalid_memory_version")
        if not isinstance(sections, dict) or set(sections) != set(SECTIONS):
            raise MemoryError("memory_sections_required")
        if any(not isinstance(v, str) or len(v) > 12000 or "\x00" in v for v in sections.values()):
            raise MemoryError("invalid_memory_section")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 300:
            raise MemoryError("memory_summary_required")
        body = canonical(sections)
        if len(body.encode("utf-8")) > 26000:
            raise MemoryError("memory_content_too_large", 413)
        hashed = hashlib.sha256(
            canonical({"value": value, "session_id": session_id}).encode()
        ).hexdigest()
        replayed = False
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if authorize is not None:
                authorize(db)
            prior = db.execute(
                "SELECT * FROM project_memory_requests WHERE actor=? AND request_id=?", (actor, rid)
            ).fetchone()
            if prior:
                if prior["digest"] != hashed:
                    raise MemoryError("memory_request_conflict", 409)
                saved_version = prior["version"]
                replayed = True
            else:
                current = (
                    db.execute(
                        "SELECT MAX(version) FROM project_memory_revisions WHERE project=?",
                        (project,),
                    ).fetchone()[0]
                    or 0
                )
                if current != version:
                    raise MemoryError("memory_version_conflict", 409)
                saved_version = version + 1
                db.execute(
                    "INSERT INTO project_memory_revisions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        project,
                        saved_version,
                        body,
                        hashlib.sha256(body.encode()).hexdigest(),
                        summary,
                        actor,
                        session_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.execute(
                    "INSERT INTO project_memory_requests VALUES(?,?,?,?,?)",
                    (actor, rid, hashed, project, saved_version),
                )
                self.store.event(db, "memory.saved", project)
        return {**self.read(project, saved_version), "request_id": rid, "replayed": replayed}
