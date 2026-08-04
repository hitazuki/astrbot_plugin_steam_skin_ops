from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path


def _read_schema_version(path: Path, *, immutable: bool) -> int:
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    uri = f"{path.resolve().as_uri()}{query}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        metadata_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
        if not metadata_exists:
            return 0
        version_row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        return int(version_row[0]) if version_row else 0
    finally:
        conn.close()


def preflight_schema_version(path: Path) -> int:
    """Read a SQLite schema version without modifying the database or its WAL."""

    path = Path(path)
    main_version = _read_schema_version(path, immutable=True)
    wal_path = Path(f"{path}-wal")
    for _ in range(3):
        try:
            if wal_path.stat().st_size == 0:
                return main_version
            before = (path.stat(), wal_path.stat())
        except FileNotFoundError:
            return _read_schema_version(path, immutable=True)

        with tempfile.TemporaryDirectory() as tmp:
            staged_path = Path(tmp) / path.name
            staged_wal_path = Path(f"{staged_path}-wal")
            try:
                shutil.copyfile(path, staged_path)
                shutil.copyfile(wal_path, staged_wal_path)
                after = (path.stat(), wal_path.stat())
            except FileNotFoundError:
                continue
            before_signature = tuple((value.st_size, value.st_mtime_ns) for value in before)
            after_signature = tuple((value.st_size, value.st_mtime_ns) for value in after)
            if before_signature != after_signature:
                continue
            return _read_schema_version(staged_path, immutable=False)
    raise RuntimeError("database WAL changed during schema preflight")
