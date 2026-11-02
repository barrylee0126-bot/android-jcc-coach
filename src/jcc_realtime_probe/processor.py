"""Actual local OCR plus unchanged 10G binding/gates; raw text stays in memory."""

import io
import math
import time

from PIL import Image

from jcc_device.scene_evidence import (
    ClockReading, assess_fields, probe_numbers, probe_scene, summarize_fields,
)

from .processing import ProcessingResult


def _valid_ocr_item(item, size):
    """Count structurally valid text in this exact OCR crop, not game meaning."""
    if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
        return False
    try:
        x, y, width, height, score = (item[k] for k in ("x", "y", "width", "height", "confidence"))
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
               for n in (x, y, width, height, score)):
            return False
        return (0 <= score <= 1 and 0 <= x < x + width <= size[0]
                and 0 <= y < y + height <= size[1])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


class _CountedOCR:
    """Count recognize invocations, including failures caught inside 10F/10G."""

    def __init__(self, backend):
        self.backend = backend
        self.requests = self.items = self.errors = 0
        self.error_types = set()

    def recognize(self, data):
        recognize = self.backend.recognize
        self.requests += 1
        try:
            items = recognize(data)
            if not isinstance(items, list):
                raise ValueError("OCR must return an item list")
            with Image.open(io.BytesIO(data)) as image:
                self.items += sum(_valid_ocr_item(item, image.size) for item in items)
            # 10F/10G must still see all original items, including malformed or
            # ambiguous ones. Filtering here could weaken their stricter gates.
            return items
        except Exception as error:
            self.errors += 1
            self.error_types.add(type(error).__name__)
            raise


class OCRProcessor:
    def __init__(self, profile, ocr, clock_id):
        self.profile, self.ocr, self.clock_id = profile, ocr, clock_id

    def __call__(self, job):
        p, f = self.profile, job.frame
        counted = _CountedOCR(self.ocr)
        details, errors, processing_failed = {}, set(), False
        started = time.monotonic_ns()
        try:
            with Image.open(io.BytesIO(f.jpeg)) as image:
                if image.size != p.image_size or image.mode != "RGB":
                    raise ValueError("Invalid processing geometry")
                for box in p.raw_boxes:
                    with image.crop(box) as crop, io.BytesIO() as out:
                        crop.save(out, format="PNG")
                        counted.recognize(out.getvalue())
            details["raw_ocr_ns"] = time.monotonic_ns() - started
            # The 10G key still binds the exact Frame. Its caught errors must
            # not turn a partial OCR failure into successful text evidence.
            scene = probe_scene(f, key=p.key, host_clock_id=self.clock_id, context_epoch=0,
                                layout=p.scene_layout, ocr=counted)
            details.update(scene=scene.summary(), fields=None)
            processing_failed = any(c.reason == "ocr_failed" for c in scene.cues)
            if p.numeric_layouts:
                numbers = probe_numbers(f, key=p.key, host_clock_id=self.clock_id, context_epoch=0,
                                        layouts=p.numeric_layouts, ocr=counted)
                details["fields"] = summarize_fields(assess_fields(
                    scene, numbers, current=scene.binding, now=ClockReading(self.clock_id, time.monotonic_ns()),
                    max_frame_age_ns=1_000_000_000, witness=None))
                processing_failed |= any(x.reason == "ocr_failed" for x in numbers.experiment.probes)
        except Exception as error:
            errors.add(type(error).__name__)
            processing_failed = True
        errors.update(counted.error_types)
        outcome = ("ocr_error" if processing_failed or counted.errors else "ocr_text" if counted.items else
                   "ocr_empty" if counted.requests else "ocr_not_run")
        result = ProcessingResult("ocr", outcome, counted.requests, counted.items, counted.errors)
        return dict(processor="ocr_backend_and_10G", **result.fields(), **details,
                    error_types=sorted(errors), numeric_layout_configured=bool(p.numeric_layouts),
                    formal_semantics="unknown_no_frame_witness")
