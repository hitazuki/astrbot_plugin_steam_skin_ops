from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .rules import MAX_RULE_STATE_INTEGER
from .sqlite_schema import preflight_schema_version
from .storage import (
    MarketRepositoryMixin,
    OutboxRepositoryMixin,
    RecommendationCacheRepositoryMixin,
    RuleRepositoryMixin,
)

SCHEMA_VERSION = 8
DEFAULT_BREAKTHROUGH_STEP = 0.03


class MonitorStorage(
    RuleRepositoryMixin,
    OutboxRepositoryMixin,
    MarketRepositoryMixin,
    RecommendationCacheRepositoryMixin,
):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        current_version = preflight_schema_version(self.path) if self.path.exists() else 0
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current_version} is newer than supported v{SCHEMA_VERSION}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(current_version)

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
            if current_version < 8:
                self._migrate_v8(conn)

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Transactionally migrate v2 recipient and notification tables."""
        conn.execute("SAVEPOINT migrate_v3")
        try:
            rule_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(rules)")}
            if "umo" in rule_columns and "recipient_key" not in rule_columns:
                conn.execute("ALTER TABLE rules RENAME COLUMN umo TO recipient_key")
            health_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(rule_health)")}
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_recipient ON rules(recipient_key, enabled)")

            snapshot_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(market_snapshots)")}
            for column in (
                "uuyp_sell_price REAL",
                "uuyp_sell_num INTEGER",
                "c5_sell_price REAL",
                "c5_sell_num INTEGER",
                "igxe_sell_price REAL",
                "igxe_sell_num INTEGER",
                "eco_sell_price REAL",
                "eco_sell_num INTEGER",
            ):
                if column.split()[0] not in snapshot_columns:
                    conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {column}")

            now = self._utcnow()
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version',?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
            """,
                ("3", now),
            )
            conn.execute("RELEASE SAVEPOINT migrate_v3")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v3")
            conn.execute("RELEASE SAVEPOINT migrate_v3")
            raise

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Add persisted breakthrough levels without replaying old alerts."""
        conn.execute("SAVEPOINT migrate_v4")
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(rule_states)")}
            if "highest_notified_level" not in columns:
                conn.execute("ALTER TABLE rule_states ADD COLUMN highest_notified_level INTEGER NOT NULL DEFAULT 0")

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
                    limit = threshold / 100 if rule_type in {"ratio", "t7"} else threshold
                    if not math.isfinite(value) or not math.isfinite(limit) or value <= 0 or limit <= 0:
                        raise ValueError("invalid persisted rule state")
                    depth = (value - limit) / limit if rule_type == "steam" else (limit - value) / limit
                    level = max(
                        0,
                        int((max(0.0, depth) + 1e-12) / DEFAULT_BREAKTHROUGH_STEP),
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
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','4',?)
                ON CONFLICT(key) DO UPDATE SET value='4',updated_at=excluded.updated_at
            """,
                (now,),
            )
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
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','5',?)
                ON CONFLICT(key) DO UPDATE SET value='5',updated_at=excluded.updated_at
            """,
                (now,),
            )
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
            conn.execute(
                """
                UPDATE rules
                SET rule_type=lower(trim(rule_type)),updated_at=?
                WHERE rule_type<>lower(trim(rule_type))
            """,
                (now,),
            )
            self._deduplicate_active_rules(conn, now)
            disabled = conn.execute("SELECT id,recipient_key FROM rules WHERE enabled=0").fetchall()
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
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','6',?)
                ON CONFLICT(key) DO UPDATE SET value='6',updated_at=excluded.updated_at
            """,
                (now,),
            )
            conn.execute("RELEASE SAVEPOINT migrate_v6")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v6")
            conn.execute("RELEASE SAVEPOINT migrate_v6")
            raise

    def _migrate_v7(self, conn: sqlite3.Connection) -> None:
        """Separate configured rules from rules that are safe to execute."""
        conn.execute("SAVEPOINT migrate_v7")
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(rules)")}
            if "operational" not in columns:
                conn.execute("ALTER TABLE rules ADD COLUMN operational INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE rules SET operational=0 WHERE enabled<>1 OR operational<>1")
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
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','7',?)
                ON CONFLICT(key) DO UPDATE SET value='7',updated_at=excluded.updated_at
            """,
                (now,),
            )
            conn.execute("RELEASE SAVEPOINT migrate_v7")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v7")
            conn.execute("RELEASE SAVEPOINT migrate_v7")
            raise

    def _migrate_v8(self, conn: sqlite3.Connection) -> None:
        """Track recommendation history freshness and cache usage."""
        conn.execute("SAVEPOINT migrate_v8")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recommendation_history_cache (
                    item_key TEXT PRIMARY KEY,
                    smis_id INTEGER NOT NULL,
                    appid INTEGER NOT NULL,
                    last_fetched_at TEXT,
                    last_used_at TEXT NOT NULL,
                    last_failed_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_recommendation_cache_used
                    ON recommendation_history_cache(last_used_at)
            """)
            now = self._utcnow()
            conn.execute(
                """
                INSERT INTO metadata(key,value,updated_at) VALUES('schema_version','8',?)
                ON CONFLICT(key) DO UPDATE SET value='8',updated_at=excluded.updated_at
            """,
                (now,),
            )
            conn.execute("RELEASE SAVEPOINT migrate_v8")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT migrate_v8")
            conn.execute("RELEASE SAVEPOINT migrate_v8")
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
        cursor = conn.execute(
            """
            DELETE FROM delivery_jobs
            WHERE status='pending' AND event_id IN (
                SELECT id FROM alert_events
                WHERE recipient_key=? AND (
                    rule_id=? OR substr(signal_key,1,?)=?
                )
            )
        """,
            (recipient_key, int(rule_id), len(prefix), prefix),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _cancel_pending_health_jobs(
        conn: sqlite3.Connection,
        item_key: str,
        recipient_key: str,
    ) -> int:
        prefix = f"health:{item_key}:{recipient_key}:"
        cursor = conn.execute(
            """
            DELETE FROM delivery_jobs
            WHERE status='pending' AND event_id IN (
                SELECT id FROM alert_events
                WHERE recipient_key=? AND substr(signal_key,1,?)=?
            )
        """,
            (recipient_key, len(prefix), prefix),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

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
