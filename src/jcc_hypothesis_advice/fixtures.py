"""Entirely generated examples, including the replay-shaped template.

No example is a real image, recording, price, mode, patch, review or game rule.
The replay-shaped example explicitly retains constructed_replay_template.
"""

from __future__ import annotations

from copy import deepcopy

from .contract import CLAIM_ROLES, HYPOTHESES, NUMBERS, SCHEMA, TIME_BASIS, digest
from .engine import ClockTick, HypothesisSession


class FixtureClock:
    def __init__(self, ms: float = 1100, review_clock_id: str = "fixture-review-clock"):
        self.ms = ms
        self.review_clock_id = review_clock_id

    def now(self) -> ClockTick:
        return ClockTick(self.review_clock_id, self.ms)

    def advance(self, ms: float) -> None:
        if ms < 0:
            raise ValueError("Fixture clock cannot move backwards")
        self.ms += ms


def example_request(*, replay_candidate: bool = False) -> dict:
    """Construct fake data; replay_candidate changes only the contract shape."""
    prefix = "private:" if replay_candidate else "synthetic:"
    scope = {
        "game": "fictional-game", "region": "fictional-region", "season": "fictional-season",
        "patch": "fictional-patch", "hotfix": "fictional-base-event", "mode": "fictional-mode",
    }
    binding = {
        "stream_id": "fixture-review-stream", "frame_id": "fixture-frame-0",
        "frame_sha256": digest({"not_an_image": "generated-frame-token-0"}),
        "sequence": 0, "snapshot_revision": 1, "context_epoch": 0,
        "review_clock_id": "fixture-review-clock", "received_review_ms": 1000,
        "assembled_review_ms": 1030,
    }
    values = {"scene": "battle", "phase": "preparation", "perspective": "self", "gold": 9,
              "scope_binding": scope, "hypothetical_spend": 3, "reserve": 6}
    claims = {}
    for name, role in CLAIM_ROLES.items():
        unit = "gold" if name in NUMBERS else "scope" if name == "scope_binding" else "context"
        provenance = "synthetic_fixture"
        if replay_candidate:
            provenance = "caller_assumption" if name in HYPOTHESES else "machine_candidate"
        assertion = {"all_values_invented": True, "field": name, "value": values[name], "scope": scope}
        basis = {
            "evidence_id": "fixture-assertion-" + name, "status": "candidate", "provenance": provenance,
            "scope": deepcopy(scope), "binding": deepcopy(binding), "asserted_value": deepcopy(values[name]),
            "unit": unit, "source_ref": prefix + "constructed-source-" + name,
            "source_sha256": digest(assertion), "source_pointer": "/generated/" + name,
            "basis_note": "纯构造的合同样例；并无真实来源、人员审阅、图像或规则核验。",
            "source_time": None, "review_ref": None, "assessed_review_ms": 1020, "expires_review_ms": 1700,
        }
        claims[name] = {
            "status": "hypothesis" if name in HYPOTHESES else "candidate", "value": deepcopy(values[name]),
            "role": role, "unit": unit, "visibility": "not_visual" if name in HYPOTHESES else "clear",
            "binding": deepcopy(binding), "observed_review_ms": 1000, "completed_review_ms": 1020,
            "expires_review_ms": 1800, "basis": [basis],
        }
    return {
        "schema_version": SCHEMA, "purpose": "hypothesis_review_only",
        "sample_kind": "replay_candidate" if replay_candidate else "synthetic_fixture",
        "time_basis": TIME_BASIS,
        "source": {
            "kind": "constructed_replay_template" if replay_candidate else "generated_fixture",
            "recording_ref": prefix + "example-only-no-recording",
            "capture_ref": prefix + "example-only-no-image-0", "historical_capture_time": None,
        },
        "binding": binding, "scope": scope, "claims": claims,
    }


def fixture_session(clock: FixtureClock | None = None, **limits) -> HypothesisSession:
    return HypothesisSession(clock=clock or FixtureClock(), max_frame_age_ms=limits.get("max_frame_age_ms", 1000),
                             max_result_ms=limits.get("max_result_ms", 300))


def set_claim(case: dict, name: str, value) -> None:
    """Update a constructed field and its constructed assertions together."""
    case["claims"][name]["value"] = deepcopy(value)
    for basis in case["claims"][name]["basis"]:
        basis["asserted_value"] = deepcopy(value)
        basis["source_sha256"] = digest({"generated_example": True, "field": name, "value": value})


def rebind(case: dict, **changes) -> dict:
    """Generated helper, not a real frame adapter or clock synchronizer."""
    result = deepcopy(case)
    result["binding"].update(changes)
    for claim in result["claims"].values():
        claim["binding"] = deepcopy(result["binding"])
        for basis in claim["basis"]:
            basis["binding"] = deepcopy(result["binding"])
    return result


def next_frame(case: dict, *, context_changed: bool = False) -> dict:
    sequence = case["binding"]["sequence"] + 1
    received = case["binding"]["received_review_ms"] + 50
    result = rebind(
        case, frame_id="fixture-frame-" + str(sequence), sequence=sequence,
        frame_sha256=digest({"not_an_image": "generated-frame-token-" + str(sequence)}),
        snapshot_revision=case["binding"]["snapshot_revision"] + 1,
        context_epoch=case["binding"]["context_epoch"] + int(context_changed),
        received_review_ms=received, assembled_review_ms=received + 30,
    )
    prefix = "private:" if case["sample_kind"] == "replay_candidate" else "synthetic:"
    result["source"]["capture_ref"] = prefix + "example-only-no-image-" + str(sequence)
    for claim in result["claims"].values():
        claim.update(observed_review_ms=received, completed_review_ms=received + 20,
                     expires_review_ms=received + 800 if claim["status"] not in ("unknown", "conflict", "stale", "not_applicable") else None)
        for basis in claim["basis"]:
            basis.update(assessed_review_ms=received + 20, expires_review_ms=received + 700)
    return result


def demo() -> dict:
    case = example_request()
    clock = FixtureClock()
    session = fixture_session(clock)
    positive = session.submit(case)
    repeated = session.view()
    clock.advance(300)
    expired = session.view()
    cannot_revive = session.submit(case)
    counter = example_request()
    set_claim(counter, "gold", 8)
    negative = fixture_session().submit(counter)
    hidden = example_request()
    hidden["claims"]["gold"]["visibility"] = "occluded"
    occluded = fixture_session().submit(hidden)
    replay = fixture_session().submit(example_request(replay_candidate=True))
    return {
        "label": "全部输入纯构造；回放形状样例也不是真实回放，不读取图像、不访问设备。",
        "synthetic_positive": positive,
        "repeated_read_same_deadline": repeated["expires_review_ms"] == positive["expires_review_ms"],
        "numeric_counterexample": negative,
        "occluded": occluded,
        "at_exact_expiry": expired,
        "old_revision_after_expiry": cannot_revive,
        "constructed_replay_shape": replay,
    }
