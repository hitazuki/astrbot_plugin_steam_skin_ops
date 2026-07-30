from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import repository
from core.repository import SCHEMA_VERSION, MonitorStorage


class RepositorySchemaTests(unittest.TestCase):
    def test_future_schema_is_rejected_without_touching_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE sentinel (value TEXT NOT NULL);
                INSERT INTO metadata VALUES('schema_version','99','unchanged');
                INSERT INTO sentinel VALUES('keep me');
            """)
            conn.commit()
            conn.close()

            before_bytes = path.read_bytes()
            before_entries = sorted(entry.name for entry in path.parent.iterdir())

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                MonitorStorage(path)

            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(
                sorted(entry.name for entry in path.parent.iterdir()),
                before_entries,
            )
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                schema = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(schema, [("metadata",), ("sentinel",)])

    def test_future_schema_in_wal_is_rejected_before_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future-wal.db"
            writer = sqlite3.connect(path)
            try:
                writer.executescript("""
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE sentinel (value TEXT NOT NULL);
                    INSERT INTO metadata VALUES('schema_version','4','main-file');
                    INSERT INTO sentinel VALUES('keep me');
                """)
                writer.commit()
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                writer.execute(
                    "UPDATE metadata SET value='99',updated_at='wal-file' "
                    "WHERE key='schema_version'"
                )
                writer.commit()
                before_schema = writer.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                before_entries = sorted(entry.name for entry in path.parent.iterdir())

                with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                    MonitorStorage(path)

                self.assertEqual(
                    writer.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()[0],
                    "99",
                )
                self.assertEqual(
                    writer.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' ORDER BY name"
                    ).fetchall(),
                    before_schema,
                )
                self.assertEqual(
                    sorted(entry.name for entry in path.parent.iterdir()),
                    before_entries,
                )
            finally:
                writer.close()

    def test_closed_wal_database_is_rejected_without_creating_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future-closed-wal.db"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO metadata VALUES('schema_version','99','unchanged');
            """)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            before_entries = sorted(entry.name for entry in path.parent.iterdir())

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                MonitorStorage(path)

            self.assertEqual(
                sorted(entry.name for entry in path.parent.iterdir()),
                before_entries,
            )

    def test_new_database_omits_unused_legacy_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            with storage.connect() as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

            self.assertEqual(storage.get_metadata("schema_version"), str(SCHEMA_VERSION))
            self.assertIn("recommendation_history_cache", tables)
            self.assertNotIn("monitor_state", tables)
            self.assertNotIn("notification_events", tables)
            self.assertFalse(hasattr(repository, "DEFAULT_STATE"))
            for method in (
                "get_state",
                "update_state",
                "record_notification",
                "was_notification_sent",
            ):
                self.assertFalse(hasattr(MonitorStorage, method))

    def test_v7_migration_adds_recommendation_cache_without_touching_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v7.db"
            storage = MonitorStorage(path)
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "smis:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })
            rule = storage.add_rule(1579, "recipient:a", "ratio", 72)
            with storage.connect() as conn:
                conn.execute("DROP TABLE recommendation_history_cache")
                conn.execute(
                    "UPDATE metadata SET value='7' WHERE key='schema_version'"
                )

            migrated = MonitorStorage(path)

            self.assertEqual(
                migrated.get_metadata("schema_version"),
                str(SCHEMA_VERSION),
            )
            self.assertEqual(migrated.list_rules()[0]["id"], rule["id"])
            with migrated.connect() as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='recommendation_history_cache'"
                ).fetchone()
            self.assertIsNotNone(table)

    def test_v3_extreme_finite_state_migrates_without_integer_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v3-extreme.db"
            storage = MonitorStorage(path)
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "smis:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })
            rule = storage.add_rule(1579, "recipient:a", "steam", 1.0)
            storage.update_rule_state(
                int(rule["id"]),
                alert_active=1,
                last_value=1e20,
            )
            storage.set_metadata("schema_version", "3")

            migrated = MonitorStorage(path)

            self.assertEqual(
                migrated.get_metadata("schema_version"),
                str(SCHEMA_VERSION),
            )
            self.assertEqual(
                migrated.get_rule_state(int(rule["id"]))["highest_notified_level"],
                0,
            )

    def test_v4_migration_disables_duplicate_rules_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v4.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO metadata VALUES('schema_version','4','old');

                CREATE TABLE items (
                    smis_id INTEGER PRIMARY KEY,
                    item_key TEXT NOT NULL UNIQUE,
                    appid INTEGER NOT NULL,
                    hash_name TEXT NOT NULL,
                    cn_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO items VALUES(
                    1579,'item:1579',730,'Test Item','测试饰品',1,'old','old'
                );

                CREATE TABLE rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    smis_id INTEGER NOT NULL,
                    recipient_key TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO rules VALUES(10,1579,'recipient:a','steam',5.4,1,'a','a');
                INSERT INTO rules VALUES(11,1579,'recipient:a','steam',5.6,1,'b','b');
                INSERT INTO rules VALUES(12,1579,'recipient:a','steam',5.8,0,'c','c');
                INSERT INTO rules VALUES(13,1579,'recipient:a','ratio',72,1,'d','d');
                INSERT INTO rules VALUES(14,1579,'recipient:a','platform',4.0,1,'e','e');
                INSERT INTO rules VALUES(15,1579,'recipient:a',' PLATFORM ',4.2,1,'f','f');
                INSERT INTO rules VALUES(16,1579,'recipient:a','t7',75,1,'g','g');
                INSERT INTO rules VALUES(17,1579,'recipient:a','t7',1e999,1,'h','h');

                CREATE TABLE rule_states (
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
                INSERT INTO rule_states VALUES(
                    10,0,0,0,6.0,5.4,'old',NULL,0,'waiting','old'
                );
                INSERT INTO rule_states VALUES(
                    11,1,0,0,6.0,5.6,'old','old',2,'active','old'
                );
                INSERT INTO rule_states VALUES(
                    17,1e999,0,0,0.7,5.6,'old','old',2,'active','old'
                );

                CREATE TABLE alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    rule_id INTEGER,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE(signal_key,recipient_key)
                );
                INSERT INTO alert_events VALUES(
                    20,'rule:10:old','recipient:a','trigger','first','body',10,'old',NULL
                );
                INSERT INTO alert_events VALUES(
                    21,'rule:11:old','recipient:a','trigger','second','body',11,'old',NULL
                );

                CREATE TABLE delivery_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    driver TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(event_id,driver)
                );
                INSERT INTO delivery_jobs VALUES(
                    30,20,'astrbot','pending',0,NULL,'old',NULL
                );
                INSERT INTO delivery_jobs VALUES(
                    31,21,'astrbot','pending',0,NULL,'old',NULL
                );

                CREATE TABLE monitor_state (item_key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO monitor_state VALUES('legacy','keep');
                CREATE TABLE notification_events (
                    id INTEGER PRIMARY KEY, signal_key TEXT NOT NULL
                );
                INSERT INTO notification_events VALUES(1,'legacy-alert');
            """)
            conn.commit()
            conn.close()

            storage = MonitorStorage(path)
            with storage.connect() as conn:
                rules = conn.execute(
                    "SELECT id,enabled,operational,rule_type,threshold,created_at "
                    "FROM rules ORDER BY id"
                ).fetchall()
                states = conn.execute(
                    "SELECT rule_id,last_value FROM rule_states ORDER BY rule_id"
                ).fetchall()
                alerts = conn.execute(
                    "SELECT id,rule_id,title FROM alert_events ORDER BY id"
                ).fetchall()
                legacy_state = conn.execute("SELECT * FROM monitor_state").fetchall()
                legacy_events = conn.execute(
                    "SELECT * FROM notification_events"
                ).fetchall()
                index = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND name='uq_rules_active_identity'"
                ).fetchone()
                pending_event_ids = conn.execute(
                    "SELECT event_id FROM delivery_jobs "
                    "WHERE status='pending' ORDER BY event_id"
                ).fetchall()

            self.assertEqual(
                storage.get_metadata("schema_version"),
                str(SCHEMA_VERSION),
            )
            self.assertEqual(
                [tuple(row) for row in rules],
                [
                    (10, 0, 0, "steam", 5.4, "a"),
                    (11, 1, 1, "steam", 5.6, "b"),
                    (12, 0, 0, "steam", 5.8, "c"),
                    (13, 1, 1, "ratio", 72.0, "d"),
                    (14, 1, 1, "platform", 4.0, "e"),
                    (15, 0, 0, "platform", 4.2, "f"),
                    (16, 1, 1, "t7", 75.0, "g"),
                    (17, 0, 0, "t7", float("inf"), "h"),
                ],
            )
            self.assertEqual(
                [tuple(row) for row in states],
                [(10, 6.0), (11, 6.0), (17, 0.7)],
            )
            self.assertEqual(
                [tuple(row) for row in alerts],
                [(20, 10, "first"), (21, 11, "second")],
            )
            self.assertEqual([tuple(row) for row in legacy_state], [("legacy", "keep")])
            self.assertEqual([tuple(row) for row in legacy_events], [(1, "legacy-alert")])
            self.assertEqual([int(row[0]) for row in pending_event_ids], [21])
            self.assertIsNotNone(index)
            self.assertIn("WHERE enabled=1", str(index[0]))
            self.assertEqual(
                [int(rule["id"]) for rule in storage.list_rules(recipient_key="recipient:a")],
                [11, 13, 14, 16],
            )
            self.assertEqual(
                len(
                    storage.list_rules(
                        recipient_key="recipient:a", enabled_only=False
                    )
                ),
                8,
            )

            with self.assertRaises(sqlite3.IntegrityError), storage.connect() as conn:
                conn.execute("""
                    INSERT INTO rules(
                        smis_id,recipient_key,rule_type,threshold,enabled,
                        created_at,updated_at
                    ) VALUES(1579,'recipient:a','steam',6.0,1,'new','new')
                """)

            reopened = MonitorStorage(path)
            self.assertEqual(
                [int(rule["id"]) for rule in reopened.list_rules(recipient_key="recipient:a")],
                [11, 13, 14, 16],
            )

            self.assertTrue(reopened.delete_rule(11, "recipient:a"))
            self.assertTrue(reopened.delete_rule(13, "recipient:a"))
            self.assertTrue(reopened.delete_rule(14, "recipient:a"))
            self.assertTrue(reopened.delete_rule(16, "recipient:a"))
            self.assertIsNotNone(reopened.get_item(1579))
            self.assertEqual(
                [
                    int(rule["id"])
                    for rule in reopened.list_rules(
                        recipient_key="recipient:a",
                        enabled_only=False,
                    )
                ],
                [10, 12, 15, 17],
            )
            for rule_id in (10, 12, 15, 17):
                self.assertTrue(reopened.delete_rule(rule_id, "recipient:a"))
            self.assertIsNone(reopened.get_item(1579))

    def test_v5_orphaned_disabled_rule_remains_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v5-orphan.db"
            storage = MonitorStorage(path)
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "smis:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })
            rule = storage.add_rule(1579, "recipient:a", "ratio", 72)
            storage.enqueue_notification(
                "health:smis:1579:recipient:a:down:3",
                "recipient:a",
                "health_down",
                "old migrated health alert",
                "body",
                driver="astrbot",
            )
            with storage.connect() as conn:
                conn.execute("UPDATE rules SET enabled=0 WHERE id=?", (rule["id"],))
                conn.execute("DELETE FROM items WHERE smis_id=1579")
                conn.execute(
                    "UPDATE metadata SET value='5' WHERE key='schema_version'"
                )

            migrated = MonitorStorage(path)

            self.assertEqual(migrated.due_notifications(), [])
            migrated.enqueue_notification(
                "health:smis:1579:recipient:a:down:4",
                "recipient:a",
                "health_down",
                "old health alert",
                "body",
                driver="astrbot",
            )
            self.assertEqual(len(migrated.due_notifications()), 1)

            self.assertIsNotNone(migrated.get_rule(int(rule["id"])))
            rules = migrated.list_rules(
                recipient_key="recipient:a",
                enabled_only=False,
            )
            self.assertEqual([int(row["id"]) for row in rules], [int(rule["id"])])
            self.assertIsNone(rules[0]["item_key"])
            self.assertTrue(migrated.delete_rule(int(rule["id"]), "recipient:a"))
            self.assertEqual(migrated.due_notifications(), [])

    def test_add_rule_is_concurrently_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "item:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })

            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(executor.map(
                    lambda _: storage.add_rule(
                        1579,
                        "recipient:a",
                        "platform",
                        4.2,
                    ),
                    range(32),
                ))

            self.assertEqual({int(result["id"]) for result in results}, {1})
            self.assertEqual(len(storage.list_rules()), 1)

    def test_storage_canonicalizes_rule_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "item:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })

            first = storage.add_rule(1579, "recipient:a", " PLATFORM ", 4.2)
            second = storage.add_rule(1579, "recipient:a", "platform", 4.2)

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["rule_type"], "platform")
            self.assertEqual(second["action"], "unchanged")

    def test_updating_threshold_cancels_old_pending_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item({
                "smis_id": 1579,
                "item_key": "item:1579",
                "appid": 730,
                "name": "Test Item",
                "name_zh": "测试饰品",
            })
            rule = storage.add_rule(1579, "recipient:a", "ratio", 72)
            storage.enqueue_notification(
                f"rule:{rule['id']}:old",
                "recipient:a",
                "rule_ratio",
                "old threshold",
                "body",
                driver="astrbot",
                rule_id=int(rule["id"]),
            )
            storage.enqueue_notification(
                f"rule:{rule['id']}:other-recipient",
                "recipient:b",
                "rule_ratio",
                "other recipient",
                "body",
                driver="astrbot",
                rule_id=int(rule["id"]),
            )

            storage.update_rule(int(rule["id"]), "recipient:a", 70)

            due = storage.due_notifications()
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["recipient_key"], "recipient:b")
            self.assertEqual(
                len(storage.list_events("recipient:a", acknowledged=None)),
                1,
            )


if __name__ == "__main__":
    unittest.main()
