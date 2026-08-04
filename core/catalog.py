from __future__ import annotations

import logging

from .errors import ServiceError
from .integrations.smis import MarketDataProvider
from .repository import MonitorStorage

logger = logging.getLogger(__name__)


class CatalogService:
    """Resolve command input against local metadata and the SMIS catalog."""

    def __init__(self, storage: MonitorStorage, source: MarketDataProvider) -> None:
        self.storage = storage
        self.source = source

    def search_items(self, query: str, limit: int = 10) -> list[dict]:
        try:
            return self.source.search_items(query, limit=limit)
        except Exception as exc:
            logger.warning("SMIS 搜索失败：%s", exc)
            raise ServiceError(
                "smis_search_failed",
                "SMIS 搜索暂时不可用",
            ) from exc

    def _resolve_local_item(self, query: str) -> dict | None:
        matches = self.storage.resolve_items(query)
        if not matches:
            return None
        if query.isdigit():
            return matches[0]
        exact = [
            row
            for row in matches
            if query.casefold()
            in {
                str(row["hash_name"]).casefold(),
                str(row["cn_name"]).casefold(),
            }
        ]
        return exact[0] if len(exact) == 1 else None

    def _market_candidates(self, query: str) -> list[dict]:
        matches = self.search_items(query, limit=10)
        exact = [row for row in matches if str(row.get("name_zh") or "").casefold() == query.casefold()]
        return exact if exact else matches

    def resolve_smis_id(self, query: str) -> int:
        target = query.strip()
        if target.isdigit():
            return int(target)
        candidates = self._market_candidates(target)
        if not candidates:
            raise ServiceError(
                "item_not_found",
                "SMIS 未找到匹配饰品，请先使用 /skin search。",
            )
        if len(candidates) > 1:
            raise ServiceError(
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
                        "item_not_found",
                        "SMIS 全市场未找到匹配饰品",
                    )
                if len(candidates) > 1:
                    raise ServiceError(
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
                "source_unavailable",
                f"SMIS 饰品信息获取失败：{exc}",
            ) from exc
        return {
            "smis_id": int(metadata["smis_id"]),
            "item_key": str(metadata["item_key"]),
            "appid": int(metadata["appid"]),
            "hash_name": str(metadata["name"]),
            "cn_name": str(metadata["name_zh"]),
        }

    def resolve_quote_item(self, query: str) -> dict:
        target = query.strip()
        local = self._resolve_local_item(target)
        return local if local is not None else self._resolve_market_item(target)
