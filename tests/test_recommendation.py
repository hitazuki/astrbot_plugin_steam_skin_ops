import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.manager import MonitoringManager, ServiceError
from core.market import MarketSnapshot, steam_net_amount
from core.presentation import format_recommendations
from core.recommendation import RecommendationPolicy, rank_recommendations
from core.repository import MonitorStorage


def candidate(
    smis_id: int,
    ratio: float,
    *,
    age_minutes: int = 0,
    platform_sell_num: int = 20,
    steam_sell_num: int = 200,
    steam_volume: int = 100,
) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    steam_price = 115.0
    return MarketSnapshot(
        item_key=f"smis:{smis_id}",
        smis_id=smis_id,
        appid=730,
        name=f"Item {smis_id}",
        name_zh=f"饰品 {smis_id}",
        observed_at=now,
        source_updated_at=now - timedelta(minutes=age_minutes),
        buff_sell_price=round(steam_net_amount(steam_price) * ratio, 2),
        buff_sell_num=platform_sell_num,
        uuyp_sell_price=0,
        uuyp_sell_num=0,
        c5_sell_price=0,
        c5_sell_num=0,
        igxe_sell_price=0,
        igxe_sell_num=0,
        eco_sell_price=0,
        eco_sell_num=0,
        steam_sell_price=steam_price,
        steam_sell_num=steam_sell_num,
        steam_transaction_quantity=steam_volume,
    )


class RecommendationPolicyTestCase(unittest.TestCase):
    def test_filters_non_executable_stale_and_illiquid_candidates(self):
        policy = RecommendationPolicy(
            min_price=30,
            max_price=100,
            max_ratio=0.8,
            min_steam_volume=50,
            max_source_age_minutes=120,
        )
        snapshots = [
            candidate(1, 0.70),
            candidate(2, 0.60, platform_sell_num=0),
            candidate(3, 0.60, age_minutes=121),
            candidate(4, 0.60, steam_volume=49),
            candidate(5, 0.81),
            candidate(6, 0.70, steam_sell_num=0),
            candidate(7, 0.60, age_minutes=-6),
        ]

        result = rank_recommendations(snapshots, policy)

        self.assertEqual(result["scanned_count"], 7)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(
            [row["smis_id"] for row in result["recommendations"]],
            [1],
        )

    def test_uses_lowest_platform_with_positive_inventory(self):
        row = candidate(1, 0.75)
        row = MarketSnapshot(**{
            **row.__dict__,
            "buff_sell_price": 60,
            "buff_sell_num": 0,
            "uuyp_sell_price": 70,
            "uuyp_sell_num": 12,
        })

        result = rank_recommendations([row], RecommendationPolicy())

        recommendation = result["recommendations"][0]
        self.assertEqual(recommendation["platform"], "悠悠有品")
        self.assertEqual(recommendation["platform_price"], 70)
        self.assertEqual(recommendation["platform_sell_num"], 12)

    def test_orders_by_ratio_then_stock_to_flow_and_limits_top_five(self):
        snapshots = [
            candidate(1, 0.70, steam_sell_num=200, steam_volume=100),
            candidate(2, 0.70, steam_sell_num=100, steam_volume=100),
            candidate(3, 0.71),
            candidate(4, 0.72),
            candidate(5, 0.73),
            candidate(6, 0.74),
        ]

        result = rank_recommendations(snapshots, RecommendationPolicy())

        self.assertEqual(result["eligible_count"], 6)
        self.assertEqual(
            [row["smis_id"] for row in result["recommendations"]],
            [2, 1, 3, 4, 5],
        )

    def test_budget_is_validated_and_caps_configured_maximum(self):
        policy = RecommendationPolicy(min_price=30, max_price=5000)
        self.assertEqual(policy.with_budget(100).max_price, 100)
        self.assertEqual(policy.with_budget(6000).max_price, 5000)
        with self.assertRaisesRegex(ValueError, "不能低于"):
            policy.with_budget(20)


class FakeRecommendationSource:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = []

    def fetch_exchange_candidates(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.rows


class RecommendationManagerTestCase(unittest.TestCase):
    def test_manager_applies_budget_to_upstream_and_local_policy(self):
        with TemporaryDirectory() as tmp:
            source = FakeRecommendationSource([candidate(1, 0.70)])
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                source,
                recommendation_policy=RecommendationPolicy(max_price=5000),
            )

            result = manager.recommend(100)

        self.assertEqual(source.calls[0]["max_price"], 100)
        self.assertEqual(result["policy"]["max_price"], 100)
        self.assertEqual(len(result["recommendations"]), 1)

    def test_manager_reports_invalid_budget_and_source_failure(self):
        with TemporaryDirectory() as tmp:
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                FakeRecommendationSource(error=RuntimeError("down")),
            )
            with self.assertRaises(ServiceError) as invalid:
                manager.recommend(10)
            self.assertEqual(invalid.exception.code, "invalid_budget")
            with self.assertRaises(ServiceError) as unavailable:
                manager.recommend()
            self.assertEqual(
                unavailable.exception.code,
                "recommendation_source_unavailable",
            )

    def test_formatter_states_scope_and_does_not_invent_missing_top_five(self):
        data = rank_recommendations(
            [candidate(1, 0.70)],
            RecommendationPolicy(),
        )

        content = format_recommendations(data)

        self.assertIn("确定性即时策略", content)
        self.assertIn("扫描 1，符合 1", content)
        self.assertIn("尚未纳入 T+7 预测", content)
        self.assertNotIn("2. 饰品", content)


if __name__ == "__main__":
    unittest.main()
