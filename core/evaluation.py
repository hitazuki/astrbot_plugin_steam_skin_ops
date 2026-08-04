from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .market import MarketSnapshot
from .risk import FORECAST_MODEL_VERSION, volume_cohort
from .sqlite_schema import preflight_schema_version

EVALUATION_SCHEMA_VERSION = 1
EVALUATION_RETENTION_DAYS = 180


class ForecastEvaluationStorage:
    """Independent forecast evaluation store safe for plugin-code rollback."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        version = preflight_schema_version(self.path) if self.path.exists() else 0
        if version > EVALUATION_SCHEMA_VERSION:
            raise RuntimeError(
                f"forecast evaluation schema v{version} is newer than supported v{EVALUATION_SCHEMA_VERSION}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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

    def _initialize(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forecast_issuances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_key TEXT NOT NULL,
                    smis_id INTEGER NOT NULL,
                    appid INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    name_zh TEXT NOT NULL,
                    issue_date TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    target_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    volume_cohort TEXT NOT NULL,
                    mode TEXT,
                    model_version TEXT NOT NULL,
                    current_steam_net REAL,
                    p20_steam_net REAL,
                    p50_steam_net REAL,
                    p80_steam_net REAL,
                    probability_up REAL,
                    interval_source TEXT,
                    feature_schema_version TEXT NOT NULL,
                    feature_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_key, issue_date, model_version)
                );
                CREATE INDEX IF NOT EXISTS idx_forecast_due
                    ON forecast_issuances(item_key, status, target_at);
                CREATE INDEX IF NOT EXISTS idx_forecast_calibration
                    ON forecast_issuances(volume_cohort, mode, issued_at);
                CREATE TABLE IF NOT EXISTS forecast_outcomes (
                    issuance_id INTEGER PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    actual_steam_net REAL NOT NULL,
                    log_residual REAL NOT NULL,
                    signed_error_pct REAL NOT NULL,
                    absolute_error_pct REAL NOT NULL,
                    direction_correct INTEGER NOT NULL,
                    interval_hit INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(issuance_id) REFERENCES forecast_issuances(id)
                );
            """)
            conn.execute(
                "INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                ("schema_version", str(EVALUATION_SCHEMA_VERSION), now),
            )

    def issue(
        self,
        snapshot: MarketSnapshot,
        forecast: dict[str, Any],
        *,
        issue_date: date,
        source_digest: str,
    ) -> bool:
        issued_at = snapshot.observed_at.astimezone(timezone.utc)
        target_at = issued_at + timedelta(days=7)
        ready = forecast.get("status") == "ready"
        status = "pending" if ready else "unavailable"
        # Keep schema v1 for rollback compatibility. The legacy feature columns now
        # carry compact provenance instead of duplicated algorithm intermediates.
        source_record = {
            "schema_version": "forecast_source_digest.v1",
            "source": snapshot.source,
            "source_cutoff_at_utc": issued_at.isoformat(),
            "source_updated_at_utc": snapshot.source_updated_at.astimezone(timezone.utc).isoformat(),
            "source_digest_sha256": str(source_digest),
        }
        payload = json.dumps(
            source_record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO forecast_issuances(
                    item_key,smis_id,appid,name,name_zh,issue_date,issued_at,
                    target_at,status,volume_cohort,mode,model_version,
                    current_steam_net,p20_steam_net,p50_steam_net,p80_steam_net,
                    probability_up,interval_source,feature_schema_version,
                    feature_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    snapshot.item_key,
                    snapshot.smis_id,
                    snapshot.appid,
                    snapshot.name,
                    snapshot.name_zh,
                    issue_date.isoformat(),
                    issued_at.isoformat(),
                    target_at.isoformat(),
                    status,
                    volume_cohort(snapshot.steam_transaction_quantity),
                    forecast.get("mode"),
                    FORECAST_MODEL_VERSION,
                    snapshot.steam_net or None,
                    forecast.get("p20_steam_net"),
                    forecast.get("predicted_steam_net"),
                    forecast.get("p80_steam_net"),
                    forecast.get("probability_up"),
                    forecast.get("interval_source"),
                    source_record["schema_version"],
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return conn.total_changes > before

    @staticmethod
    def _direction(change: float) -> int:
        if change >= 0.01:
            return 1
        if change <= -0.01:
            return -1
        return 0

    def resolve(self, snapshot: MarketSnapshot, *, stale: bool) -> int:
        if stale or snapshot.steam_net <= 0:
            return 0
        observed = snapshot.observed_at.astimezone(timezone.utc)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM forecast_issuances
                WHERE item_key=? AND status='pending' AND target_at<=?
                  AND target_at>=?
                ORDER BY target_at,id
            """,
                (
                    snapshot.item_key,
                    observed.isoformat(),
                    (observed - timedelta(hours=24)).isoformat(),
                ),
            ).fetchall()
            resolved = 0
            for row in rows:
                predicted = float(row["p50_steam_net"])
                current = float(row["current_steam_net"])
                actual = snapshot.steam_net
                log_residual = math.log(actual / predicted)
                signed_error = (predicted / actual - 1) * 100
                direction_correct = self._direction(predicted / current - 1) == (self._direction(actual / current - 1))
                interval_hit = float(row["p20_steam_net"]) <= actual <= float(row["p80_steam_net"])
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO forecast_outcomes(
                        issuance_id,observed_at,source_updated_at,actual_steam_net,
                        log_residual,signed_error_pct,absolute_error_pct,
                        direction_correct,interval_hit,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                    (
                        int(row["id"]),
                        observed.isoformat(),
                        snapshot.source_updated_at.astimezone(timezone.utc).isoformat(),
                        actual,
                        log_residual,
                        signed_error,
                        abs(signed_error),
                        int(direction_correct),
                        int(interval_hit),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE forecast_issuances SET status='resolved',updated_at=? WHERE id=?",
                    (now, int(row["id"])),
                )
                resolved += 1
            return resolved

    def mark_missed(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                "UPDATE forecast_issuances SET status='missed',updated_at=? WHERE status='pending' AND target_at<?",
                (
                    current.isoformat(),
                    (current - timedelta(hours=48)).isoformat(),
                ),
            )
            return conn.total_changes - before

    def pooled_residuals(
        self,
        cohort: str,
        *,
        days: int = EVALUATION_RETENTION_DAYS,
    ) -> dict[str, list[float]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.mode,o.log_residual
                FROM forecast_outcomes o
                JOIN forecast_issuances i ON i.id=o.issuance_id
                WHERE i.volume_cohort=? AND i.issued_at>=? AND i.mode IS NOT NULL
                ORDER BY i.issued_at
            """,
                (cohort, cutoff),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(str(row["mode"]), []).append(float(row["log_residual"]))
        return grouped

    def summary(self, *, days: int = 30, cohort: str = "all") -> dict[str, Any]:
        allowed_days = {7, 30, 90, 180}
        if int(days) not in allowed_days:
            raise ValueError("评估天数只支持 7、30、90 或 180")
        cohort_map = {
            "all": None,
            "lt100": ("volume_lt_5", "volume_5_99"),
            "100-999": ("volume_100_999",),
            "1000+": ("volume_1000_plus",),
        }
        if cohort not in cohort_map:
            raise ValueError("成交量层只支持 all、lt100、100-999 或 1000+")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        cohort_values = cohort_map[cohort]
        filters = ["i.issued_at>=?"]
        params: list[Any] = [cutoff]
        if cohort_values:
            placeholders = ",".join("?" for _ in cohort_values)
            filters.append(f"i.volume_cohort IN ({placeholders})")
            params.extend(cohort_values)
        where = " AND ".join(filters)
        with self.connect() as conn:
            status_rows = conn.execute(
                f"SELECT i.status,COUNT(*) AS count FROM forecast_issuances i WHERE {where} GROUP BY i.status",
                params,
            ).fetchall()
            metric_rows = conn.execute(
                f"""SELECT i.volume_cohort,i.mode,COUNT(*) AS resolved_count,
                    AVG(o.absolute_error_pct) AS mae_pct,
                    AVG(o.signed_error_pct) AS bias_pct,
                    AVG(o.direction_correct) AS direction_accuracy,
                    AVG(o.interval_hit) AS interval_coverage
                FROM forecast_issuances i
                JOIN forecast_outcomes o ON o.issuance_id=i.id
                WHERE {where}
                GROUP BY i.volume_cohort,i.mode
                ORDER BY i.volume_cohort,i.mode""",
                params,
            ).fetchall()
        statuses = {str(row["status"]): int(row["count"]) for row in status_rows}
        groups = [
            {
                "volume_cohort": str(row["volume_cohort"]),
                "mode": str(row["mode"] or "unknown"),
                "resolved_count": int(row["resolved_count"]),
                "mae_pct": round(float(row["mae_pct"]), 2),
                "bias_pct": round(float(row["bias_pct"]), 2),
                "direction_accuracy": round(float(row["direction_accuracy"]), 4),
                "interval_coverage": round(float(row["interval_coverage"]), 4),
                "sample_quality": ("sufficient" if int(row["resolved_count"]) >= 30 else "small_sample"),
            }
            for row in metric_rows
        ]
        return {
            "schema_version": "forecast_evaluation_summary.v1",
            "days": int(days),
            "cohort_filter": cohort,
            "issued_count": sum(statuses.values()),
            "status_counts": statuses,
            "groups": groups,
            "expected_interval_coverage": 0.60,
        }

    def prune(self, retain_days: int = EVALUATION_RETENTION_DAYS) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(retain_days)))).isoformat()
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                DELETE FROM forecast_outcomes WHERE issuance_id IN (
                    SELECT id FROM forecast_issuances WHERE issued_at<?
                )
            """,
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM forecast_issuances WHERE issued_at<?",
                (cutoff,),
            )
            return conn.total_changes - before

    def backup(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        return destination
