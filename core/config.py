from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PluginSettings:
    enabled: bool = True
    interval_seconds: int = 1800
    quote_cache_seconds: int = 60
    max_items: int = 20
    max_workers: int = 2
    breakthrough_step_percent: float = 3.0
    daily_summary_time: str = "09:00"
    smis_timeout_seconds: float = 15.0
    smis_max_retries: int = 3
    smis_min_request_interval_seconds: float = 1.0


class ConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("；".join(errors))
        self.errors = errors


def _positive(raw: Any, name: str, cast: type[int] | type[float], errors: list[str]):
    if isinstance(raw, bool):
        errors.append(f"{name} 必须是正数")
        return cast(1)
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        errors.append(f"{name} 必须是正数")
        return cast(1)
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{name} 必须是有限正数")
    elif value <= 0:
        errors.append(f"{name} 必须大于 0")
    return value


def load_settings(config: Mapping[str, Any]) -> PluginSettings:
    errors: list[str] = []
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("enabled 必须是布尔值")
        enabled = False
    daily_summary_time = str(config.get("daily_summary_time", "09:00")).strip()
    try:
        datetime.strptime(daily_summary_time, "%H:%M")
    except ValueError:
        errors.append("daily_summary_time 必须使用 HH:MM 格式")

    settings = PluginSettings(
        enabled=enabled,
        interval_seconds=_positive(
            config.get("interval_seconds", 1800), "interval_seconds", int, errors
        ),
        quote_cache_seconds=_positive(
            config.get("quote_cache_seconds", 60), "quote_cache_seconds", int, errors
        ),
        max_items=_positive(config.get("max_items", 20), "max_items", int, errors),
        max_workers=_positive(
            config.get("max_workers", 2), "max_workers", int, errors
        ),
        breakthrough_step_percent=_positive(
            config.get("breakthrough_step_percent", 3),
            "breakthrough_step_percent",
            float,
            errors,
        ),
        daily_summary_time=daily_summary_time,
        smis_timeout_seconds=_positive(
            config.get("smis_timeout_seconds", 15),
            "smis_timeout_seconds",
            float,
            errors,
        ),
        smis_max_retries=_positive(
            config.get("smis_max_retries", 3), "smis_max_retries", int, errors
        ),
        smis_min_request_interval_seconds=_positive(
            config.get("smis_min_request_interval_seconds", 1),
            "smis_min_request_interval_seconds",
            float,
            errors,
        ),
    )
    if settings.max_workers > 8:
        errors.append("max_workers 不能大于 8")
    if settings.max_items > 200:
        errors.append("max_items 不能大于 200")
    if errors:
        raise ConfigError(errors)
    return settings
