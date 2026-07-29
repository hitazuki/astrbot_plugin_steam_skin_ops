#!/usr/bin/env python3
"""Validate commit subjects used by local hooks and GitHub Actions."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

COMMIT_PATTERN = re.compile(
    r"^(feat|fix|docs|refactor|perf|test|build|ci|chore|revert)"
    r"\([a-z0-9][a-z0-9-]*\)(!)?: .+$"
)
FORMAT_HINT = "<type>(<lowercase-scope>): <description>"


def validate_subject(subject: str) -> str | None:
    subject = subject.strip()
    if COMMIT_PATTERN.fullmatch(subject):
        return None
    return f"invalid commit subject: {subject!r}\nexpected: {FORMAT_HINT}"


def subject_from_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def subjects_from_range(revision_range: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s", revision_range],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--message", help="validate one commit subject")
    source.add_argument("--file", type=Path, help="read a commit message file")
    source.add_argument("--env", help="read a commit subject from an environment variable")
    source.add_argument("--range", dest="revision_range", help="validate git subjects in a revision range")
    args = parser.parse_args()

    if args.message is not None:
        subjects = [args.message]
    elif args.file is not None:
        subjects = [subject_from_file(args.file)]
    elif args.env is not None:
        subjects = [os.environ.get(args.env, "")]
    else:
        subjects = subjects_from_range(args.revision_range)

    errors = [error for subject in subjects if (error := validate_subject(subject))]
    if errors:
        print("\n\n".join(errors), file=sys.stderr)
        return 1
    print(f"validated {len(subjects)} commit subject(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
