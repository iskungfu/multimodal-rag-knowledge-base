"""复盘索引内部的租约与重试规则，写入和状态展示使用同一判断。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LEASE = timedelta(seconds=180)
COOLDOWN = timedelta(seconds=30)
ATTEMPT_WINDOW = timedelta(minutes=10)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class IndexState:
    status: str
    retry_after: datetime | None
    can_retry: bool
    attempts: int
    window: datetime


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def index_state(document, now):
    deadline = utc(document.get("index_lease_until"))
    leased = deadline is not None and deadline > now
    status = document.get("index_status", "pending")
    if status == "processing" and not leased:
        status = "interrupted"
    window = utc(document.get("index_window"))
    reset = window is None or window <= now - ATTEMPT_WINDOW
    attempts = 0 if reset else document.get("index_attempts", 0)
    window = now if reset else window
    retry = utc(document.get("index_retry_after"))
    if attempts >= MAX_ATTEMPTS:
        retry = max(retry or now, window + ATTEMPT_WINDOW)
    eligible = status not in {"completed", "processing"} and not leased and (retry is None or retry <= now)
    return IndexState(status, retry, eligible, attempts, window)
