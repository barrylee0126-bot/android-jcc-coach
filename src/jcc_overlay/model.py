"""A fail-closed presentation boundary for explicitly synthetic messages.

This is a presentation fixture, not a game-state or strategy validator. All times
belong to this store's process-local monotonic clock. No real inputs are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable
from uuid import uuid4


COPY = {
    "unknown": ("信息不足", "信息不足，暂不建议", "没有足够的演示信息，等待新提示。"),
    "conflict": ("信息冲突", "信息不一致，暂不建议", "两份演示信息不一致，旧提示已撤下。"),
    "disconnected": ("已断连", "连接中断，提示已撤下", "演示输入已中断，等待重新开始。"),
    "invalidated": ("已失效", "画面变化，提示已撤下", "演示内容已变化，需要重新判断。"),
    "expired": ("已失效", "提示已到期", "旧提示已撤下，等待新的演示提示。"),
}


def monotonic_ms() -> float:
    return time.monotonic_ns() / 1_000_000


def _number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value.strip()) <= maximum
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


@dataclass(frozen=True)
class Advice:
    advice_id: str
    target: str
    action: str
    reason: str
    created_ms: float
    expires_ms: float


@dataclass(frozen=True)
class OverlayInput:
    sample_kind: str
    host_clock_id: str
    stream_id: str
    source_revision: int
    context_epoch: int
    status: str
    advice: Advice | None = None


class OverlayStore:
    """Single synthetic stream, atomic updates, fixed deadlines, no resurrection.

    Invalid input withdraws the previous hint. A caller must publish a newer
    source revision after a barrier; republishing an old hint cannot renew it.
    A clock fault permanently closes the store until a new instance is created.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic_ms) -> None:
        self.host_clock_id = "overlay-clock-" + uuid4().hex
        self.stream_id = "overlay-synthetic-" + uuid4().hex
        self.session_id = uuid4().hex
        self._clock = clock
        self._lock = threading.RLock()
        self._last_now: float | None = None
        self._clock_fault = False
        self._source_revision = 0
        self._context_epoch = 0
        self._revision = 0
        self._status = "unknown"
        self._advice: Advice | None = None
        self._retired: set[str] = set()

    def now(self) -> float:
        with self._lock:
            tick = self._read_clock()
            if tick is None:
                raise ValueError("overlay clock is unavailable")
            return tick

    def _read_clock(self) -> float | None:
        try:
            tick = self._clock()
        except Exception:
            tick = None
        if (
            self._clock_fault
            or not _number(tick)
            or tick < 0
            or (self._last_now is not None and tick < self._last_now)
        ):
            self._clock_fault = True
            self._withdraw("unknown")
            return None
        self._last_now = float(tick)
        return float(tick)

    def _withdraw(self, status: str) -> None:
        if self._advice is not None:
            self._retired.add(self._advice.advice_id)
        if self._advice is not None or self._status != status:
            self._revision += 1
        self._advice = None
        self._status = status

    def _well_formed(self, message: OverlayInput, now: float) -> bool:
        if (
            type(message) is not OverlayInput
            or message.sample_kind != "synthetic"
            or message.host_clock_id != self.host_clock_id
            or message.stream_id != self.stream_id
            or not _integer(message.source_revision, 1)
            or message.source_revision <= self._source_revision
            or not _integer(message.context_epoch)
            or message.context_epoch < self._context_epoch
            or message.status not in {"valid", *COPY}
        ):
            return False
        if message.status != "valid":
            return message.advice is None
        advice = message.advice
        return (
            type(advice) is Advice
            and _text(advice.advice_id, 80)
            and advice.advice_id not in self._retired
            and (self._advice is None or advice.advice_id != self._advice.advice_id)
            and _text(advice.target, 28)
            and _text(advice.action, 20)
            and _text(advice.reason, 60)
            and _number(advice.created_ms)
            and _number(advice.expires_ms)
            and 0 <= advice.created_ms <= now
            and 0 < advice.expires_ms - advice.created_ms <= 30_000
        )

    def publish(self, message: OverlayInput) -> bool:
        with self._lock:
            now = self._read_clock()
            try:
                valid = now is not None and self._well_formed(message, now)
            except (TypeError, ValueError, AttributeError):
                valid = False
            if not valid:
                self._withdraw("unknown")
                return False
            self._withdraw(message.status if message.status != "valid" else "unknown")
            self._source_revision = message.source_revision
            self._context_epoch = message.context_epoch
            self._revision += 1
            self._status = message.status
            self._advice = message.advice
            if self._advice is not None and now >= self._advice.expires_ms:
                self._withdraw("expired")
            return True

    def view(self) -> dict:
        with self._lock:
            now = self._read_clock()
            advice = self._advice
            if advice is not None and now is not None and now >= advice.expires_ms:
                self._withdraw("expired")
                advice = None
            remaining = max(0.0, advice.expires_ms - now) if advice is not None else 0.0
            status = "expiring" if advice is not None and remaining <= 3_000 else self._status
            label, headline, detail = COPY.get(status, ("演示有效", "", ""))
            if status == "expiring":
                label = "即将到期"
            return {
                "schema_version": "overlay-demo-v1",
                "session_id": self.session_id,
                "sample_kind": "synthetic",
                "simulation": True,
                "execution_eligible": False,
                "revision": self._revision,
                "status": status,
                "label": label,
                "headline": headline,
                "detail": detail,
                "remaining_ms": remaining,
                "lease_ms": min(1_200.0, remaining),
                "advice": None if advice is None else {
                    "id": advice.advice_id,
                    "target": advice.target,
                    "action": advice.action,
                    "reason": advice.reason,
                },
            }
