from __future__ import annotations

from datetime import datetime
from statistics import median

from .market import steam_net_amount


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def t7_stats(rows: list[dict]) -> dict:
    values = [steam_net_amount(float(row["steam_sell_price"])) for row in rows]
    values = [value for value in values if value > 0]
    span_days = 0.0
    if len(rows) >= 2:
        first = datetime.fromisoformat(rows[0]["source_updated_at"])
        last = datetime.fromisoformat(rows[-1]["source_updated_at"])
        span_days = max(0.0, (last - first).total_seconds() / 86400)
    return {
        "t7_sample_count": len(values),
        "t7_span_days": round(span_days, 2),
        "t7_sufficient": len(values) >= 12 and span_days >= 3,
        "t7_steam_net_low": min(values) if values else None,
        "t7_steam_net_p25": percentile(values, 0.25),
        "t7_steam_net_median": median(values) if values else None,
    }
