from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .rules import (
    MAX_RULE_STATE_INTEGER,
    RuleDefinition,
    RuleMeasurement,
    parse_rule_state_integer,
)

AlertStyle = Literal["rule", "breakthrough"]


@dataclass(frozen=True)
class AlertSignal:
    signal_key: str
    event_type: str
    style: AlertStyle
    value: float
    level: int = 0
    depth: float = 0.0


@dataclass(frozen=True)
class RuleTransition:
    changes: dict[str, Any]
    signal: AlertSignal | None = None


def _finite_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_rule_transition(
    *,
    rule_id: int,
    definition: RuleDefinition,
    threshold: float,
    state: Mapping[str, Any],
    measurement: RuleMeasurement,
    observed_at: str,
    confirmations: int,
    clear_confirmations: int,
    breakthrough_step: float,
) -> RuleTransition:
    """Evaluate one rule without storage, formatting, or delivery side effects."""

    value = measurement.value
    finite_value = _finite_number(value)
    if finite_value is not None and finite_value <= 0:
        finite_value = None
    baseline = measurement.baseline
    finite_baseline = _finite_number(baseline)
    state_values = {
        key: parse_rule_state_integer(state.get(key, 0))
        for key in (
            "alert_active",
            "qualifying_count",
            "clearing_count",
            "highest_notified_level",
        )
    }
    if (
        any(number is None for number in state_values.values())
        or state_values["alert_active"] not in {0, 1}
    ):
        return RuleTransition({
            "alert_active": 0,
            "qualifying_count": 0,
            "clearing_count": 0,
            "last_value": None,
            "last_baseline": None,
            "last_observed_at": observed_at,
            "highest_notified_level": 0,
            "status": "规则状态无效",
        })
    active = int(state_values["alert_active"])
    qualifying_count = int(state_values["qualifying_count"])
    clearing_count = int(state_values["clearing_count"])
    highest_level = int(state_values["highest_notified_level"]) if active else 0
    changes: dict[str, Any] = {
        "alert_active": active,
        "qualifying_count": qualifying_count,
        "clearing_count": clearing_count,
        "last_value": finite_value,
        "last_baseline": finite_baseline,
        "last_observed_at": observed_at,
        "highest_notified_level": highest_level,
        "status": (
            measurement.status
            if value is None or finite_value is not None
            else "测量值无效"
        ),
    }
    if finite_value is None:
        changes.update({"qualifying_count": 0, "clearing_count": 0})
        return RuleTransition(changes)
    value = finite_value

    try:
        limit = definition.limit(threshold)
    except (TypeError, ValueError):
        limit = math.nan
    if not math.isfinite(limit) or limit <= 0:
        changes.update({
            "alert_active": 0,
            "qualifying_count": 0,
            "clearing_count": 0,
            "highest_notified_level": 0,
            "status": "规则阈值无效",
        })
        return RuleTransition(changes)
    qualifies = definition.qualifies(value, limit)
    if not active:
        qualifying = (
            min(qualifying_count + 1, MAX_RULE_STATE_INTEGER)
            if qualifies
            else 0
        )
        changes.update({"qualifying_count": qualifying, "clearing_count": 0})
        if qualifying < confirmations:
            return RuleTransition(changes)

        try:
            level, depth = definition.breakthrough(value, limit, breakthrough_step)
        except (ArithmeticError, TypeError, ValueError):
            changes.update({"qualifying_count": 0, "status": "测量值无效"})
            return RuleTransition(changes)
        changes.update({
            "alert_active": 1,
            "qualifying_count": qualifying,
            "last_signal_at": observed_at,
            "highest_notified_level": level,
        })
        return RuleTransition(
            changes,
            AlertSignal(
                signal_key=f"rule:{rule_id}:{observed_at}",
                event_type=f"rule_{definition.rule_type}",
                style="breakthrough" if level > 0 else "rule",
                value=value,
                level=level,
                depth=depth,
            ),
        )

    clearing = (
        min(clearing_count + 1, MAX_RULE_STATE_INTEGER)
        if definition.clears(value, limit)
        else 0
    )
    changes.update({"qualifying_count": 0, "clearing_count": clearing})
    if clearing >= clear_confirmations:
        changes.update({
            "alert_active": 0,
            "clearing_count": 0,
            "highest_notified_level": 0,
        })
        return RuleTransition(changes)

    try:
        level, depth = definition.breakthrough(value, limit, breakthrough_step)
    except (ArithmeticError, TypeError, ValueError):
        changes.update({
            "qualifying_count": 0,
            "clearing_count": 0,
            "status": "测量值无效",
        })
        return RuleTransition(changes)
    previous_level = highest_level
    signal = None
    if qualifies and level > previous_level:
        changes["last_signal_at"] = observed_at
        signal = AlertSignal(
            signal_key=f"rule:{rule_id}:breakthrough:{level}:{observed_at}",
            event_type="rule_breakthrough",
            style="breakthrough",
            value=value,
            level=level,
            depth=depth,
        )
    # This persisted legacy field tracks the current level so re-entry alerts.
    changes["highest_notified_level"] = level if qualifies else 0
    return RuleTransition(changes, signal)
