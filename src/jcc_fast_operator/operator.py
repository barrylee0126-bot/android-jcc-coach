"""Gate a single coordinate action behind caller-supplied frame evidence.

The package deliberately contains no screen capture loop, device discovery,
strategy, queue, retry loop, or implicit subprocess call.  A caller injects a
command runner only after it has selected the project default-device wrapper.
Consequently, a successful return means only that the injected runner accepted
one command; a separate, newer frame must establish whether the game changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Callable, Literal, Sequence


# This wrapper loads ``设备配置.env`` and exports its configured ANDROID_SERIAL.
# The serial is intentionally never accepted, stored, or printed by this module.
DEFAULT_ADB_WRAPPER = Path(__file__).resolve().parents[2] / "tools" / "adb"

_PHASE_PREPARATION = "preparation"
_SCENE_IN_GAME = "in_game"
_ORIENTATIONS = {"landscape", "portrait", "square"}
_OUTCOMES = {"confirmed", "rejected", "unknown", "withdrawn"}


def _plain_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class FrameEvidence:
    """Caller-reviewed properties of one decoded frame.

    ``freshness`` and scene/phase are assertions supplied by the upstream
    observer.  This class cannot verify pixels, frame binding, or game rules.
    """

    frame_id: str
    received_monotonic_ns: int
    context_epoch: int
    width_px: int
    height_px: int
    orientation: str | None
    applied_rotation_degrees: int | None
    scene: str | None
    phase: str | None
    source_kind: str

    def __post_init__(self) -> None:
        _plain_text(self.frame_id, "frame_id")
        _nonnegative_int(self.received_monotonic_ns, "received_monotonic_ns")
        _nonnegative_int(self.context_epoch, "context_epoch")
        if type(self.width_px) is not int or self.width_px <= 0:
            raise ValueError("width_px must be a positive integer")
        if type(self.height_px) is not int or self.height_px <= 0:
            raise ValueError("height_px must be a positive integer")
        expected_orientation = ("landscape" if self.width_px > self.height_px else
                                "portrait" if self.width_px < self.height_px else "square")
        if self.orientation != expected_orientation:
            raise ValueError("orientation must match decoded width and height")
        if (type(self.applied_rotation_degrees) is not int
                or self.applied_rotation_degrees not in (0, 90, 180, 270)):
            raise ValueError("applied_rotation_degrees must be an explicit right angle")
        _plain_text(self.source_kind, "source_kind")


@dataclass(frozen=True)
class CoordinateCalibration:
    """A reviewed mapping for a particular decoded geometry/context epoch."""

    calibration_id: str
    context_epoch: int
    width_px: int
    height_px: int
    orientation: str
    applied_rotation_degrees: int
    reviewed: bool

    def __post_init__(self) -> None:
        _plain_text(self.calibration_id, "calibration_id")
        _nonnegative_int(self.context_epoch, "context_epoch")
        if type(self.width_px) is not int or self.width_px <= 0:
            raise ValueError("width_px must be a positive integer")
        if type(self.height_px) is not int or self.height_px <= 0:
            raise ValueError("height_px must be a positive integer")
        if self.orientation not in _ORIENTATIONS:
            raise ValueError("orientation must be a known decoded orientation")
        if (type(self.applied_rotation_degrees) is not int
                or self.applied_rotation_degrees not in (0, 90, 180, 270)):
            raise ValueError("applied_rotation_degrees must be an explicit right angle")
        if type(self.reviewed) is not bool:
            raise ValueError("reviewed must be a boolean")


@dataclass(frozen=True)
class Action:
    """One immediate touch request; this module never stores a future queue."""

    action_id: str
    located_frame_id: str
    kind: Literal["tap", "swipe"]
    x1: int
    y1: int
    x2: int | None = None
    y2: int | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        _plain_text(self.action_id, "action_id")
        _plain_text(self.located_frame_id, "located_frame_id")
        if self.kind not in ("tap", "swipe"):
            raise ValueError("kind must be tap or swipe")
        for name in ("x1", "y1"):
            _nonnegative_int(getattr(self, name), name)
        if self.kind == "tap":
            if self.x2 is not None or self.y2 is not None or self.duration_ms is not None:
                raise ValueError("tap accepts only x1 and y1")
        else:
            for name in ("x2", "y2", "duration_ms"):
                _nonnegative_int(getattr(self, name), name)
            if self.duration_ms == 0:
                raise ValueError("swipe duration_ms must be positive")

    def points(self) -> tuple[tuple[int, int], ...]:
        if self.kind == "tap":
            return ((self.x1, self.y1),)
        assert self.x2 is not None and self.y2 is not None
        return ((self.x1, self.y1), (self.x2, self.y2))


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class DispatchReceipt:
    action_id: str
    source_frame_id: str
    calibration_id: str
    dispatched_monotonic_ns: int | None
    runner_returned_monotonic_ns: int | None
    elapsed_ns: int | None
    command: tuple[str, ...] | None
    status: Literal["runner_returned", "blocked", "delivery_unknown", "busy"]
    reason: str


@dataclass(frozen=True)
class ActionOutcome:
    """A caller-supplied readback outcome, never proof that a game action won."""

    receipt: DispatchReceipt
    readback_frame_id: str | None
    readback_monotonic_ns: int | None
    outcome: Literal["confirmed", "rejected", "unknown", "withdrawn"]
    reason: str


CommandRunner = Callable[[Sequence[str]], None]


class FastOperator:
    """Fast, one-at-a-time dispatch with no actual device dependency by default."""

    def __init__(self, *, max_frame_age_ms: int = 750,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if type(max_frame_age_ms) is not int or max_frame_age_ms <= 0:
            raise ValueError("max_frame_age_ms must be a positive integer")
        self._max_age_ns = max_frame_age_ms * 1_000_000
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._used_action_ids: set[str] = set()
        self._consumed_frame_ids: set[str] = set()
        self._attempts: dict[str, DispatchReceipt] = {}
        self._minimum_next_frame_received_ns: int | None = None

    def evaluate(self, action: Action, evidence: FrameEvidence,
                 calibration: CoordinateCalibration, *, now_ns: int | None = None) -> GateDecision:
        """Return a reason before any runner is invoked."""
        if now_ns is None:
            now_ns = self._clock_ns()
        _nonnegative_int(now_ns, "now_ns")
        age_ns = now_ns - evidence.received_monotonic_ns
        if age_ns < 0:
            return GateDecision(False, "frame_time_in_future")
        if age_ns > self._max_age_ns:
            return GateDecision(False, "frame_stale")
        if evidence.scene != _SCENE_IN_GAME:
            return GateDecision(False, "scene_unknown_or_not_in_game")
        if evidence.phase != _PHASE_PREPARATION:
            return GateDecision(False, "phase_unknown_or_not_preparation")
        if evidence.source_kind != "live":
            return GateDecision(False, "source_not_live")
        if action.located_frame_id != evidence.frame_id:
            return GateDecision(False, "action_not_relocated_on_current_frame")
        if (self._minimum_next_frame_received_ns is not None
                and evidence.received_monotonic_ns <= self._minimum_next_frame_received_ns):
            return GateDecision(False, "frame_not_captured_after_prior_action")
        if not calibration.reviewed:
            return GateDecision(False, "calibration_not_reviewed")
        if (calibration.context_epoch != evidence.context_epoch
                or calibration.width_px != evidence.width_px
                or calibration.height_px != evidence.height_px
                or calibration.orientation != evidence.orientation
                or calibration.applied_rotation_degrees != evidence.applied_rotation_degrees):
            return GateDecision(False, "calibration_geometry_mismatch")
        for x, y in action.points():
            if x >= evidence.width_px or y >= evidence.height_px:
                return GateDecision(False, "coordinate_outside_calibrated_frame")
        return GateDecision(True, "ready")

    @staticmethod
    def adb_command(action: Action, *, adb_wrapper: Path = DEFAULT_ADB_WRAPPER) -> tuple[str, ...]:
        """Build a wrapper command without a serial or an implicit invocation."""
        path = Path(adb_wrapper)
        if path != DEFAULT_ADB_WRAPPER:
            raise ValueError("adb_wrapper must be the project default-device tools/adb path")
        base = (str(path), "shell", "input")
        if action.kind == "tap":
            return base + ("tap", str(action.x1), str(action.y1))
        assert action.x2 is not None and action.y2 is not None and action.duration_ms is not None
        return base + ("swipe", str(action.x1), str(action.y1), str(action.x2), str(action.y2),
                       str(action.duration_ms))

    def dispatch(self, action: Action, evidence: FrameEvidence, calibration: CoordinateCalibration,
                 runner: CommandRunner) -> DispatchReceipt:
        """Attempt one command under a non-blocking instance lock.

        A runner invocation is treated as delivery-unknown on *any* exception,
        including ``KeyboardInterrupt``: the source frame and action ID remain
        consumed before the exception is re-raised.  A caller can inspect it
        with :meth:`attempt_for` before deciding to obtain a newer frame.
        """
        if not callable(runner):
            raise ValueError("runner must be callable")
        if not self._lock.acquire(blocking=False):
            return DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                   None, None, None, None, "busy", "action_already_dispatching")
        try:
            if action.action_id in self._used_action_ids:
                return DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                       None, None, None, None, "blocked", "action_id_already_consumed")
            if evidence.frame_id in self._consumed_frame_ids:
                return DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                       None, None, None, None, "blocked", "source_frame_already_consumed")
            # The only freshness decision that can lead to input is made while
            # the lock is held, immediately before runner invocation.  There is
            # no caller-provided time override on this mutating method.
            decision = self.evaluate(action, evidence, calibration, now_ns=self._clock_ns())
            if not decision.allowed:
                return DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                       None, None, None, None, "blocked", decision.reason)
            command = self.adb_command(action)
            # A planning-time check cannot authorize input if the frame became
            # stale while the instance lock was acquired.  Use the final clock
            # sample immediately before the runner call as the gate.
            started = self._clock_ns()
            decision = self.evaluate(action, evidence, calibration, now_ns=started)
            if not decision.allowed:
                return DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                       None, None, None, None, "blocked", decision.reason)
            self._used_action_ids.add(action.action_id)
            self._consumed_frame_ids.add(evidence.frame_id)
            try:
                runner(command)
            except BaseException:
                ended = self._clock_ns()
                self._minimum_next_frame_received_ns = ended
                receipt = DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                          started, ended, max(0, ended - started), command, "delivery_unknown",
                                          "runner_raised_after_invocation")
                self._attempts[action.action_id] = receipt
                raise
            ended = self._clock_ns()
            self._minimum_next_frame_received_ns = ended
            receipt = DispatchReceipt(action.action_id, evidence.frame_id, calibration.calibration_id,
                                      started, ended, max(0, ended - started), command, "runner_returned",
                                      "awaiting_newer_readback")
            self._attempts[action.action_id] = receipt
            return receipt
        finally:
            self._lock.release()

    def attempt_for(self, action_id: str) -> DispatchReceipt | None:
        """Return the conservative receipt for an already-consumed action ID."""
        _plain_text(action_id, "action_id")
        with self._lock:
            return self._attempts.get(action_id)

    def record_outcome(self, receipt: DispatchReceipt, *, readback_frame_id: str | None,
                       readback_monotonic_ns: int | None, outcome: str, reason: str) -> ActionOutcome:
        """Record a readback label for this instance's own receipt.

        Receipt identity is intentionally checked instead of dataclass equality,
        so a caller cannot manufacture an apparently runner-returned receipt.
        Pixel/readback truth remains a caller assertion.
        """
        with self._lock:
            if self._attempts.get(receipt.action_id) is not receipt:
                raise ValueError("receipt was not dispatched by this FastOperator instance")
        if outcome not in _OUTCOMES:
            raise ValueError("outcome must be confirmed, rejected, unknown, or withdrawn")
        _plain_text(reason, "reason")
        if readback_frame_id is not None:
            _plain_text(readback_frame_id, "readback_frame_id")
        if readback_monotonic_ns is not None:
            _nonnegative_int(readback_monotonic_ns, "readback_monotonic_ns")
        if outcome == "confirmed":
            if receipt.status != "runner_returned":
                raise ValueError("only a runner-returned action can be marked confirmed")
            if readback_frame_id is None or readback_frame_id == receipt.source_frame_id:
                raise ValueError("confirmed needs a different newer readback frame")
            if readback_monotonic_ns is None or receipt.dispatched_monotonic_ns is None:
                raise ValueError("confirmed needs a timed newer readback")
        if receipt.runner_returned_monotonic_ns is not None and readback_monotonic_ns is not None:
            if readback_monotonic_ns <= receipt.runner_returned_monotonic_ns:
                raise ValueError("readback must be newer than runner return")
        return ActionOutcome(receipt, readback_frame_id, readback_monotonic_ns, outcome, reason)
