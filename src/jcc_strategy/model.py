"""Small declarative strategy contracts; all suggestions in v1 are test-only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from typing import Any, Protocol
import uuid


def canonical(value: Any) -> str:
    """Freeze JSON without bool/number coercion or caller-owned containers."""
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("Expected finite JSON values and string object keys")

    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def valid_ms(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def require_text(value: Any) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError("Expected a non-empty string")


def pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("Expected an RFC 6901 JSON pointer")
    parts = pointer[1:].split("/") if pointer else []
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError("Invalid JSON pointer escape")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def at_pointer(value: Any, pointer: str) -> Any:
    for part in pointer_parts(pointer):
        if type(value) is list:
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise KeyError(pointer)
            value = value[int(part)]
        elif type(value) is dict:
            value = value[part]
        else:
            raise KeyError(pointer)
    return value


@dataclass(frozen=True, init=False)
class Literal:
    """An immutable JSON value; value() always returns a fresh copy."""

    encoded: str

    def __init__(self, value: Any):
        object.__setattr__(self, "encoded", canonical(value))

    def value(self) -> Any:
        return json.loads(self.encoded)


@dataclass(frozen=True)
class ClockTick:
    host_clock_id: str
    monotonic_ms: float

    def __post_init__(self):
        require_text(self.host_clock_id)
        if not valid_ms(self.monotonic_ms):
            raise ValueError("Clock time must be finite, non-negative monotonic milliseconds")


class Clock(Protocol):
    def now(self) -> ClockTick: ...


class MonotonicClock:
    """Share this clock's domain with the producer; never re-label stored time."""

    def __init__(self, host_clock_id: str | None = None):
        self.host_clock_id = host_clock_id or "strategy-host-" + uuid.uuid4().hex
        require_text(self.host_clock_id)

    def now(self) -> ClockTick:
        return ClockTick(self.host_clock_id, time.monotonic_ns() / 1_000_000)


@dataclass(frozen=True)
class Scope:
    region: str
    season: str
    patch: str
    mode: str

    def __post_init__(self):
        for value in (self.region, self.season, self.patch, self.mode):
            require_text(value)

    def to_dict(self) -> dict:
        return dict(region=self.region, season=self.season, patch=self.patch, mode=self.mode)


CORE_DEPENDENCIES = ("/scene", "/phase", "/round", "/mode", "/game_version")
CATEGORIES = {
    "lineup": ("consider_lineup",),
    "trade": ("consider_buy", "consider_sell", "consider_keep"),
    "equipment": ("consider_equip", "consider_hold_item"),
}


@dataclass(frozen=True)
class FieldValue:
    """pointer addresses one observation envelope, not arbitrary state data."""

    pointer: str
    value_pointer: str = ""

    def __post_init__(self):
        if not pointer_parts(self.pointer):
            raise ValueError("A dependency must address an observation")
        pointer_parts(self.value_pointer)


@dataclass(frozen=True)
class EvidenceValue:
    reference_id: str

    def __post_init__(self):
        require_text(self.reference_id)


@dataclass(frozen=True)
class EvidenceNeed:
    reference_id: str
    purpose: str

    def __post_init__(self):
        require_text(self.reference_id)
        require_text(self.purpose)
        if self.purpose == "runtime_binding":
            raise ValueError("The runtime binding is derived from game_version, not supplied by a rule")


@dataclass(frozen=True)
class Condition:
    left: FieldValue
    operator: str
    right: Literal | FieldValue | EvidenceValue
    explanation: str

    def __post_init__(self):
        if not isinstance(self.left, FieldValue) or not isinstance(self.right, (Literal, FieldValue, EvidenceValue)):
            raise ValueError("Conditions accept only declared field, literal and evidence operands")
        if self.operator not in ("eq", "ge", "gt", "le", "lt"):
            raise ValueError("Unsupported condition operator")
        require_text(self.explanation)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    revision: str
    category: str
    scope: Scope
    intent: str
    target: FieldValue
    target_label: str
    conditions: tuple[Condition, ...]
    evidence: tuple[EvidenceNeed, ...]
    applicability: str
    rationale: str
    invalidates_when: tuple[str, ...]
    max_age_ms: float
    material_kind: str = "synthetic_fixture"

    def __post_init__(self):
        for value in (self.rule_id, self.revision, self.target_label, self.applicability, self.rationale):
            require_text(value)
        if self.category not in CATEGORIES or self.intent not in CATEGORIES[self.category]:
            raise ValueError("Unknown strategy category or semantic intent")
        if not isinstance(self.scope, Scope) or not isinstance(self.target, FieldValue):
            raise ValueError("A rule needs an exact scope and declared target")
        for name, kind in (("conditions", Condition), ("evidence", EvidenceNeed), ("invalidates_when", str)):
            values = tuple(getattr(self, name))
            if not values or not all(isinstance(value, kind) for value in values):
                raise ValueError("A rule needs non-empty, typed conditions, evidence and invalidation explanations")
            object.__setattr__(self, name, values)
        for value in self.invalidates_when:
            require_text(value)
        refs = [need.reference_id for need in self.evidence]
        if len(refs) != len(set(refs)) or not any(need.purpose == "rule_basis" for need in self.evidence):
            raise ValueError("Evidence must be unique by reference and include the rule basis")
        if any(isinstance(c.right, EvidenceValue) and c.right.reference_id not in refs for c in self.conditions):
            raise ValueError("An evidence operand must have a declared requirement")
        if not valid_ms(self.max_age_ms) or self.max_age_ms <= 0:
            raise ValueError("A rule needs a positive finite maximum suggestion age")
        if self.material_kind not in ("synthetic_fixture", "unreviewed"):
            raise ValueError("v1 has no registered production-rule material kind")

    @property
    def dependencies(self) -> tuple[str, ...]:
        values = list(CORE_DEPENDENCIES) + [self.target.pointer]
        for condition in self.conditions:
            values.append(condition.left.pointer)
            if isinstance(condition.right, FieldValue):
                values.append(condition.right.pointer)
        return tuple(dict.fromkeys(values))

    def to_dict(self) -> dict:
        def operand(value):
            if isinstance(value, Literal):
                return {"literal": value.value()}
            if isinstance(value, FieldValue):
                return {"field": value.pointer, "value_pointer": value.value_pointer}
            return {"evidence": value.reference_id}

        return {
            "rule_id": self.rule_id, "revision": self.revision, "category": self.category,
            "scope": self.scope.to_dict(), "intent": self.intent, "target": operand(self.target),
            "target_label": self.target_label, "dependencies": list(self.dependencies),
            "conditions": [{"left": operand(c.left), "operator": c.operator, "right": operand(c.right),
                            "explanation": c.explanation} for c in self.conditions],
            "evidence": [{"reference_id": n.reference_id, "purpose": n.purpose} for n in self.evidence],
            "applicability": self.applicability, "rationale": self.rationale,
            "invalidates_when": list(self.invalidates_when), "max_age_ms": self.max_age_ms,
            "material_kind": self.material_kind,
        }


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    pointer: str | None = None
    reference_id: str | None = None

    def to_dict(self) -> dict:
        return dict(code=self.code, message=self.message, pointer=self.pointer, reference_id=self.reference_id)


@dataclass(frozen=True)
class GuardResult:
    valid: bool
    reasons: tuple[Reason, ...] = ()

    def to_dict(self) -> dict:
        return {"valid": self.valid, "reasons": [reason.to_dict() for reason in self.reasons]}


@dataclass(frozen=True)
class Advice:
    """Immutable issued result. Display consumers must call engine.revalidate()."""

    engine_id: str
    generation: int
    rule_id: str
    rule_digest: str
    snapshot_digest: str
    evidence_digest: str
    host_clock_id: str
    evaluated_host_monotonic_ms: float
    expires_host_monotonic_ms: float
    payload: Literal
    issuance_tag: str = ""

    def to_dict(self) -> dict:
        return {
            **self.payload.value(),
            "engine_id": self.engine_id, "generation": self.generation,
            "rule_digest": self.rule_digest, "snapshot_digest": self.snapshot_digest,
            "evidence_digest": self.evidence_digest, "host_clock_id": self.host_clock_id,
            "evaluated_host_monotonic_ms": self.evaluated_host_monotonic_ms,
            "expires_host_monotonic_ms": self.expires_host_monotonic_ms,
            "issuance_tag": self.issuance_tag,
            "execution_eligible": False, "real_rules_verified": False,
        }


@dataclass(frozen=True)
class Decision:
    status: str
    rule_id: str
    reasons: tuple[Reason, ...]
    advice: Advice | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "rule_id": self.rule_id,
                "reasons": [reason.to_dict() for reason in self.reasons],
                "advice": self.advice.to_dict() if self.advice else None}
