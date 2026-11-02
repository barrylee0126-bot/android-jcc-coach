"""Explicit strategy-side port, independent of jcc_catalog's evidence internals.

There is intentionally no implementation that promotes real records to verified
game rules. Only synthetic_fixture evidence is consumable by the v1 engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import ClockTick, Literal, Scope


@dataclass(frozen=True)
class EvidenceQuery:
    reference_id: str
    purpose: str
    scope: Scope
    stream_id: str
    sample_kind: str
    rule_id: str
    rule_revision: str
    rule_digest: str


@dataclass(frozen=True)
class Citation:
    """A locator, never proof by itself. Fixture locators contain no real data."""

    snapshot_id: str
    record_key: str
    field_pointer: str
    source_sha256: str
    locator: str
    review_ref: str

    def to_dict(self) -> dict:
        return dict(snapshot_id=self.snapshot_id, record_key=self.record_key,
                    field_pointer=self.field_pointer, source_sha256=self.source_sha256,
                    locator=self.locator, review_ref=self.review_ref)


@dataclass(frozen=True)
class EvidenceReport:
    reference_id: str
    purpose: str
    status: str
    material_kind: str = "unreviewed"
    scope: Scope | None = None
    stream_id: str | None = None
    host_clock_id: str | None = None
    assessed_host_monotonic_ms: float | None = None
    expires_host_monotonic_ms: float | None = None
    value: Literal | None = None
    citations: tuple[Citation, ...] = ()
    conditions: str | None = None
    unit: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "reference_id": self.reference_id, "purpose": self.purpose, "status": self.status,
            "material_kind": self.material_kind, "scope": self.scope.to_dict() if self.scope else None,
            "stream_id": self.stream_id, "host_clock_id": self.host_clock_id,
            "assessed_host_monotonic_ms": self.assessed_host_monotonic_ms,
            "expires_host_monotonic_ms": self.expires_host_monotonic_ms,
            "value": self.value.value() if self.value else None,
            "citations": [citation.to_dict() for citation in self.citations],
            "conditions": self.conditions, "unit": self.unit, "reason": self.reason,
        }


class EvidencePort(Protocol):
    """Implementations must not infer review from source metadata or booleans.

    revision() is a cheap, non-blocking consistency barrier, never a verification
    flag. It must change atomically whenever any report can change or be revoked. resolve()
    must not mutate reports in place. The engine checks the revision before
    and after evaluation, and resolves reports again during revalidation.
    A future real adapter needs a separately reviewed production acceptance
    policy; returning a report here does not enable live/replay suggestions.
    """

    def revision(self) -> str: ...

    def resolve(self, query: EvidenceQuery, *, at: ClockTick) -> EvidenceReport: ...


class RejectingEvidencePort:
    """Default: even a structurally recognized verification_ref is unresolved."""

    def resolve(self, query: EvidenceQuery, *, at: ClockTick) -> EvidenceReport:
        return EvidenceReport(query.reference_id, query.purpose, "unverifiable",
                              reason="Real evidence verification has not been integrated")

    def revision(self) -> str:
        return "unintegrated-evidence-port-v1"
