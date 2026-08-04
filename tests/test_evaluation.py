import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.evaluation import EVALUATION_SCHEMA_VERSION, ForecastEvaluationStorage
from core.market import MarketSnapshot


def snapshot(when: datetime, *, steam_price: float = 11.5) -> MarketSnapshot:
    return MarketSnapshot(
        item_key="smis:1579",
        smis_id=1579,
        appid=730,
        name="Fracture Case",
        name_zh="裂空武器箱",
        observed_at=when,
        source_updated_at=when,
        buff_sell_price=7.0,
        buff_sell_num=100,
        steam_sell_price=steam_price,
        steam_sell_num=100,
        steam_transaction_quantity=100,
    )


def ready_forecast() -> dict:
    return {
        "status": "ready",
        "mode": "persistence",
        "p20_steam_net": 9.0,
        "predicted_steam_net": 10.0,
        "p80_steam_net": 11.0,
        "probability_up": 0.5,
        "interval_source": "realized_volatility",
    }


class ForecastEvaluationStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "forecast_evaluation.db"
        self.storage = ForecastEvaluationStorage(self.path)
        self.issued_at = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def _issue(self, when: datetime | None = None) -> bool:
        return self.storage.issue(
            snapshot(when or self.issued_at),
            ready_forecast(),
            issue_date=(when or self.issued_at).date(),
            source_digest="a" * 64,
        )

    def test_schema_v1_and_daily_item_dedup_do_not_store_recipient(self):
        self.assertTrue(self._issue())
        self.assertFalse(self._issue(self.issued_at + timedelta(hours=1)))
        with self.storage.connect() as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(forecast_issuances)")
            }
            count = connection.execute(
                "SELECT COUNT(*) FROM forecast_issuances"
            ).fetchone()[0]
        self.assertEqual(int(version), EVALUATION_SCHEMA_VERSION)
        self.assertEqual(count, 1)
        self.assertNotIn("recipient", " ".join(columns).casefold())

    def test_first_fresh_snapshot_in_window_resolves_and_feeds_pool(self):
        self._issue()
        target = self.issued_at + timedelta(days=7, hours=1)
        self.assertEqual(self.storage.resolve(snapshot(target), stale=True), 0)
        self.assertEqual(self.storage.resolve(snapshot(target), stale=False), 1)
        self.assertEqual(self.storage.resolve(snapshot(target), stale=False), 0)
        pooled = self.storage.pooled_residuals("volume_100_999", days=9999)
        self.assertEqual(len(pooled["persistence"]), 1)

    def test_late_pending_prediction_becomes_missed(self):
        self._issue()
        changed = self.storage.mark_missed(
            self.issued_at + timedelta(days=9, hours=1)
        )
        self.assertEqual(changed, 1)
        with self.storage.connect() as connection:
            status = connection.execute(
                "SELECT status FROM forecast_issuances"
            ).fetchone()[0]
        self.assertEqual(status, "missed")

    def test_prune_backup_and_future_version_rejection(self):
        old = datetime.now(timezone.utc) - timedelta(days=181)
        self._issue(old)
        self.assertGreaterEqual(self.storage.prune(180), 1)
        destination = Path(self.tmp.name) / "backups" / "evaluation.db"
        self.assertEqual(self.storage.backup(destination), destination)
        connection = sqlite3.connect(destination)
        try:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            connection.close()

        future = Path(self.tmp.name) / "future.db"
        connection = sqlite3.connect(future)
        try:
            connection.execute(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
            )
            connection.execute(
                "INSERT INTO metadata VALUES('schema_version',?,?)",
                (str(EVALUATION_SCHEMA_VERSION + 1), self.issued_at.isoformat()),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            ForecastEvaluationStorage(future)

    def test_feature_json_only_keeps_compact_source_provenance(self):
        self._issue()
        with self.storage.connect() as connection:
            payload = connection.execute(
                "SELECT feature_json FROM forecast_issuances"
            ).fetchone()[0]
        record = json.loads(payload)
        self.assertEqual(record["schema_version"], "forecast_source_digest.v1")
        self.assertEqual(record["source_digest_sha256"], "a" * 64)
        self.assertNotIn("derived_history", record)
        self.assertNotIn("model_output", record)

    def test_summary_groups_resolved_metrics_without_schema_change(self):
        recent = datetime.now(timezone.utc) - timedelta(days=8)
        self._issue(recent)
        self.assertEqual(
            self.storage.resolve(
                snapshot(recent + timedelta(days=7)), stale=False
            ),
            1,
        )

        summary = self.storage.summary(days=30, cohort="100-999")

        self.assertEqual(summary["schema_version"], "forecast_evaluation_summary.v1")
        self.assertEqual(summary["issued_count"], 1)
        self.assertEqual(summary["status_counts"], {"resolved": 1})
        self.assertEqual(summary["groups"][0]["volume_cohort"], "volume_100_999")
        self.assertEqual(summary["groups"][0]["resolved_count"], 1)
        self.assertEqual(summary["groups"][0]["sample_quality"], "small_sample")
        self.assertEqual(EVALUATION_SCHEMA_VERSION, 1)

    def test_summary_rejects_unsupported_window_and_cohort(self):
        with self.assertRaises(ValueError):
            self.storage.summary(days=14)
        with self.assertRaises(ValueError):
            self.storage.summary(cohort="unknown")


if __name__ == "__main__":
    unittest.main()
