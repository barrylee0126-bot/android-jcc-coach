"""Narrow offline numeric-candidate probe; no game-state or advice output."""

from dataclasses import replace
import time

from jcc_device.roi_recognition import CANDIDATE, probe_frame, reconcile_variants
from jcc_replay_demo.gold import check_gold_glyph

from .processing import ProcessingResult
from .processor import _CountedOCR


class _TimedOCR(_CountedOCR):
    def __init__(self, backend):
        super().__init__(backend)
        self.call_ms = []

    def recognize(self, data):
        start = time.monotonic_ns()
        try:
            return super().recognize(data)
        finally:
            self.call_ms.append((time.monotonic_ns() - start) / 1e6)


class ReplayGoldOCR:
    """Actual OCR with 1x/2x agreement and glyph veto, not semantic proof."""

    def __init__(self, profile, backend):
        if (profile.key.sample_kind != "replay" or len(profile.numeric_layouts) != 1
                or len(profile.numeric_layouts[0].fields) != 1
                or profile.numeric_layouts[0].fields[0].field_name != "gold"):
            raise ValueError("Replay requires exactly one explicit gold-region layout")
        self.profile, self.backend = profile, backend
        layout = profile.numeric_layouts[0]
        self.layouts = tuple(replace(layout, fields=(replace(layout.fields[0], scale=scale),))
                             for scale in (1, 2))

    def __call__(self, job):
        counted = _TimedOCR(self.backend)
        start = time.monotonic_ns()
        numeric_status, guard_status = "unknown", "not_checked"
        errors = set()
        probe_end = guard_end = start
        try:
            variants = [probe_frame(job.frame, geometry=self.profile.key.geometry, sample_kind="replay",
                                    layout=layout, ocr=counted) for layout in self.layouts]
            combined = reconcile_variants(variants)
            probe = next(p for p in combined.probes if p.field_name == "gold")
            probe_end = time.monotonic_ns()
            value = probe.candidate_value if (probe.status == CANDIDATE and type(probe.candidate_value) is int
                                               and probe.candidate_value >= 0) else None
            guard = check_gold_glyph(job.frame.jpeg, roi=self.layouts[0].fields[0].roi.box,
                                     frame_ref=job.frame.source.frame_id, proposed_value=value)
            guard_status = guard["status"]
            if value is not None and len(counted.call_ms) == 2 and guard_status in ("not_required", "consistent"):
                numeric_status = "development_candidate"
            guard_end = time.monotonic_ns()
        except Exception as error:
            errors.add(type(error).__name__)
            probe_end = guard_end = time.monotonic_ns()
        errors.update(counted.error_types)
        if errors or counted.errors:
            outcome = "ocr_error"
        elif numeric_status == "development_candidate":
            outcome = "ocr_text"
        elif counted.items:
            outcome = "ocr_unresolved"
        elif counted.requests:
            outcome = "ocr_empty"
        else:
            outcome = "ocr_not_run"
        result = ProcessingResult("ocr", outcome, counted.requests, counted.items, counted.errors)
        return dict(processor="offline_two_scale_numeric_candidate", **result.fields(),
                    numeric_status=numeric_status, glyph_status=guard_status,
                    probe_and_reconcile_ms=(probe_end - start) / 1e6,
                    glyph_guard_ms=(guard_end - probe_end) / 1e6, ocr_call_ms=counted.call_ms,
                    error_types=sorted(errors), formal_semantics="unknown_no_frame_witness")
