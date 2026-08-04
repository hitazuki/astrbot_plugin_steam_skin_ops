from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .market import MarketSnapshot, steam_net_amount
from .risk import (
    FORECAST_MODEL_VERSION,
    MarketAnalysis,
    _daily_field_points,
    _daily_price_points,
    volume_cohort,
)

CANDIDATE_SCHEMA_VERSION = "recommendation_candidate.v1"
RANKING_POLICY_VERSION = "t7_risk_instant_score_v3"
RANKING_WEIGHTS = {"t7": 0.40, "risk_ratio": 0.35, "instant": 0.25}


def _daily_series(canonical_rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    prices = dict(_daily_price_points(canonical_rows))
    closes: dict[Any, tuple[datetime, float]] = {}
    for row in canonical_rows:
        value = row.get("steam_sell_price")
        if value is None or float(value) <= 0:
            continue
        timestamp = datetime.fromisoformat(str(row["_grid_at"]))
        day = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        net = round(steam_net_amount(float(value)), 4)
        if day not in closes or timestamp > closes[day][0]:
            closes[day] = (timestamp, net)
    volume = dict(_daily_field_points(canonical_rows, "steam_transaction_quantity"))
    steam_stock = dict(_daily_field_points(canonical_rows, "steam_sell_num"))
    buff_stock = dict(_daily_field_points(canonical_rows, "buff_sell_num"))
    days = sorted(set(prices) | set(volume) | set(steam_stock) | set(buff_stock))[-21:]
    return {
        "date": [day.date().isoformat() for day in days],
        "steam_net_cny": [round(prices[day], 4) if day in prices else None for day in days],
        "steam_close_net_cny": [closes[day][1] if day in closes else None for day in days],
        "steam_daily_volume": [round(volume[day], 2) if day in volume else None for day in days],
        "steam_sell_count": [round(steam_stock[day], 2) if day in steam_stock else None for day in days],
        "buff_sell_count": [round(buff_stock[day], 2) if day in buff_stock else None for day in days],
    }


def market_observation_digest(
    snapshot: MarketSnapshot,
    analysis: MarketAnalysis,
) -> str:
    """Identify the market facts used at issuance without storing them twice."""
    facts = {
        "item_key": snapshot.item_key,
        "observed_at_utc": snapshot.observed_at.isoformat(),
        "source_updated_at_utc": snapshot.source_updated_at.isoformat(),
        "steam_gross_cny": snapshot.steam_sell_price,
        "steam_net_cny": snapshot.steam_net,
        "steam_sell_count": snapshot.steam_sell_num,
        "steam_daily_volume": snapshot.steam_transaction_quantity,
        "daily_series_21d": _daily_series(analysis.canonical_rows),
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_candidate_record(
    snapshot: MarketSnapshot,
    analysis: MarketAnalysis,
    *,
    ranking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    forecast = analysis.forecast
    risk = analysis.risk
    executable = [
        {"platform": name, "sell_price_cny": price, "sell_count": count}
        for name, price, count in snapshot.platform_quotes
        if count > 0
    ]
    lowest = min(executable, key=lambda row: (row["sell_price_cny"], row["platform"])) if executable else None
    missing: list[str] = []
    if forecast.get("status") != "ready":
        missing.extend(
            [
                "model_output.forecast.p20_steam_net_cny",
                "model_output.forecast.p50_steam_net_cny",
                "model_output.forecast.p80_steam_net_cny",
            ]
        )
    if risk.get("status") != "ready":
        missing.append("model_output.risk")
    ranking = ranking or {}
    daily_series = _daily_series(analysis.canonical_rows)
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "record_id": f"{snapshot.item_key}@{snapshot.observed_at.isoformat()}",
        "identity": {
            "item_key": snapshot.item_key,
            "smis_id": snapshot.smis_id,
            "appid": snapshot.appid,
            "name": snapshot.name,
            "name_zh": snapshot.name_zh,
        },
        "provenance": {
            "source": snapshot.source,
            "observed_at_utc": snapshot.observed_at.isoformat(),
            "source_updated_at_utc": snapshot.source_updated_at.isoformat(),
            "volume_cohort": volume_cohort(snapshot.steam_transaction_quantity),
        },
        "observed_market": {
            "steam_gross_cny": snapshot.steam_sell_price,
            "steam_net_cny": snapshot.steam_net,
            "steam_sell_count": snapshot.steam_sell_num,
            "steam_daily_volume": snapshot.steam_transaction_quantity,
            "platform_quotes": executable,
            "lowest_executable_platform": lowest["platform"] if lowest else None,
            "lowest_executable_price_cny": (lowest["sell_price_cny"] if lowest else None),
            "instant_balance_ratio": ranking.get("instant_balance_ratio", snapshot.calculated_ratio),
            "stock_to_flow_days": (
                round(float(snapshot.steam_sell_num) / snapshot.steam_transaction_quantity, 2)
                if snapshot.steam_sell_num is not None
                and snapshot.steam_transaction_quantity
                and snapshot.steam_transaction_quantity > 0
                else None
            ),
        },
        "derived_history": {
            "daily_point_count": len(daily_series["date"]),
            "coverage": forecast.get("coverage", 0),
            "trend_regime": forecast.get("trend_regime", "unknown"),
            "daily_series_21d": daily_series,
        },
        "model_output": {
            "forecast": {
                "status": forecast.get("status"),
                "model_version": FORECAST_MODEL_VERSION,
                "mode": forecast.get("mode"),
                "confidence": forecast.get("confidence"),
                "horizon_days": forecast.get("horizon_days", 7),
                "p20_steam_net_cny": forecast.get("p20_steam_net"),
                "p50_steam_net_cny": forecast.get("predicted_steam_net"),
                "p80_steam_net_cny": forecast.get("p80_steam_net"),
                "change_pct": forecast.get("change_pct"),
                "balance_ratio_p50": forecast.get("forecast_balance_ratio"),
                "balance_ratio_p20": forecast.get("risk_balance_ratio"),
                "probability_up": forecast.get("probability_up"),
                "interval_source": forecast.get("interval_source"),
                "exact_t7_validation_count": forecast.get("exact_t7_validation_count", 0),
                "reasons": forecast.get("reasons") or [],
            },
            "risk": risk,
        },
        "deterministic_ranking": {
            "policy_version": RANKING_POLICY_VERSION,
            "eligible": bool(ranking),
            "score": ranking.get("score"),
            "components": ranking.get("score_components"),
            "deductions": ranking.get("score_deductions") or [],
            "weights": dict(RANKING_WEIGHTS),
        },
        "data_quality": {
            "missing_fields": missing,
            "forecast_status": forecast.get("status"),
            "risk_status": risk.get("status"),
        },
        "llm_contract": {
            "role": "analyze_observations_compare_deterministic",
            "may_override_deterministic_eligibility": False,
            "may_override_deterministic_score": False,
            "may_challenge_deterministic_results": True,
            "must_cite_record_fields": True,
        },
    }
