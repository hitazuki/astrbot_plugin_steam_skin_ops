from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from hashlib import sha1

from .integrations.smis import MarketDataProvider
from .market import MarketSnapshot
from .repository import MonitorStorage
from .risk import has_usable_history_coverage, snapshot_fingerprint

logger = logging.getLogger(__name__)


class HistoryBackfiller:
    """Own bootstrap and observation-gap recovery for market history."""

    def __init__(
        self,
        storage: MonitorStorage,
        source: MarketDataProvider,
        *,
        poll_interval_seconds: int,
    ) -> None:
        self.storage = storage
        self.source = source
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))

    def ensure_history(self, item: dict) -> bool | None:
        rows = self.storage.market_history_rows(item["item_key"], days=30)
        if has_usable_history_coverage(
            rows,
            max_current_lag_seconds=self.poll_interval_seconds,
        ):
            return False
        key = f"history-bootstrap:{item['item_key']}:30"
        attempted = self.storage.get_metadata(key)
        if attempted and attempted != "done":
            try:
                if (
                    datetime.now(timezone.utc) - datetime.fromisoformat(attempted)
                    < timedelta(hours=6)
                ):
                    return None
            except ValueError:
                pass
        self.storage.set_metadata(key, datetime.now(timezone.utc).isoformat())
        snapshots = self.source.fetch_history(item, 30)
        self.storage.save_snapshots(snapshots)
        return True

    def _attempt_pending_gap(self, item: dict) -> bool:
        pending_key = f"history-gap-pending:{item['item_key']}"
        raw = self.storage.get_metadata(pending_key)
        if not raw:
            return False
        try:
            pending = json.loads(raw)
            attempted = pending.get("attempted_at")
            if attempted and (
                datetime.now(timezone.utc) - datetime.fromisoformat(attempted)
                < timedelta(hours=6)
            ):
                return True
            pending["attempted_at"] = datetime.now(timezone.utc).isoformat()
            self.storage.set_metadata(pending_key, json.dumps(pending))
            snapshots = self.source.fetch_history(item, int(pending["days"]))
            self.storage.save_snapshots(snapshots)
            self.storage.set_metadata(str(pending["done_key"]), "done")
            self.storage.set_metadata(pending_key, "")
            return False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("忽略无效的缺口回填状态 item_key=%s", item["item_key"])
            self.storage.set_metadata(pending_key, "")
            return False

    def backfill_changed_gap(
        self,
        item: dict,
        previous: dict | None,
        snapshot: MarketSnapshot,
        rules: list[dict],
    ) -> None:
        if self._attempt_pending_gap(item) or previous is None:
            return
        previous_observed = datetime.fromisoformat(str(previous["observed_at"]))
        gap_seconds = max(
            0.0,
            (snapshot.observed_at - previous_observed).total_seconds(),
        )
        had_failure = any(
            int(
                self.storage.get_health_state(
                    item["smis_id"], str(rule["recipient_key"])
                )["fetch_failures"]
            ) > 0
            for rule in rules
        )
        missed_observation = gap_seconds > self.poll_interval_seconds * 2
        if not had_failure and not missed_observation:
            return
        if snapshot_fingerprint(previous) == snapshot_fingerprint(snapshot):
            return
        days = max(1, min(30, math.ceil(gap_seconds / 86400)))
        signature = (
            f"{item['item_key']}|{previous['observed_at']}|"
            f"{snapshot.observed_at.isoformat()}|{days}"
        )
        digest = sha1(signature.encode("utf-8")).hexdigest()[:16]
        key = f"history-gap:{item['item_key']}:{digest}"
        if self.storage.get_metadata(key) == "done":
            return
        pending_key = f"history-gap-pending:{item['item_key']}"
        pending = {
            "done_key": key,
            "days": days,
            "gap_started_at": previous["observed_at"],
            "gap_ended_at": snapshot.observed_at.isoformat(),
            "attempted_at": None,
        }
        self.storage.set_metadata(pending_key, json.dumps(pending))
        self._attempt_pending_gap(item)
