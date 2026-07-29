from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .market import MarketSnapshot

RuleDirection = Literal["at_or_below", "at_or_above"]
MAX_RULE_STATE_INTEGER = (1 << 63) - 1


def parse_rule_state_integer(value: object) -> int | None:
    """Parse SQLite state without losing 64-bit integer precision."""
    if isinstance(value, int):
        return value if 0 <= value <= MAX_RULE_STATE_INTEGER else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(number)
        or number < 0
        or number > MAX_RULE_STATE_INTEGER
        or not number.is_integer()
    ):
        return None
    return int(number)


@dataclass(frozen=True)
class RuleDefinition:
    """Stable behavior and display metadata for a persisted rule identifier."""

    rule_type: str
    list_label: str
    metric_label: str
    daily_label: str
    alert_prefix: str
    direction: RuleDirection
    percentage: bool = False

    @property
    def unit(self) -> str:
        return "%" if self.percentage else "元"

    def limit(self, threshold: float) -> float:
        return float(threshold) / 100 if self.percentage else float(threshold)

    def qualifies(self, value: float, limit: float) -> bool:
        if self.direction == "at_or_above":
            return value >= limit
        return value <= limit

    def clears(self, value: float, limit: float) -> bool:
        boundary = limit * (0.97 if self.direction == "at_or_above" else 1.03)
        if self.direction == "at_or_above":
            return value < boundary
        return value > boundary

    def breakthrough(self, value: float, limit: float, step: float) -> tuple[int, float]:
        if not math.isfinite(step) or step <= 0:
            raise ValueError("突破档位步长无效")
        depth = (
            (value - limit) / limit
            if self.direction == "at_or_above"
            else (limit - value) / limit
        )
        depth = max(0.0, depth)
        if not math.isfinite(depth):
            raise ValueError("突破幅度无效")
        level = math.floor((depth + 1e-12) / step)
        if level > MAX_RULE_STATE_INTEGER:
            raise OverflowError("突破档位超出存储范围")
        return level, depth

    def format_value(self, value: float) -> str:
        return f"{value:.2%}" if self.percentage else f"¥{value:.2f}"

    def format_threshold(self, threshold: float) -> str:
        return f"{threshold:.2f}%" if self.percentage else f"¥{threshold:.2f}"


RULE_DEFINITIONS: dict[str, RuleDefinition] = {
    "ratio": RuleDefinition(
        "ratio", "即时比例", "即时比例", "即时比例", "【即时挂刀】",
        "at_or_below", percentage=True,
    ),
    "t7": RuleDefinition(
        "t7", "T+7预测比例", "七日预测比例", "T+7 预测比例",
        "【T+7预测挂刀】", "at_or_below", percentage=True,
    ),
    "platform": RuleDefinition(
        "platform", "平台买入价", "最低平台价", "平台价", "【平台到价】",
        "at_or_below",
    ),
    "steam": RuleDefinition(
        "steam", "Steam清仓价", "Steam 售价", "Steam 价", "【Steam清仓】",
        "at_or_above",
    ),
}

# Persisted public API values. Keep this set stable across compatible releases.
RULE_TYPES = set(RULE_DEFINITIONS)


@dataclass(frozen=True)
class RuleMeasurement:
    value: float | None
    baseline: float | None
    status: str


def get_rule_definition(rule_type: str) -> RuleDefinition | None:
    return RULE_DEFINITIONS.get(str(rule_type))


def _finite_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_rule(rule_type: str, threshold: float) -> tuple[str, str] | None:
    definition = get_rule_definition(rule_type)
    if definition is None:
        return "invalid_rule_type", "规则类型必须是 ratio、t7、platform 或 steam"
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return "invalid_threshold", "阈值必须是有限数值"
    if not math.isfinite(value):
        return "invalid_threshold", "阈值必须是有限数值"
    if definition.percentage and not 1 <= value <= 100:
        return "invalid_threshold", "比例阈值必须在 1 到 100 之间"
    if not definition.percentage and value <= 0:
        return "invalid_threshold", "价格阈值必须大于 0"
    return None


def measure_rule(
    snapshot: MarketSnapshot, forecast: dict, rule_type: str,
) -> RuleMeasurement:
    definition = get_rule_definition(rule_type)
    if definition is None:
        return RuleMeasurement(None, None, f"不支持的规则类型：{rule_type}")
    if definition.rule_type == "steam":
        value = _finite_number(snapshot.steam_sell_price)
        if snapshot.steam_sell_price is not None and value is None:
            return RuleMeasurement(None, None, "价格数据无效")
        value = value or 0.0
        return RuleMeasurement(value if value > 0 else None, None, "ready")
    lowest = snapshot.lowest_platform
    if lowest is None:
        return RuleMeasurement(None, None, "价格缺失")
    platform_price = _finite_number(lowest[1])
    if platform_price is None:
        return RuleMeasurement(None, None, "价格数据无效")
    if definition.rule_type == "platform":
        return RuleMeasurement(platform_price, None, "ready")
    if definition.rule_type == "ratio":
        try:
            ratio = snapshot.calculated_ratio
            baseline = _finite_number(snapshot.steam_net)
        except (ArithmeticError, TypeError, ValueError):
            return RuleMeasurement(None, None, "价格数据无效")
        finite_ratio = _finite_number(ratio)
        if ratio is not None and finite_ratio is None:
            return RuleMeasurement(None, baseline, "价格数据无效")
        return RuleMeasurement(finite_ratio, baseline, "ready")
    predicted = forecast.get("predicted_steam_net")
    ratio = forecast.get("forecast_balance_ratio")
    finite_predicted = _finite_number(predicted)
    finite_ratio = _finite_number(ratio)
    if (
        forecast.get("status") != "ready"
        or finite_predicted is None
        or finite_ratio is None
        or finite_predicted <= 0
        or finite_ratio <= 0
    ):
        reason = (forecast.get("reasons") or ["历史或当前行情不足"])[0]
        return RuleMeasurement(None, finite_predicted, f"七日预测不可用：{reason}")
    return RuleMeasurement(finite_ratio, finite_predicted, "ready")


def rule_value(
    snapshot: MarketSnapshot, forecast: dict, rule_type: str,
) -> tuple[float | None, float | None, str]:
    """Compatibility tuple for existing callers; new code should use measure_rule."""

    measurement = measure_rule(snapshot, forecast, rule_type)
    return measurement.value, measurement.baseline, measurement.status
