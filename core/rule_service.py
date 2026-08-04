from __future__ import annotations

import logging

from .errors import RuleItemLimitError, ServiceError
from .history_backfill import HistoryBackfiller
from .integrations.smis import MarketDataProvider
from .items import item_from_row
from .repository import MonitorStorage
from .rules import validate_rule

logger = logging.getLogger(__name__)


class RuleService:
    """Manage recipient-scoped persisted rules and their bootstrap history."""

    def __init__(
        self,
        storage: MonitorStorage,
        source: MarketDataProvider,
        history_backfiller: HistoryBackfiller,
        *,
        max_items: int,
    ) -> None:
        self.storage = storage
        self.source = source
        self.history_backfiller = history_backfiller
        self.max_items = int(max_items)

    @staticmethod
    def _validate_rule(rule_type: str, threshold: float) -> None:
        error = validate_rule(rule_type, threshold)
        if error:
            raise ServiceError(error[0], error[1])

    def list_rules(
        self,
        recipient_key: str,
        smis_id: int | None = None,
    ) -> list[dict]:
        rows = self.storage.list_rules(
            recipient_key=recipient_key,
            smis_id=smis_id,
        )
        for row in rows:
            row["state"] = self.storage.get_rule_state(row["id"])
        return rows

    def add_rule(
        self,
        recipient_key: str,
        smis_id: int,
        rule_type: str,
        threshold: float,
    ) -> dict:
        rule_type = rule_type.strip().lower()
        self._validate_rule(rule_type, threshold)
        if (
            not self.storage.list_rules(smis_id=smis_id, operational_only=True)
            and self.storage.count_rule_items() >= self.max_items
        ):
            raise ServiceError(
                "item_limit",
                f"最多只能监控 {self.max_items} 个饰品",
            )
        item = self.storage.get_item(smis_id)
        if item is None:
            try:
                metadata = self.source.fetch_metadata(smis_id)
            except Exception as exc:
                raise ServiceError(
                    "source_unavailable",
                    f"SMIS 饰品信息获取失败：{exc}",
                ) from exc
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
                "item_limit",
                f"最多只能监控 {self.max_items} 个饰品",
            ) from exc
        try:
            self.history_backfiller.ensure_history(item_from_row(item))
        except Exception as exc:
            logger.warning("规则历史回填失败 smis_id=%s: %s", smis_id, exc)
        rule["state"] = self.storage.get_rule_state(rule["id"])
        return rule

    def update_rule(
        self,
        recipient_key: str,
        rule_id: int,
        threshold: float,
    ) -> dict:
        rule = self.storage.get_rule(rule_id)
        if not rule or str(rule["recipient_key"]) != recipient_key:
            raise ServiceError("rule_not_found", "当前会话未找到该规则")
        self._validate_rule(str(rule["rule_type"]), threshold)
        try:
            updated = self.storage.update_rule(
                rule_id,
                recipient_key,
                threshold,
                max_items=self.max_items,
            )
        except RuleItemLimitError as exc:
            raise ServiceError(
                "item_limit",
                f"最多只能监控 {self.max_items} 个饰品",
            ) from exc
        if updated is None:
            raise ServiceError("rule_not_found", "当前会话未找到该规则")
        return updated

    def remove_rule(self, recipient_key: str, rule_id: int) -> None:
        if not self.storage.delete_rule(rule_id, recipient_key):
            raise ServiceError("rule_not_found", "当前会话未找到该规则")
