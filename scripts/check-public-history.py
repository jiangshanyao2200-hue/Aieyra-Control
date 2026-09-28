#!/usr/bin/env python3
"""Check every reachable public revision, including removed test artifacts."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_export", ROOT / "scripts/export-source.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def check_history(repository):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    commits = git("rev-list", "--all").decode().splitlines()
    if not commits:
        raise ValueError("public_history_has_no_commits")
    paths = {}
    failures = []
    for commit in commits:
        try:
            exporter.validate_public_files(
                {"commit-message.txt": git("cat-file", "commit", commit)}
            )
        except (ValueError, UnicodeError) as error:
            failures.append({"commit": commit, "rule": str(error)})
        for row in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = name.decode("utf-8")
            if kind != "blob" or mode not in {"100644", "100755"}:
                failures.append({"path": name, "rule": "unreviewed_link_or_submodule"})
            else:
                paths.setdefault(oid, set()).add(name)
    objects = list(paths)
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input=("\n".join(objects) + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    offset = 0
    for oid in objects:
        end = result.index(b"\n", offset)
        actual, kind, size = result[offset:end].decode().split()
        assert actual == oid and kind == "blob"
        offset = end + 1
        raw = result[offset : offset + int(size)]
        offset += int(size) + 1
        for name in sorted(paths[oid]):
            try:
                exporter.validate_public_files({name: raw})
                if name.endswith("release-baseline.json"):
                    baseline = json.loads(raw)
                    # Baselines are immutable historical claims. Their hashes can
                    # refer to removed artifacts, but embedded bytes need review.
                    if "payload" in baseline:
                        import base64

                        payload = base64.b64decode(baseline["payload"], validate=True)
                        exporter.validate_public_files({"baseline-payload.json": payload})
            except (ValueError, UnicodeError) as error:
                failures.append({"path": name, "blob": oid, "rule": str(error)})
    return {"commits": len(commits), "blobs": len(objects), "failures": failures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    args = parser.parse_args()
    report = check_history(args.repository)
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if report["failures"] else 0)
