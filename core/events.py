from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotifyResult:
    success: bool
    message: str


class AlertDriver(Protocol):
    name: str

    def send_to(self, recipient_key: str, title: str, content: str) -> NotifyResult: ...


class StoreAlertDriver:
    """No-op delivery driver used when clients consume persisted events via API."""

    name = "store"

    def send_to(self, recipient_key: str, title: str, content: str) -> NotifyResult:
        logger.info("[事件已存储] recipient=%s title=%s", recipient_key, title)
        return NotifyResult(True, "stored")
