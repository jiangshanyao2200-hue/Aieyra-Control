"""Run the repository's pinned formatters and Python correctness checks."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON = [
    "service",
    "cloud",
    "scripts",
    "tests/service",
    "tests/web",
    "tests/desktop-notifications",
    "desktop/test",
]
WEB = [
    "web/*.{js,css,html}",
    "cloud/site/*.{js,css,html}",
    "desktop/*.{cjs,css,html}",
    "scripts/*.cjs",
    "tests/unit/*.mjs",
    "tests/web/*.mjs",
    "tests/desktop-notifications/*.cjs",
    "desktop/test/*.cjs",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format", action="store_true", help="Apply formatting; no behavioral lint fixes"
    )
    args = parser.parse_args()
    prettier = ROOT / "node_modules/prettier/bin/prettier.cjs"
    if not prettier.is_file():
        parser.error("Run npm ci from the repository root first")
    commands = [
        [sys.executable, "-m", "ruff", "format", *([] if args.format else ["--check"]), *PYTHON],
        [sys.executable, "-m", "ruff", "check", *PYTHON],
        [
            "node",
            str(prettier),
            "--write" if args.format else "--check",
            "--no-error-on-unmatched-pattern",
            *WEB,
        ],
    ]
    failed = False
    for command in commands:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
