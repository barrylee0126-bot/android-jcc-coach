"""Processing provenance, independent of the origin of the input pixels."""

from dataclasses import asdict, dataclass


PROCESSING_KINDS = {"simulated_work", "ocr"}
CANDIDATE_STATUSES = {"candidate", "simulated_candidate"}


@dataclass(frozen=True)
class ProcessingResult:
    processing_kind: str
    processing_outcome: str
    ocr_requests: int
    ocr_items_count: int | None
    ocr_errors: int = 0

    def __post_init__(self):
        if (self.processing_kind not in PROCESSING_KINDS
                or type(self.ocr_requests) is not int or self.ocr_requests < 0
                or type(self.ocr_errors) is not int or not 0 <= self.ocr_errors <= self.ocr_requests):
            raise ValueError("Explicit processing provenance and actual OCR call counts required")
        if self.processing_kind == "simulated_work":
            if (self.processing_outcome not in {"simulated_ready", "simulated_no_output", "simulated_error"}
                    or self.ocr_requests != 0 or self.ocr_items_count is not None or self.ocr_errors != 0):
                raise ValueError("Simulated work cannot claim OCR calls or returned items")
            return
        if type(self.ocr_items_count) is not int or self.ocr_items_count < 0:
            raise ValueError("OCR item counts must be actual nonnegative integers")
        if self.ocr_items_count > 0 and self.ocr_requests <= self.ocr_errors:
            raise ValueError("Returned items require at least one successful OCR call")
        if self.processing_outcome == "ocr_text":
            valid = self.ocr_requests > 0 and self.ocr_items_count > 0 and self.ocr_errors == 0
        elif self.processing_outcome == "ocr_empty":
            valid = self.ocr_requests > 0 and self.ocr_items_count == 0 and self.ocr_errors == 0
        elif self.processing_outcome == "ocr_unresolved":
            # Successful OCR returned text, but candidate checks disagree or
            # are inconclusive; preserve actual counts without publishing it.
            valid = self.ocr_requests > 0 and self.ocr_items_count > 0 and self.ocr_errors == 0
        elif self.processing_outcome == "ocr_not_run":
            valid = self.ocr_requests == self.ocr_items_count == self.ocr_errors == 0
        elif self.processing_outcome == "ocr_error":
            # A preprocessing error may happen before the first OCR call. Any
            # partial successful returns still keep their actual item counts.
            valid = self.ocr_requests > 0 or self.ocr_items_count == self.ocr_errors == 0
        else:
            valid = False
        if not valid:
            raise ValueError("OCR outcome contradicts the actual call and item counts")

    @classmethod
    def from_report(cls, report):
        try:
            return cls(**{key: report[key] for key in cls.__dataclass_fields__})
        except (KeyError, TypeError) as error:
            raise ValueError("Completion requires the explicit processing result contract") from error

    def fields(self):
        return asdict(self)

    @property
    def failed(self):
        return self.processing_outcome in {"simulated_error", "ocr_error"}

    def completion(self):
        return {
            "simulated_ready": ("simulated_candidate", "simulated_pixel_inputs_only"),
            "simulated_no_output": ("no_output", "simulated_no_output"),
            "simulated_error": ("rejected", "simulated_failed"),
            "ocr_text": ("candidate", "pixel_inputs_only"),
            "ocr_empty": ("no_output", "ocr_empty"),
            "ocr_unresolved": ("no_output", "ocr_unresolved"),
            "ocr_not_run": ("no_output", "ocr_not_run"),
            "ocr_error": ("rejected", "ocr_failed" if self.ocr_errors else "processing_failed"),
        }[self.processing_outcome]


def simulated_report(*, outcome="simulated_ready", simulated_processing_ns=None):
    """A hypothetical processing result; no dummy text item is manufactured."""
    result = ProcessingResult("simulated_work", outcome, 0, None)
    return dict(processor="simulated_work_not_ocr", **result.fields(),
                simulated_processing_ns=simulated_processing_ns)
