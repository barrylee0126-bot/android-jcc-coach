"""Read-only latest-frame -> game-state-v1 integration.

Only a deliberately narrow, opt-in history-page experiment exists. No combat
numbers, shop identities, current mode or patch are inferred from OCR text.
Images and OCR strings stay inside the recognizer; snapshots contain neither.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import io
import json
import math
import threading
import time
from typing import Callable
from uuid import uuid4

from .game_state_contract import CORE_FIELDS, iter_observations, make_validator, validate_state
from .latest import Frame, FrameRef, LatestFrameStream
from .recognition import FrameProcessor, FrameResult


HOST_CLOCK_ID = "python-monotonic-" + uuid4().hex  # New domain identity per process.
METHOD = {"kind": "ocr", "name": "experimental-history-layout", "version": "v1"}
UNAVAILABLE = {"kind": "unavailable", "name": "state-pipeline", "version": "v1"}
NON_BATTLE = ("phase", "round", "hp", "gold", "level")


class FrameAdmissionRejected(ValueError):
    """A stale/foreign frame is expected at a latest-mailbox boundary."""


def image_metadata(frame: Frame) -> dict:
    """Read actual decoded dimensions; no rotation, viewport or ROI is guessed."""
    from PIL import Image

    with Image.open(io.BytesIO(frame.jpeg)) as image:
        image.load()
        width, height = image.size
    return {"width_px": width, "height_px": height,
            "orientation": "landscape" if width > height else "portrait" if width < height else "square",
            "rotation_applied_degrees": 0,
            "coordinate_space": "decoded_frame_normalized_top_left",
            "game_viewport": None, "calibration_id": None}


@dataclass(frozen=True)
class FrameContext:
    source: FrameRef
    host_clock_id: str
    context_epoch: int
    image_size: tuple[int, int]


@dataclass(frozen=True)
class SceneEvidence:
    """Sanitized recognizer output. Evidence codes are fixed, never OCR strings."""

    scene: str | None = None
    reason: str = "scene_recognizer_disabled"
    region: dict | None = field(default=None, repr=False)
    evidence_codes: tuple[str, ...] = ()
    ocr_items_count: int = 0


def _box(item: dict, width: int, height: int) -> dict | None:
    try:
        x, y, w, h = (item[key] for key in ("x", "y", "width", "height"))
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or
               not math.isfinite(n) for n in (x, y, w, h)):
            return None
        if not 0 <= x < x + w <= width or not 0 <= y < y + h <= height:
            return None
    except (KeyError, TypeError):
        return None
    return {"x_min": x / width, "y_min": y / height,
            "x_max": (x + w) / width, "y_max": (y + h) / height}


def history_evidence(items: list[dict], image: dict) -> SceneEvidence:
    """Experimental exact headings plus separated supporting label, landscape only.

    A header must be in the upper half; a different supporting label must be
    below it. Ambiguous/multiple headers and known dialog labels cause abstention.
    These positions and phrases are heuristics, not a calibrated game layout.
    """
    if image["orientation"] != "landscape":
        return SceneEvidence(reason="layout_unsupported", ocr_items_count=len(items))
    titles = {"对局记录", "历史战绩", "历史对局"}
    supports = {"最近对局", "对局详情", "对局时间", "对局模式", "战绩详情"}
    blockers = {"确认", "取消", "确定", "重新连接", "登录"}
    headers, anchors, blocked = [], [], False
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        text = "".join(item["text"].split())
        region = _box(item, image["width_px"], image["height_px"])
        if region is None:
            continue
        if text in blockers:
            blocked = True
        if text in titles and region["y_max"] <= 0.5:
            headers.append(region)
        if text in supports:
            anchors.append(region)
    codes = []
    if len(headers) == 1:
        codes.append("history_header_upper_half")
        if any(anchor["y_min"] > headers[0]["y_max"] for anchor in anchors):
            codes.append("history_support_below_header")
    elif len(headers) > 1:
        codes.append("multiple_history_headers")
    if blocked:
        codes.append("dialog_label_present")
    if codes == ["history_header_upper_half", "history_support_below_header"]:
        return SceneEvidence("history", "experimental_history_cues", headers[0],
                             tuple(codes), len(items))
    return SceneEvidence(reason="history_evidence_insufficient", evidence_codes=tuple(codes),
                         ocr_items_count=len(items))


class SceneRecognizer:
    """Callable for FrameProcessor; raw OCR results are never retained or returned."""

    def __init__(self, ocr=None, *, experimental_history: bool = False):
        self.ocr = ocr
        self.experimental_history = experimental_history

    def __call__(self, frame: Frame) -> SceneEvidence:
        if not self.experimental_history:
            return SceneEvidence()
        if self.ocr is None:
            return SceneEvidence(reason="ocr_unavailable")
        image = image_metadata(frame)
        try:
            items = self.ocr.recognize(frame.jpeg)
        except Exception:
            return SceneEvidence(reason="ocr_failed")
        return history_evidence(items, image)


class StatePipeline:
    """One serialized state owner for one stream/clock; all inputs are barriers.

    No calibrated change detector exists here. Every admitted frame advances
    context_epoch and resets every field to unknown before recognition. Reuse
    across frames is intentionally absent. TTL is experimental, receive-based.
    Public dictionaries are copies, not mutable references to accepted state.
    """

    def __init__(self, *, stream_id: str, sample_kind: str, scene_ttl_ms: float = 2000,
                 host_clock_id: str = HOST_CLOCK_ID, clock_ns: Callable[[], int] = time.monotonic_ns):
        if not stream_id or not host_clock_id or sample_kind not in ("synthetic", "live", "replay"):
            raise ValueError("Explicit stream, host clock and sample kind are required")
        if isinstance(scene_ttl_ms, bool) or not math.isfinite(scene_ttl_ms) or scene_ttl_ms <= 0:
            raise ValueError("scene_ttl_ms must be finite and positive")
        self.stream_id, self.sample_kind = stream_id, sample_kind
        self.host_clock_id, self._scene_ttl_ms, self._clock = host_clock_id, scene_ttl_ms, clock_ns
        self.freshness_policy_id = f"state-pipeline-v1:all-input-barrier:scene-{scene_ttl_ms:g}ms:uncalibrated"
        self._validator = make_validator()
        self._lock = threading.RLock()
        self._state: dict | None = None
        self._context: FrameContext | None = None
        self._input_valid, self._published, self._disconnected = False, False, False
        self._ingress_sequence = 0
        self._counts = dict(frames_admitted=0, ingress_invalidations=0, results_accepted=0,
                            results_rejected=0, expiration_updates=0)
        self._rejections: dict[str, int] = {}

    @property
    def scene_ttl_ms(self) -> float:
        return self._scene_ttl_ms

    def _now(self) -> float:
        now = self._clock() / 1_000_000
        if not math.isfinite(now) or now < 0 or (
                self._state is not None and now < self._state["assembled_host_monotonic_ms"]):
            raise ValueError("Host monotonic clock regressed or is invalid")
        return now

    @staticmethod
    def _unknown(frame: dict, completed: float, reason: str, *, applicability="undetermined") -> dict:
        return {"status": "unknown", "value": None, "applicability": applicability,
                "scope": "context", "source_frame_id": frame["frame_id"],
                "observed_host_monotonic_ms": frame["host_received_monotonic_ms"],
                "completed_host_monotonic_ms": completed, "expires_host_monotonic_ms": None,
                "method": dict(UNAVAILABLE), "region": None, "reason": reason}

    def _commit(self, state: dict, now: float) -> None:
        state["assembled_host_monotonic_ms"] = now
        state["snapshot_revision"] = 0 if self._state is None else self._state["snapshot_revision"] + 1
        validate_state(state, previous=self._state, validator=self._validator)
        self._state = state

    def advance(self, frame: Frame) -> FrameContext:
        """Register one actual image. Failures never invent dimensions or timestamps."""
        with self._lock:
            source, now = frame.source, self._now()
            if (self._disconnected or source.stream_id != self.stream_id or
                    type(source.sequence) is not int or source.sequence < 1 or
                    type(source.host_received_monotonic_ns) is not int or
                    not 0 <= source.host_received_monotonic_ns / 1_000_000 <= now or
                    source.sequence < self._ingress_sequence):
                raise FrameAdmissionRejected("Frame is not from the current healthy stream or input watermark")
            if self._context is not None and (
                    source.sequence <= self._context.source.sequence or
                    source.host_received_monotonic_ns < self._context.source.host_received_monotonic_ns):
                raise FrameAdmissionRejected("Frame order or reception time regressed")
            try:
                image = image_metadata(frame)
            except Exception:
                self._invalidate("invalidated", now)
                raise
            now = self._now()
            epoch = 0 if self._context is None else self._context.context_epoch + 1
            record = {"frame_id": source.frame_id, "sequence": source.sequence, "context_epoch": epoch,
                      "host_received_monotonic_ms": source.host_received_monotonic_ns / 1_000_000,
                      "device_capture_time": None, "image": image}
            state = {"schema_version": "game-state-v1", "sample_kind": self.sample_kind,
                     "stream_id": self.stream_id, "host_clock_id": self.host_clock_id,
                     "context_epoch": epoch, "frame_id": source.frame_id,
                     "freshness_policy_id": self.freshness_policy_id, "frames": [record]}
            state.update({key: self._unknown(record, now, "not_attempted") for key in CORE_FIELDS})
            state["scene"]["applicability"] = "applicable"
            state["mode"]["reason"] = "current_mode_unverified"
            state["game_version"]["reason"] = "version_unverified"
            state["shop"] = [{"slot": slot, "content": self._unknown(record, now, "not_attempted")}
                             for slot in range(1, 6)]
            self._commit(state, now)
            self._context = FrameContext(source, self.host_clock_id, epoch,
                                         (image["width_px"], image["height_px"]))
            self._input_valid, self._published = True, False
            self._counts["frames_admitted"] += 1
            return self._context

    def _reject(self, reason: str) -> bool:
        self._counts["results_rejected"] += 1
        self._rejections[reason] = self._rejections.get(reason, 0) + 1
        return False

    def publish(self, context: FrameContext, result: FrameResult[SceneEvidence]) -> bool:
        """One sanitized scene result per frame. This is not a multi-method merger."""
        with self._lock:
            now = self._now()
            if self._disconnected or not self._input_valid or context != self._context:
                return self._reject("obsolete_context_or_stream")
            if result.source != context.source or context.host_clock_id != self.host_clock_id:
                return self._reject("source_or_clock_mismatch")
            if self._published:
                return self._reject("duplicate_result")
            observed = result.source.host_received_monotonic_ns / 1_000_000
            started = result.processing_started_monotonic_ns / 1_000_000
            finished = result.processing_finished_monotonic_ns / 1_000_000
            if not observed <= started <= finished <= now:
                return self._reject("invalid_processing_time")
            if finished < self._state["scene"]["completed_host_monotonic_ms"]:
                return self._reject("completion_precedes_input_barrier")
            if now >= observed + self.scene_ttl_ms:
                return self._reject("expired_result")
            evidence = result.value
            # The only implemented positive rule is history. Do not accept an
            # external arbitrary scene value via this deliberately small API.
            if not isinstance(evidence, SceneEvidence) or evidence.scene not in (None, "history"):
                return self._reject("unsupported_scene_result")
            state = deepcopy(self._state)
            record = state["frames"][-1]
            completed = finished  # Preserve actual completion; never retimestamp a late result.
            if evidence.scene is None:
                state["scene"] = self._unknown(record, completed, evidence.reason, applicability="applicable")
            else:
                if (evidence.region is None or evidence.evidence_codes !=
                        ("history_header_upper_half", "history_support_below_header")):
                    return self._reject("missing_history_evidence")
                scene = self._unknown(record, completed, "not_attempted", applicability="applicable")
                scene.update(status="recognized", value="history", reason=None, method=dict(METHOD),
                             region=deepcopy(evidence.region), expires_host_monotonic_ms=observed + self.scene_ttl_ms)
                state["scene"] = scene
                for key in NON_BATTLE:
                    state[key] = self._unknown(record, completed, "not_applicable", applicability="not_applicable")
                    state[key]["method"] = {"kind": "fusion", "name": "history-page-applicability", "version": "v1"}
                for slot in state["shop"]:
                    slot["content"] = self._unknown(record, completed, "not_applicable", applicability="not_applicable")
                    slot["content"]["method"] = {"kind": "fusion", "name": "history-page-applicability", "version": "v1"}
            self._commit(state, now)
            self._published = True
            self._counts["results_accepted"] += 1
            return True

    def _invalidate(self, reason: str, now: float) -> None:
        self._input_valid = False
        if self._state is None:
            return
        state = deepcopy(self._state)
        record = state["frames"][-1]
        changed = False
        for _, observation in iter_observations(state):
            if observation["status"] == "recognized":
                observation.update(status="stale", last_known_value=observation["value"], value=None, reason=reason)
                changed = True
            elif observation["status"] == "unknown" and observation["reason"] != reason:
                observation.update(self._unknown(record, now, reason,
                                                 applicability="applicable" if observation is state["scene"] else "undetermined"))
                changed = True
        if changed:
            self._commit(state, now)

    def note_ingress(self, sequence: int) -> None:
        """Revoke old input BEFORE a newly received JPEG enters the latest mailbox.

        InvalidatingFrameSource uses the same valid-byte count as LatestFrameStream.
        No invented FrameRef is created before that layer assigns its real time.
        """
        with self._lock:
            if sequence <= self._ingress_sequence:
                raise ValueError("Ingress sequence must increase")
            self._ingress_sequence = sequence
            self._counts["ingress_invalidations"] += 1
            self._invalidate("invalidated", self._now())

    def invalidate_context(self) -> None:
        """Explicit overlay/layout notification; next actual frame gets a new epoch."""
        with self._lock:
            self._invalidate("invalidated", self._now())

    def disconnect(self) -> None:
        with self._lock:
            self._disconnected = True
            self._invalidate("disconnected", self._now())

    def snapshot(self) -> dict | None:
        """Runtime read: expire scene and revoke its derived non-applicability."""
        with self._lock:
            now = self._now()
            if self._state is not None and self._state["scene"]["status"] == "recognized" and (
                    now >= self._state["scene"]["expires_host_monotonic_ms"]):
                self._invalidate("expired", now)
                self._counts["expiration_updates"] += 1
            return deepcopy(self._state)

    def stats(self) -> dict:
        with self._lock:
            return {**self._counts, "rejections": dict(self._rejections), "disconnected": self._disconnected}


class InvalidatingFrameSource:
    """Read-only source adapter: receipt/failure invalidation reaches the owner.

    Bind once before starting LatestFrameStream. It must be the sole reader;
    its valid-byte sequence is then identical to the latest receiver's sequence.
    """

    def __init__(self, source):
        self.source, self.pipeline, self._sequence = source, None, 0

    def bind(self, pipeline: StatePipeline) -> None:
        if self.pipeline is not None or self._sequence:
            raise RuntimeError("Source must be bound once before reading")
        self.pipeline = pipeline

    def read_frame(self, timeout: float = 10.0) -> bytes:
        if self.pipeline is None:
            raise RuntimeError("Bind a pipeline before capture")
        try:
            jpeg = self.source.read_frame(timeout=timeout)
            if not isinstance(jpeg, bytes) or not jpeg:
                raise ValueError("Expected nonempty immutable image bytes")
        except TimeoutError:
            raise  # Recoverable silence; runtime reads still enforce TTL.
        except Exception:
            self.pipeline.disconnect()
            raise
        self._sequence += 1
        self.pipeline.note_ingress(self._sequence)
        return jpeg

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.disconnect()
        self.source.close()


def state_summary(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {"frame_id": state["frame_id"], "context_epoch": state["context_epoch"],
            "snapshot_revision": state["snapshot_revision"],
            "fields": {path: {key: observation[key] for key in ("status", "value", "applicability", "reason")}
                       for path, observation in iter_observations(state)}}


class _SyntheticSource:
    """Generated non-game image only; fake OCR is separately labelled in reports."""

    def __init__(self):
        from PIL import Image

        with Image.new("RGB", (800, 500), "#202020") as image, io.BytesIO() as encoded:
            image.save(encoded, format="JPEG")
            self.jpeg = encoded.getvalue()
        self.next_at = time.monotonic()

    def read_frame(self, timeout=10):
        remaining = max(0, self.next_at - time.monotonic())
        time.sleep(min(remaining, timeout))
        if remaining > timeout:
            raise TimeoutError("Synthetic source not due")
        self.next_at = time.monotonic() + 0.25
        return self.jpeg

    def close(self):
        pass


class _SyntheticOCR:
    def recognize(self, image_bytes):
        return [{"text": "对局记录", "x": 50, "y": 30, "width": 100, "height": 30},
                {"text": "最近对局", "x": 50, "y": 100, "width": 100, "height": 30}]


def run_probe(*, source_name: str, observations: int = 1, experimental_history: bool = False,
              scene_ttl_ms: float = 2000, timeout_seconds: float = 15) -> dict:
    """Bounded smoke run; one OCR job, continued receipt/admission, sanitized JSON.

    A per-frame observation is reported even if it is rejected by the live owner.
    It is explicitly NOT a current state, nor an accuracy measurement.
    """
    from contextlib import ExitStack

    started = time.monotonic_ns()
    report = {"source": source_name, "sample_kind": "live" if source_name == "tablet" else "synthetic",
              "images_saved": 0, "ocr_text_emitted": False, "device_capture_time_measured": False,
              "experimental_history": experimental_history, "single_frame_observations": [],
              "observation_is_runtime_current": False,
              "latency_boundary": "Mac complete-JPEG receipt only; device display/capture and advice not measured"}
    stream = pipeline = processor = None
    try:
        with ExitStack() as stack:
            if source_name == "tablet":
                from .stream import TabletStream
                raw = stack.enter_context(TabletStream(capture_diagnostics=False))
            elif source_name == "synthetic":
                raw = _SyntheticSource()
            else:
                raise ValueError("Unsupported source")
            barrier_source = InvalidatingFrameSource(raw)
            stream = LatestFrameStream(barrier_source)
            pipeline = StatePipeline(stream_id=stream.stream_id, sample_kind=report["sample_kind"],
                                     scene_ttl_ms=scene_ttl_ms)
            barrier_source.bind(pipeline)
            stack.enter_context(stream)
            ocr = None
            if experimental_history:
                if source_name == "tablet":
                    from .vision_ocr import VisionOCR
                    ocr = stack.enter_context(VisionOCR(timeout=min(timeout_seconds, 8)))
                else:
                    ocr = _SyntheticOCR()
            processor = FrameProcessor(SceneRecognizer(ocr, experimental_history=experimental_history))
            # Executor exits before OCR/stream teardown; the OCR exchange itself
            # has a bounded timeout. There is never a queue of submitted frames.
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            future = ticket = None
            deadline = time.monotonic() + timeout_seconds
            while len(report["single_frame_observations"]) < observations:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Probe deadline elapsed")
                if future is not None and future.done():
                    result = future.result()
                    accepted = pipeline.publish(ticket, result)
                    evidence = result.value
                    report["single_frame_observations"].append({
                        "source_frame_id": result.source.frame_id,
                        "host_clock_id": ticket.host_clock_id, "context_epoch": ticket.context_epoch,
                        "image_size_px": list(ticket.image_size),
                        "scene_observed": evidence.scene, "reason": evidence.reason,
                        "evidence_codes": list(evidence.evidence_codes), "ocr_items_count": evidence.ocr_items_count,
                        "method": dict(METHOD) if experimental_history else dict(UNAVAILABLE),
                        "synthetic_ocr": source_name == "synthetic" and experimental_history,
                        "accepted_by_runtime": accepted, "timing": result.timing()})
                    future = None
                    if len(report["single_frame_observations"]) >= observations:
                        break
                try:
                    frame = stream.read_latest(timeout=0.05, max_age_seconds=1)
                except TimeoutError:
                    pipeline.snapshot()  # Silence must not keep a cached scene alive.
                    continue
                if "first_frame_seconds" not in report:
                    report["first_frame_seconds"] = (frame.source.host_received_monotonic_ns - started) / 1e9
                try:
                    context = pipeline.advance(frame)
                except FrameAdmissionRejected:
                    # A newer raw receipt may invalidate a delivered frame while
                    # the receiver is assigning metadata; await its latest slot.
                    continue
                if future is None:
                    ticket = context
                    future = executor.submit(processor.process, frame)
            report["runtime_before_close"] = state_summary(pipeline.snapshot())
            report["status"] = "ok"
    except Exception as exc:
        # Source/OCR messages may contain account text or device identifiers.
        report.update(status="error", error_type=type(exc).__name__)
    finally:
        if pipeline is not None:
            pipeline.disconnect()
            report["state"] = pipeline.snapshot()  # A stopped probe is not a live cache.
            if report["state"] is not None:
                validate_state(report["state"], validator=pipeline._validator)
                report["validation"] = "schema_and_semantics_passed"
            else:
                report["validation"] = "no_frame_no_state"
            report["pipeline_stats"] = pipeline.stats()
        if stream is not None:
            report["capture_stats"] = stream.stats()
        if processor is not None:
            report["processor_stats"] = processor.stats()
        report["total_seconds"] = (time.monotonic_ns() - started) / 1e9
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="最新帧与 game-state-v1 只读联调；不保存图像或 OCR 原文")
    parser.add_argument("--source", choices=("synthetic", "tablet"), default="synthetic")
    parser.add_argument("--observations", type=int, default=1)
    parser.add_argument("--experimental-history", action="store_true")
    parser.add_argument("--scene-ttl-ms", type=float, default=2000)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--state-only", action="store_true", help="只输出校验后的最终状态；停止后值已撤销")
    args = parser.parse_args()
    if (args.observations < 1 or not math.isfinite(args.scene_ttl_ms) or args.scene_ttl_ms <= 0 or
            not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0 or args.timeout_seconds > 60):
        parser.error("Positive counts/TTL and a timeout in (0, 60] are required")
    report = run_probe(source_name=args.source, observations=args.observations,
                       experimental_history=args.experimental_history, scene_ttl_ms=args.scene_ttl_ms,
                       timeout_seconds=args.timeout_seconds)
    print(json.dumps(report.get("state") if args.state_only else report, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
