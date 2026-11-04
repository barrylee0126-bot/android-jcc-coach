"""Replay-only shadow tracking; no certified game context or device access.

The labels, locations, thresholds, timebase and sequence come from the caller's
experiment.  OCR and pixel checks run on the actual supplied frame, but neither
that frame nor its source metadata is authenticated here.  A shadow candidate
must never be promoted to a real-time instruction or a self-board assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import math
import time
from uuid import uuid4

from PIL import Image, ImageChops, ImageStat

from jcc_device.latest import Frame, FrameRef
from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import FieldROI, ImageGeometry, ROILayout, probe_frame
from jcc_device.scene_evidence import CueSpec, LayoutKey, SceneLayout, probe_scene


@dataclass(frozen=True)
class Layout:
    """Caller-calibrated replay geometry, not an authenticated game layout."""

    image_size: tuple[int, int]
    round_box: tuple[int, int, int, int]
    experience_box: tuple[int, int, int, int]
    refresh_box: tuple[int, int, int, int]
    banner_box: tuple[int, int, int, int]
    marker_box: tuple[int, int, int, int]
    marker_max_mean_delta: float
    scale: int = 2

    def __post_init__(self):
        geometry = ImageGeometry(self.image_size)
        for name in ("round", "experience", "refresh", "banner", "marker"):
            box = tuple(getattr(self, name + "_box"))
            PixelROI(name, geometry.image_size, box).validate(geometry.image_size)
            object.__setattr__(self, name + "_box", box)
        threshold = self.marker_max_mean_delta
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold) or not 0 <= threshold <= 255):
            raise ValueError("An explicit finite marker difference threshold is required")
        if type(self.scale) is not int or not 1 <= self.scale <= 4:
            raise ValueError("Scale must be an integer between one and four")
        object.__setattr__(self, "image_size", geometry.image_size)


def _int(value):
    return type(value) is int and value >= 0


def _valid_source_id(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


class ShadowTracker:
    """Track only a conditioned, replay-local *candidate* seeded by OCR.

    Sequence and PTS are declared by the replay decoder, not authenticated by
    this class.  They must represent every decoded frame, without renumbering a
    subset.  The result never says that the board is the player's, unobstructed,
    current, correct, safe for advice, or safe for control.
    """

    def __init__(self, layout: Layout, ocr, *, source_id: str, max_frame_gap_ns: int,
                 max_anchor_age_ns: int):
        if not _valid_source_id(source_id) or not isinstance(layout, Layout) \
                or not _int(max_frame_gap_ns) or max_frame_gap_ns == 0 \
                or not _int(max_anchor_age_ns) or max_anchor_age_ns == 0:
            raise ValueError("A declared source and positive timeline limits are required")
        self.layout, self.ocr = layout, ocr
        self._declared_source_id = source_id
        self._source_invalidated = False
        self.max_frame_gap_ns, self.max_anchor_age_ns = max_frame_gap_ns, max_anchor_age_ns
        self._session = uuid4().hex
        self._last_seq = self._last_pts = None
        self._clock_pts = None
        self._anchor = None
        self._marker_conflict_locked = False
        self._closed = False
        self._last_result = None
        geometry = ImageGeometry(layout.image_size)
        self._key = LayoutKey("prep-shadow-experiment", geometry, "replay", "video_frame")

        def cue(cue_id, box, texts=()):
            return CueSpec(cue_id, PixelROI(cue_id, layout.image_size, box), texts, layout.scale)

        self._scene_layout = SceneLayout(self._key, (
            cue("round_header", layout.round_box),
            cue("shop_experience", layout.experience_box, ("购买经验",)),
            cue("shop_refresh", layout.refresh_box, ("刷新",)),
            cue("phase_preparation", layout.banner_box, ("准备阶段",)),
            cue("phase_combat", layout.banner_box, ("战斗开始",)),
        ))
        numeric_roi = FieldROI("round", PixelROI("round_value", layout.image_size,
                                                  layout.round_box), layout.scale)
        self._round_layout = ROILayout("prep-shadow-experiment", geometry, "replay", (numeric_roi,))

    def _result(self, *, status="unknown", reason, frame_sha256=None, round_value=None,
                phase=None, marker_delta=None):
        anchor = self._anchor
        result = {
            "method": "prep-context-shadow-v1", "status": status, "reason": reason,
            "phase_candidate": phase,
            "round_candidate": tuple(round_value) if round_value is not None else None,
            "frame_sha256_private": frame_sha256,
            "anchor_sequence": anchor["sequence"] if anchor else None,
            "anchor_pts_ns_declared": anchor["pts"] if anchor else None,
            "anchor_deadline_pts_ns_declared": anchor["pts"] + self.max_anchor_age_ns if anchor else None,
            "marker_mean_delta": marker_delta,
            "marker_conflict_locked": self._marker_conflict_locked,
            "source_metadata_authenticated": False, "ocr_authenticated": False,
            "board_owner": "unknown", "visibility": "unknown", "human_ground_truth": None,
            "independent_human_review": False, "accuracy": None,
            "advice_eligible": False, "execution_eligible": False,
        }
        self._last_result = result
        return dict(result)

    def _withdraw(self, reason, **kwargs):
        self._anchor = None
        return self._result(reason=reason, **kwargs)

    def observe(self, image_bytes: bytes, *, sequence: int, pts_ns: int, source_id=None) -> dict:
        """Inspect one declared successive replay frame; PTS is never wall time."""
        if self._closed:
            return self._result(reason="closed")
        if self._source_invalidated:
            return self._withdraw("source_rebind_required")
        if not _valid_source_id(source_id):
            return self._withdraw("invalid_declared_source")
        if source_id != self._declared_source_id:
            self._source_invalidated = True
            return self._withdraw("declared_source_changed")
        if self._marker_conflict_locked:
            return self._withdraw("marker_conflict_locked")
        if not _int(sequence) or not _int(pts_ns):
            return self._withdraw("invalid_replay_metadata")
        if self._clock_pts is not None and pts_ns <= self._clock_pts:
            return self._withdraw("replay_clock_regressed")
        if self._last_seq is not None:
            if sequence <= self._last_seq or pts_ns <= self._last_pts:
                return self._withdraw("duplicate_or_out_of_order")
            if sequence != self._last_seq + 1 or pts_ns - self._last_pts > self.max_frame_gap_ns:
                self._last_seq, self._last_pts = sequence, pts_ns
                self._clock_pts = pts_ns
                return self._withdraw("replay_gap")
        self._last_seq, self._last_pts = sequence, pts_ns
        self._clock_pts = pts_ns
        if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= 25 * 1024 * 1024:
            return self._withdraw("image_unreadable")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                if (image.format not in ("PNG", "JPEG") or image.size != self.layout.image_size
                        or image.getexif().get(274, 1) != 1 or getattr(image, "n_frames", 1) != 1
                        or image.width * image.height > 16_000_000):
                    return self._withdraw("image_geometry_or_format_mismatch")
                marker = image.convert("RGB").crop(self.layout.marker_box)
        except Exception:
            return self._withdraw("image_unreadable")

        sha = hashlib.sha256(image_bytes).hexdigest()
        # A normal expiry is distinct from a marker conflict.  An expired
        # anchor cannot conflict with a frame at or beyond its deadline.
        if self._anchor and pts_ns >= self._anchor["pts"] + self.max_anchor_age_ns:
            return self._withdraw("anchor_expired", frame_sha256=sha)

        # Check the anchored marker before OCR: a missing shop or phase cue
        # must not hide a simultaneously observable marker conflict.  A
        # persistent banner or later OCR result cannot release this latch.
        mean_delta = None
        if self._anchor:
            diff = ImageChops.difference(self._anchor["marker"], marker)
            mean_delta = sum(ImageStat.Stat(diff).mean) / 3.0
            if mean_delta > self.layout.marker_max_mean_delta:
                self._marker_conflict_locked = True
                return self._withdraw("marker_changed", frame_sha256=sha, marker_delta=mean_delta)

        received = time.monotonic_ns()
        frame = Frame(FrameRef(self._session, sequence, received,
                               datetime.now(timezone.utc).isoformat()), image_bytes)
        scene = probe_scene(frame, key=self._key, host_clock_id=self._session, context_epoch=0,
                            layout=self._scene_layout, ocr=self.ocr)
        # An explicit combat banner revokes even if the other scene cues fail.
        cues = {item.cue_id: item for item in scene.cues}
        if cues.get("phase_combat") is not None and cues["phase_combat"].present:
            return self._withdraw("combat_banner", frame_sha256=sha)
        if scene.failure_reason or scene.scene.status != "development_candidate":
            return self._withdraw("scene_cues_missing", frame_sha256=sha)
        phase_cues = [cues.get("phase_preparation"), cues.get("phase_combat")]
        if any(item is None or (not item.present and item.reason not in ("no_text", "unmatched_text"))
               for item in phase_cues):
            return self._withdraw("phase_cue_unreadable", frame_sha256=sha)
        if not any(item.present for item in phase_cues) and any(
                item.reason == "unmatched_text" for item in phase_cues):
            return self._withdraw("unrecognized_banner_text", frame_sha256=sha)

        numbers = probe_frame(frame, geometry=self._key.geometry, sample_kind="replay",
                              layout=self._round_layout, ocr=self.ocr)
        round_probe = numbers.probes[0]
        if round_probe.status != "development_candidate" or not isinstance(round_probe.candidate_value, dict):
            return self._withdraw("round_unknown", frame_sha256=sha)
        current_round = (round_probe.candidate_value["stage"], round_probe.candidate_value["step"])
        if self._anchor and self._anchor["round"] != current_round:
            return self._withdraw("round_changed", frame_sha256=sha)

        if scene.phase.status == "development_candidate" and scene.phase.value == "preparation":
            if self._anchor is None:
                self._anchor = {"sequence": sequence, "pts": pts_ns,
                                "round": current_round, "marker": marker.copy()}
            return self._result(status="development_candidate", reason="preparation_banner",
                                frame_sha256=sha, round_value=list(current_round), phase="preparation",
                                marker_delta=mean_delta)
        if self._anchor is None:
            return self._result(reason="preparation_anchor_missing", frame_sha256=sha)
        return self._result(status="development_candidate", reason="shadow_continuation",
                            frame_sha256=sha, round_value=list(current_round), phase="preparation",
                            marker_delta=mean_delta)

    def current(self, *, pts_ns: int, source_id=None) -> dict:
        """Check a caller-driven replay clock; no background expiry thread exists."""
        if self._closed:
            return self._result(reason="closed")
        if self._source_invalidated:
            return self._withdraw("source_rebind_required")
        if not _valid_source_id(source_id):
            return self._withdraw("invalid_declared_source")
        if source_id != self._declared_source_id:
            self._source_invalidated = True
            return self._withdraw("declared_source_changed")
        if self._marker_conflict_locked:
            return self._withdraw("marker_conflict_locked")
        if (not _int(pts_ns) or self._last_pts is None
                or self._clock_pts is not None and pts_ns < self._clock_pts):
            return self._withdraw("invalid_replay_clock")
        self._clock_pts = pts_ns
        if self._anchor and pts_ns >= self._anchor["pts"] + self.max_anchor_age_ns:
            return self._withdraw("anchor_expired")
        if pts_ns - self._last_pts > self.max_frame_gap_ns:
            return self._withdraw("replay_silence")
        return dict(self._last_result) if self._last_result is not None else self._result(reason="no_frame")

    def close(self):
        self._anchor = None
        self._closed = True
