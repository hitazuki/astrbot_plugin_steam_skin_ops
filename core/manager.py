from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timezone
from urllib.parse import quote

from .alerting import evaluate_rule_transition
from .history import seven_day_history_stats
from .history_backfill import HistoryBackfiller
from .integrations.smis import MarketDataProvider
from .market import MAX_SOURCE_FUTURE_SKEW_SECONDS, MarketSnapshot
from .presentation import (
    daily_analysis_suffix,
    format_breakthrough_alert,
    format_rule_alert,
)
from .repository import MonitorStorage, RuleItemLimitError
from .risk import forecast_and_risk
from .rules import (
    MAX_RULE_STATE_INTEGER,
    RuleMeasurement,
    get_rule_definition,
    measure_rule,
    parse_rule_state_integer,
    validate_rule,
)

logger = logging.getLogger(__name__)


def _positive_finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _state_count(value: object) -> int:
    return parse_rule_state_integer(value) or 0


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, data: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data


def item_from_row(row: dict) -> dict:
    return {
        "smis_id": int(row["smis_id"]),
        "item_key": str(row["item_key"]),
        "appid": int(row["appid"]),
        "name": str(row["hash_name"]),
        "name_zh": str(row["cn_name"]),
    }


class MonitoringManager:
    def __init__(
        self,
        storage: MonitorStorage,
        source: MarketDataProvider,
        *,
        delivery_driver: str = "astrbot",
        max_items: int = 20,
        quote_cache_seconds: int = 60,
        confirmations: int = 1,
        clear_confirmations: int = 1,
        health_failure_threshold: int = 3,
        breakthrough_step_percent: float = 3,
        poll_interval_seconds: int = 1800,
    ) -> None:
        self.storage = storage
        self.source = source
        self.delivery_driver = delivery_driver
        self.max_items = max_items
        self.quote_cache_seconds = quote_cache_seconds
        self.confirmations = max(1, int(confirmations))
        self.clear_confirmations = max(1, int(clear_confirmations))
        self.health_failure_threshold = max(1, int(health_failure_threshold))
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))
        self.history_backfiller = HistoryBackfiller(
            storage,
            source,
            poll_interval_seconds=self.poll_interval_seconds,
        )
        if not math.isfinite(breakthrough_step_percent) or breakthrough_step_percent <= 0:
            raise ValueError("突破档位步长必须大于 0")
        self.breakthrough_step = float(breakthrough_step_percent) / 100
        self._locks_guard = threading.Lock()
        self._item_locks: dict[int, threading.Lock] = {}
        self._audit_rule_configurations()
        self._audit_rule_states()
        self._sync_t7_rule_basis()
        self._sync_breakthrough_behavior()
        self._sync_breakthrough_step()

    @staticmethod
    def _rule_configuration_error(rule_type: str, threshold: object) -> str | None:
        if get_rule_definition(rule_type) is None:
            return f"不支持的规则类型：{rule_type}"
        validation = validate_rule(rule_type, threshold)
        return f"规则配置无效：{validation[1]}" if validation else None

    def _quarantine_rule(self, rule: dict, status: str) -> None:
        rule_id = int(rule["id"])
        recipient_key = str(rule["recipient_key"])
        self.storage.cancel_pending_rule_notifications(rule_id, recipient_key)
        self.storage.update_rule_state(
            rule_id,
            alert_active=0,
            qualifying_count=0,
            clearing_count=0,
            last_value=None,
            last_baseline=None,
            highest_notified_level=0,
            status=status,
        )
        self.storage.set_rule_operational(rule_id, recipient_key, False)

    def _audit_rule_configurations(self) -> None:
        for rule in self.storage.list_rules():
            operational = parse_rule_state_integer(rule.get("operational"))
            if operational not in {0, 1}:
                self._quarantine_rule(rule, "规则运行状态无效")
                continue
            status = self._rule_configuration_error(
                str(rule["rule_type"]),
                rule["threshold"],
            )
            if status is not None:
                self._quarantine_rule(rule, status)

    def _audit_rule_states(self) -> None:
        integer_fields = (
            "alert_active",
            "qualifying_count",
            "clearing_count",
            "highest_notified_level",
        )
        for rule in self.storage.list_rules():
            state = self.storage.get_rule_state(int(rule["id"]))
            integers = {
                key: parse_rule_state_integer(state.get(key, 0))
                for key in integer_fields
            }
            if any(value is None for value in integers.values()):
                self._quarantine_rule(rule, "规则状态无效")
                continue
            if (
                integers["alert_active"] not in {0, 1}
            ):
                self._quarantine_rule(rule, "规则状态无效")
                continue
            numeric_values = (
                state.get("last_value"),
                state.get("last_baseline"),
            )
            if any(
                value is not None and _positive_finite(value) is None
                for value in numeric_values
            ):
                self._quarantine_rule(rule, "测量值无效")

    def _sync_t7_rule_basis(self) -> None:
        key = "alerting:t7_basis"
        value = "seven_day_dynamic_forecast:v1"
        if self.storage.get_metadata(key) == value:
            return
        self.storage.reset_rule_states("t7", operational_only=True)
        self.storage.set_metadata(key, value)

    def _sync_breakthrough_behavior(self) -> None:
        key = "alerting:breakthrough_behavior"
        value = "reentry:v1"
        if self.storage.get_metadata(key) == value:
            return
        self._recalculate_active_breakthrough_levels()
        self.storage.set_metadata(key, value)

    def _recalculate_active_breakthrough_levels(self) -> None:
        """Store the current active level in the legacy highest-level column."""
        for rule in self.storage.list_rules(operational_only=True):
            state = self.storage.get_rule_state(int(rule["id"]))
            current = state.get("last_value")
            if not _state_count(state.get("alert_active")) or current is None:
                continue
            rule_type = str(rule["rule_type"])
            definition = get_rule_definition(rule_type)
            status = self._rule_configuration_error(rule_type, rule["threshold"])
            if definition is None or status is not None:
                self._quarantine_rule(
                    rule,
                    status or f"不支持的规则类型：{rule_type}",
                )
                continue
            current_value = _positive_finite(current)
            if current_value is None:
                self._quarantine_rule(rule, "测量值无效")
                continue
            threshold = float(rule["threshold"])
            limit = definition.limit(threshold)
            try:
                level, _ = definition.breakthrough(
                    current_value,
                    limit,
                    self.breakthrough_step,
                )
            except (ArithmeticError, TypeError, ValueError):
                self._quarantine_rule(rule, "测量值无效")
                continue
            self.storage.update_rule_state(
                int(rule["id"]),
                alert_active=1,
                qualifying_count=_state_count(state.get("qualifying_count")),
                clearing_count=_state_count(state.get("clearing_count")),
                last_value=current_value,
                last_baseline=_positive_finite(state.get("last_baseline")),
                highest_notified_level=level,
            )

    def _sync_breakthrough_step(self) -> None:
        key = "alerting:breakthrough_step"
        value = f"{self.breakthrough_step:.12g}"
        if self.storage.get_metadata(key) == value:
            return
        self._recalculate_active_breakthrough_levels()
        self.storage.set_metadata(key, value)

    def _lock_for(self, smis_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._item_locks.setdefault(int(smis_id), threading.Lock())

    def list_items(self, recipient_key: str) -> list[dict]:
        result = []
        active_ids = {
            int(rule["smis_id"])
            for rule in self.storage.list_rules(recipient_key=recipient_key)
        }
        for item in self.storage.list_items():
            if int(item["smis_id"]) not in active_ids:
                continue
            latest = self.storage.latest_snapshot(item["item_key"])
            try:
                latest_payload = self._snapshot_row_to_dict(latest) if latest else None
            except Exception as exc:
                logger.warning(
                    "忽略损坏的饰品缓存 smis_id=%s: %s",
                    item["smis_id"],
                    exc,
                )
                latest_payload = None
            result.append({**item, "latest": latest_payload})
        return result

    def search_items(self, query: str, limit: int = 10) -> list[dict]:
        try:
            return self.source.search_items(query, limit=limit)
        except Exception as exc:
            logger.warning("SMIS 搜索失败：%s", exc)
            raise ServiceError(503, "smis_search_failed", "SMIS 搜索暂时不可用") from exc

    @staticmethod
    def _validate_rule(rule_type: str, threshold: float) -> None:
        error = validate_rule(rule_type, threshold)
        if error:
            raise ServiceError(422, error[0], error[1])

    def list_rules(self, recipient_key: str, smis_id: int | None = None) -> list[dict]:
        rows = self.storage.list_rules(recipient_key=recipient_key, smis_id=smis_id)
        for row in rows:
            row["state"] = self.storage.get_rule_state(row["id"])
        return rows

    def add_rule(
        self, recipient_key: str, smis_id: int, rule_type: str, threshold: float
    ) -> dict:
        rule_type = rule_type.strip().lower()
        self._validate_rule(rule_type, threshold)
        if (
            not self.storage.list_rules(smis_id=smis_id, operational_only=True)
            and self.storage.count_rule_items() >= self.max_items
        ):
            raise ServiceError(409, "item_limit", f"最多只能监控 {self.max_items} 个饰品")
        item = self.storage.get_item(smis_id)
        if item is None:
            try:
                metadata = self.source.fetch_metadata(smis_id)
            except Exception as exc:
                raise ServiceError(503, "source_unavailable", f"SMIS 饰品信息获取失败：{exc}") from exc
            item = self.storage.upsert_item(metadata)
        try:
            rule = self.storage.add_rule(
                smis_id,
                recipient_key,
                rule_type,
                threshold,
                max_items=self.max_items,
            )
        except RuleItemLimitError as exc:
            self.storage.delete_item_if_unreferenced(smis_id)
            raise ServiceError(
                409,
                "item_limit",
                f"最多只能监控 {self.max_items} 个饰品",
            ) from exc
        try:
            self.history_backfiller.ensure_history(item_from_row(item))
        except Exception as exc:
            logger.warning("规则历史回填失败 smis_id=%s: %s", smis_id, exc)
        rule["state"] = self.storage.get_rule_state(rule["id"])
        return rule

    def update_rule(self, recipient_key: str, rule_id: int, threshold: float) -> dict:
        rule = self.storage.get_rule(rule_id)
        if not rule or str(rule["recipient_key"]) != recipient_key:
            raise ServiceError(404, "rule_not_found", "当前会话未找到该规则")
        self._validate_rule(str(rule["rule_type"]), threshold)
        try:
            return self.storage.update_rule(
                rule_id,
                recipient_key,
                threshold,
                max_items=self.max_items,
            )
        except RuleItemLimitError as exc:
            raise ServiceError(
                409,
                "item_limit",
                f"最多只能监控 {self.max_items} 个饰品",
            ) from exc

    def remove_rule(self, recipient_key: str, rule_id: int) -> None:
        if not self.storage.delete_rule(rule_id, recipient_key):
            raise ServiceError(404, "rule_not_found", "当前会话未找到该规则")

    def _resolve_local_item(self, query: str) -> dict | None:
        matches = self.storage.resolve_items(query)
        if not matches:
            return None
        if query.isdigit():
            return matches[0]
        exact = [
            row for row in matches
            if query.casefold() in {str(row["hash_name"]).casefold(), str(row["cn_name"]).casefold()}
        ]
        if len(exact) == 1:
            return exact[0]
        return None

    def _market_candidates(self, query: str) -> list[dict]:
        matches = self.search_items(query, limit=10)
        exact = [
            row for row in matches
            if str(row.get("name_zh") or "").casefold() == query.casefold()
        ]
        return exact if exact else matches

    def resolve_smis_id(self, query: str) -> int:
        """Resolve command input through the same catalog matching policy as quotes."""

        target = query.strip()
        if target.isdigit():
            return int(target)
        candidates = self._market_candidates(target)
        if not candidates:
            raise ServiceError(
                404,
                "item_not_found",
                "SMIS 未找到匹配饰品，请先使用 /skin search。",
            )
        if len(candidates) > 1:
            raise ServiceError(
                409,
                "ambiguous_item",
                "名称匹配到多个饰品，请改用 SMIS ID",
                candidates,
            )
        return int(candidates[0]["smis_id"])

    def _resolve_market_item(self, query: str) -> dict:
        try:
            if query.isdigit():
                metadata = self.source.fetch_metadata(int(query))
            else:
                candidates = self._market_candidates(query)
                if not candidates:
                    raise ServiceError(
                        404, "item_not_found", "SMIS 全市场未找到匹配饰品"
                    )
                if len(candidates) > 1:
                    raise ServiceError(
                        409,
                        "ambiguous_item",
                        "名称匹配到多个饰品，请使用 SMIS ID 查询",
                        candidates,
                    )
                metadata = self.source.fetch_metadata(int(candidates[0]["smis_id"]))
        except ServiceError:
            raise
        except Exception as exc:
            logger.warning("SMIS 全市场饰品解析失败：%s", exc)
            raise ServiceError(
                503, "source_unavailable", f"SMIS 饰品信息获取失败：{exc}"
            ) from exc
        return {
            "smis_id": int(metadata["smis_id"]),
            "item_key": str(metadata["item_key"]),
            "appid": int(metadata["appid"]),
            "hash_name": str(metadata["name"]),
            "cn_name": str(metadata["name_zh"]),
        }

    def _resolve_quote_item(self, query: str) -> dict:
        query = query.strip()
        local = self._resolve_local_item(query)
        return local if local is not None else self._resolve_market_item(query)

    def quote(self, query: str) -> dict:
        item_row = self._resolve_quote_item(query)
        item = item_from_row(item_row)
        bootstrap_status: bool | None = False
        try:
            bootstrap_status = self.history_backfiller.ensure_history(item)
        except Exception as exc:
            bootstrap_status = None
            logger.warning("报价历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
        latest = self.storage.latest_snapshot(item["item_key"])
        cached_payload = self._cached_quote_payload(
            item_row,
            latest,
            log_invalid=False,
        )
        if cached_payload is not None:
            return cached_payload

        lock = self._lock_for(item["smis_id"])
        with lock:
            latest = self.storage.latest_snapshot(item["item_key"])
            cached_payload = self._cached_quote_payload(item_row, latest)
            if cached_payload is not None:
                return cached_payload
            try:
                snapshot = self.source.fetch_current(item)
                if bootstrap_status is False:
                    try:
                        self.history_backfiller.backfill_changed_gap(
                            item, latest, snapshot,
                            self.storage.list_rules(
                                smis_id=item["smis_id"],
                                operational_only=True,
                            ),
                        )
                    except Exception as exc:
                        logger.warning(
                            "报价缺口历史回填失败 smis_id=%s: %s",
                            item["smis_id"], exc,
                        )
                self.storage.save_snapshots([snapshot])
                row = self._snapshot_to_row(snapshot)
                return self._quote_payload(
                    item_row,
                    row,
                    cached=False,
                    stale=self._source_snapshot_stale(row),
                )
            except Exception as exc:
                if latest:
                    try:
                        payload = self._quote_payload(
                            item_row,
                            latest,
                            cached=True,
                            stale=True,
                        )
                        payload["warning"] = f"实时刷新失败，返回最近快照：{exc}"
                        return payload
                    except Exception as cache_exc:
                        logger.warning(
                            "忽略损坏的报价缓存 smis_id=%s: %s",
                            item["smis_id"],
                            cache_exc,
                        )
                raise ServiceError(503, "source_unavailable", f"行情获取失败：{exc}") from exc

    def _cached_quote_payload(
        self,
        item: dict,
        row: dict | None,
        *,
        log_invalid: bool = True,
    ) -> dict | None:
        if row is None:
            return None
        try:
            if self._snapshot_age_seconds(row) > self.quote_cache_seconds:
                return None
            return self._quote_payload(
                item,
                row,
                cached=True,
                stale=self._source_snapshot_stale(row),
            )
        except Exception as exc:
            if log_invalid:
                logger.warning(
                    "忽略损坏的报价缓存 smis_id=%s: %s",
                    item["smis_id"],
                    exc,
                )
            return None

    @staticmethod
    def _snapshot_to_row(snapshot: MarketSnapshot) -> dict:
        row = asdict(snapshot)
        row["observed_at"] = snapshot.observed_at.isoformat()
        row["source_updated_at"] = snapshot.source_updated_at.isoformat()
        return row

    @staticmethod
    def _snapshot_age_seconds(row: dict) -> float:
        observed = datetime.fromisoformat(str(row["observed_at"]))
        age_seconds = (datetime.now(timezone.utc) - observed).total_seconds()
        if age_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS:
            return math.inf
        return max(0.0, age_seconds)

    def _source_snapshot_stale(self, snapshot: MarketSnapshot | dict) -> bool:
        value = (
            snapshot.source_updated_at
            if isinstance(snapshot, MarketSnapshot)
            else datetime.fromisoformat(str(snapshot["source_updated_at"]))
        )
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - value.astimezone(timezone.utc)
        ).total_seconds()
        return (
            age_seconds < -MAX_SOURCE_FUTURE_SKEW_SECONDS
            or age_seconds > self.poll_interval_seconds
        )

    @staticmethod
    def _snapshot_from_row(row: dict) -> MarketSnapshot:
        return MarketSnapshot(
            item_key=row["item_key"], smis_id=row["smis_id"], appid=row["appid"],
            name=row["name"], name_zh=row["name_zh"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source_updated_at=datetime.fromisoformat(row["source_updated_at"]),
            buff_sell_price=row["buff_sell_price"], buff_sell_num=row["buff_sell_num"],
            uuyp_sell_price=row.get("uuyp_sell_price"), uuyp_sell_num=row.get("uuyp_sell_num"),
            c5_sell_price=row.get("c5_sell_price"), c5_sell_num=row.get("c5_sell_num"),
            igxe_sell_price=row.get("igxe_sell_price"), igxe_sell_num=row.get("igxe_sell_num"),
            eco_sell_price=row.get("eco_sell_price"), eco_sell_num=row.get("eco_sell_num"),
            steam_sell_price=row["steam_sell_price"], steam_sell_num=row["steam_sell_num"],
            steam_transaction_quantity=row["steam_transaction_quantity"],
            buff_to_steam_ratio=row["buff_to_steam_ratio"], kind=row["kind"], source=row["source"],
        )

    @classmethod
    def _snapshot_row_to_dict(cls, row: dict | None) -> dict | None:
        if not row:
            return None
        snapshot = cls._snapshot_from_row(row)
        lowest = snapshot.lowest_platform
        return {
            "observed_at": snapshot.observed_at.isoformat(),
            "source_updated_at": snapshot.source_updated_at.isoformat(),
            "buff_sell_price": snapshot.buff_sell_price,
            "buff_sell_num": snapshot.buff_sell_num,
            "steam_sell_price": snapshot.steam_sell_price,
            "steam_sell_num": snapshot.steam_sell_num,
            "steam_net": snapshot.steam_net,
            "steam_transaction_quantity": snapshot.steam_transaction_quantity,
            "ratio": snapshot.calculated_ratio,
            "platforms": [
                {
                    "name": name,
                    "sell_price": price,
                    "sell_num": count,
                    "is_lowest": lowest is not None and price == lowest[1],
                }
                for name, price, count in snapshot.platform_quotes
            ],
            "lowest_platform": (
                {"name": lowest[0], "sell_price": lowest[1], "sell_num": lowest[2]}
                if lowest else None
            ),
        }

    def _quote_payload(self, item: dict, row: dict, *, cached: bool, stale: bool) -> dict:
        snapshot_object = self._snapshot_from_row(row)
        snapshot = self._snapshot_row_to_dict(row)
        stats = self._seven_day_history_stats(str(item["item_key"]))
        forecast, risk = self._forecast_and_risk(
            str(item["item_key"]), snapshot_object, stats, stale=stale
        )
        return {
            "smis_id": int(item["smis_id"]), "appid": int(item["appid"]),
            "name": item["hash_name"], "name_zh": item["cn_name"],
            **snapshot, **stats, "forecast": forecast, "risk_assessment": risk,
            "cached": cached, "stale": stale,
            "links": {
                "smis": f"https://smis.club/commodity/{int(item['smis_id'])}",
                "steam": f"https://steamcommunity.com/market/listings/{int(item['appid'])}/{quote(str(item['hash_name']))}",
            },
        }

    def _seven_day_history_stats(self, item_key: str) -> dict:
        return seven_day_history_stats(
            self.storage.steam_history(
                item_key,
                days=7,
                max_current_lag_seconds=self.poll_interval_seconds,
            )
        )

    def _forecast_and_risk(
        self, item_key: str, snapshot: MarketSnapshot, stats: dict, *, stale: bool = False,
    ) -> tuple[dict, dict]:
        rows = self.storage.market_history_rows(item_key, days=30)
        return forecast_and_risk(
            rows,
            snapshot,
            stats,
            stale=stale,
            max_current_lag_seconds=self.poll_interval_seconds,
        )

    def monitor_item(self, item_row: dict) -> dict:
        item = item_from_row(item_row)
        rules = self.storage.list_rules(
            smis_id=item["smis_id"],
            operational_only=True,
        )
        if not rules:
            return {"smis_id": item["smis_id"], "skipped": True}
        previous = self.storage.latest_snapshot(item["item_key"])
        bootstrap_status: bool | None = False
        try:
            try:
                bootstrap_status = self.history_backfiller.ensure_history(item)
            except Exception as exc:
                bootstrap_status = None
                logger.warning("T+7 历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
            with self._lock_for(item["smis_id"]):
                snapshot = self.source.fetch_current(item)
                if bootstrap_status is False:
                    try:
                        self.history_backfiller.backfill_changed_gap(
                            item,
                            previous,
                            snapshot,
                            rules,
                        )
                    except Exception as exc:
                        logger.warning("缺口历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
                self.storage.save_snapshots([snapshot])
        except Exception as exc:
            self._handle_item_failure(item, rules, exc)
            return {"smis_id": item["smis_id"], "success": False, "error": str(exc)}

        self._handle_item_recovery(item, rules, snapshot)
        stats = self._seven_day_history_stats(item["item_key"])
        stale = self._source_snapshot_stale(snapshot)
        forecast, risk = self._forecast_and_risk(
            item["item_key"],
            snapshot,
            stats,
            stale=stale,
        )
        unavailable_status = "行情数据已过期" if stale else None
        for rule in rules:
            self._evaluate_rule(
                snapshot,
                stats,
                forecast,
                risk,
                rule,
                unavailable_status=unavailable_status,
            )
        return {"smis_id": item["smis_id"], "success": True, "stale": stale}

    def _evaluate_rule(
        self, snapshot: MarketSnapshot, stats: dict, forecast: dict,
        risk: dict, rule: dict, *, unavailable_status: str | None = None,
    ) -> None:
        rule_id = int(rule["id"])
        rule_type = str(rule["rule_type"])
        definition = get_rule_definition(rule_type)
        status = self._rule_configuration_error(rule_type, rule["threshold"])
        if definition is None or status is not None:
            self._quarantine_rule(
                rule,
                status or f"不支持的规则类型：{rule_type}",
            )
            return
        state = self.storage.get_rule_state(rule_id)
        measurement = (
            RuleMeasurement(None, None, unavailable_status)
            if unavailable_status is not None
            else measure_rule(snapshot, forecast, rule_type)
        )
        observed = snapshot.observed_at.isoformat()
        transition = evaluate_rule_transition(
            rule_id=rule_id,
            definition=definition,
            threshold=float(rule["threshold"]),
            state=state,
            measurement=measurement,
            observed_at=observed,
            confirmations=self.confirmations,
            clear_confirmations=self.clear_confirmations,
            breakthrough_step=self.breakthrough_step,
        )
        if transition.changes.get("status") == "规则状态无效":
            self._quarantine_rule(rule, "规则状态无效")
            return
        if transition.signal is not None:
            signal = transition.signal
            if signal.style == "breakthrough":
                title, content = format_breakthrough_alert(
                    snapshot,
                    stats,
                    forecast,
                    risk,
                    rule,
                    signal.value,
                    signal.depth,
                    signal.level,
                )
            else:
                title, content = format_rule_alert(
                    snapshot, stats, forecast, risk, rule, signal.value
                )
            self.storage.enqueue_notification(
                signal.signal_key,
                str(rule["recipient_key"]),
                signal.event_type,
                title,
                content,
                driver=self.delivery_driver,
                rule_id=rule_id,
            )
        self.storage.update_rule_state(rule_id, **transition.changes)

    def enqueue_daily_summary(self, summary_date: date) -> int:
        date_text = summary_date.isoformat()
        key = "daily_summary:last_date"
        if self.storage.get_metadata(key) == date_text:
            return 0

        grouped: dict[str, list[tuple[dict, dict, int, float]]] = defaultdict(list)
        item_freshness: dict[str, bool] = {}
        for rule in self.storage.list_rules(operational_only=True):
            item_key = str(rule["item_key"])
            if item_key not in item_freshness:
                latest = self.storage.latest_snapshot(item_key)
                try:
                    item_freshness[item_key] = bool(
                        latest
                        and self._snapshot_age_seconds(latest)
                        <= self.poll_interval_seconds * 2
                        and not self._source_snapshot_stale(latest)
                    )
                except Exception as exc:
                    logger.warning(
                        "日报忽略损坏的行情缓存 item_key=%s: %s",
                        item_key,
                        exc,
                    )
                    item_freshness[item_key] = False
            if not item_freshness[item_key]:
                continue
            state = self.storage.get_rule_state(int(rule["id"]))
            value = state.get("last_value")
            if not _state_count(state.get("alert_active")) or value is None:
                continue
            rule_type = str(rule["rule_type"])
            definition = get_rule_definition(rule_type)
            status = self._rule_configuration_error(rule_type, rule["threshold"])
            if definition is None or status is not None:
                self._quarantine_rule(
                    rule,
                    status or f"不支持的规则类型：{rule_type}",
                )
                continue
            current_value = _positive_finite(value)
            if current_value is None:
                self._quarantine_rule(rule, "测量值无效")
                continue
            threshold = float(rule["threshold"])
            limit = definition.limit(threshold)
            if not definition.qualifies(current_value, limit):
                continue
            try:
                level, depth = definition.breakthrough(
                    current_value,
                    limit,
                    self.breakthrough_step,
                )
            except (ArithmeticError, TypeError, ValueError):
                self._quarantine_rule(rule, "测量值无效")
                continue
            grouped[str(rule["recipient_key"])].append((rule, state, level, depth))

        queued = 0
        analysis_cache: dict[str, tuple[dict, dict]] = {}
        for recipient_key, entries in sorted(grouped.items()):
            lines = [f"当前仍满足条件的交易告警共 {len(entries)} 条："]
            for rule, state, level, depth in entries:
                rule_type = str(rule["rule_type"])
                definition = get_rule_definition(rule_type)
                if definition is None:
                    continue
                value = _positive_finite(state["last_value"])
                if value is None:
                    continue
                threshold = float(rule["threshold"])
                if definition.percentage:
                    values = f"{value:.2%} / 阈值 {threshold:.2f}%"
                else:
                    values = f"¥{value:.2f} / 阈值 ¥{threshold:.2f}"
                item_key = str(rule["item_key"])
                if item_key not in analysis_cache:
                    latest = self.storage.latest_snapshot(item_key)
                    if latest:
                        snapshot = self._snapshot_from_row(latest)
                        stats = self._seven_day_history_stats(item_key)
                        stale = (
                            self._snapshot_age_seconds(latest)
                            > self.poll_interval_seconds * 2
                            or self._source_snapshot_stale(latest)
                        )
                        analysis_cache[item_key] = self._forecast_and_risk(
                            item_key, snapshot, stats, stale=stale
                        )
                    else:
                        unavailable = {"status": "unavailable"}
                        analysis_cache[item_key] = (unavailable, unavailable)
                forecast, risk = analysis_cache[item_key]
                analysis_suffix = daily_analysis_suffix(forecast, risk)
                lines.append(
                    f"#{int(rule['id'])} {rule['cn_name']} · {definition.daily_label} "
                    f"{values} · 第 {level} 档（{depth:.2%}）{analysis_suffix}"
                )
            queued += int(self.storage.enqueue_notification(
                f"daily-summary:{date_text}", recipient_key, "daily_summary",
                f"【每日交易告警汇总】{date_text}", "\n".join(lines),
                driver=self.delivery_driver,
            ))
        self.storage.set_metadata(key, date_text)
        return queued

    def _handle_item_failure(self, item: dict, rules: list[dict], exc: Exception) -> None:
        for recipient_key in sorted({str(rule["recipient_key"]) for rule in rules}):
            state = self.storage.get_health_state(item["smis_id"], recipient_key)
            failures = min(
                _state_count(state.get("fetch_failures")) + 1,
                MAX_RULE_STATE_INTEGER,
            )
            alerted = 1 if _state_count(state.get("health_alerted")) else 0
            if failures >= self.health_failure_threshold and not alerted:
                self.storage.enqueue_notification(
                    f"health:{item['item_key']}:{recipient_key}:down:{failures}",
                    recipient_key, "health_down",
                    f"【监控异常】{item['name_zh']}",
                    f"SMIS 行情连续 {failures} 轮请求失败：{exc}",
                    driver=self.delivery_driver,
                )
                alerted = 1
            self.storage.update_health_state(
                item["smis_id"], recipient_key,
                fetch_failures=failures, health_alerted=alerted,
            )

    def _handle_item_recovery(self, item: dict, rules: list[dict], snapshot: MarketSnapshot) -> None:
        for recipient_key in sorted({str(rule["recipient_key"]) for rule in rules}):
            state = self.storage.get_health_state(item["smis_id"], recipient_key)
            if _state_count(state.get("health_alerted")):
                self.storage.enqueue_notification(
                    f"health:{item['item_key']}:{recipient_key}:recovered:"
                    f"{snapshot.observed_at.isoformat()}",
                    recipient_key, "health_recovered", f"【监控恢复】{item['name_zh']}",
                    "SMIS 行情请求已恢复。",
                    driver=self.delivery_driver,
                )
            self.storage.update_health_state(
                item["smis_id"], recipient_key, fetch_failures=0, health_alerted=0
            )

    def run_cycle(self, max_workers: int = 4) -> list[dict]:
        active_ids = {
            int(rule["smis_id"])
            for rule in self.storage.list_rules(operational_only=True)
        }
        items = [
            item for item in self.storage.list_items(enabled_only=True)
            if int(item["smis_id"]) in active_ids
        ]
        results: list[dict] = []
        if items:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
                futures = [executor.submit(self.monitor_item, item) for item in items]
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        logger.exception("监控任务异常")
                        results.append({"success": False, "error": str(exc)})
        return results

    def test_event(self, recipient_key: str) -> dict:
        now = datetime.now().astimezone()
        self.storage.enqueue_notification(
            f"test:{recipient_key}:{now.isoformat()}", recipient_key, "test",
            "【Steam Skin Ops】告警测试",
            f"服务链路正常。\n测试时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            driver=self.delivery_driver,
        )
        return self.storage.list_events(recipient_key, limit=1)[0]
