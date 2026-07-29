from __future__ import annotations

import math
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .market import MAX_SOURCE_FUTURE_SKEW_SECONDS, MarketSnapshot
from .rules import MAX_RULE_STATE_INTEGER, parse_rule_state_integer

SCHEMA_VERSION = 7
DEFAULT_BREAKTHROUGH_STEP = 0.03


DEFAULT_RULE_STATE = {
    "alert_active": 0,
    "qualifying_count": 0,
    "clearing_count": 0,
    "last_value": None,
    "last_baseline": None,
    "last_observed_at": None,
    "last_signal_at": None,
    "highest_notified_level": 0,
    "status": "waiting",
}

DEFAULT_HEALTH_STATE = {"fetch_failures": 0, "health_alerted": 0}


class RuleItemLimitError(RuntimeError):
    pass


class MonitorStorage:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        current_version = self._preflight_schema_version() if self.path.exists() else 0
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current_version} is newer than supported "
                f"v{SCHEMA_VERSION}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(current_version)

    def _preflight_schema_version(self) -> int:
        """Read an existing database version without allowing SQLite to write it."""
        main_version = self._read_schema_version(self.path, immutable=True)
        if main_version > SCHEMA_VERSION:
            return main_version

        wal_path = Path(f"{self.path}-wal")
        for _ in range(3):
            try:
                if wal_path.stat().st_size == 0:
                    return main_version
                before = (self.path.stat(), wal_path.stat())
            except FileNotFoundError:
                return self._read_schema_version(self.path, immutable=True)

            with tempfile.TemporaryDirectory() as tmp:
                staged_path = Path(tmp) / self.path.name
                staged_wal_path = Path(f"{staged_path}-wal")
                try:
                    shutil.copyfile(self.path, staged_path)
                    shutil.copyfile(wal_path, staged_wal_path)
                    after = (self.path.stat(), wal_path.stat())
                except FileNotFoundError:
                    continue
                before_signature = tuple(
                    (value.st_size, value.st_mtime_ns) for value in before
                )
                after_signature = tuple(
                    (value.st_size, value.st_mtime_ns) for value in after
                )
                if before_signature != after_signature:
                    continue
                return self._read_schema_version(staged_path, immutable=False)
        raise RuntimeError("database WAL changed during schema preflight")

    @staticmethod
    def _read_schema_version(path: Path, *, immutable: bool) -> int:
        query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        uri = f"{path.resolve().as_uri()}{query}"
        conn = sqlite3.connect(uri, uri=True)
        try:
            metadata_exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='metadata'"
            ).fetchone()
            if not metadata_exists:
                return 0
            version_row = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            return int(version_row[0]) if version_row else 0
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self, current_version: int) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    smis_id INTEGER NOT NULL,
                    appid INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    name_zh TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    buff_sell_price REAL,
                    buff_sell_num INTEGER,
                    steam_sell_price REAL,
                    steam_sell_num INTEGER,
                    steam_transaction_quantity INTEGER,
                    buff_to_steam_ratio REAL,
                    UNIQUE(source, item_key, observed_at, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_item_time
                    ON market_snapshots(item_key, observed_at DESC);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    smis_id INTEGER PRIMARY KEY,
                    item_key TEXT NOT NULL UNIQUE,
                    appid INTEGER NOT NULL,
                    hash_name TEXT NOT NULL,
                    cn_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    rule_id INTEGER,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE(signal_key, recipient_key)
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_recipient
                    ON alert_events(recipient_key, acknowledged_at, id DESC);
                CREATE TABLE IF NOT EXISTS delivery_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    driver TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(event_id, driver)
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_due
                    ON delivery_jobs(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    smis_id INTEGER NOT NULL,
                    recipient_key TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    operational INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rules_item ON rules(smis_id, enabled);
                CREATE TABLE IF NOT EXISTS rule_states (
                    rule_id INTEGER PRIMARY KEY,
                    alert_active INTEGER NOT NULL DEFAULT 0,
                    qualifying_count INTEGER NOT NULL DEFAULT 0,
                    clearing_count INTEGER NOT NULL DEFAULT 0,
                    last_value REAL,
                    last_baseline REAL,
                    last_observed_at TEXT,
                    last_signal_at TEXT,
                    highest_notified_level INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_health (
                    smis_id INTEGER NOT NULL,
                    recipient_key TEXT NOT NULL,
                    fetch_failures INTEGER NOT NULL DEFAULT 0,
                    health_alerted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(smis_id, recipient_key)
                );
            """)
            if current_version < 3:
                self._migrate_v3(conn)
            if current_version < 4:
                self._migrate_v4(conn)
            if current_version < 5:
                self._migrate_v5(conn)
            if current_version < 6:
                self._migrate_v6(conn)
            if current_version < 7:
                self._migrate_v7(conn)

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Transactionally migrate v2 recipient and notification tables."""
        conn.execute("SAVEPOINT migrate_v3")
        try:
            rule_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(rules)")}
            if "umo" in rule_columns and "recipient_key" not in rule_columns:
                conn.execute("ALTER TABLE rules RENAME COLUMN umo TO recipient_key")
            health_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(rule_health)")
            }
            if "umo" in health_columns and "recipient_key" not in health_columns:
                conn.execute("ALTER TABLE rule_health RENAME COLUMN umo TO recipient_key")

            old_outbox = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_outbox'"
            ).fetchone()
            if old_outbox:
                conn.execute("""
                    INSERT OR IGNORE INTO alert_events(
                        signal_key,recipient_key,event_type,title,content,created_at
                    )
                    SELECT signal_key,umo,event_type,title,content,created_at
                    FROM notification_outbox
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO delivery_jobs(
                        event_id,driver,status,attempts,last_error,next_attempt_at,delivered_at
                    )
                    SELECT e.id,'astrbot',o.status,o.attempts,o.last_error,
                           o.next_attempt_at,o.sent_at
                    FROM notification_outbox o
                    JOIN alert_events e
                      ON e.signal_key=o.signal_key AND e.recipient_key=o.umo
                """)
                conn.execute("DROP TABLE notification_outbox")

            conn.execute("DROP TABLE IF EXISTS subscription_state")
            conn.execute("DROP TABLE IF EXISTS subscriptions")
            conn.execute("DROP INDEX IF EXISTS idx_rules_umo")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_recipient "
                         "ON rules(recipient_key, enabled)")

            snapshot_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(market_snapshots)")
            }
            for column in (
                "uuyp_sell_price REAL", "uuyp_sell_num INTEGER",
                "c5_sell_price REAL", "c5_sell_num INTEGER",
                "igxe_sell_price REAL", "igxe_sell_num INTEGER",
                "eco_sell_price REAL", "eco_sell_num INTEGER",
            ):
                if column.split()[0] not in snapshot_columns:
                    conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {column}")

            now = self._utcnow()
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version',?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
            """, ("3", now))
            conn.execute("RELEASE SAVEPOINT migrate_v3")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v3")
            conn.execute("RELEASE SAVEPOINT migrate_v3")
            raise

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Add persisted breakthrough levels without replaying old alerts."""
        conn.execute("SAVEPOINT migrate_v4")
        try:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(rule_states)")
            }
            if "highest_notified_level" not in columns:
                conn.execute(
                    "ALTER TABLE rule_states ADD COLUMN "
                    "highest_notified_level INTEGER NOT NULL DEFAULT 0"
                )

            rows = conn.execute("""
                SELECT s.rule_id,s.last_value,r.rule_type,r.threshold
                FROM rule_states s JOIN rules r ON r.id=s.rule_id
                WHERE s.alert_active=1 AND s.last_value IS NOT NULL
            """).fetchall()
            for row in rows:
                try:
                    value = float(row["last_value"])
                    threshold = float(row["threshold"])
                    rule_type = str(row["rule_type"]).strip().lower()
                    limit = (
                        threshold / 100
                        if rule_type in {"ratio", "t7"}
                        else threshold
                    )
                    if (
                        not math.isfinite(value)
                        or not math.isfinite(limit)
                        or value <= 0
                        or limit <= 0
                    ):
                        raise ValueError("invalid persisted rule state")
                    depth = (
                        (value - limit) / limit
                        if rule_type == "steam"
                        else (limit - value) / limit
                    )
                    level = max(
                        0,
                        int(
                            (max(0.0, depth) + 1e-12)
                            / DEFAULT_BREAKTHROUGH_STEP
                        ),
                    )
                    if level > MAX_RULE_STATE_INTEGER:
                        level = 0
                except (TypeError, ValueError, OverflowError):
                    level = 0
                conn.execute(
                    "UPDATE rule_states SET highest_notified_level=? WHERE rule_id=?",
                    (level, int(row["rule_id"])),
                )

            now = self._utcnow()
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','4',?)
                ON CONFLICT(key) DO UPDATE SET value='4',updated_at=excluded.updated_at
            """, (now,))
            conn.execute("RELEASE SAVEPOINT migrate_v4")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v4")
            conn.execute("RELEASE SAVEPOINT migrate_v4")
            raise

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        """Enforce one active rule per item, recipient, and rule type."""
        conn.execute("SAVEPOINT migrate_v5")
        try:
            now = self._utcnow()
            self._deduplicate_active_rules(conn, now)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_rules_active_identity
                ON rules(smis_id,recipient_key,rule_type) WHERE enabled=1
            """)
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','5',?)
                ON CONFLICT(key) DO UPDATE SET value='5',updated_at=excluded.updated_at
            """, (now,))
            conn.execute("RELEASE SAVEPOINT migrate_v5")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v5")
            conn.execute("RELEASE SAVEPOINT migrate_v5")
            raise

    def _migrate_v6(self, conn: sqlite3.Connection) -> None:
        """Canonicalize public rule IDs and reconcile disabled-rule delivery state."""
        conn.execute("SAVEPOINT migrate_v6")
        try:
            now = self._utcnow()
            conn.execute("DROP INDEX IF EXISTS uq_rules_active_identity")
            conn.execute("""
                UPDATE rules
                SET rule_type=lower(trim(rule_type)),updated_at=?
                WHERE rule_type<>lower(trim(rule_type))
            """, (now,))
            self._deduplicate_active_rules(conn, now)
            disabled = conn.execute(
                "SELECT id,recipient_key FROM rules WHERE enabled=0"
            ).fetchall()
            for row in disabled:
                self._cancel_pending_rule_jobs(
                    conn,
                    int(row["id"]),
                    str(row["recipient_key"]),
                )
            conn.execute("""
                CREATE UNIQUE INDEX uq_rules_active_identity
                ON rules(smis_id,recipient_key,rule_type) WHERE enabled=1
            """)
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','6',?)
                ON CONFLICT(key) DO UPDATE SET value='6',updated_at=excluded.updated_at
            """, (now,))
            conn.execute("RELEASE SAVEPOINT migrate_v6")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v6")
            conn.execute("RELEASE SAVEPOINT migrate_v6")
            raise

    def _migrate_v7(self, conn: sqlite3.Connection) -> None:
        """Separate configured rules from rules that are safe to execute."""
        conn.execute("SAVEPOINT migrate_v7")
        try:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(rules)")
            }
            if "operational" not in columns:
                conn.execute(
                    "ALTER TABLE rules ADD COLUMN "
                    "operational INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                "UPDATE rules SET operational=0 "
                "WHERE enabled<>1 OR operational<>1"
            )
            inactive_sessions = conn.execute("""
                SELECT DISTINCT r.smis_id,r.recipient_key,
                       COALESCE(i.item_key,(
                           SELECT s.item_key FROM market_snapshots s
                           WHERE s.smis_id=r.smis_id ORDER BY s.id DESC LIMIT 1
                       ),'smis:' || r.smis_id) AS item_key
                FROM rules r LEFT JOIN items i ON i.smis_id=r.smis_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM rules active
                    WHERE active.smis_id=r.smis_id
                      AND active.recipient_key=r.recipient_key
                      AND active.enabled=1 AND active.operational=1
                )
            """).fetchall()
            for row in inactive_sessions:
                conn.execute(
                    "DELETE FROM rule_health WHERE smis_id=? AND recipient_key=?",
                    (int(row["smis_id"]), str(row["recipient_key"])),
                )
                self._cancel_pending_health_jobs(
                    conn,
                    str(row["item_key"]),
                    str(row["recipient_key"]),
                )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rules_operational
                ON rules(smis_id,enabled,operational)
            """)
            now = self._utcnow()
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','7',?)
                ON CONFLICT(key) DO UPDATE SET value='7',updated_at=excluded.updated_at
            """, (now,))
            conn.execute("RELEASE SAVEPOINT migrate_v7")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v7")
            conn.execute("RELEASE SAVEPOINT migrate_v7")
            raise

    def _deduplicate_active_rules(
        self,
        conn: sqlite3.Connection,
        updated_at: str,
    ) -> None:
        from .rules import validate_rule

        rows = conn.execute("""
            SELECT r.id,r.smis_id,r.recipient_key,r.rule_type,r.threshold,
                   COALESCE(s.alert_active,0) AS alert_active
            FROM rules r
            LEFT JOIN rule_states s ON s.rule_id=r.id
            WHERE r.enabled=1
        """).fetchall()

        def priority(row: sqlite3.Row) -> tuple[int, str, str, int, int, int]:
            valid = validate_rule(str(row["rule_type"]), row["threshold"]) is None
            try:
                active_value = float(row["alert_active"])
                active = active_value == 1
            except (TypeError, ValueError):
                active = False
            return (
                int(row["smis_id"]),
                str(row["recipient_key"]),
                str(row["rule_type"]),
                0 if valid else 1,
                0 if active else 1,
                int(row["id"]),
            )

        seen: set[tuple[int, str, str]] = set()
        for row in sorted(rows, key=priority):
            identity = (
                int(row["smis_id"]),
                str(row["recipient_key"]),
                str(row["rule_type"]),
            )
            if identity not in seen:
                seen.add(identity)
                continue
            rule_id = int(row["id"])
            recipient_key = str(row["recipient_key"])
            conn.execute(
                "UPDATE rules SET enabled=0,updated_at=? WHERE id=?",
                (updated_at, rule_id),
            )
            self._cancel_pending_rule_jobs(conn, rule_id, recipient_key)

    @staticmethod
    def _cancel_pending_rule_jobs(
        conn: sqlite3.Connection,
        rule_id: int,
        recipient_key: str,
    ) -> int:
        prefix = f"rule:{int(rule_id)}:"
        cursor = conn.execute("""
            DELETE FROM delivery_jobs
            WHERE status='pending' AND event_id IN (
                SELECT id FROM alert_events
                WHERE recipient_key=? AND (
                    rule_id=? OR substr(signal_key,1,?)=?
                )
            )
        """, (recipient_key, int(rule_id), len(prefix), prefix))
        return int(cursor.rowcount)

    @staticmethod
    def _cancel_pending_health_jobs(
        conn: sqlite3.Connection,
        item_key: str,
        recipient_key: str,
    ) -> int:
        prefix = f"health:{item_key}:{recipient_key}:"
        cursor = conn.execute("""
            DELETE FROM delivery_jobs
            WHERE status='pending' AND event_id IN (
                SELECT id FROM alert_events
                WHERE recipient_key=? AND substr(signal_key,1,?)=?
            )
        """, (recipient_key, len(prefix), prefix))
        return int(cursor.rowcount)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    def count_items(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        return int(row[0])

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO items(
                    smis_id,item_key,appid,hash_name,cn_name,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT(smis_id) DO UPDATE SET
                    item_key=excluded.item_key, appid=excluded.appid,
                    hash_name=excluded.hash_name, cn_name=excluded.cn_name,
                    enabled=1, updated_at=excluded.updated_at
            """, (
                int(item["smis_id"]), str(item["item_key"]), int(item["appid"]),
                str(item["name"]), str(item["name_zh"]), now, now,
            ))
        return self.get_item(int(item["smis_id"]))

    def get_item(self, smis_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE smis_id=?", (int(smis_id),)).fetchone()
        return dict(row) if row else None

    def list_items(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY cn_name COLLATE NOCASE, smis_id"
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def resolve_items(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if query.isdigit():
            item = self.get_item(int(query))
            return [item] if item else []
        pattern = f"%{query.lower()}%"
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM items
                WHERE lower(hash_name) LIKE ? OR lower(cn_name) LIKE ?
                ORDER BY
                    CASE WHEN lower(hash_name)=? OR lower(cn_name)=? THEN 0 ELSE 1 END,
                    cn_name COLLATE NOCASE, smis_id
            """, (pattern, pattern, query.lower(), query.lower())).fetchall()
        return [dict(row) for row in rows]

    def count_rule_items(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT smis_id) FROM rules "
                "WHERE enabled=1 AND operational=1"
            ).fetchone()
        return int(row[0])

    def add_rule(
        self,
        smis_id: int,
        recipient_key: str,
        rule_type: str,
        threshold: float,
        *,
        max_items: int | None = None,
    ) -> dict:
        now = self._utcnow()
        rule_type = str(rule_type).strip().lower()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            has_active_rule = conn.execute(
                "SELECT 1 FROM rules "
                "WHERE smis_id=? AND enabled=1 AND operational=1 LIMIT 1",
                (int(smis_id),),
            ).fetchone()
            if not has_active_rule and max_items is not None:
                active_items = int(conn.execute(
                    "SELECT COUNT(DISTINCT smis_id) FROM rules "
                    "WHERE enabled=1 AND operational=1"
                ).fetchone()[0])
                if active_items >= int(max_items):
                    raise RuleItemLimitError(int(max_items))
            existing = conn.execute("""
                SELECT id,threshold,operational FROM rules
                WHERE smis_id=? AND recipient_key=? AND rule_type=? AND enabled=1
                ORDER BY id
            """, (int(smis_id), recipient_key, rule_type)).fetchall()
            if existing:
                rule_id = int(existing[0]["id"])
                previous_threshold = float(existing[0]["threshold"])
                duplicate_ids = [int(row["id"]) for row in existing[1:]]
                changed = (
                    previous_threshold != float(threshold)
                    or parse_rule_state_integer(existing[0]["operational"]) != 1
                    or bool(duplicate_ids)
                )
                if changed:
                    conn.execute(
                        "UPDATE rules SET threshold=?,operational=1,updated_at=? "
                        "WHERE id=?",
                        (float(threshold), now, rule_id),
                    )
                    affected_ids = [rule_id, *duplicate_ids]
                    placeholders = ",".join("?" for _ in affected_ids)
                    conn.execute(
                        f"DELETE FROM rule_states WHERE rule_id IN ({placeholders})",
                        affected_ids,
                    )
                    for affected_id in affected_ids:
                        self._cancel_pending_rule_jobs(
                            conn,
                            affected_id,
                            recipient_key,
                        )
                    if duplicate_ids:
                        duplicate_placeholders = ",".join("?" for _ in duplicate_ids)
                        conn.execute(
                            f"DELETE FROM rules WHERE id IN ({duplicate_placeholders})",
                            duplicate_ids,
                        )
                action = "updated" if changed else "unchanged"
            else:
                cursor = conn.execute("""
                    INSERT INTO rules(
                        smis_id,recipient_key,rule_type,threshold,enabled,operational,
                        created_at,updated_at
                    )
                    VALUES(?,?,?,?,1,1,?,?)
                """, (int(smis_id), recipient_key, rule_type, float(threshold), now, now))
                rule_id = int(cursor.lastrowid)
                previous_threshold = None
                action = "created"
        rule = self.get_rule(rule_id)
        rule["action"] = action
        rule["previous_threshold"] = previous_threshold
        return rule

    def delete_item_if_unreferenced(self, smis_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("""
                DELETE FROM items
                WHERE smis_id=? AND NOT EXISTS (
                    SELECT 1 FROM rules WHERE rules.smis_id=items.smis_id
                )
            """, (int(smis_id),))
            return bool(cursor.rowcount)

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT r.*,i.item_key,i.appid,i.hash_name,i.cn_name
                FROM rules r LEFT JOIN items i USING(smis_id) WHERE r.id=?
            """, (int(rule_id),)).fetchone()
        return dict(row) if row else None

    def list_rules(
        self, *, recipient_key: str | None = None, smis_id: int | None = None,
        enabled_only: bool = True, operational_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if recipient_key is not None:
            clauses.append("r.recipient_key=?")
            params.append(recipient_key)
        if smis_id is not None:
            clauses.append("r.smis_id=?")
            params.append(int(smis_id))
        if enabled_only or operational_only:
            clauses.append("r.enabled=1")
        if operational_only:
            clauses.append("r.operational=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT r.*,i.item_key,i.appid,i.hash_name,i.cn_name
                FROM rules r LEFT JOIN items i USING(smis_id) {where}
                ORDER BY i.cn_name COLLATE NOCASE,r.id
            """, params).fetchall()
        return [dict(row) for row in rows]

    def update_rule(
        self, rule_id: int, recipient_key: str, threshold: float, *,
        max_items: int | None = None,
    ) -> dict | None:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT smis_id,operational FROM rules "
                "WHERE id=? AND recipient_key=? AND enabled=1",
                (int(rule_id), recipient_key),
            ).fetchone()
            if current is None:
                return None
            if (
                parse_rule_state_integer(current["operational"]) != 1
                and max_items is not None
            ):
                has_active_rule = conn.execute(
                    "SELECT 1 FROM rules WHERE smis_id=? AND enabled=1 "
                    "AND operational=1 LIMIT 1",
                    (int(current["smis_id"]),),
                ).fetchone()
                active_items = int(conn.execute(
                    "SELECT COUNT(DISTINCT smis_id) FROM rules "
                    "WHERE enabled=1 AND operational=1"
                ).fetchone()[0])
                if not has_active_rule and active_items >= int(max_items):
                    raise RuleItemLimitError(int(max_items))
            cursor = conn.execute(
                "UPDATE rules SET threshold=?,operational=1,updated_at=? "
                "WHERE id=? AND recipient_key=? AND enabled=1",
                (float(threshold), now, int(rule_id), recipient_key),
            )
            if not cursor.rowcount:
                return None
            conn.execute("DELETE FROM rule_states WHERE rule_id=?", (int(rule_id),))
            self._cancel_pending_rule_jobs(conn, int(rule_id), recipient_key)
        return self.get_rule(rule_id)

    def set_rule_operational(
        self,
        rule_id: int,
        recipient_key: str,
        operational: bool,
    ) -> bool:
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT r.smis_id,COALESCE(i.item_key,("
                "SELECT s.item_key FROM market_snapshots s "
                "WHERE s.smis_id=r.smis_id ORDER BY s.id DESC LIMIT 1"
                "),'smis:' || r.smis_id) AS item_key FROM rules r "
                "LEFT JOIN items i ON i.smis_id=r.smis_id "
                "WHERE r.id=? AND r.recipient_key=? AND r.enabled=1",
                (int(rule_id), recipient_key),
            ).fetchone()
            if rule is None:
                return False
            cursor = conn.execute(
                "UPDATE rules SET operational=?,updated_at=? "
                "WHERE id=? AND recipient_key=? AND enabled=1",
                (
                    int(bool(operational)),
                    self._utcnow(),
                    int(rule_id),
                    recipient_key,
                ),
            )
            if not operational:
                smis_id = int(rule["smis_id"])
                remaining = conn.execute(
                    "SELECT 1 FROM rules WHERE smis_id=? AND recipient_key=? "
                    "AND enabled=1 AND operational=1 LIMIT 1",
                    (smis_id, recipient_key),
                ).fetchone()
                if remaining is None:
                    conn.execute(
                        "DELETE FROM rule_health WHERE smis_id=? AND recipient_key=?",
                        (smis_id, recipient_key),
                    )
                    self._cancel_pending_health_jobs(
                        conn,
                        str(rule["item_key"]),
                        recipient_key,
                    )
        return bool(cursor.rowcount)

    def delete_rule(self, rule_id: int, recipient_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT r.smis_id,COALESCE(i.item_key,("
                "SELECT s.item_key FROM market_snapshots s "
                "WHERE s.smis_id=r.smis_id ORDER BY s.id DESC LIMIT 1"
                "),'smis:' || r.smis_id) AS item_key FROM rules r "
                "LEFT JOIN items i ON i.smis_id=r.smis_id "
                "WHERE r.id=? AND r.recipient_key=?",
                (int(rule_id), recipient_key),
            ).fetchone()
            if not row:
                return False
            smis_id = int(row["smis_id"])
            item_key = str(row["item_key"])
            conn.execute("DELETE FROM rules WHERE id=?", (int(rule_id),))
            conn.execute("DELETE FROM rule_states WHERE rule_id=?", (int(rule_id),))
            self._cancel_pending_rule_jobs(conn, int(rule_id), recipient_key)
            session_remaining = conn.execute(
                "SELECT COUNT(*) FROM rules "
                "WHERE smis_id=? AND recipient_key=? AND enabled=1 AND operational=1",
                (smis_id, recipient_key),
            ).fetchone()[0]
            if not session_remaining:
                conn.execute(
                    "DELETE FROM rule_health WHERE smis_id=? AND recipient_key=?",
                    (smis_id, recipient_key),
                )
                self._cancel_pending_health_jobs(conn, item_key, recipient_key)
            remaining = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE smis_id=?", (smis_id,)
            ).fetchone()[0]
            if not remaining:
                conn.execute("DELETE FROM items WHERE smis_id=?", (smis_id,))
                conn.execute("DELETE FROM rule_health WHERE smis_id=?", (smis_id,))
        return True

    def cancel_pending_rule_notifications(
        self,
        rule_id: int,
        recipient_key: str,
    ) -> int:
        with self.connect() as conn:
            return self._cancel_pending_rule_jobs(conn, rule_id, recipient_key)

    def get_rule_state(self, rule_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rule_states WHERE rule_id=?", (int(rule_id),)
            ).fetchone()
        state = dict(DEFAULT_RULE_STATE)
        if row:
            state.update(dict(row))
        state["rule_id"] = int(rule_id)
        return state

    def update_rule_state(self, rule_id: int, **changes: Any) -> dict[str, Any]:
        state = self.get_rule_state(rule_id)
        for key, value in changes.items():
            if key not in DEFAULT_RULE_STATE:
                raise KeyError(f"未知规则状态字段: {key}")
            state[key] = value
        integer_values = {
            key: parse_rule_state_integer(state[key])
            for key in (
                "alert_active",
                "qualifying_count",
                "clearing_count",
                "highest_notified_level",
            )
        }
        if (
            any(value is None for value in integer_values.values())
            or integer_values["alert_active"] not in {0, 1}
        ):
            raise ValueError("规则状态整数超出有效范围")
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO rule_states(
                    rule_id,alert_active,qualifying_count,clearing_count,last_value,
                    last_baseline,last_observed_at,last_signal_at,
                    highest_notified_level,status,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    alert_active=excluded.alert_active,
                    qualifying_count=excluded.qualifying_count,
                    clearing_count=excluded.clearing_count,
                    last_value=excluded.last_value,
                    last_baseline=excluded.last_baseline,
                    last_observed_at=excluded.last_observed_at,
                    last_signal_at=excluded.last_signal_at,
                    highest_notified_level=excluded.highest_notified_level,
                    status=excluded.status,updated_at=excluded.updated_at
            """, (
                int(rule_id), integer_values["alert_active"],
                integer_values["qualifying_count"], integer_values["clearing_count"],
                state["last_value"], state["last_baseline"],
                state["last_observed_at"], state["last_signal_at"],
                integer_values["highest_notified_level"], state["status"], now,
            ))
        return self.get_rule_state(rule_id)

    def reset_rule_states(
        self,
        rule_type: str,
        *,
        operational_only: bool = False,
    ) -> int:
        """Reset evaluation state and cancel pending alerts for one rule type."""
        operational_clause = " AND enabled=1 AND operational=1" if operational_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,recipient_key FROM rules WHERE rule_type=?"
                f"{operational_clause}",
                (rule_type,),
            ).fetchall()
            for row in rows:
                self._cancel_pending_rule_jobs(
                    conn,
                    int(row["id"]),
                    str(row["recipient_key"]),
                )
                conn.execute(
                    "DELETE FROM rule_states WHERE rule_id=?", (int(row["id"]),)
                )
        return len(rows)

    def get_health_state(self, smis_id: int, recipient_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rule_health WHERE smis_id=? AND recipient_key=?",
                (int(smis_id), recipient_key),
            ).fetchone()
        state = dict(DEFAULT_HEALTH_STATE)
        if row:
            state.update(dict(row))
        return state

    def update_health_state(
        self, smis_id: int, recipient_key: str, **changes: Any
    ) -> dict:
        state = self.get_health_state(smis_id, recipient_key)
        state.update(changes)
        failures = parse_rule_state_integer(state["fetch_failures"])
        alerted = parse_rule_state_integer(state["health_alerted"])
        if failures is None or alerted not in {0, 1}:
            raise ValueError("健康状态整数超出有效范围")
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO rule_health(
                    smis_id,recipient_key,fetch_failures,health_alerted,updated_at
                ) VALUES(?,?,?,?,?) ON CONFLICT(smis_id,recipient_key) DO UPDATE SET
                    fetch_failures=excluded.fetch_failures,
                    health_alerted=excluded.health_alerted,updated_at=excluded.updated_at
            """, (int(smis_id), recipient_key, failures, alerted, now))
        return self.get_health_state(smis_id, recipient_key)

    def enqueue_notification(
        self, signal_key: str, recipient_key: str, event_type: str, title: str,
        content: str, *, driver: str, rule_id: int | None = None,
    ) -> bool:
        now = self._utcnow()
        with self.connect() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO alert_events(
                    signal_key,recipient_key,event_type,title,content,rule_id,created_at
                ) VALUES(?,?,?,?,?,?,?)
            """, (signal_key, recipient_key, event_type, title, content, rule_id, now))
            if not cursor.rowcount:
                return False
            conn.execute("""
                INSERT INTO delivery_jobs(event_id,driver,status,attempts,next_attempt_at)
                VALUES(?,?,'pending',0,?)
            """, (int(cursor.lastrowid), driver, now))
            return True

    def due_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        now = self._utcnow()
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT j.id,j.event_id,j.driver,j.status,j.attempts,j.last_error,
                       j.next_attempt_at,e.recipient_key,e.title,e.content,e.event_type
                FROM delivery_jobs j JOIN alert_events e ON e.id=j.event_id
                WHERE j.status='pending' AND j.next_attempt_at<=?
                ORDER BY j.id LIMIT ?
            """, (now, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        now = self._utcnow()
        with self.connect() as conn:
            conn.execute("""
                UPDATE delivery_jobs SET status='sent', delivered_at=?, last_error=NULL
                WHERE id=?
            """, (now, int(notification_id)))

    def mark_notification_failed(self, notification_id: int, error: str) -> None:
        from datetime import timedelta

        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM delivery_jobs WHERE id=?", (int(notification_id),)
            ).fetchone()
            attempts = int(row[0] if row else 0) + 1
            delay = min(60 * (2 ** min(attempts - 1, 5)), 1800)
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            conn.execute("""
                UPDATE delivery_jobs
                SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?
            """, (attempts, error[:1000], next_attempt, int(notification_id)))

    def outbox_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM delivery_jobs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_events(
        self, recipient_key: str, *, acknowledged: bool | None = False, limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["e.recipient_key=?"]
        params: list[Any] = [recipient_key]
        if acknowledged is not None:
            clauses.append("e.acknowledged_at IS NOT NULL" if acknowledged else
                           "e.acknowledged_at IS NULL")
        params.append(max(1, min(int(limit), 100)))
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT e.*,
                       COALESCE(j.status,'stored') AS delivery_status,
                       j.driver AS delivery_driver,j.attempts,j.last_error,j.delivered_at
                FROM alert_events e
                LEFT JOIN delivery_jobs j ON j.event_id=e.id
                WHERE {' AND '.join(clauses)}
                ORDER BY e.id DESC LIMIT ?
            """, params).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_event(self, event_id: int, recipient_key: str) -> dict | None:
        now = self._utcnow()
        with self.connect() as conn:
            cursor = conn.execute("""
                UPDATE alert_events SET acknowledged_at=COALESCE(acknowledged_at,?)
                WHERE id=? AND recipient_key=?
            """, (now, int(event_id), recipient_key))
            if not cursor.rowcount:
                return None
            row = conn.execute("SELECT * FROM alert_events WHERE id=?", (int(event_id),)).fetchone()
        return dict(row) if row else None

    def backup(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def save_snapshots(self, snapshots: list[MarketSnapshot]) -> int:
        if not snapshots:
            return 0
        rows = []
        for s in snapshots:
            rows.append((
                s.source, s.item_key, s.smis_id, s.appid, s.name, s.name_zh,
                s.observed_at.isoformat(), s.source_updated_at.isoformat(), s.kind,
                s.buff_sell_price, s.buff_sell_num,
                s.uuyp_sell_price, s.uuyp_sell_num, s.c5_sell_price, s.c5_sell_num,
                s.igxe_sell_price, s.igxe_sell_num, s.eco_sell_price, s.eco_sell_num,
                s.steam_sell_price,
                s.steam_sell_num, s.steam_transaction_quantity,
                s.ratio(),
            ))
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany("""
                INSERT OR IGNORE INTO market_snapshots (
                    source,item_key,smis_id,appid,name,name_zh,observed_at,
                    source_updated_at,kind,buff_sell_price,buff_sell_num,
                    uuyp_sell_price,uuyp_sell_num,c5_sell_price,c5_sell_num,
                    igxe_sell_price,igxe_sell_num,eco_sell_price,eco_sell_num,
                    steam_sell_price,steam_sell_num,steam_transaction_quantity,
                    buff_to_steam_ratio
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            return conn.total_changes - before

    def steam_history(
        self,
        item_key: str,
        days: int = 7,
        *,
        max_current_lag_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT kind,observed_at,source_updated_at,steam_sell_price
                FROM market_snapshots
                WHERE item_key=? AND source_updated_at>=? AND steam_sell_price>0
                ORDER BY source_updated_at
            """, (item_key, cutoff)).fetchall()
        deduped: dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                source = datetime.fromisoformat(str(row["source_updated_at"]))
                if source.tzinfo is None:
                    source = source.replace(tzinfo=timezone.utc)
                if (
                    source.astimezone(timezone.utc)
                    > now + timedelta(seconds=MAX_SOURCE_FUTURE_SKEW_SECONDS)
                ):
                    continue
                if (
                    str(row["kind"]) == "current"
                    and max_current_lag_seconds is not None
                ):
                    observed = datetime.fromisoformat(str(row["observed_at"]))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    lag_seconds = (observed - source).total_seconds()
                    if (
                        lag_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS
                        or lag_seconds > max(0, int(max_current_lag_seconds))
                    ):
                        continue
            except (TypeError, ValueError):
                continue
            deduped[str(row["source_updated_at"])] = float(row["steam_sell_price"])
        return [
            {"source_updated_at": key, "steam_sell_price": value}
            for key, value in sorted(deduped.items())
        ]

    def market_history_rows(
        self, item_key: str, days: int = 30,
    ) -> list[dict[str, Any]]:
        """Return raw successful observations for canonical analysis."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM market_snapshots
                WHERE item_key=? AND observed_at>=?
                ORDER BY observed_at,id
            """, (item_key, cutoff)).fetchall()
        return [dict(row) for row in rows]

    def prune_snapshots(self, retain_days: int = 30) -> int:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retain_days))).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM market_snapshots "
                "WHERE observed_at<? OR datetime(observed_at) IS NULL",
                (cutoff,),
            )
            return int(cursor.rowcount)

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, now))

    def history_ratios(self, item_key: str) -> list[float]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT buff_to_steam_ratio FROM market_snapshots
                WHERE item_key=? AND kind='history' AND buff_to_steam_ratio IS NOT NULL
                ORDER BY observed_at
            """, (item_key,)).fetchall()
        return [float(row[0]) for row in rows]

    def latest_snapshot(self, item_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT * FROM market_snapshots WHERE item_key=? AND kind='current'
                  AND datetime(observed_at) IS NOT NULL
                ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1
            """, (item_key,)).fetchone()
        return dict(row) if row else None

    def recent_current(self, item_key: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM market_snapshots WHERE item_key=? AND kind='current'
                  AND datetime(observed_at) IS NOT NULL
                ORDER BY julianday(observed_at) DESC,id DESC LIMIT ?
            """, (item_key, limit)).fetchall()
        return [dict(row) for row in rows]
