"""Hand-authored fiction only; this module has no game or model dependencies."""

import threading

from .model import Advice, OverlayInput, OverlayStore


SCENARIOS = ("valid", "expiring", "expired", "unknown", "conflict", "disconnected", "invalidated")


class DemoController:
    def __init__(self, store: OverlayStore | None = None) -> None:
        self.store = store if store is not None else OverlayStore()
        self._lock = threading.Lock()
        self._source_revision = 0
        self._context_epoch = 0
        self._play_started: float | None = None
        self._cue = -1

    def _issue(self, scenario: str, *, duration_ms: float = 12_000) -> None:
        self._source_revision += 1
        self._context_epoch += 1
        now = self.store.now()
        advice = None
        status = scenario
        if scenario in {"valid", "expiring"}:
            status = "valid"
            duration = 2_500 if scenario == "expiring" else duration_ms
            advice = Advice(
                advice_id=f"paper-fixture-{self._source_revision}",
                target="第 2 格 · 纸片小方",
                action="可考虑保留",
                reason="虚构条件：已有同名纸片，留作配对。",
                created_ms=now,
                expires_ms=now + duration,
            )
        self.store.publish(OverlayInput(
            sample_kind="synthetic",
            host_clock_id=self.store.host_clock_id,
            stream_id=self.store.stream_id,
            source_revision=self._source_revision,
            context_epoch=self._context_epoch,
            status=status,
            advice=advice,
        ))

    def choose(self, scenario: str) -> dict:
        if scenario not in {*SCENARIOS, "play"}:
            raise ValueError("unknown demonstration")
        with self._lock:
            self._play_started = None
            if scenario == "play":
                self._play_started = self.store.now()
                self._cue = 0
                self._issue("valid", duration_ms=8_000)
            else:
                self._issue(scenario)
            return self._view()

    def _view(self) -> dict:
        view = self.store.view()
        view["playing"] = self._play_started is not None
        return view

    def view(self) -> dict:
        with self._lock:
            if self._play_started is not None:
                elapsed = self.store.now() - self._play_started
                cues = ((0, "valid"), (10_000, "unknown"), (13_000, "conflict"), (16_000, "disconnected"))
                cue = max(index for index, (offset, _) in enumerate(cues) if elapsed >= offset)
                if cue > self._cue:
                    self._cue = cue
                    self._issue(cues[cue][1])
                if cue == len(cues) - 1:
                    self._play_started = None
            return self._view()
