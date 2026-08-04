import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event

from core.alerting import evaluate_rule_transition
from core.manager import MonitoringManager, ServiceError
from core.market import MarketSnapshot
from core.repository import MonitorStorage
from core.rules import (
    MAX_RULE_STATE_INTEGER,
    RuleMeasurement,
    get_rule_definition,
    measure_rule,
    parse_rule_state_integer,
    validate_rule,
)


def market_snapshot() -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        item_key="smis:1579",
        smis_id=1579,
        appid=730,
        name="Fracture Case",
        name_zh="裂空武器箱",
        observed_at=now,
        source_updated_at=now,
        buff_sell_price=3.20,
        buff_sell_num=100,
        steam_sell_price=5.34,
        steam_sell_num=100,
        steam_transaction_quantity=50,
    )


class StaticSource:
    def __init__(self):
        self.current_calls = 0

    def search_items(self, query, limit=10):
        return []

    def fetch_metadata(self, smis_id):
        raise RuntimeError("unused")

    def fetch_current(self, item):
        self.current_calls += 1
        return market_snapshot()

    def fetch_history(self, item, days=30):
        return []


class BarrierCatalogSource(StaticSource):
    def __init__(self, parties: int):
        self.barrier = Barrier(parties)

    def fetch_metadata(self, smis_id):
        self.barrier.wait(timeout=5)
        return {
            "smis_id": int(smis_id),
            "item_key": f"smis:{int(smis_id)}",
            "appid": 730,
            "name": f"Item {int(smis_id)}",
            "name_zh": f"饰品 {int(smis_id)}",
        }


class BlockingSource(StaticSource):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def fetch_current(self, item):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release current fetch")
        return super().fetch_current(item)


class RuleContractTestCase(unittest.TestCase):
    def test_non_finite_thresholds_are_rejected(self):
        for rule_type in ("ratio", "t7", "platform", "steam"):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(rule_type=rule_type, value=value):
                    error = validate_rule(rule_type, value)
                    self.assertEqual(error[0], "invalid_threshold")

    def test_unknown_rule_measurement_fails_closed(self):
        measurement = measure_rule(market_snapshot(), {}, "future-rule")

        self.assertIsNone(measurement.value)
        self.assertIsNone(measurement.baseline)
        self.assertIn("不支持的规则类型", measurement.status)

    def test_persisted_rule_identity_is_strict(self):
        self.assertIsNone(get_rule_definition(" PLATFORM "))
        self.assertIsNone(get_rule_definition("Platform"))

    def test_transition_is_pure_and_preserves_reentry_semantics(self):
        definition = get_rule_definition("ratio")
        state = {
            "alert_active": 1,
            "qualifying_count": 0,
            "clearing_count": 0,
            "highest_notified_level": 0,
        }
        transition = evaluate_rule_transition(
            rule_id=7,
            definition=definition,
            threshold=72,
            state=state,
            measurement=RuleMeasurement(0.69, 4.67, "ready"),
            observed_at="2026-07-29T00:00:00+00:00",
            confirmations=1,
            clear_confirmations=1,
            breakthrough_step=0.03,
        )

        self.assertEqual(state["highest_notified_level"], 0)
        self.assertEqual(transition.signal.style, "breakthrough")
        self.assertEqual(transition.signal.level, 1)
        self.assertEqual(transition.changes["highest_notified_level"], 1)

    def test_invalid_measurements_fail_closed(self):
        definition = get_rule_definition("platform")
        state = {
            "alert_active": 0,
            "qualifying_count": 0,
            "clearing_count": 0,
            "highest_notified_level": 0,
        }
        for value in (math.nan, math.inf, -math.inf, 0, -1):
            with self.subTest(value=value):
                transition = evaluate_rule_transition(
                    rule_id=7,
                    definition=definition,
                    threshold=4.0,
                    state=state,
                    measurement=RuleMeasurement(value, math.inf, "ready"),
                    observed_at="2026-07-29T00:00:00+00:00",
                    confirmations=1,
                    clear_confirmations=1,
                    breakthrough_step=0.03,
                )
                self.assertIsNone(transition.signal)
                self.assertIsNone(transition.changes["last_value"])
                self.assertIsNone(transition.changes["last_baseline"])
                self.assertEqual(transition.changes["status"], "测量值无效")

    def test_corrupt_persisted_counters_are_normalized(self):
        definition = get_rule_definition("platform")
        transition = evaluate_rule_transition(
            rule_id=7,
            definition=definition,
            threshold=4.0,
            state={
                "alert_active": math.inf,
                "qualifying_count": "broken",
                "clearing_count": None,
                "highest_notified_level": "broken",
            },
            measurement=RuleMeasurement(3.2, None, "ready"),
            observed_at="2026-07-29T00:00:00+00:00",
            confirmations=1,
            clear_confirmations=1,
            breakthrough_step=0.03,
        )

        self.assertIsNone(transition.signal)
        self.assertEqual(transition.changes["alert_active"], 0)
        self.assertEqual(transition.changes["status"], "规则状态无效")

    def test_fractional_and_oversized_state_counters_fail_closed(self):
        definition = get_rule_definition("platform")
        for value in (0.5, 1e20):
            with self.subTest(value=value):
                transition = evaluate_rule_transition(
                    rule_id=7,
                    definition=definition,
                    threshold=4.0,
                    state={
                        "alert_active": 0,
                        "qualifying_count": value,
                        "clearing_count": 0,
                        "highest_notified_level": 0,
                    },
                    measurement=RuleMeasurement(3.2, None, "ready"),
                    observed_at="2026-07-29T00:00:00+00:00",
                    confirmations=1,
                    clear_confirmations=1,
                    breakthrough_step=0.03,
                )

                self.assertIsNone(transition.signal)
                self.assertEqual(transition.changes["status"], "规则状态无效")

    def test_state_integer_parser_preserves_sqlite_integer_maximum(self):
        self.assertEqual(
            parse_rule_state_integer(MAX_RULE_STATE_INTEGER),
            MAX_RULE_STATE_INTEGER,
        )
        self.assertIsNone(parse_rule_state_integer(MAX_RULE_STATE_INTEGER + 1))

    def test_non_finite_market_prices_fail_closed(self):
        base = market_snapshot()
        platform_snapshot = MarketSnapshot(
            **{
                **base.__dict__,
                "buff_sell_price": math.inf,
            }
        )
        steam_snapshot = MarketSnapshot(
            **{
                **base.__dict__,
                "steam_sell_price": math.inf,
            }
        )

        self.assertIsNone(measure_rule(platform_snapshot, {}, "platform").value)
        self.assertIsNone(measure_rule(steam_snapshot, {}, "steam").value)
        self.assertIsNone(measure_rule(steam_snapshot, {}, "ratio").value)

    def test_corrupt_rule_does_not_block_later_valid_rule(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            invalid = storage.add_rule(1579, "umo:a", "future-rule", 10)
            valid = storage.add_rule(1579, "umo:a", "ratio", 72)
            manager = MonitoringManager(storage, StaticSource())

            result = manager.run_cycle(max_workers=1)

            self.assertTrue(result[0]["success"])
            invalid_state = storage.get_rule_state(invalid["id"])
            self.assertFalse(invalid_state["alert_active"])
            self.assertIn("不支持的规则类型", invalid_state["status"])
            self.assertTrue(storage.get_rule_state(valid["id"])["alert_active"])
            events = storage.list_events("umo:a", acknowledged=None)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "rule_ratio")

    def test_manager_rejects_non_finite_threshold_before_storage(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            manager = MonitoringManager(storage, StaticSource())
            with self.assertRaises(ServiceError):
                manager.add_rule("umo:a", 1579, "steam", math.inf)
            self.assertEqual(storage.list_rules(), [])

    def test_persisted_non_finite_threshold_is_quarantined(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", math.inf)
            storage.update_rule_state(
                int(rule["id"]),
                alert_active=1,
                last_value=3.2,
            )
            storage.enqueue_notification(
                f"rule:{rule['id']}:old",
                "umo:a",
                "rule_platform",
                "old invalid threshold",
                "body",
                driver="astrbot",
                rule_id=int(rule["id"]),
            )
            storage.update_health_state(
                1579,
                "umo:a",
                fetch_failures=3,
                health_alerted=1,
            )
            storage.enqueue_notification(
                "health:smis:1579:umo:a:down:3",
                "umo:a",
                "health_down",
                "old health alert",
                "body",
                driver="astrbot",
            )

            source = StaticSource()
            manager = MonitoringManager(storage, source)

            state = storage.get_rule_state(int(rule["id"]))
            self.assertFalse(state["alert_active"])
            self.assertIsNone(state["last_value"])
            self.assertIn("规则配置无效", state["status"])
            self.assertEqual(storage.due_notifications(), [])
            self.assertEqual(
                storage.get_health_state(1579, "umo:a")["fetch_failures"],
                0,
            )
            self.assertEqual(
                len(storage.list_events("umo:a", acknowledged=None)),
                2,
            )
            quarantined = storage.get_rule(int(rule["id"]))
            self.assertEqual(quarantined["operational"], 0)
            self.assertEqual(storage.count_rule_items(), 0)
            self.assertEqual(manager.run_cycle(max_workers=1), [])
            self.assertEqual(source.current_calls, 0)
            self.assertEqual(len(manager.list_rules("umo:a")), 1)

            updated = manager.update_rule("umo:a", int(rule["id"]), 4.0)
            self.assertEqual(updated["operational"], 1)
            self.assertEqual(storage.count_rule_items(), 1)

    def test_corrupt_persisted_state_is_quarantined_during_startup(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", 4.0)
            storage.update_rule_state(int(rule["id"]), alert_active=1, last_value=3.2)
            with storage.connect() as conn:
                conn.execute(
                    """
                    UPDATE rule_states
                    SET alert_active='broken',qualifying_count='broken',
                        clearing_count='broken',last_value='broken',
                        highest_notified_level='broken'
                    WHERE rule_id=?
                """,
                    (int(rule["id"]),),
                )

            MonitoringManager(storage, StaticSource())

            state = storage.get_rule_state(int(rule["id"]))
            self.assertFalse(state["alert_active"])
            self.assertEqual(state["qualifying_count"], 0)
            self.assertEqual(state["clearing_count"], 0)
            self.assertIsNone(state["last_value"])
            self.assertEqual(state["highest_notified_level"], 0)
            self.assertEqual(state["status"], "规则状态无效")
            self.assertEqual(storage.get_rule(int(rule["id"]))["operational"], 0)

    def test_oversized_finite_state_is_quarantined_before_alerting(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", 4.0)
            storage.update_rule_state(int(rule["id"]), qualifying_count=1)
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE rule_states SET qualifying_count=1e20 WHERE rule_id=?",
                    (int(rule["id"]),),
                )

            source = StaticSource()
            manager = MonitoringManager(storage, source)

            self.assertEqual(storage.get_rule(int(rule["id"]))["operational"], 0)
            self.assertEqual(manager.run_cycle(max_workers=1), [])
            self.assertEqual(source.current_calls, 0)
            self.assertEqual(storage.list_events("umo:a", acknowledged=None), [])

    def test_active_fractional_state_is_audited_before_level_sync(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", 4.0)
            storage.update_rule_state(
                int(rule["id"]),
                alert_active=1,
                last_value=3.2,
            )
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE rule_states SET qualifying_count=0.5,highest_notified_level=0.5 WHERE rule_id=?",
                    (int(rule["id"]),),
                )

            MonitoringManager(storage, StaticSource())

            self.assertEqual(storage.get_rule(int(rule["id"]))["operational"], 0)
            self.assertEqual(
                storage.get_rule_state(int(rule["id"]))["status"],
                "规则状态无效",
            )

    def test_runtime_state_corruption_quarantines_rule_permanently(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", 4.0)
            manager = MonitoringManager(storage, StaticSource())
            storage.update_rule_state(int(rule["id"]), qualifying_count=1)
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE rule_states SET qualifying_count=0.5 WHERE rule_id=?",
                    (int(rule["id"]),),
                )

            first = manager.run_cycle(max_workers=1)
            second = manager.run_cycle(max_workers=1)

            self.assertTrue(first[0]["success"])
            self.assertEqual(second, [])
            self.assertEqual(storage.get_rule(int(rule["id"]))["operational"], 0)
            self.assertEqual(storage.list_events("umo:a", acknowledged=None), [])

    def test_concurrent_new_items_cannot_exceed_limit(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            for index in range(19):
                smis_id = 1000 + index
                storage.upsert_item(
                    {
                        "smis_id": smis_id,
                        "item_key": f"smis:{smis_id}",
                        "appid": 730,
                        "name": f"Item {smis_id}",
                        "name_zh": f"饰品 {smis_id}",
                    }
                )
                storage.add_rule(
                    smis_id,
                    f"umo:{index}",
                    "ratio",
                    72,
                )
            manager = MonitoringManager(
                storage,
                BarrierCatalogSource(2),
                max_items=20,
            )

            def add_item(smis_id: int) -> str:
                try:
                    manager.add_rule("umo:new", smis_id, "ratio", 72)
                except ServiceError as exc:
                    return exc.code
                return "ok"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(add_item, (2000, 2001)))

            self.assertEqual(sorted(results), ["item_limit", "ok"])
            self.assertEqual(storage.count_rule_items(), 20)
            self.assertEqual(storage.count_items(), 20)

    def test_deleted_rule_cannot_commit_stale_transition_or_alert(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = storage.add_rule(1579, "umo:a", "platform", 4.0)
            source = BlockingSource()
            manager = MonitoringManager(storage, source)
            item = storage.get_item(1579)

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(manager.monitor_item, item)
                self.assertTrue(source.started.wait(timeout=5))
                self.assertTrue(storage.delete_rule(int(rule["id"]), "umo:a"))
                source.release.set()
                self.assertTrue(future.result(timeout=5)["success"])

            self.assertIsNone(storage.get_rule(int(rule["id"])))
            self.assertEqual(
                storage.list_events("umo:a", acknowledged=None),
                [],
            )
            with storage.connect() as conn:
                state = conn.execute(
                    "SELECT 1 FROM rule_states WHERE rule_id=?",
                    (int(rule["id"]),),
                ).fetchone()
            self.assertIsNone(state)

    def test_corrupt_operational_value_cannot_bypass_item_limit(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            for smis_id in (1001, 1002):
                storage.upsert_item(
                    {
                        "smis_id": smis_id,
                        "item_key": f"smis:{smis_id}",
                        "appid": 730,
                        "name": f"Item {smis_id}",
                        "name_zh": f"饰品 {smis_id}",
                    }
                )
            storage.add_rule(1001, "umo:a", "ratio", 72)
            second = storage.add_rule(1002, "umo:a", "ratio", 72)
            storage.set_rule_operational(
                int(second["id"]),
                "umo:a",
                False,
            )
            manager = MonitoringManager(storage, StaticSource(), max_items=1)
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE rules SET operational=1e20 WHERE id=?",
                    (int(second["id"]),),
                )

            with self.assertRaises(ServiceError) as caught:
                manager.update_rule("umo:a", int(second["id"]), 73)

            self.assertEqual(caught.exception.code, "item_limit")
            self.assertEqual(storage.count_rule_items(), 1)


if __name__ == "__main__":
    unittest.main()
