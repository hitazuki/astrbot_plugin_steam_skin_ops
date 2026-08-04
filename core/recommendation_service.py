from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .candidate_features import RANKING_POLICY_VERSION, build_candidate_record
from .errors import ServiceError
from .evaluation import ForecastEvaluationStorage
from .integrations.smis import MarketDataProvider
from .market import MarketSnapshot
from .recommendation import (
    RecommendationPolicy,
    dynamic_analysis_limit,
    final_recommendation_rank_key,
    rank_recommendations,
    score_recommendation,
)
from .repository import MonitorStorage
from .risk import analyze_market, volume_cohort

logger = logging.getLogger(__name__)

HISTORY_DAYS = 30
HISTORY_FRESH_HOURS = 6
HISTORY_USABLE_HOURS = 24
HISTORY_FAILURE_COOLDOWN_MINUTES = 30
HISTORY_REQUESTS_PER_MINUTE = 8
HISTORY_REFRESH_LIMIT = 2


@dataclass
class RecommendationCacheState:
    status: str
    rows: list[dict]
    age_hours: float
    failure_cooldown: bool

    @property
    def usable(self) -> bool:
        return self.status in {"fresh", "usable"}


@dataclass
class RecommendationCandidateContext:
    snapshot: MarketSnapshot
    instant_row: dict
    cache: RecommendationCacheState


class RecommendationService:
    """Build deterministic recommendations without owning monitoring concerns."""

    def __init__(
        self,
        storage: MonitorStorage,
        source: MarketDataProvider,
        *,
        policy: RecommendationPolicy,
        poll_interval_seconds: int,
        lock_for: Callable[[int], threading.Lock],
        evaluation_storage: ForecastEvaluationStorage | None = None,
    ) -> None:
        self.storage = storage
        self.source = source
        self.policy = policy
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))
        self.lock_for = lock_for
        self.evaluation_storage = evaluation_storage
        self._rate_lock = threading.Lock()
        self._history_requests: deque[float] = deque()

    @staticmethod
    def _item(snapshot: MarketSnapshot) -> dict:
        return {
            "item_key": str(snapshot.item_key),
            "smis_id": int(snapshot.smis_id),
            "appid": int(snapshot.appid),
            "name": str(snapshot.name),
            "name_zh": str(snapshot.name_zh),
        }

    @staticmethod
    def _parse_utc(value: object) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _cache_state(
        self,
        snapshot: MarketSnapshot,
        *,
        now: datetime,
    ) -> RecommendationCacheState:
        record = self.storage.recommendation_cache_record(snapshot.item_key)
        fetched_at = self._parse_utc(record.get("last_fetched_at")) if record else None
        failed_at = self._parse_utc(record.get("last_failed_at")) if record else None
        rows = self.storage.market_history_rows(snapshot.item_key, days=HISTORY_DAYS)
        age_hours = max(0.0, (now - fetched_at).total_seconds() / 3600) if fetched_at is not None else math.inf
        failure_cooldown = bool(
            failed_at is not None and now - failed_at < timedelta(minutes=HISTORY_FAILURE_COOLDOWN_MINUTES)
        )
        if not rows or fetched_at is None:
            status = "cooldown" if failure_cooldown else "missing"
        elif age_hours <= HISTORY_FRESH_HOURS:
            status = "fresh"
        elif age_hours <= HISTORY_USABLE_HOURS:
            status = "usable"
        else:
            status = "cooldown" if failure_cooldown else "expired"
        return RecommendationCacheState(
            status=status,
            rows=rows,
            age_hours=age_hours,
            failure_cooldown=failure_cooldown,
        )

    def _claim_history_slot(self) -> bool:
        current = time.monotonic()
        with self._rate_lock:
            while self._history_requests and current - self._history_requests[0] >= 60:
                self._history_requests.popleft()
            if len(self._history_requests) >= HISTORY_REQUESTS_PER_MINUTE:
                return False
            self._history_requests.append(current)
            return True

    def _fetch_history(
        self,
        context: RecommendationCandidateContext,
        policy: RecommendationPolicy,
        diagnostics: dict,
        *,
        refresh: bool,
    ) -> None:
        if diagnostics["history_fetches"] >= policy.history_fetch_budget:
            return
        snapshot = context.snapshot
        with self.lock_for(snapshot.smis_id):
            current = datetime.now(timezone.utc)
            context.cache = self._cache_state(snapshot, now=current)
            if context.cache.failure_cooldown:
                return
            if refresh:
                if context.cache.status != "usable":
                    return
            elif context.cache.status not in {"missing", "expired"}:
                return
            if not self._claim_history_slot():
                diagnostics["rate_limited"] += 1
                return
            diagnostics["history_fetches"] += 1
            started = time.monotonic()
            try:
                snapshots = self.source.fetch_history(
                    self._item(snapshot),
                    HISTORY_DAYS,
                )
                if not snapshots:
                    raise RuntimeError("SMIS 历史数据为空")
                self.storage.save_snapshots(snapshots)
                self.storage.mark_recommendation_cache_fetched(
                    snapshot.item_key,
                    snapshot.smis_id,
                    snapshot.appid,
                    at=current,
                )
                if refresh:
                    diagnostics["history_refreshes"] += 1
            except Exception as exc:
                diagnostics["history_failures"] += 1
                self.storage.mark_recommendation_cache_failed(
                    snapshot.item_key,
                    snapshot.smis_id,
                    snapshot.appid,
                    at=current,
                )
                logger.warning(
                    "推荐历史获取失败 smis_id=%s: %s",
                    snapshot.smis_id,
                    exc,
                )
            finally:
                diagnostics["history_seconds"] += time.monotonic() - started
                context.cache = self._cache_state(
                    snapshot,
                    now=datetime.now(timezone.utc),
                )

    def _pooled_residuals(
        self,
        snapshot: MarketSnapshot,
    ) -> dict[str, list[float]]:
        if self.evaluation_storage is None:
            return {}
        return self.evaluation_storage.pooled_residuals(volume_cohort(snapshot.steam_transaction_quantity))

    def _score(
        self,
        context: RecommendationCandidateContext,
        policy: RecommendationPolicy,
        *,
        now: datetime,
    ) -> dict | None:
        if not context.cache.usable:
            return None
        snapshot = context.snapshot
        self.storage.mark_recommendation_cache_used(
            snapshot.item_key,
            snapshot.smis_id,
            snapshot.appid,
            at=now,
        )
        analysis = analyze_market(
            context.cache.rows,
            snapshot,
            max_current_lag_seconds=self.poll_interval_seconds,
            pooled_residuals_by_mode=self._pooled_residuals(snapshot),
        )
        scored = score_recommendation(
            context.instant_row,
            analysis.forecast,
            analysis.risk,
            policy,
            history_age_hours=float(context.cache.age_hours),
        )
        if scored is None:
            return None
        scored["volume_cohort"] = volume_cohort(snapshot.steam_transaction_quantity)
        scored["recommendation_candidate"] = build_candidate_record(
            snapshot,
            analysis,
            ranking={
                **scored,
                "instant_balance_ratio": context.instant_row.get("ratio"),
            },
        )
        return scored

    @staticmethod
    def _diagnostics(contexts: list[RecommendationCandidateContext]) -> dict:
        return {
            "cache_hits": sum(context.cache.usable for context in contexts),
            "history_fetches": 0,
            "history_refreshes": 0,
            "history_failures": 0,
            "history_skipped": 0,
            "rate_limited": 0,
            "history_seconds": 0.0,
        }

    def recommend(
        self,
        max_price: float | None = None,
        min_price: float | None = None,
        min_steam_volume: int | None = None,
    ) -> dict:
        started = time.monotonic()
        try:
            policy = self.policy.with_overrides(
                max_price=max_price,
                min_price=min_price,
                min_steam_volume=min_steam_volume,
            )
        except ValueError as exc:
            raise ServiceError("invalid_recommendation_filter", str(exc)) from exc
        try:
            snapshots = self.source.fetch_exchange_candidates(
                min_price=policy.min_price,
                max_price=policy.max_price,
                min_transaction_quantity=policy.min_steam_volume,
                max_ratio=policy.max_ratio,
                max_age_minutes=policy.max_source_age_minutes,
                limit=policy.candidate_limit,
            )
        except Exception as exc:
            logger.warning("SMIS 挂刀候选查询失败：%s", exc)
            raise ServiceError(
                "recommendation_source_unavailable",
                "SMIS 挂刀候选暂时不可用",
            ) from exc

        instant = rank_recommendations(
            snapshots,
            policy,
            limit=policy.candidate_limit,
        )
        pool_limit = dynamic_analysis_limit(instant["recommendations"], policy)
        snapshot_by_id = {int(snapshot.smis_id): snapshot for snapshot in snapshots}
        now = datetime.now(timezone.utc)
        contexts = [
            RecommendationCandidateContext(
                snapshot=snapshot_by_id[int(row["smis_id"])],
                instant_row=row,
                cache=self._cache_state(
                    snapshot_by_id[int(row["smis_id"])],
                    now=now,
                ),
            )
            for row in instant["recommendations"][:pool_limit]
            if int(row["smis_id"]) in snapshot_by_id
        ]
        diagnostics = self._diagnostics(contexts)

        for context in contexts:
            if context.cache.status in {"missing", "expired"}:
                self._fetch_history(
                    context,
                    policy,
                    diagnostics,
                    refresh=False,
                )

        scored = [
            candidate
            for context in contexts
            if (
                candidate := self._score(
                    context,
                    policy,
                    now=datetime.now(timezone.utc),
                )
            )
            is not None
        ]
        if len(scored) >= policy.result_limit:
            stale_contexts = [context for context in contexts if context.cache.status == "usable"][
                :HISTORY_REFRESH_LIMIT
            ]
            for context in stale_contexts:
                self._fetch_history(
                    context,
                    policy,
                    diagnostics,
                    refresh=True,
                )
            if diagnostics["history_refreshes"]:
                scored = [
                    candidate
                    for context in contexts
                    if (
                        candidate := self._score(
                            context,
                            policy,
                            now=datetime.now(timezone.utc),
                        )
                    )
                    is not None
                ]

        diagnostics["history_skipped"] = sum(not context.cache.usable for context in contexts)
        scored.sort(key=final_recommendation_rank_key)
        diagnostics["history_seconds"] = round(diagnostics["history_seconds"], 3)
        diagnostics["total_seconds"] = round(time.monotonic() - started, 3)
        logger.info(
            "推荐完成 scanned=%s eligible=%s pool=%s ready=%s cache_hits=%s "
            "history_fetches=%s refreshes=%s skipped=%s duration=%.3fs",
            instant["scanned_count"],
            instant["eligible_count"],
            len(contexts),
            len(scored),
            diagnostics["cache_hits"],
            diagnostics["history_fetches"],
            diagnostics["history_refreshes"],
            diagnostics["history_skipped"],
            diagnostics["total_seconds"],
        )
        selected = scored[: policy.result_limit]
        return {
            **instant,
            "recommendations": selected,
            "analysis_pool_count": len(contexts),
            "analyzed_count": len(scored),
            "strategy": RANKING_POLICY_VERSION,
            "candidate_records": [row["recommendation_candidate"] for row in selected],
            "diagnostics": diagnostics,
        }
