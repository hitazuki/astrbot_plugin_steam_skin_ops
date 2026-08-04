from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

from .market import MAX_SOURCE_FUTURE_SKEW_SECONDS, MarketSnapshot


@dataclass(frozen=True)
class RecommendationPolicy:
    """Deterministic eligibility, enrichment, and scoring policy."""

    min_price: float = 1.0
    max_price: float = 5000.0
    max_ratio: float = 0.80
    min_steam_volume: int = 50
    max_source_age_minutes: int = 120
    candidate_limit: int = 50
    result_limit: int = 5
    history_fetch_budget: int = 8

    def __post_init__(self) -> None:
        numeric = (self.min_price, self.max_price, self.max_ratio)
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("推荐策略价格和比例必须是有限数")
        if self.min_price <= 0 or self.max_price < self.min_price:
            raise ValueError("推荐策略价格范围无效")
        if self.max_ratio <= 0 or self.max_ratio > 1:
            raise ValueError("推荐策略最大比例必须在 0～100% 之间")
        if self.min_steam_volume < 1:
            raise ValueError("推荐策略最低 Steam 日成交量不能低于 1")
        if self.max_source_age_minutes <= 0:
            raise ValueError("推荐策略最大数据年龄必须大于 0")
        if not 1 <= self.candidate_limit <= 50:
            raise ValueError("推荐候选数量必须在 1～50 之间")
        if not 1 <= self.result_limit <= self.candidate_limit:
            raise ValueError("推荐结果数量必须在 1～候选数量之间")
        if not 1 <= self.history_fetch_budget <= self.candidate_limit:
            raise ValueError("推荐历史请求预算必须在 1～候选数量之间")

    def with_overrides(
        self,
        max_price: float | None = None,
        min_price: float | None = None,
        min_steam_volume: int | None = None,
    ) -> RecommendationPolicy:
        effective_min = self.min_price
        if min_price is not None:
            try:
                effective_min = float(min_price)
            except (TypeError, ValueError) as exc:
                raise ValueError("价格下限必须是有效金额") from exc
            if not math.isfinite(effective_min) or effective_min < self.min_price:
                raise ValueError(f"价格下限不能低于 ¥{self.min_price:.2f}")

        effective_max = self.max_price
        if max_price is not None:
            try:
                requested_max = float(max_price)
            except (TypeError, ValueError) as exc:
                raise ValueError("价格上限必须是有效金额") from exc
            if not math.isfinite(requested_max):
                raise ValueError("价格上限必须是有限金额")
            effective_max = min(self.max_price, requested_max)
        if effective_max < effective_min:
            raise ValueError("价格上限不能低于价格下限")

        effective_volume = self.min_steam_volume
        if min_steam_volume is not None:
            if isinstance(min_steam_volume, bool):
                raise ValueError("最低 Steam 日成交量必须是大于等于 1 的整数")
            try:
                effective_volume = int(min_steam_volume)
            except (TypeError, ValueError) as exc:
                raise ValueError("最低 Steam 日成交量必须是大于等于 1 的整数") from exc
            try:
                exact_volume = float(min_steam_volume)
            except (TypeError, ValueError) as exc:
                raise ValueError("最低 Steam 日成交量必须是大于等于 1 的整数") from exc
            if not math.isfinite(exact_volume) or exact_volume != effective_volume or effective_volume < 1:
                raise ValueError("最低 Steam 日成交量必须是大于等于 1 的整数")

        return replace(
            self,
            min_price=effective_min,
            max_price=effective_max,
            min_steam_volume=effective_volume,
        )

    def with_budget(self, budget: float | None) -> RecommendationPolicy:
        """Compatibility wrapper for the original one-argument command."""
        return self.with_overrides(max_price=budget)

    def display_dict(self) -> dict:
        result = asdict(self)
        result["max_ratio_percent"] = self.max_ratio * 100
        return result


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _instant_rank_key(row: dict) -> tuple:
    return (
        float(row["ratio"]),
        float(row["stock_to_flow_days"]),
        -int(row["steam_transaction_quantity"]),
        -int(row["platform_sell_num"]),
        -float(row["source_timestamp"]),
        int(row["smis_id"]),
    )


def _game_label(appid: int) -> str:
    return {730: "CS2", 570: "Dota 2"}.get(int(appid), f"App {int(appid)}")


def rank_recommendations(
    snapshots: list[MarketSnapshot],
    policy: RecommendationPolicy,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict:
    """Filter unsafe candidates and return the deterministic instant ordering."""

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    eligible_by_id: dict[int, dict] = {}
    for snapshot in snapshots:
        source_time = snapshot.source_updated_at
        if source_time.tzinfo is None:
            continue
        source_time = source_time.astimezone(timezone.utc)
        age_seconds = (current_time - source_time).total_seconds()
        if age_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS or age_seconds > policy.max_source_age_minutes * 60:
            continue

        executable_quotes = [
            (name, price, count)
            for name, price, count in snapshot.platform_quotes
            if math.isfinite(price) and price > 0 and count > 0
        ]
        if not executable_quotes:
            continue
        platform, platform_price, platform_sell_num = min(
            executable_quotes,
            key=lambda row: (row[1], row[0]),
        )
        steam_sell_price = _finite_positive(snapshot.steam_sell_price)
        steam_net = _finite_positive(snapshot.steam_net)
        if steam_sell_price is None or steam_net is None:
            continue
        steam_sell_num = int(snapshot.steam_sell_num or 0)
        steam_volume = int(snapshot.steam_transaction_quantity or 0)
        if steam_sell_num <= 0 or steam_volume < policy.min_steam_volume:
            continue
        if not policy.min_price <= platform_price <= policy.max_price:
            continue

        ratio = platform_price / steam_net
        if not math.isfinite(ratio) or ratio <= 0 or ratio > policy.max_ratio:
            continue
        stock_to_flow_days = steam_sell_num / steam_volume
        row = {
            "smis_id": int(snapshot.smis_id),
            "item_key": str(snapshot.item_key),
            "appid": int(snapshot.appid),
            "game": _game_label(snapshot.appid),
            "name": str(snapshot.name),
            "name_zh": str(snapshot.name_zh),
            "platform": platform,
            "platform_price": round(platform_price, 2),
            "platform_sell_num": int(platform_sell_num),
            "steam_sell_price": round(steam_sell_price, 2),
            "steam_net": round(steam_net, 2),
            "steam_sell_num": steam_sell_num,
            "steam_transaction_quantity": steam_volume,
            "ratio": round(ratio, 4),
            "stock_to_flow_days": round(stock_to_flow_days, 2),
            "source_updated_at": source_time.isoformat(),
            "source_timestamp": source_time.timestamp(),
            "links": {
                "smis": f"https://smis.club/commodity/{int(snapshot.smis_id)}",
                "steam": (
                    f"https://steamcommunity.com/market/listings/{int(snapshot.appid)}/{quote(str(snapshot.name))}"
                ),
            },
        }
        existing = eligible_by_id.get(int(snapshot.smis_id))
        if existing is None or _instant_rank_key(row) < _instant_rank_key(existing):
            eligible_by_id[int(snapshot.smis_id)] = row

    ranked = sorted(eligible_by_id.values(), key=_instant_rank_key)
    selected = ranked[: policy.result_limit if limit is None else max(0, int(limit))]
    for row in selected:
        row.pop("source_timestamp", None)
    return {
        "recommendations": selected,
        "scanned_count": len(snapshots),
        "eligible_count": len(ranked),
        "policy": policy.display_dict(),
        "strategy": "instant_ratio_liquidity_v1",
    }


def dynamic_analysis_limit(rows: list[dict], policy: RecommendationPolicy) -> int:
    """Choose 6/8/10/12 history candidates from the fifth instant ratio."""
    if len(rows) <= policy.result_limit:
        return len(rows)
    fifth_ratio = float(rows[policy.result_limit - 1]["ratio"])
    advantage = policy.max_ratio - fifth_ratio
    if advantage >= 0.20:
        target = 12
    elif advantage >= 0.10:
        target = 10
    elif advantage >= 0.05:
        target = 8
    else:
        target = 6
    return min(len(rows), target)


def _ratio_score(value: float, max_ratio: float) -> float:
    return max(0.0, min(100.0, (max_ratio - value) / max_ratio * 100))


def score_recommendation(
    row: dict,
    forecast: dict,
    risk: dict,
    policy: RecommendationPolicy,
    *,
    history_age_hours: float,
) -> dict | None:
    """Attach the fixed T+7/risk/instant score, or reject incomplete data."""
    if forecast.get("status") != "ready" or risk.get("status") != "ready":
        return None
    instant_ratio = _finite_positive(row.get("ratio"))
    forecast_ratio = _finite_positive(forecast.get("forecast_balance_ratio"))
    risk_ratio = _finite_positive(risk.get("risk_balance_ratio"))
    risk_level = str(risk.get("overall_level") or "unknown")
    if (
        instant_ratio is None
        or forecast_ratio is None
        or risk_ratio is None
        or risk_level not in {"low", "medium", "high"}
        or not math.isfinite(history_age_hours)
        or history_age_hours < 0
        or history_age_hours > 24
    ):
        return None

    components = {
        "t7": _ratio_score(forecast_ratio, policy.max_ratio),
        "risk": _ratio_score(risk_ratio, policy.max_ratio),
        "instant": _ratio_score(instant_ratio, policy.max_ratio),
    }
    score = components["t7"] * 0.40
    score += components["risk"] * 0.35
    score += components["instant"] * 0.25
    deductions: list[dict[str, float | str]] = []

    risk_deduction = {"low": 0.0, "medium": 5.0, "high": 15.0}[risk_level]
    if risk_deduction:
        deductions.append({"reason": f"{risk_level}_risk", "points": risk_deduction})
        score -= risk_deduction
    if forecast.get("confidence") != "normal":
        deductions.append({"reason": "low_t7_confidence", "points": 3.0})
        score -= 3
    if risk.get("confidence") != "normal":
        deductions.append({"reason": "low_risk_confidence", "points": 3.0})
        score -= 3
    if history_age_hours > 6:
        deductions.append({"reason": "aged_history_cache", "points": 2.0})
        score -= 2

    return {
        **row,
        "score": round(max(0.0, min(100.0, score)), 2),
        "score_components": {key: round(value, 2) for key, value in components.items()},
        "score_deductions": deductions,
        "forecast": forecast,
        "risk_assessment": risk,
        "history_age_hours": round(history_age_hours, 2),
    }


def final_recommendation_rank_key(row: dict) -> tuple:
    forecast = row["forecast"]
    risk = row["risk_assessment"]
    return (
        -float(row["score"]),
        float(risk["risk_balance_ratio"]),
        float(forecast["forecast_balance_ratio"]),
        float(row["ratio"]),
        -int(row["steam_transaction_quantity"]),
        int(row["smis_id"]),
    )
