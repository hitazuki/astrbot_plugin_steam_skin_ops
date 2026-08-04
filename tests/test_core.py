import json
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.evaluation import ForecastEvaluationStorage
from core.manager import MonitoringManager
from core.market import MarketSnapshot, steam_net_amount
from core.presentation import analysis_reference_lines
from core.repository import SCHEMA_VERSION, MonitorStorage
from core.rules import get_rule_definition

ITEM = {
    "smis_id": 1579,
    "item_key": "smis:1579",
    "appid": 730,
    "name": "Fracture Case",
    "name_zh": "裂空武器箱",
}


def snapshot(ratio=0.70, offset=0):
    now = datetime.now(timezone.utc) + timedelta(seconds=offset)
    steam_price = 5.34
    return MarketSnapshot(
        item_key=ITEM["item_key"],
        smis_id=1579,
        appid=730,
        name=ITEM["name"],
        name_zh=ITEM["name_zh"],
        observed_at=now,
        source_updated_at=now,
        buff_sell_price=round(steam_net_amount(steam_price) * ratio, 2),
        buff_sell_num=0,
        uuyp_sell_price=0,
        uuyp_sell_num=0,
        c5_sell_price=0,
        c5_sell_num=0,
        igxe_sell_price=0,
        igxe_sell_num=0,
        eco_sell_price=0,
        eco_sell_num=0,
        steam_sell_price=steam_price,
        steam_sell_num=0,
        steam_transaction_quantity=0,
        buff_to_steam_ratio=ratio,
    )


class FakeSource:
    def __init__(self, ratios=None):
        self.ratios = list(ratios or [0.70])
        self.current_calls = 0
        self.history_calls = 0
        self.history_days = []

    def fetch_metadata(self, smis_id):
        if int(smis_id) != 1579:
            raise RuntimeError("not found")
        return dict(ITEM)

    def search_items(self, query, limit=10):
        if "裂空" not in query and "Fracture" not in query:
            return []
        return [{"smis_id": 1579, "name_zh": "裂空武器箱", "rarity": "普通级"}][:limit]

    def fetch_current(self, item):
        self.current_calls += 1
        ratio = self.ratios.pop(0) if len(self.ratios) > 1 else self.ratios[0]
        if isinstance(ratio, Exception):
            raise ratio
        return snapshot(float(ratio), self.current_calls)

    def fetch_history(self, item, days):
        self.history_calls += 1
        self.history_days.append(days)
        result = []
        for index in range(13):
            history = snapshot(0.74, -(index * 12 * 3600))
            result.append(MarketSnapshot(**{**history.__dict__, "kind": "history"}))
        return result


class ForecastHistorySource(FakeSource):
    def fetch_history(self, item, days):
        self.history_calls += 1
        self.history_days.append(days)
        result = []
        for index in range(1, 30 * 12 + 1):
            history = snapshot(0.74, -(index * 2 * 3600))
            result.append(MarketSnapshot(**{**history.__dict__, "kind": "history"}))
        return result


class StaleSource(FakeSource):
    def fetch_current(self, item):
        value = super().fetch_current(item)
        return MarketSnapshot(
            **{
                **value.__dict__,
                "source_updated_at": datetime.now(timezone.utc) - timedelta(minutes=30),
            }
        )


class FutureSource(FakeSource):
    def fetch_current(self, item):
        value = super().fetch_current(item)
        return MarketSnapshot(
            **{
                **value.__dict__,
                "source_updated_at": datetime.now(timezone.utc) + timedelta(days=2),
            }
        )


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.storage = MonitorStorage(Path(self.tmp.name) / "monitor.db")
        self.storage.upsert_item(ITEM)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, ratios=None, cache=60):
        return MonitoringManager(
            self.storage,
            FakeSource(ratios),
            quote_cache_seconds=cache,
        )

    def stored_messages(self, event_type=None):
        sql = "SELECT recipient_key,title,content FROM alert_events"
        params = ()
        if event_type is not None:
            sql += " WHERE event_type=?"
            params = (event_type,)
        sql += " ORDER BY id"
        with self.storage.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [tuple(row) for row in rows]

    def test_existing_snapshot_table_gets_platform_columns(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute("""
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                item_key TEXT NOT NULL, smis_id INTEGER NOT NULL, appid INTEGER NOT NULL,
                name TEXT NOT NULL, name_zh TEXT NOT NULL, observed_at TEXT NOT NULL,
                source_updated_at TEXT NOT NULL, kind TEXT NOT NULL,
                buff_sell_price REAL, buff_sell_num INTEGER, steam_sell_price REAL,
                steam_sell_num INTEGER, steam_transaction_quantity INTEGER,
                buff_to_steam_ratio REAL,
                UNIQUE(source,item_key,observed_at,kind)
            )
        """)
        connection.commit()
        connection.close()
        migrated = MonitorStorage(legacy_path)
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(market_snapshots)")}
        self.assertTrue({"uuyp_sell_price", "c5_sell_price", "igxe_sell_price", "eco_sell_price"}.issubset(columns))

    def test_v2_database_migrates_recipient_and_outbox(self):
        legacy_path = Path(self.tmp.name) / "v2.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, smis_id INTEGER NOT NULL,
                umo TEXT NOT NULL, rule_type TEXT NOT NULL, threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE rule_health (
                smis_id INTEGER NOT NULL, umo TEXT NOT NULL,
                fetch_failures INTEGER NOT NULL DEFAULT 0,
                health_alerted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(smis_id,umo)
            );
            CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_key TEXT NOT NULL,
                umo TEXT NOT NULL, event_type TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                last_error TEXT, next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL,
                sent_at TEXT, UNIQUE(signal_key,umo)
            );
            INSERT INTO rules VALUES(1,1579,'recipient:a','steam',5.4,1,'now','now');
            INSERT INTO notification_outbox VALUES(
                1,'signal:a','recipient:a','test','title','body','sent',0,NULL,
                'now','now','now'
            );
        """)
        connection.commit()
        connection.close()

        migrated = MonitorStorage(legacy_path)
        migrated.upsert_item(ITEM)

        self.assertEqual(migrated.get_rule(1)["recipient_key"], "recipient:a")
        event = migrated.list_events("recipient:a", acknowledged=None)[0]
        self.assertEqual(event["delivery_status"], "sent")
        with migrated.connect() as connection:
            version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(version, str(SCHEMA_VERSION))

        reopened = MonitorStorage(legacy_path)
        self.assertEqual(reopened.get_rule(1)["recipient_key"], "recipient:a")
        self.assertEqual(len(reopened.list_events("recipient:a", acknowledged=None)), 1)

    def test_newer_database_schema_is_rejected(self):
        path = Path(self.tmp.name) / "future.db"
        storage = MonitorStorage(path)
        with storage.connect() as connection:
            connection.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            MonitorStorage(path)

    def test_v3_database_migrates_active_breakthrough_level(self):
        path = Path(self.tmp.name) / "v3.db"
        storage = MonitorStorage(path)
        storage.upsert_item(ITEM)
        rule = storage.add_rule(1579, "umo:a", "ratio", 72)
        storage.update_rule_state(rule["id"], alert_active=1, last_value=0.67)
        with storage.connect() as connection:
            connection.execute("ALTER TABLE rule_states DROP COLUMN highest_notified_level")
            connection.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")

        migrated = MonitorStorage(path)
        state = migrated.get_rule_state(rule["id"])
        self.assertEqual(state["highest_notified_level"], 2)
        MonitoringManager(
            migrated,
            FakeSource([0.67]),
            breakthrough_step_percent=1,
        )
        self.assertEqual(migrated.get_rule_state(rule["id"])["highest_notified_level"], 6)

    def test_t7_forecast_basis_upgrade_resets_only_t7_rule_state(self):
        t7_rule = self.storage.add_rule(1579, "umo:a", "t7", 72)
        ratio_rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        self.storage.update_rule_state(t7_rule["id"], alert_active=1, last_value=0.70, status="active")
        self.storage.update_rule_state(ratio_rule["id"], alert_active=1, last_value=0.70, status="active")

        MonitoringManager(self.storage, FakeSource())

        self.assertEqual(
            self.storage.get_metadata("alerting:t7_basis"),
            "seven_day_probabilistic:v2",
        )
        self.assertEqual(self.storage.get_rule_state(t7_rule["id"])["alert_active"], 0)
        self.assertEqual(self.storage.get_rule_state(ratio_rule["id"])["alert_active"], 1)

    def test_quote_uses_sixty_second_cache(self):
        manager = self.manager([0.70])
        first = manager.quote("1579")
        second = manager.quote("裂空")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(manager.source.current_calls, 1)

    def test_quote_by_id_queries_entire_market_without_registering_item(self):
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM items WHERE smis_id=1579")
        manager = self.manager([0.70])

        result = manager.quote("1579")

        self.assertEqual(result["smis_id"], 1579)
        self.assertFalse(result["cached"])
        self.assertIsNone(self.storage.get_item(1579))
        self.assertEqual(self.storage.count_items(), 0)

    def test_quote_by_name_queries_entire_market(self):
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM items WHERE smis_id=1579")
        manager = self.manager([0.70])

        result = manager.quote("裂空")

        self.assertEqual(result["name"], "Fracture Case")
        self.assertEqual(result["name_zh"], "裂空武器箱")
        self.assertEqual(manager.source.current_calls, 1)

    def test_quote_returns_all_available_platforms_and_marks_every_lowest(self):
        manager = self.manager()
        current = snapshot()
        manager.source.fetch_current = lambda item: MarketSnapshot(
            **{
                **current.__dict__,
                "buff_sell_price": 3.30,
                "buff_sell_num": 100,
                "uuyp_sell_price": 3.20,
                "uuyp_sell_num": 80,
                "c5_sell_price": 3.10,
                "c5_sell_num": 60,
                "igxe_sell_price": 3.40,
                "igxe_sell_num": 40,
                "eco_sell_price": 3.10,
                "eco_sell_num": 20,
            }
        )

        result = manager.quote("1579")

        self.assertEqual(
            [row["name"] for row in result["platforms"]],
            ["BUFF", "悠悠有品", "C5", "IGXE", "ECO"],
        )
        self.assertEqual(
            [row["name"] for row in result["platforms"] if row["is_lowest"]],
            ["C5", "ECO"],
        )

    def test_search_uses_smis_catalog(self):
        results = self.manager().search_items("裂空", limit=5)
        self.assertEqual(
            results,
            [
                {
                    "smis_id": 1579,
                    "name_zh": "裂空武器箱",
                    "rarity": "普通级",
                }
            ],
        )

    def test_item_listing_is_scoped_to_current_recipient(self):
        manager = self.manager()
        self.storage.upsert_item(
            {
                "smis_id": 2000,
                "item_key": "smis:2000",
                "appid": 730,
                "name": "Other Item",
                "name_zh": "其他饰品",
            }
        )
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        self.storage.add_rule(2000, "umo:b", "ratio", 72)

        rows = manager.list_items("umo:a")

        self.assertEqual([int(row["smis_id"]) for row in rows], [1579])

    def test_stale_quote_falls_back_when_source_fails(self):
        self.storage.save_snapshots([snapshot(0.70, -120)])
        manager = self.manager([RuntimeError("down")], cache=1)
        result = manager.quote("1579")
        self.assertTrue(result["stale"])
        self.assertIn("实时刷新失败", result["warning"])
        self.assertEqual(result["forecast"]["status"], "stale")
        self.assertEqual(result["risk_assessment"]["status"], "unavailable")

    def test_ratio_rules_are_session_isolated_and_rearm_after_three_percent_margin(self):
        manager = self.manager([0.70, 0.70, 0.75, 0.75, 0.70, 0.70])
        first = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        second = self.storage.add_rule(1579, "umo:b", "ratio", 68)
        for _ in range(6):
            manager.run_cycle(max_workers=1)
        recipients = [message[0] for message in self.stored_messages()]
        self.assertEqual(recipients, ["umo:a", "umo:a"])
        self.assertEqual(self.storage.get_rule_state(second["id"])["alert_active"], 0)
        self.assertEqual(self.storage.get_rule_state(first["id"])["alert_active"], 1)

    def test_rule_fires_on_first_qualifying_cycle(self):
        manager = self.manager([0.70])
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        manager.run_cycle(max_workers=1)

        self.assertEqual(len(self.stored_messages()), 1)
        self.assertEqual(self.storage.get_rule_state(rule["id"])["alert_active"], 1)

    def test_breakthrough_levels_are_arithmetic_and_do_not_repeat(self):
        manager = self.manager([0.72, 0.69, 0.70, 0.66, 0.75, 0.69])
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        manager.run_cycle(max_workers=1)  # initial, level 0
        manager.run_cycle(max_workers=1)  # level 1
        manager.run_cycle(max_workers=1)  # back below level 1, no repeat
        manager.run_cycle(max_workers=1)  # jumps to level 2
        manager.run_cycle(max_workers=1)  # clears in one cycle
        manager.run_cycle(max_workers=1)  # new incident

        messages = self.stored_messages()
        titles = [message[1] for message in messages]
        self.assertEqual(len(titles), 4)
        self.assertIn("第 1 档", titles[1])
        self.assertIn("第 2 档", titles[2])
        self.assertIn("第 1 档", titles[3])
        breakthrough = messages[1][2]
        self.assertIn("最低平台：", breakthrough)
        self.assertIn("Steam 预计到手：", breakthrough)
        self.assertIn("七日历史基准：", breakthrough)
        self.assertIn("七日预测：", breakthrough)
        self.assertIn("风险评估：", breakthrough)
        self.assertIn("https://smis.club/commodity/1579", breakthrough)
        self.assertIn("https://steamcommunity.com/market/listings/730/", breakthrough)
        self.assertEqual(self.storage.get_rule_state(rule["id"])["highest_notified_level"], 1)

    def test_breakthrough_alerts_again_after_level_retreat_and_reentry(self):
        manager = self.manager([0.72, 0.66, 0.69, 0.66])
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        for _ in range(4):
            manager.run_cycle(max_workers=1)

        titles = [message[1] for message in self.stored_messages()]
        self.assertEqual(len(titles), 3)
        self.assertNotIn("第", titles[0])
        self.assertIn("第 2 档", titles[1])
        self.assertIn("第 2 档", titles[2])
        self.assertEqual(self.storage.get_rule_state(rule["id"])["highest_notified_level"], 2)

    def test_breakthrough_level_includes_exact_three_percent_boundary(self):
        cases = (
            ("ratio", 0.72, 0.6984, 1),
            ("t7", 0.72, 0.6984, 1),
            ("platform", 100, 97, 1),
            ("steam", 100, 106, 2),
        )
        for rule_type, limit, value, expected in cases:
            with self.subTest(rule_type=rule_type):
                definition = get_rule_definition(rule_type)
                self.assertEqual(
                    definition.breakthrough(value, limit, 0.03)[0],
                    expected,
                )

    def test_ready_analysis_message_separates_forecast_and_risk_ratios(self):
        forecast = {
            "status": "ready",
            "predicted_steam_net": 4.42,
            "change_pct": -5.4,
            "forecast_balance_ratio": 0.7217,
            "window_days": 21,
            "mode_label": "稳健对数趋势",
            "confidence": "normal",
        }
        risk = {
            "status": "ready",
            "overall_level": "high",
            "confidence": "normal",
            "risk_balance_ratio": 0.7384,
            "dimensions": {
                "price": {"level": "high"},
                "volatility": {"level": "medium"},
                "inventory": {"level": "high"},
                "volume": {"level": "medium"},
            },
            "reasons": ["预计七日下跌 5.4%"],
        }

        lines = analysis_reference_lines(forecast, risk)
        content = "\n".join(lines)

        self.assertIn("预测倒余额比例 72.17%", content)
        self.assertIn("风险倒余额比例：73.84%", content)
        self.assertIn("价格高、波动中、库存高、成交量中", content)

    def test_analysis_message_deduplicates_risk_ratio_by_floor_source(self):
        forecast = {
            "status": "ready",
            "predicted_steam_net": 4.42,
            "change_pct": -5.4,
            "forecast_balance_ratio": 0.7217,
            "window_days": 21,
            "mode_label": "稳健对数趋势",
            "confidence": "normal",
        }
        risk = {
            "status": "ready",
            "overall_level": "high",
            "confidence": "normal",
            "risk_steam_net": 4.67,
            "risk_balance_ratio": 0.6831,
            "dimensions": {key: {"level": "low"} for key in ("price", "volatility", "inventory", "volume")},
            "reasons": [],
        }

        current_lines = analysis_reference_lines(
            forecast,
            risk,
            current_steam_net=4.67,
            t7_steam_net_p25=4.60,
        )
        self.assertIn(
            "风险倒余额比例：同即时比例（当前到手价为风险底价）",
            current_lines,
        )

        risk.update({"risk_steam_net": 4.42, "risk_balance_ratio": 0.7217})
        forecast_lines = analysis_reference_lines(
            forecast,
            risk,
            current_steam_net=4.67,
            t7_steam_net_p25=4.60,
        )
        self.assertIn(
            "风险倒余额比例：同预测比例（七日预测价为风险底价）",
            forecast_lines,
        )

    def test_daily_summary_groups_active_rules_once_per_day(self):
        manager = self.manager([0.70])
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        self.storage.add_rule(1579, "umo:a", "platform", 3.40)
        manager.run_cycle(max_workers=1)

        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 1)
        messages = self.stored_messages("daily_summary")
        self.assertEqual(len(messages), 1)
        self.assertIn("共 2 条", messages[0][2])
        self.assertIn("预测", messages[0][2])
        self.assertIn("风险", messages[0][2])
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)
        self.assertEqual(len(self.stored_messages("daily_summary")), 1)

    def test_daily_forecast_is_shared_across_recipients_and_rules(self):
        evaluation = ForecastEvaluationStorage(Path(self.tmp.name) / "forecast_evaluation.db")
        manager = MonitoringManager(
            self.storage,
            ForecastHistorySource([0.70]),
            evaluation_storage=evaluation,
        )
        self.storage.add_rule(1579, "umo:a", "ratio", 1)
        self.storage.add_rule(1579, "umo:b", "platform", 1)
        manager.run_cycle(max_workers=1)

        manager.enqueue_daily_summary(date(2026, 7, 25))
        with evaluation.connect() as connection:
            rows = connection.execute("SELECT feature_json FROM forecast_issuances").fetchall()

        self.assertEqual(len(rows), 1)
        self.assertNotIn("umo:a", rows[0][0])
        self.assertNotIn("umo:b", rows[0][0])
        self.assertIn("forecast_source_digest.v1", rows[0][0])
        self.assertNotIn("daily_series_21d", rows[0][0])

    def test_empty_daily_check_prevents_later_same_day_summary(self):
        manager = self.manager([0.70])
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager.run_cycle(max_workers=1)
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)

    def test_daily_summary_skips_active_rule_when_latest_source_is_stale(self):
        manager = self.manager([0.70])
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager.run_cycle(max_workers=1)
        self.assertEqual(
            self.storage.get_rule_state(int(rule["id"]))["alert_active"],
            1,
        )
        with self.storage.connect() as connection:
            connection.execute(
                "UPDATE market_snapshots SET source_updated_at=? WHERE kind='current'",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
            )

        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 26)), 0)
        self.assertEqual(self.stored_messages("daily_summary"), [])

    def test_corrupt_cache_does_not_block_items_or_daily_summary(self):
        manager = self.manager([0.70])
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager.run_cycle(max_workers=1)
        with self.storage.connect() as connection:
            connection.execute("UPDATE market_snapshots SET source_updated_at='broken' WHERE kind='current'")

        items = manager.list_items("umo:a")
        queued = manager.enqueue_daily_summary(date(2026, 7, 27))

        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["latest"])
        self.assertEqual(queued, 0)
        self.assertEqual(self.stored_messages("daily_summary"), [])

    def test_daily_summary_excludes_recovery_band_and_missing_values(self):
        manager = self.manager()
        recovery = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        missing = self.storage.add_rule(1579, "umo:a", "platform", 3.40)
        self.storage.update_rule_state(recovery["id"], alert_active=1, last_value=0.73)
        self.storage.update_rule_state(missing["id"], alert_active=1, last_value=None)

        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)

    def test_lowest_platform_ignores_liquidity(self):
        value = snapshot()
        value = MarketSnapshot(
            **{
                **value.__dict__,
                "buff_sell_price": 3.30,
                "buff_sell_num": 1000,
                "c5_sell_price": 3.10,
                "c5_sell_num": 0,
                "uuyp_sell_price": 3.20,
                "uuyp_sell_num": 1,
            }
        )
        self.assertEqual(value.lowest_platform, ("C5", 3.10, 0))
        self.assertEqual(value.calculated_ratio, round(3.10 / value.steam_net, 4))

    def test_t7_rule_uses_displayed_dynamic_forecast_ratio(self):
        manager = MonitoringManager(self.storage, ForecastHistorySource([0.70]))
        rule = manager.add_rule("umo:a", 1579, "t7", 72)
        manager.run_cycle(max_workers=1)
        state = self.storage.get_rule_state(rule["id"])
        quote = manager.quote("1579")

        self.assertEqual(quote["forecast"]["status"], "ready")
        self.assertEqual(state["last_value"], quote["forecast"]["forecast_balance_ratio"])
        self.assertEqual(state["last_baseline"], quote["forecast"]["predicted_steam_net"])
        self.assertEqual(state["alert_active"], 1)
        messages = self.stored_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("T+7预测挂刀", messages[0][1])
        self.assertIn("七日预测比例", messages[0][2])

    def test_t7_rule_waits_when_dynamic_forecast_is_unavailable(self):
        manager = self.manager([0.70])
        rule = manager.add_rule("umo:a", 1579, "t7", 72)

        manager.run_cycle(max_workers=1)

        state = self.storage.get_rule_state(rule["id"])
        self.assertIsNone(state["last_value"])
        self.assertIn("七日预测不可用", state["status"])
        self.assertEqual(self.stored_messages(), [])

    def test_stale_poll_history_cannot_make_t7_forecast_ready(self):
        now = datetime.now(timezone.utc)
        stale_rows = []
        for index in range(1, 30 * 12 + 1):
            observed = now - timedelta(hours=index * 2)
            value = snapshot(0.70)
            stale_rows.append(
                MarketSnapshot(
                    **{
                        **value.__dict__,
                        "observed_at": observed,
                        "source_updated_at": observed - timedelta(hours=2),
                    }
                )
            )
        self.storage.save_snapshots(stale_rows)
        manager = self.manager([0.70])
        rule = self.storage.add_rule(1579, "umo:a", "t7", 100)

        manager.run_cycle(max_workers=1)

        state = self.storage.get_rule_state(int(rule["id"]))
        quote = manager.quote("1579")
        self.assertEqual(manager.source.history_calls, 1)
        self.assertNotEqual(quote["forecast"]["status"], "ready")
        self.assertIsNone(state["last_value"])
        self.assertEqual(self.stored_messages(), [])

    def test_platform_and_steam_rules_trigger_without_volume(self):
        manager = self.manager([0.70, 0.70])
        self.storage.add_rule(1579, "umo:a", "platform", 3.30)
        self.storage.add_rule(1579, "umo:a", "steam", 5.30)
        manager.run_cycle(max_workers=1)
        manager.run_cycle(max_workers=1)
        self.assertEqual(
            {message[1].split("】", 1)[0] + "】" for message in self.stored_messages()},
            {
                "【平台到价】",
                "【Steam清仓】",
            },
        )

    def test_stale_source_time_skips_trade_alert_without_health_failure(self):
        manager = MonitoringManager(self.storage, StaleSource([0.70]))
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        for _ in range(3):
            manager.run_cycle(max_workers=1)
        messages = self.stored_messages()
        self.assertEqual(messages, [])
        state = self.storage.get_health_state(1579, "umo:a")
        self.assertEqual(state["fetch_failures"], 0)
        self.assertEqual(state["health_alerted"], 0)
        rule = self.storage.list_rules(recipient_key="umo:a")[0]
        rule_state = self.storage.get_rule_state(int(rule["id"]))
        self.assertIsNone(rule_state["last_value"])
        self.assertEqual(rule_state["status"], "行情数据已过期")

        quote = manager.quote("1579")
        self.assertTrue(quote["cached"])
        self.assertTrue(quote["stale"])
        self.assertNotEqual(quote["forecast"]["status"], "ready")
        self.assertNotEqual(quote["risk_assessment"]["status"], "ready")

    def test_future_source_time_is_invalid_and_cannot_trigger_trade_alert(self):
        manager = MonitoringManager(self.storage, FutureSource([0.70]))
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        result = manager.run_cycle(max_workers=1)

        self.assertTrue(result[0]["stale"])
        self.assertEqual(self.stored_messages(), [])
        state = self.storage.get_rule_state(int(rule["id"]))
        self.assertIsNone(state["last_value"])
        self.assertEqual(state["status"], "行情数据已过期")
        history = self.storage.steam_history(
            ITEM["item_key"],
            days=7,
            max_current_lag_seconds=manager.poll_interval_seconds,
        )
        future_limit = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertTrue(all(datetime.fromisoformat(row["source_updated_at"]) <= future_limit for row in history))

    def test_corrupt_cached_timestamp_falls_through_to_upstream(self):
        manager = self.manager([0.70])
        self.storage.save_snapshots([snapshot(0.71)])
        with self.storage.connect() as connection:
            connection.execute("UPDATE market_snapshots SET source_updated_at='broken' WHERE kind='current'")

        quote = manager.quote("1579")
        cached = manager.quote("1579")

        self.assertEqual(manager.source.current_calls, 1)
        self.assertFalse(quote["cached"])
        self.assertFalse(quote["stale"])
        self.assertTrue(cached["cached"])

    def test_three_real_request_failures_trigger_health_alert(self):
        manager = self.manager([RuntimeError("down")] * 3)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        for _ in range(3):
            manager.run_cycle(max_workers=1)
        messages = self.stored_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("监控异常", messages[0][1])

    def test_snapshot_pruning_keeps_only_rolling_eight_days(self):
        old = snapshot(0.70, -(9 * 86400))
        recent = snapshot(0.71, -(7 * 86400))
        self.storage.save_snapshots([old, recent])

        self.assertEqual(self.storage.prune_snapshots(retain_days=8), 1)
        with self.storage.connect() as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_history_backfill_only_runs_when_local_coverage_is_insufficient(self):
        manager = self.manager()
        item = dict(ITEM)
        manager.history_backfiller.ensure_history(item)
        self.assertEqual(manager.source.history_calls, 1)
        self.assertEqual(manager.source.history_days, [30])
        manager.history_backfiller.ensure_history(item)
        self.assertEqual(manager.source.history_calls, 1)

    def test_sparse_cache_and_legacy_done_marker_still_retry_upstream(self):
        manager = self.manager()
        old = snapshot(0.70, -(30 * 86400))
        self.storage.save_snapshots([MarketSnapshot(**{**old.__dict__, "kind": "history"})])
        key = f"history-bootstrap:{ITEM['item_key']}:30"
        self.storage.set_metadata(key, "done")

        result = manager.history_backfiller.ensure_history(dict(ITEM))
        second = manager.history_backfiller.ensure_history(dict(ITEM))

        self.assertTrue(result)
        self.assertIsNone(second)
        self.assertEqual(manager.source.history_calls, 1)

    def test_unchanged_market_after_observation_gap_does_not_backfill(self):
        manager = self.manager()
        old = snapshot(0.70, -(4 * 3600))
        current = MarketSnapshot(
            **{
                **old.__dict__,
                "observed_at": datetime.now(timezone.utc),
            }
        )
        previous = {
            **old.__dict__,
            "observed_at": old.observed_at.isoformat(),
            "source_updated_at": old.source_updated_at.isoformat(),
        }

        manager.history_backfiller.backfill_changed_gap(dict(ITEM), previous, current, [])

        self.assertEqual(manager.source.history_calls, 0)

    def test_changed_gap_uses_minimum_integer_day_window(self):
        manager = self.manager()
        now = datetime.now(timezone.utc)
        for hours, expected_days in ((23, 1), (25, 2)):
            old = snapshot(0.70, -(hours * 3600))
            current = snapshot(0.75)
            previous = {
                **old.__dict__,
                "observed_at": (now - timedelta(hours=hours)).isoformat(),
                "source_updated_at": old.source_updated_at.isoformat(),
            }
            current = MarketSnapshot(
                **{
                    **current.__dict__,
                    "observed_at": now,
                }
            )
            manager.history_backfiller.backfill_changed_gap(dict(ITEM), previous, current, [])
            self.assertEqual(manager.source.history_days[-1], expected_days)

        self.assertEqual(manager.source.history_days, [1, 2])

    def test_failed_gap_backfill_is_retained_for_six_hour_retry(self):
        class FailOnceHistorySource(FakeSource):
            def fetch_history(self, item, days):
                self.history_calls += 1
                self.history_days.append(days)
                if self.history_calls == 1:
                    raise RuntimeError("temporary history failure")
                return []

        source = FailOnceHistorySource()
        manager = MonitoringManager(self.storage, source)
        now = datetime.now(timezone.utc)
        old = snapshot(0.70, -(25 * 3600))
        previous = {
            **old.__dict__,
            "observed_at": (now - timedelta(hours=25)).isoformat(),
            "source_updated_at": old.source_updated_at.isoformat(),
        }
        current = MarketSnapshot(
            **{
                **snapshot(0.75).__dict__,
                "observed_at": now,
            }
        )

        with self.assertRaisesRegex(RuntimeError, "temporary"):
            manager.history_backfiller.backfill_changed_gap(dict(ITEM), previous, current, [])
        manager.history_backfiller.backfill_changed_gap(dict(ITEM), previous, current, [])
        self.assertEqual(source.history_calls, 1)

        pending_key = f"history-gap-pending:{ITEM['item_key']}"
        pending = json.loads(self.storage.get_metadata(pending_key))
        pending["attempted_at"] = (datetime.now(timezone.utc) - timedelta(hours=6, minutes=1)).isoformat()
        self.storage.set_metadata(pending_key, json.dumps(pending))
        manager.history_backfiller.backfill_changed_gap(dict(ITEM), previous, current, [])

        self.assertEqual(source.history_days, [2, 2])
        self.assertEqual(self.storage.get_metadata(pending_key), "")

    def test_pruning_uses_successful_observation_time_for_cold_items(self):
        old_source = datetime.now(timezone.utc) - timedelta(days=40)
        current = snapshot(0.70)
        current = MarketSnapshot(
            **{
                **current.__dict__,
                "source_updated_at": old_source,
            }
        )
        self.storage.save_snapshots([current])

        self.assertEqual(self.storage.prune_snapshots(retain_days=30), 0)
        self.assertIsNotNone(self.storage.latest_snapshot(ITEM["item_key"]))

    def test_adding_same_rule_updates_existing_rule_and_resets_state(self):
        manager = self.manager()
        first = manager.add_rule("umo:a", 1579, "steam", 5.50)
        self.assertEqual(first["action"], "created")
        self.storage.update_rule_state(first["id"], alert_active=1, qualifying_count=2, status="active")

        second = manager.add_rule("umo:a", 1579, "steam", 5.40)

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["previous_threshold"], 5.50)
        self.assertEqual(second["threshold"], 5.40)
        self.assertEqual(len(self.storage.list_rules(recipient_key="umo:a", smis_id=1579)), 1)
        self.assertEqual(self.storage.get_rule_state(first["id"])["alert_active"], 0)

        unchanged = manager.add_rule("umo:a", 1579, "steam", 5.40)
        self.assertEqual(unchanged["id"], first["id"])
        self.assertEqual(unchanged["action"], "unchanged")
        self.assertEqual(len(self.storage.list_rules(recipient_key="umo:a", smis_id=1579)), 1)

    def test_item_limit_is_enforced_before_source_request(self):
        for smis_id in range(1, 21):
            self.storage.upsert_item(
                {
                    "smis_id": smis_id,
                    "item_key": f"smis:{smis_id}",
                    "appid": 730,
                    "name": f"Item {smis_id}",
                    "name_zh": f"饰品 {smis_id}",
                }
            )
            self.storage.add_rule(smis_id, "umo:test", "ratio", 72)
        manager = self.manager()
        with self.assertRaisesRegex(Exception, "最多只能监控 20 个饰品"):
            manager.add_rule("umo:test", 9999, "ratio", 72)

    def test_removing_last_rule_releases_item_slot(self):
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager = self.manager()
        manager.remove_rule("umo:a", rule["id"])
        self.assertIsNone(self.storage.get_item(1579))


if __name__ == "__main__":
    unittest.main()
