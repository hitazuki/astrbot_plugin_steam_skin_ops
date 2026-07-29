from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from .market import MAX_SOURCE_FUTURE_SKEW_SECONDS, MarketSnapshot


@dataclass(frozen=True)
class RecommendationPolicy:
    """Deterministic eligibility and ordering policy for instant candidates."""

    min_price: float = 30.0
    max_price: float = 5000.0
    max_ratio: float = 0.80
    min_steam_volume: int = 50
    max_source_age_minutes: int = 120
    candidate_limit: int = 50
    result_limit: int = 5

    def __post_init__(self) -> None:
        numeric = (self.min_price, self.max_price, self.max_ratio)
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("推荐策略价格和比例必须是有限数值")
        if self.min_price <= 0 or self.max_price < self.min_price:
            raise ValueError("推荐策略价格范围无效")
        if self.max_ratio <= 0 or self.max_ratio > 1:
            raise ValueError("推荐策略最大比例必须在 0～100% 之间")
        if self.min_steam_volume <= 0:
            raise ValueError("推荐策略最低 Steam 日成交量必须大于 0")
        if self.max_source_age_minutes <= 0:
            raise ValueError("推荐策略最大数据年龄必须大于 0")
        if not 1 <= self.candidate_limit <= 50:
            raise ValueError("推荐候选数量必须在 1～50 之间")
        if not 1 <= self.result_limit <= self.candidate_limit:
            raise ValueError("推荐结果数量必须在 1～候选数量之间")

    def with_budget(self, budget: float | None) -> RecommendationPolicy:
        if budget is None:
            return self
        try:
            value = float(budget)
        except (TypeError, ValueError) as exc:
            raise ValueError("预算上限必须是有效金额") from exc
        if not math.isfinite(value) or value < self.min_price:
            raise ValueError(f"预算上限不能低于 ¥{self.min_price:.2f}")
        return replace(self, max_price=min(self.max_price, value))

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


def _rank_key(row: dict) -> tuple:
    return (
        float(row["ratio"]),
        float(row["stock_to_flow_days"]),
        -int(row["steam_transaction_quantity"]),
        -int(row["platform_sell_num"]),
        -float(row["source_timestamp"]),
        int(row["smis_id"]),
    )


def rank_recommendations(
    snapshots: list[MarketSnapshot],
    policy: RecommendationPolicy,
    *,
    now: datetime | None = None,
) -> dict:
    """Filter unsafe candidates and return a reproducibly ordered Top N."""

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    eligible_by_id: dict[int, dict] = {}
    for snapshot in snapshots:
        if int(snapshot.appid) != 730:
            continue
        source_time = snapshot.source_updated_at
        if source_time.tzinfo is None:
            continue
        source_time = source_time.astimezone(timezone.utc)
        age_seconds = (current_time - source_time).total_seconds()
        if (
            age_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS
            or age_seconds > policy.max_source_age_minutes * 60
        ):
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
            "appid": int(snapshot.appid),
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
            },
        }
        existing = eligible_by_id.get(int(snapshot.smis_id))
        if existing is None or _rank_key(row) < _rank_key(existing):
            eligible_by_id[int(snapshot.smis_id)] = row

    ranked = sorted(eligible_by_id.values(), key=_rank_key)
    for row in ranked:
        row.pop("source_timestamp", None)
    return {
        "recommendations": ranked[: policy.result_limit],
        "scanned_count": len(snapshots),
        "eligible_count": len(ranked),
        "policy": policy.display_dict(),
        "strategy": "instant_ratio_liquidity_v1",
    }
