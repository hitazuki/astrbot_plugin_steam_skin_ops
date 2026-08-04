from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from ..market import MarketSnapshot

logger = logging.getLogger(__name__)


class SmisClientError(RuntimeError):
    pass


class SmisRetryableError(SmisClientError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MarketDataProvider(Protocol):
    def search_items(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
    def fetch_metadata(self, smis_id: int) -> dict[str, Any]: ...
    def fetch_current(self, item: dict[str, Any]) -> MarketSnapshot: ...
    def fetch_history(self, item: dict[str, Any], days: int = 30) -> list[MarketSnapshot]: ...
    def fetch_exchange_candidates(
        self,
        *,
        min_price: float,
        max_price: float,
        min_transaction_quantity: int,
        max_ratio: float,
        max_age_minutes: int,
        limit: int = 50,
    ) -> list[MarketSnapshot]: ...


class SmisClient:
    BASE_URL = "https://smis.club/api"
    DEFAULT_AUTH_KEY = "CMDDTYDF&WY196KJ"
    DEFAULT_AUTH2 = "3d4b7647-283e-5f5b-8c1c-65ff8b166e97"
    HISTORY_KEYS = (
        "buffSellPrice",
        "buffSellNum",
        "steamSellPrice",
        "steamSellNum",
        "steamTransactionQuantity",
        "buffExchangeSteamBySell",
    )
    EXCHANGE_PLATFORM_SETTINGS = {
        "buffTax": 0.015,
        "buffLimit": 9_999_999,
        "uuypTax": 0.01,
        "uuypLimit": 9_999_999,
        "c5Tax": 0.01,
        "c5Limit": 100,
        "igxeTax": 0.006,
        "igxeLimit": 9_999_999,
        "ecoTax": 0,
        "ecoLimit": 9_999_999,
    }

    def __init__(
        self,
        timeout: float = 15,
        max_retries: int = 3,
        min_request_interval: float = 1.0,
        auth_key: str = DEFAULT_AUTH_KEY,
        auth2: str = DEFAULT_AUTH2,
        session: httpx.Client | None = None,
    ) -> None:
        if len(auth_key.encode("utf-8")) not in (16, 24, 32):
            raise ValueError("SMIS auth_key 必须是有效 AES key 长度")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        if min_request_interval <= 0:
            raise ValueError("SMIS 请求最小间隔必须大于 0")
        self.min_request_interval = float(min_request_interval)
        self.auth_key = auth_key
        self.auth2 = auth2
        self.session = session or httpx.Client(
            headers={
                "User-Agent": "astrbot-plugin-steam-skin-ops/1.0",
                "Origin": "https://smis.club",
                "Referer": "https://smis.club/exchange",
            }
        )
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        self._clock = time.monotonic
        self._sleep = time.sleep
        if session is not None and hasattr(self.session, "headers"):
            self.session.headers.update(
                {
                    "User-Agent": "astrbot-plugin-steam-skin-ops/1.0",
                    "Origin": "https://smis.club",
                    "Referer": "https://smis.club/exchange",
                }
            )

    def build_auth_headers(self, timestamp_ms: int | None = None) -> dict[str, str]:
        timestamp_ms = timestamp_ms or int(time.time() * 1000)
        key = self.auth_key.encode("utf-8")
        cipher = AES.new(key, AES.MODE_CBC, iv=key)
        encrypted = cipher.encrypt(pad(str(timestamp_ms).encode("utf-8"), AES.block_size))
        return {"Auth": base64.b64encode(encrypted).decode("ascii"), "Auth2": self.auth2}

    def _send(self, method: str, path: str, headers: dict[str, str], **kwargs: Any):
        with self._request_lock:
            delay = self._next_request_at - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next_request_at = self._clock() + self.min_request_interval
            return self.session.request(
                method,
                f"{self.BASE_URL}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        return min(max(seconds, 0.0), 60.0)

    def _check_http_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status in {401, 403}:
            raise SmisClientError(f"SMIS 拒绝访问（HTTP {status}）")
        if status == 429:
            raise SmisRetryableError(
                "SMIS 请求过于频繁（HTTP 429）",
                retry_after=self._retry_after_seconds(response),
            )
        if status in {408, 425} or 500 <= status < 600:
            raise SmisRetryableError(f"SMIS 暂时不可用（HTTP {status}）")
        raise SmisClientError(f"SMIS 请求不可接受（HTTP {status}）")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        base_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(self.max_retries):
            try:
                headers = dict(base_headers)
                headers.update(self.build_auth_headers())
                response = self._send(method, path, headers, **kwargs)
                self._check_http_status(response)
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SmisRetryableError("SMIS 返回了无效 JSON 结构")
                if payload.get("code") != 200:
                    code = payload.get("code")
                    message = f"SMIS 返回错误 code={code}: {payload.get('message')}"
                    if isinstance(code, int) and code >= 500:
                        raise SmisRetryableError(message)
                    raise SmisClientError(message)
                return payload.get("data")
            except SmisRetryableError as exc:
                last_error = exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
            except ValueError as exc:
                last_error = SmisRetryableError(f"SMIS 返回内容无法解析: {exc}")
            except httpx.HTTPError as exc:
                last_error = exc
            except SmisClientError:
                raise
            if attempt + 1 < self.max_retries:
                retry_after = last_error.retry_after if isinstance(last_error, SmisRetryableError) else None
                self._sleep(retry_after if retry_after is not None else min(2**attempt, 4))
        raise SmisClientError(f"SMIS 请求重试耗尽: {last_error}") from last_error

    @staticmethod
    def _smis_time(value: str) -> datetime:
        try:
            local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return local.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise SmisClientError(f"SMIS 更新时间无效: {value!r}") from exc

    def fetch_current(self, item: dict[str, Any]) -> MarketSnapshot:
        data = self._request("GET", f"/commodity/{int(item['smis_id'])}")
        if not isinstance(data, dict):
            raise SmisClientError("SMIS 商品详情结构无效")
        required = {
            "hashName",
            "updateTime",
            "buffSellPrice",
            "buffSellNum",
            "steamSellPrice",
            "steamSellNum",
            "steamTransactionQuantity",
        }
        missing = sorted(required - data.keys())
        if missing:
            raise SmisClientError(f"SMIS 商品详情缺少字段: {', '.join(missing)}")
        if str(data.get("hashName")) != str(item["name"]):
            raise SmisClientError(f"SMIS 商品不匹配: 期望 {item['name']}, 实际 {data.get('hashName')}")
        snapshot = MarketSnapshot(
            item_key=str(item["item_key"]),
            smis_id=int(item["smis_id"]),
            appid=int(item["appid"]),
            name=str(item["name"]),
            name_zh=str(item["name_zh"]),
            observed_at=datetime.now(timezone.utc),
            source_updated_at=self._smis_time(data.get("updateTime")),
            buff_sell_price=float(data.get("buffSellPrice") or 0),
            buff_sell_num=int(data.get("buffSellNum") or 0),
            uuyp_sell_price=float(data.get("uuypSellPrice") or 0),
            uuyp_sell_num=int(data.get("uuypSellNum") or 0),
            c5_sell_price=float(data.get("c5SellPrice") or 0),
            c5_sell_num=int(data.get("c5SellNum") or 0),
            igxe_sell_price=float(data.get("igxeSellPrice") or 0),
            igxe_sell_num=int(data.get("igxeSellNum") or 0),
            eco_sell_price=float(data.get("ecoSellPrice") or 0),
            eco_sell_num=int(data.get("ecoSellNum") or 0),
            steam_sell_price=float(data.get("steamSellPrice") or 0),
            steam_sell_num=int(data.get("steamSellNum") or 0),
            steam_transaction_quantity=int(data.get("steamTransactionQuantity") or 0),
            kind="current",
        )
        return snapshot

    def fetch_metadata(self, smis_id: int) -> dict[str, Any]:
        """Fetch and validate the stable fields needed to register an item."""
        data = self._request("GET", f"/commodity/{int(smis_id)}")
        if not isinstance(data, dict):
            raise SmisClientError("SMIS 商品详情结构无效")
        required = {"id", "appid", "hashName", "cnName"}
        missing = sorted(required - data.keys())
        if missing:
            raise SmisClientError(f"SMIS 商品详情缺少字段: {', '.join(missing)}")
        if int(data["id"]) != int(smis_id):
            raise SmisClientError(f"SMIS 商品 ID 不匹配: 期望 {smis_id}, 实际 {data.get('id')}")
        return {
            "smis_id": int(data["id"]),
            "item_key": f"smis:{int(data['id'])}",
            "appid": int(data["appid"]),
            "name": str(data["hashName"]),
            "name_zh": str(data["cnName"] or data["hashName"]),
        }

    def search_items(self, query: str, limit: int = 10, game: str = "csgo") -> list[dict[str, Any]]:
        """Search the SMIS catalog and return lightweight, display-safe candidates."""
        query = str(query).strip()
        if not query:
            return []
        data = self._request("POST", "/commodity/suggest", json={"game": game, "text": query})
        if not isinstance(data, list):
            raise SmisClientError("SMIS 搜索结果结构无效")
        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                smis_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            name_zh = str(row.get("value") or "").strip()
            if smis_id <= 0 or not name_zh or smis_id in seen:
                continue
            seen.add(smis_id)
            results.append(
                {
                    "smis_id": smis_id,
                    "name_zh": name_zh,
                    "rarity": str(row.get("rarity") or "").strip() or None,
                }
            )
            if len(results) >= max(1, min(int(limit), 20)):
                break
        return results

    def fetch_exchange_candidates(
        self,
        *,
        min_price: float,
        max_price: float,
        min_transaction_quantity: int,
        max_ratio: float,
        max_age_minutes: int,
        limit: int = 50,
    ) -> list[MarketSnapshot]:
        """Fetch the current SMIS platform-to-Steam exchange candidate page."""

        page_size = max(1, min(int(limit), 50))
        time_after = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=max(1, int(max_age_minutes)))
        payload = {
            "model": 0,
            "platforms": ["buff", "uuyp", "c5", "igxe", "eco"],
            "appid": None,
            "minPrice": float(min_price),
            "maxPrice": float(max_price),
            "minTransactionQuantity": int(min_transaction_quantity),
            "minRate": 0,
            "maxRate": float(max_ratio),
            "timeAfter": time_after.strftime("%Y-%m-%d %H:%M:%S"),
            "pageNum": 1,
            "pageSize": page_size,
            **self.EXCHANGE_PLATFORM_SETTINGS,
        }
        data = self._request("POST", "/commodity/exchange", json=payload)
        if not isinstance(data, list):
            raise SmisClientError("SMIS 挂刀候选结构无效")

        observed_at = datetime.now(timezone.utc)
        snapshots: list[MarketSnapshot] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                smis_id = int(row["id"])
                appid = int(row["appid"])
                name = str(row["hashName"]).strip()
                name_zh = str(row.get("cnName") or name).strip()
                source_updated_at = self._smis_time(row["updateTime"])
                if smis_id <= 0 or appid <= 0 or not name or not name_zh:
                    raise ValueError("identity")
                snapshot = MarketSnapshot(
                    item_key=f"smis:{smis_id}",
                    smis_id=smis_id,
                    appid=appid,
                    name=name,
                    name_zh=name_zh,
                    observed_at=observed_at,
                    source_updated_at=source_updated_at,
                    buff_sell_price=float(row.get("buffSellPrice") or 0),
                    buff_sell_num=int(row.get("buffSellNum") or 0),
                    uuyp_sell_price=float(row.get("uuypSellPrice") or 0),
                    uuyp_sell_num=int(row.get("uuypSellNum") or 0),
                    c5_sell_price=float(row.get("c5SellPrice") or 0),
                    c5_sell_num=int(row.get("c5SellNum") or 0),
                    igxe_sell_price=float(row.get("igxeSellPrice") or 0),
                    igxe_sell_num=int(row.get("igxeSellNum") or 0),
                    eco_sell_price=float(row.get("ecoSellPrice") or 0),
                    eco_sell_num=int(row.get("ecoSellNum") or 0),
                    steam_sell_price=float(row.get("steamSellPrice") or 0),
                    steam_sell_num=int(row.get("steamSellNum") or 0),
                    steam_transaction_quantity=int(row.get("steamTransactionQuantity") or 0),
                    kind="current",
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("忽略无效的 SMIS 挂刀候选：%s", exc)
                continue
            snapshots.append(snapshot)
        return snapshots

    def fetch_history(self, item: dict[str, Any], days: int = 30) -> list[MarketSnapshot]:
        data = self._request(
            "POST",
            "/commodity/history/line",
            json={
                "commodityId": int(item["smis_id"]),
                "days": int(days),
                "keys": list(self.HISTORY_KEYS),
            },
        )
        expected = len(self.HISTORY_KEYS) + 1
        if not isinstance(data, list) or len(data) != expected or not data[0]:
            raise SmisClientError("SMIS 历史数据结构无效")
        row_count = len(data[0])
        if any(not isinstance(series, list) or len(series) != row_count for series in data):
            raise SmisClientError("SMIS 历史数据序列长度不一致")
        snapshots: list[MarketSnapshot] = []
        for row in zip(*data):
            observed = datetime.fromtimestamp(float(row[0]) / 1000, timezone.utc)
            snapshots.append(
                MarketSnapshot(
                    item_key=str(item["item_key"]),
                    smis_id=int(item["smis_id"]),
                    appid=int(item["appid"]),
                    name=str(item["name"]),
                    name_zh=str(item["name_zh"]),
                    observed_at=observed,
                    source_updated_at=observed,
                    buff_sell_price=float(row[1] or 0),
                    buff_sell_num=int(row[2] or 0),
                    steam_sell_price=float(row[3] or 0),
                    steam_sell_num=int(row[4] or 0),
                    steam_transaction_quantity=int(row[5] or 0),
                    buff_to_steam_ratio=float(row[6]) if row[6] is not None else None,
                    kind="history",
                )
            )
        return snapshots
