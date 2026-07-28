from __future__ import annotations

from .market import MarketSnapshot

RULE_TYPES = {"ratio", "t7", "platform", "steam"}


def validate_rule(rule_type: str, threshold: float) -> tuple[str, str] | None:
    if rule_type not in RULE_TYPES:
        return "invalid_rule_type", "规则类型必须是 ratio、t7、platform 或 steam"
    if rule_type in {"ratio", "t7"} and not 1 <= threshold <= 100:
        return "invalid_threshold", "比例阈值必须在 1 到 100 之间"
    if rule_type in {"platform", "steam"} and threshold <= 0:
        return "invalid_threshold", "价格阈值必须大于 0"
    return None


def rule_value(
    snapshot: MarketSnapshot, stats: dict, rule_type: str,
) -> tuple[float | None, float | None, str]:
    lowest = snapshot.lowest_platform
    if rule_type == "steam":
        value = float(snapshot.steam_sell_price or 0)
        return (value if value > 0 else None), None, "ready"
    if lowest is None:
        return None, None, "价格缺失"
    platform_price = float(lowest[1])
    if rule_type == "platform":
        return platform_price, None, "ready"
    if rule_type == "ratio":
        return snapshot.calculated_ratio, snapshot.steam_net, "ready"
    if not stats["t7_sufficient"] or not stats["t7_steam_net_p25"]:
        return None, stats.get("t7_steam_net_p25"), "历史不足"
    baseline = float(stats["t7_steam_net_p25"])
    return platform_price / baseline, baseline, "ready"
