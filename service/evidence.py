"""Explicitly registered engineering briefs, with source freshness checks."""

import datetime as dt
import hashlib
import json
from pathlib import Path


LIMIT = 262144


def read_brief(configuration):
    path = Path(configuration["path"])
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("brief_too_large")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("features"), list):
        raise ValueError("invalid_brief")
    root = Path(configuration["root"]).resolve()

    def reference(item):
        if not isinstance(item, dict):
            return {"freshness": "unknown"}
        result = {
            key: item[key]
            for key in ("path", "line", "name", "sha256", "observed_at")
            if key in item
        }
        result["freshness"] = "unknown"
        location = item.get("path")
        expected = item.get("sha256")
        if not isinstance(location, str) or not isinstance(expected, str):
            return result
        file = (root / location).resolve()
        if Path(location).is_absolute() or not file.is_relative_to(root):
            result["freshness"] = "outside_project"
            return result
        try:
            if file.stat().st_size > 16 * 1024 * 1024:
                return result
            actual = hashlib.sha256(file.read_bytes()).hexdigest()
            result["freshness"] = "matches_record" if actual == expected else "source_changed"
        except OSError:
            result["freshness"] = "unavailable"
        return result

    result = {
        key: value[key]
        for key in (
            "schema_version",
            "project_id",
            "workstation_id",
            "observed_at",
            "scope",
            "coverage",
        )
        if key in value
    }
    result.update(
        source_ref=str(path),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        checked_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        features=[],
    )
    for item in value["features"][:200]:
        if not isinstance(item, dict):
            continue
        feature = {key: item[key] for key in ("id", "summary", "verification") if key in item}
        feature.update(source=reference(item.get("source")), test=reference(item.get("test")))
        result["features"].append(feature)
    result["verification_receipt"] = reference(value.get("verification_receipt"))
    result["needs_coordination"] = [
        v[:2000] for v in value.get("needs_coordination", [])[:30] if isinstance(v, str)
    ]
    return result
