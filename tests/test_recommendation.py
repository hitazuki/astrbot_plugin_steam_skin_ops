import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import jsonschema

from core.candidate_features import build_candidate_record
from core.evaluation import ForecastEvaluationStorage
from core.manager import MonitoringManager, ServiceError
from core.market import MarketSnapshot, steam_net_amount
from core.presentation import format_recommendations
from core.recommendation import (
    RecommendationPolicy,
    dynamic_analysis_limit,
    rank_recommendations,
    score_recommendation,
)
from core.repository import MonitorStorage
from core.risk import analyze_market


def candidate(
    smis_id: int,
    ratio: float,
    *,
    age_minutes: int = 0,
    platform_sell_num: int = 20,
    steam_sell_num: int = 200,
    steam_volume: int = 100,
    appid: int = 730,
) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    steam_price = 115.0
    return MarketSnapshot(
        item_key=f"smis:{smis_id}",
        smis_id=smis_id,
        appid=appid,
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


def history(candidate_row: MarketSnapshot) -> list[MarketSnapshot]:
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(30 * 12):
        observed = now - timedelta(hours=(30 * 12 - index) * 2)
        rows.append(
            MarketSnapshot(
                item_key=candidate_row.item_key,
                smis_id=candidate_row.smis_id,
                appid=candidate_row.appid,
                name=candidate_row.name,
                name_zh=candidate_row.name_zh,
                observed_at=observed,
                source_updated_at=observed,
                buff_sell_price=candidate_row.buff_sell_price,
                buff_sell_num=200 - index // 12,
                steam_sell_price=110 + index / 360,
                steam_sell_num=220 - index // 20,
                steam_transaction_quantity=100,
                kind="history",
            )
        )
    return rows


class RecommendationPolicyTestCase(unittest.TestCase):
    def test_candidate_series_reuses_forecast_canonical_rows(self):
        current = candidate(1, 0.70, age_minutes=120)
        row = {
            **current.__dict__,
            "observed_at": current.observed_at.isoformat(),
            "source_updated_at": current.source_updated_at.isoformat(),
        }

        analysis = analyze_market(
            [row],
            current,
            stale=True,
            now=current.observed_at,
            max_current_lag_seconds=1800,
        )
        record = build_candidate_record(current, analysis)

        self.assertEqual(analysis.canonical_rows, [])
        self.assertEqual(
            record["derived_history"]["daily_series_21d"]["date"],
            [],
        )

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
        row = MarketSnapshot(
            **{
                **row.__dict__,
                "buff_sell_price": 60,
                "buff_sell_num": 0,
                "uuyp_sell_price": 70,
                "uuyp_sell_num": 12,
            }
        )

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

    def test_allows_non_cs_candidates(self):
        result = rank_recommendations(
            [candidate(7, 0.70, appid=570)],
            RecommendationPolicy(),
        )

        self.assertEqual(result["recommendations"][0]["game"], "Dota 2")

    def test_dynamic_analysis_pool_uses_fifth_ratio_advantage(self):
        for fifth_ratio, expected in (
            (0.60, 12),
            (0.70, 10),
            (0.75, 8),
            (0.76, 6),
        ):
            with self.subTest(fifth_ratio=fifth_ratio):
                rows = [{"ratio": fifth_ratio}] * 12
                self.assertEqual(
                    dynamic_analysis_limit(rows, RecommendationPolicy()),
                    expected,
                )

    def test_composite_score_uses_ratios_risk_and_confidence_deductions(self):
        policy = RecommendationPolicy(max_ratio=0.8)
        row = rank_recommendations(
            [candidate(1, 0.60)],
            policy,
        )["recommendations"][0]
        forecast = {
            "status": "ready",
            "confidence": "normal",
            "forecast_balance_ratio": 0.64,
        }
        risk = {
            "status": "ready",
            "confidence": "normal",
            "overall_level": "medium",
            "risk_balance_ratio": 0.68,
        }

        scored = score_recommendation(
            row,
            forecast,
            risk,
            policy,
            history_age_hours=8,
        )

        self.assertAlmostEqual(scored["score"], 12.5)
        self.assertEqual(
            [deduction["points"] for deduction in scored["score_deductions"]],
            [5.0, 2.0],
        )

    def test_budget_is_validated_and_caps_configured_maximum(self):
        policy = RecommendationPolicy(min_price=30, max_price=5000)
        self.assertEqual(policy.with_budget(100).max_price, 100)
        self.assertEqual(policy.with_budget(6000).max_price, 5000)
        with self.assertRaisesRegex(ValueError, "不能低于"):
            policy.with_overrides(min_price=0.5)
        overridden = RecommendationPolicy().with_overrides(
            max_price=500,
            min_price=1,
            min_steam_volume=1,
        )
        self.assertEqual(overridden.min_steam_volume, 1)
        self.assertEqual(overridden.min_price, 1)


class FakeRecommendationSource:
    def __init__(self, rows=None, error=None, history_error=None):
        self.rows = list(rows or [])
        self.error = error
        self.history_error = history_error
        self.calls = []
        self.history_calls = []

    def fetch_exchange_candidates(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.rows

    def fetch_history(self, item, days=30):
        self.history_calls.append((item, days))
        if self.history_error:
            raise self.history_error
        row = next(row for row in self.rows if row.smis_id == item["smis_id"])
        return history(row)


class RecommendationManagerTestCase(unittest.TestCase):
    def test_manager_applies_all_overrides_and_reuses_history_cache(self):
        with TemporaryDirectory() as tmp:
            source = FakeRecommendationSource([candidate(1, 0.70)])
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                source,
                recommendation_policy=RecommendationPolicy(max_price=5000),
            )

            result = manager.recommend(100, 1, 1)
            second = manager.recommend(100, 1, 1)

        self.assertEqual(source.calls[0]["max_price"], 100)
        self.assertEqual(source.calls[0]["min_price"], 1)
        self.assertEqual(source.calls[0]["min_transaction_quantity"], 1)
        self.assertEqual(result["policy"]["max_price"], 100)
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["diagnostics"]["history_fetches"], 1)
        self.assertEqual(second["diagnostics"]["history_fetches"], 0)
        self.assertEqual(second["diagnostics"]["cache_hits"], 1)

    def test_v3_candidate_matches_schema_and_does_not_issue_evaluation(self):
        with TemporaryDirectory() as tmp:
            source = FakeRecommendationSource([candidate(1, 0.70)])
            evaluation = ForecastEvaluationStorage(Path(tmp) / "forecast_evaluation.db")
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                source,
                evaluation_storage=evaluation,
            )

            result = manager.recommend()
            record = result["candidate_records"][0]
            schema = json.loads(
                (Path(__file__).parents[1] / "docs/recommendation_candidate.v1.schema.json").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(record)
            series = record["derived_history"]["daily_series_21d"]
            self.assertEqual(len({len(values) for values in series.values()}), 1)
            self.assertEqual(result["strategy"], "t7_risk_instant_score_v3")
            self.assertEqual(
                record["deterministic_ranking"]["weights"],
                {"t7": 0.4, "risk_ratio": 0.35, "instant": 0.25},
            )
            self.assertFalse(record["llm_contract"]["may_override_deterministic_score"])
            with evaluation.connect() as connection:
                issued = connection.execute("SELECT COUNT(*) FROM forecast_issuances").fetchone()[0]
            self.assertEqual(issued, 0)

    def test_manager_reports_invalid_budget_and_source_failure(self):
        with TemporaryDirectory() as tmp:
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                FakeRecommendationSource(error=RuntimeError("down")),
            )
            with self.assertRaises(ServiceError) as invalid:
                manager.recommend(10, 0.5)
            self.assertEqual(invalid.exception.code, "invalid_recommendation_filter")
            with self.assertRaises(ServiceError) as unavailable:
                manager.recommend()
            self.assertEqual(
                unavailable.exception.code,
                "recommendation_source_unavailable",
            )

    def test_formatter_states_scope_and_does_not_invent_missing_top_five(self):
        with TemporaryDirectory() as tmp:
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                FakeRecommendationSource([candidate(1, 0.70)]),
            )
            data = manager.recommend()

        content = format_recommendations(data)

        self.assertIn("确定性综合评分", content)
        self.assertIn("扫描 1，符合 1", content)
        self.assertIn("T+7", content)
        self.assertNotIn("2. 饰品", content)

    def test_cold_history_requests_are_capped_at_eight(self):
        with TemporaryDirectory() as tmp:
            source = FakeRecommendationSource([candidate(index, 0.60) for index in range(1, 13)])
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                source,
            )

            result = manager.recommend()
            second = manager.recommend()

        self.assertEqual(result["analysis_pool_count"], 12)
        self.assertEqual(result["diagnostics"]["history_fetches"], 8)
        self.assertEqual(second["diagnostics"]["history_fetches"], 0)
        self.assertEqual(second["diagnostics"]["rate_limited"], 4)
        self.assertEqual(len(source.history_calls), 8)

    def test_aged_usable_cache_refreshes_at_most_two_after_five_ready(self):
        with TemporaryDirectory() as tmp:
            rows = [candidate(index, 0.70) for index in range(1, 6)]
            source = FakeRecommendationSource(rows)
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            for row in rows:
                storage.save_snapshots(history(row))
                storage.mark_recommendation_cache_fetched(
                    row.item_key,
                    row.smis_id,
                    row.appid,
                    at=datetime.now(timezone.utc) - timedelta(hours=8),
                )
            manager = MonitoringManager(storage, source)

            result = manager.recommend()

        self.assertEqual(result["diagnostics"]["cache_hits"], 5)
        self.assertEqual(result["diagnostics"]["history_refreshes"], 2)
        self.assertEqual(len(source.history_calls), 2)

    def test_failed_history_fetch_has_thirty_minute_cooldown(self):
        with TemporaryDirectory() as tmp:
            source = FakeRecommendationSource(
                [candidate(1, 0.70)],
                history_error=RuntimeError("history down"),
            )
            manager = MonitoringManager(
                MonitorStorage(Path(tmp) / "monitor.db"),
                source,
            )

            first = manager.recommend()
            second = manager.recommend()

        self.assertEqual(first["diagnostics"]["history_fetches"], 1)
        self.assertEqual(second["diagnostics"]["history_fetches"], 0)
        self.assertEqual(len(source.history_calls), 1)


class RecommendationCacheTestCase(unittest.TestCase):
    def test_pruning_removes_unused_recommendation_only_history(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            row = candidate(1, 0.70)
            storage.save_snapshots(history(row))
            storage.mark_recommendation_cache_fetched(
                row.item_key,
                row.smis_id,
                row.appid,
                at=datetime.now(timezone.utc) - timedelta(days=15),
            )

            removed = storage.prune_recommendation_cache()

            self.assertEqual(removed["cache_entries"], 1)
            self.assertGreater(removed["snapshots"], 0)
            self.assertIsNone(storage.recommendation_cache_record(row.item_key))
            self.assertEqual(storage.market_history_rows(row.item_key), [])

    def test_pruning_preserves_history_shared_with_monitored_item(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            row = candidate(1, 0.70)
            storage.upsert_item(
                {
                    "smis_id": row.smis_id,
                    "item_key": row.item_key,
                    "appid": row.appid,
                    "name": row.name,
                    "name_zh": row.name_zh,
                }
            )
            storage.save_snapshots(history(row))
            storage.mark_recommendation_cache_fetched(
                row.item_key,
                row.smis_id,
                row.appid,
                at=datetime.now(timezone.utc) - timedelta(days=15),
            )

            removed = storage.prune_recommendation_cache()

            self.assertEqual(removed["cache_entries"], 1)
            self.assertEqual(removed["snapshots"], 0)
            self.assertTrue(storage.market_history_rows(row.item_key))


if __name__ == "__main__":
    unittest.main()
