"""Evidence links and applicability diagnostics, without a current-value authority.

All source classifications, readings, reviews, coverage and client bindings are
recorded inputs. They are not authenticated by this module. In particular, no
combination of these inputs unlocks a current-client decision value in v1.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

from .catalog import Catalog, CatalogError, FieldRef, SCOPE_KEYS


EVIDENCE_KINDS = ("official_documented", "client_observed", "third_party_only", "unknown")
LOCATOR_KEYS = ("url", "local_path", "sha256", "json_pointer", "section", "observation_id")


class EvidenceError(CatalogError):
    """Stable error code; reasons contain codes only, never source values."""

    def __init__(self, code: str, message: str, *, reasons: tuple[str, ...] = ()):
        super().__init__(code, message)
        self.reasons = reasons


@dataclass(frozen=True)
class SourceEvidence:
    evidence_id: str
    evidence_kind: str
    locator: Mapping[str, str | None]
    scope: Mapping[str, str | None] = field(default_factory=dict)
    source_published_at: str | None = None
    retrieved_at: str | None = None
    observed_at: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    read_method: str | None = None


@dataclass(frozen=True)
class FieldAssertion:
    assertion_id: str
    evidence_id: str
    subject_key: str
    field_key: str
    asserted_value: Any
    scope: Mapping[str, str | None] = field(default_factory=dict)
    variant_key: str | None = None
    semantic: str | None = None
    unit: str | None = None
    conditions: Mapping[str, Any] | None = None
    interpretation_note: str | None = None
    release_event_id: str | None = None
    announced_effective_at: str | None = None
    observed_effective_at: str | None = None
    valid_until: str | None = None


@dataclass(frozen=True)
class RevisionEvent:
    revision_id: str
    evidence_id: str
    assertion_ids: tuple[str, ...]
    reason: str
    scope: Mapping[str, str | None] = field(default_factory=dict)
    effective_at: str | None = None
    replacement_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RevisionCoverage:
    """A caller's coverage report, for exact assertions and a closed interval."""

    coverage_id: str
    evidence_id: str
    assertion_ids: tuple[str, ...]
    checked_from: str
    checked_through: str
    method: str
    scope: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ClientBinding:
    """A reported observation context, not an authenticated device binding."""

    context_id: str
    scope: Mapping[str, str | None] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    release_event_ids: tuple[str, ...] = ()
    observed_at: str | None = None
    expires_at: str | None = None


def _text(value: Any, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("invalid_text", "Expected a nonempty string or an allowed null.")


def _time(value: Any, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError("not a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        return parsed
    except (TypeError, ValueError) as error:
        raise EvidenceError("invalid_timestamp", "Use an ISO timestamp with a timezone.") from error


def _pointer(value: Any) -> None:
    if not isinstance(value, str) or (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
        raise EvidenceError("invalid_pointer", "Use an RFC 6901 pointer.")


def _scope(value: Any) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or set(value) - set(SCOPE_KEYS):
        raise EvidenceError("invalid_scope", "Scope contains an unsupported dimension.")
    for item in value.values():
        _text(item, optional=True)
    return {key: value.get(key) for key in SCOPE_KEYS}


def _scope_relation(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    different = [key for key in SCOPE_KEYS
                 if left[key] is not None and right[key] is not None and left[key] != right[key]]
    unknown = [key for key in SCOPE_KEYS if left[key] is None or right[key] is None]
    return {"status": "different" if different else "unknown" if unknown else "same",
            "different_dimensions": different, "unknown_dimensions": unknown}


def _json(value: Any) -> str:
    def check(item: Any) -> None:
        if type(item) is dict:
            if any(not isinstance(key, str) for key in item):
                raise ValueError("non-string object key")
            for child in item.values():
                check(child)
        elif type(item) in (list, tuple):
            for child in item:
                check(child)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("not a JSON value")

    try:
        check(value)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise EvidenceError("invalid_json_value", "Evidence must contain finite UTF-8 JSON values.") from error


def _record(value: Any, expected: type) -> dict[str, Any]:
    if type(value) is not expected:
        raise EvidenceError("invalid_record_type", "Use the documented evidence record type.")
    try:
        return json.loads(_json(asdict(value)))
    except RecursionError as error:
        raise EvidenceError("invalid_json_value", "Evidence nesting is unsupported.") from error


def _ids(value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise EvidenceError("invalid_references", "Expected a list of distinct references.")
    for item in value:
        _text(item)
    if len(value) != len(set(value)):
        raise EvidenceError("duplicate_reference", "Repeated references are not independent evidence.")
    return value


def _unknown_value(value: Any) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _future_source(source: Mapping[str, Any], at: datetime) -> bool:
    return any(source[key] is not None and _time(source[key]) > at
               for key in ("source_published_at", "retrieved_at", "observed_at", "reviewed_at"))


class EvidenceIndex:
    """In-memory, append-only evidence graph over one existing candidate catalog.

    References are explicit IDs and exact catalog FieldRefs. This class performs
    no fetching, file writes, source authentication, semantic parsing or rule
    adjudication. Public results are copies, suitable for local JSON reports.
    """

    def __init__(self, catalog: Catalog):
        if not isinstance(catalog, Catalog):
            raise EvidenceError("invalid_catalog", "An existing candidate Catalog is required.")
        self._catalog = catalog
        self._sources: dict[str, dict[str, Any]] = {}
        self._assertions: dict[str, dict[str, Any]] = {}
        self._links: dict[str, dict[str, Any]] = {}
        self._comparisons: dict[str, dict[str, Any]] = {}
        self._revisions: dict[str, dict[str, Any]] = {}
        self._coverage: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _get(records: Mapping[str, dict[str, Any]], key: str, code: str) -> dict[str, Any]:
        try:
            return records[key]
        except (KeyError, TypeError) as error:
            raise EvidenceError(code, "Referenced evidence record not found.") from error

    @staticmethod
    def _insert(records: dict[str, Any], key: str, value: dict[str, Any]) -> None:
        _text(key)
        if key in records:
            raise EvidenceError("duplicate_evidence_id", "Evidence IDs cannot overwrite existing records.")
        records[key] = value

    def add_source(self, source: SourceEvidence) -> dict[str, Any]:
        data = _record(source, SourceEvidence)
        _text(data["evidence_id"])
        if data["evidence_kind"] not in EVIDENCE_KINDS:
            raise EvidenceError("invalid_evidence_kind", "Unsupported evidence classification.")
        locator = data["locator"]
        if not isinstance(locator, dict) or set(locator) - set(LOCATOR_KEYS):
            raise EvidenceError("invalid_locator", "Locator contains unsupported fields.")
        for key, value in locator.items():
            if key == "json_pointer":
                if value is not None:
                    _pointer(value)
            else:
                _text(value, optional=True)
        if not any(locator.get(key) for key in ("url", "local_path", "observation_id")):
            raise EvidenceError("missing_locator", "A URL, local path or observation reference is required.")
        if locator.get("url"):
            try:
                url = urlsplit(locator["url"])
                if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                    raise ValueError("unsupported URL")
                _ = url.port
            except ValueError as error:
                raise EvidenceError("invalid_source_url", "Use an HTTP(S) locator without credentials.") from error
        if locator.get("sha256") and not re.fullmatch(r"[0-9a-f]{64}", locator["sha256"]):
            raise EvidenceError("invalid_digest", "SHA-256 must be 64 lowercase hexadecimal characters.")
        data["locator"] = {key: locator.get(key) for key in LOCATOR_KEYS}
        data["scope"] = _scope(data["scope"])
        for key in ("source_published_at", "retrieved_at", "observed_at", "reviewed_at"):
            _time(data[key])
        _text(data["review_note"], optional=True)
        _text(data["read_method"], optional=True)
        if (data["review_note"] is None) != (data["reviewed_at"] is None):
            raise EvidenceError("incomplete_review", "A review note and review time must be recorded together.")
        if data["observed_at"] is not None and data["evidence_kind"] != "client_observed":
            raise EvidenceError("not_an_observation", "Document retrieval is not a client observation.")
        if locator.get("observation_id") and data["evidence_kind"] != "client_observed":
            raise EvidenceError("not_an_observation", "Observation references require client_observed evidence.")
        data.update(classification_origin="caller_supplied",
                    authenticity="review_recorded" if data["review_note"] else "unknown",
                    authenticity_authenticated=False)
        self._insert(self._sources, data["evidence_id"], data)
        return copy.deepcopy(data)

    def source(self, evidence_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._get(self._sources, evidence_id, "unknown_evidence"))

    def add_assertion(self, assertion: FieldAssertion) -> dict[str, Any]:
        data = _record(assertion, FieldAssertion)
        for key in ("assertion_id", "evidence_id", "subject_key"):
            _text(data[key])
        _text(data["field_key"])
        _pointer(data["field_key"])
        for key in ("variant_key", "semantic", "unit", "interpretation_note", "release_event_id"):
            _text(data[key], optional=True)
        if data["conditions"] is not None and not isinstance(data["conditions"], dict):
            raise EvidenceError("invalid_conditions", "Use a condition object, or null for unknown conditions.")
        data["scope"] = _scope(data["scope"])
        source = self._get(self._sources, data["evidence_id"], "unknown_evidence")
        for key in ("announced_effective_at", "observed_effective_at", "valid_until"):
            _time(data[key])
        if data["observed_effective_at"] is not None and (
                source["evidence_kind"] != "client_observed" or source["observed_at"] is None):
            raise EvidenceError("not_an_observation", "An announcement cannot establish observed effectiveness.")
        end = _time(data["valid_until"])
        if end is not None and any(data[key] is not None and _time(data[key]) >= end
                                   for key in ("announced_effective_at", "observed_effective_at")):
            raise EvidenceError("invalid_interval", "An explicit end must follow the effective start.")
        data.update(interpretation_origin="caller_supplied", adjudication="unresolved", decision_eligible=False)
        self._insert(self._assertions, data["assertion_id"], data)
        return copy.deepcopy(data)

    def assertion(self, assertion_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._get(self._assertions, assertion_id, "unknown_assertion"))

    def find_assertions(self, *, subject_key: str | None = None, variant_key: str | None = None,
                        field_key: str | None = None, release_event_id: str | None = None,
                        scope: Mapping[str, str | None] | None = None) -> list[dict[str, Any]]:
        """Exact filters only; omitted variant_key means no variant filter."""
        filters = {"subject_key": subject_key, "variant_key": variant_key,
                   "field_key": field_key, "release_event_id": release_event_id}
        for value in filters.values():
            _text(value, optional=True)
        if field_key is not None:
            _pointer(field_key)
        if scope is not None:
            _scope(scope)
        return copy.deepcopy([item for item in self._assertions.values()
                              if all(value is None or item[key] == value for key, value in filters.items())
                              and (scope is None or all(item["scope"][key] == value for key, value in scope.items()))])

    def link_field(self, ref: FieldRef, assertion_id: str, *, reason: str,
                   relationship: str = "unresolved") -> dict[str, Any]:
        """Record a reading hypothesis; it is neither an identity merge nor a correction."""
        _text(reason)
        if relationship not in ("unresolved", "same_fact"):
            raise EvidenceError("invalid_relationship", "Use unresolved or same_fact.")
        if type(ref) is not FieldRef:
            raise EvidenceError("invalid_field_reference", "Use an exact catalog FieldRef.")
        self._catalog.field(ref.record_key, ref.pointer)
        self._get(self._assertions, assertion_id, "unknown_assertion")
        link_id = "link-" + hashlib.sha256(_json([ref.record_key, ref.pointer, assertion_id]).encode("utf-8")).hexdigest()
        data = {"link_id": link_id, "record_key": ref.record_key, "field_pointer": ref.pointer,
                "assertion_id": assertion_id, "relationship": relationship, "reason": reason,
                "relationship_origin": "caller_hypothesis"}
        self._insert(self._links, link_id, data)
        return copy.deepcopy(data)

    def trace_field(self, ref: FieldRef) -> dict[str, Any]:
        if type(ref) is not FieldRef:
            raise EvidenceError("invalid_field_reference", "Use an exact catalog FieldRef.")
        candidate = self._catalog.field(ref.record_key, ref.pointer)
        raw = candidate["candidates"][0]
        links = []
        for link in self._links.values():
            if (link["record_key"], link["field_pointer"]) != (ref.record_key, ref.pointer):
                continue
            assertion = self._assertions[link["assertion_id"]]
            relation = _scope_relation(raw["source_claimed_scope"], assertion["scope"])
            usable = raw["status"] == "candidate" and not _unknown_value(assertion["asserted_value"])
            raw_relation = ("equal" if _json(raw["source_value"]) == _json(assertion["asserted_value"]) else "different") if usable else "unknown"
            links.append({**link, "assertion": assertion, "source": self._sources[assertion["evidence_id"]],
                          "scope_relation": relation, "raw_value_relation": raw_relation,
                          "semantic_consistency": "not_determined", "resolution": "unresolved"})
        return copy.deepcopy({"record": self._catalog.record(ref.record_key), "field": candidate,
                              "links": links, "value": None, "decision_eligible": False})

    def compare_assertions(self, left_id: str, right_id: str, *, relationship: str = "unresolved",
                           reason: str | None = None) -> dict[str, Any]:
        if relationship not in ("unresolved", "same_fact"):
            raise EvidenceError("invalid_relationship", "Use unresolved or same_fact.")
        _text(reason, optional=relationship == "unresolved")
        left = self._get(self._assertions, left_id, "unknown_assertion")
        right = self._get(self._assertions, right_id, "unknown_assertion")
        if left_id == right_id:
            raise EvidenceError("duplicate_reference", "Repeated assertions are not independent evidence.")
        scope = _scope_relation(left["scope"], right["scope"])
        origin_scopes = [_scope_relation(item["scope"], self._sources[item["evidence_id"]]["scope"])
                         for item in (left, right)]
        identity = "different" if left["subject_key"] != right["subject_key"] or (
            left["variant_key"] is not None and right["variant_key"] is not None
            and left["variant_key"] != right["variant_key"]) else (
                "unknown" if left["variant_key"] is None or right["variant_key"] is None else "same")
        meaning_keys = ("field_key", "semantic", "unit", "conditions")
        meaning = "different" if any(left[key] is not None and right[key] is not None
                                      and _json(left[key]) != _json(right[key]) for key in meaning_keys) else (
            "unknown" if any(left[key] is None or right[key] is None for key in meaning_keys) else "same")
        raw_relation = "unknown" if any(_unknown_value(item["asserted_value"]) for item in (left, right)) else (
            "equal" if _json(left["asserted_value"]) == _json(right["asserted_value"]) else "different")
        if identity == "different":
            comparison = "different_subject_or_variant"
        elif scope["status"] == "different" or any(item["status"] == "different" for item in origin_scopes):
            comparison = "different_scope"
        elif meaning == "different":
            comparison = "different_meaning_unit_or_conditions"
        elif raw_relation == "unknown":
            comparison = "value_unknown"
        elif identity == "unknown" or meaning == "unknown" or scope["status"] == "unknown" or any(
                item["status"] == "unknown" for item in origin_scopes):
            comparison = "identity_meaning_or_scope_unknown"
        elif relationship != "same_fact":
            comparison = "relationship_unresolved"
        else:
            comparison = "raw_value_disagreement" if raw_relation == "different" else "agreeing_assertions"
        status = "conflict" if comparison == "raw_value_disagreement" else "unknown" if raw_relation == "unknown" else "candidate"
        return copy.deepcopy({"status": status, "value": None, "decision_eligible": False,
                              "assertions": [left, right], "comparison": comparison,
                              "relationship": relationship, "reason": reason, "identity_relation": identity,
                              "meaning_relation": meaning, "scope_relation": scope,
                              "source_scope_relations": origin_scopes, "raw_value_relation": raw_relation,
                              "semantic_contradiction": "not_determined", "resolution": "unresolved",
                              "source_independence": "not_determined"})

    def record_comparison(self, comparison_id: str, left_id: str, right_id: str, *,
                          reason: str, relationship: str = "same_fact") -> dict[str, Any]:
        """Persist an explicit comparison hypothesis without selecting either value."""
        assessment = self.compare_assertions(left_id, right_id, relationship=relationship, reason=reason)
        data = {"comparison_id": comparison_id, "assertion_ids": [left_id, right_id],
                "relationship": relationship, "reason": reason, "assessment": assessment,
                "authority": "caller_hypothesis"}
        self._insert(self._comparisons, comparison_id, data)
        return copy.deepcopy(data)

    def _targeted_record(self, value: Any, expected: type) -> dict[str, Any]:
        data = _record(value, expected)
        self._get(self._sources, data["evidence_id"], "unknown_evidence")
        for assertion_id in _ids(data["assertion_ids"]):
            self._get(self._assertions, assertion_id, "unknown_assertion")
        data["scope"] = _scope(data["scope"])
        data["authority"] = "caller_report_only"
        return data

    def add_revision(self, revision: RevisionEvent) -> dict[str, Any]:
        data = self._targeted_record(revision, RevisionEvent)
        _text(data["reason"])
        _time(data["effective_at"])
        for replacement_id in _ids(data["replacement_ids"], allow_empty=True):
            self._get(self._assertions, replacement_id, "unknown_assertion")
        if set(data["assertion_ids"]) & set(data["replacement_ids"]):
            raise EvidenceError("self_revision", "An assertion cannot replace itself.")
        self._insert(self._revisions, data["revision_id"], data)
        return copy.deepcopy(data)

    def add_coverage(self, coverage: RevisionCoverage) -> dict[str, Any]:
        data = self._targeted_record(coverage, RevisionCoverage)
        _text(data["method"])
        if _time(data["checked_from"], required=True) > _time(data["checked_through"], required=True):
            raise EvidenceError("invalid_interval", "Coverage end must not precede its start.")
        self._insert(self._coverage, data["coverage_id"], data)
        return copy.deepcopy(data)

    def _binding(self, binding: ClientBinding, assertion: dict[str, Any], at: datetime) -> dict[str, Any]:
        data = _record(binding, ClientBinding)
        _text(data["context_id"])
        data["scope"] = _scope(data["scope"])
        _ids(data["evidence_ids"], allow_empty=True)
        _ids(data["release_event_ids"], allow_empty=True)
        observed = _time(data["observed_at"])
        expires = _time(data["expires_at"])
        if observed is not None and expires is not None and expires <= observed:
            raise EvidenceError("invalid_interval", "Binding expiry must follow its observation.")
        sources = [self._get(self._sources, key, "unknown_evidence") for key in data["evidence_ids"]]
        relation = _scope_relation(data["scope"], assertion["scope"])
        source_relations = [_scope_relation(data["scope"], source["scope"]) for source in sources]
        reasons = []
        if relation["status"] != "same":
            reasons.append("client_scope_" + relation["status"])
        if not sources:
            reasons.append("client_observation_evidence_missing")
        if any(source["evidence_kind"] != "client_observed" or source["observed_at"] is None for source in sources):
            reasons.append("client_evidence_is_not_an_observation")
        if any(item["status"] != "same" for item in source_relations):
            reasons.append("client_evidence_scope_unresolved")
        if observed is None:
            reasons.append("client_observation_time_unknown")
        elif observed > at:
            reasons.append("client_observation_from_future")
        if any(_future_source(source, at) for source in sources):
            reasons.append("client_evidence_from_future")
        if expires is None:
            reasons.append("client_binding_expiry_unknown")
        elif at >= expires:
            reasons.append("client_binding_expired")
        if assertion["release_event_id"] is None or assertion["release_event_id"] not in data["release_event_ids"]:
            reasons.append("client_release_event_unbound")
        reasons.append("client_binding_not_authenticated")
        return {"record": data, "scope_relation": relation, "source_scope_relations": source_relations,
                "authority": "caller_report_only", "reasons": reasons}

    def explain(self, assertion_id: str, *, at: str, client: ClientBinding | None = None) -> dict[str, Any]:
        """Explain historical evidence and current-value blockers at an explicit time."""
        instant = _time(at, required=True)
        assertion = self._get(self._assertions, assertion_id, "unknown_assertion")
        source = self._sources[assertion["evidence_id"]]
        relation = _scope_relation(source["scope"], assertion["scope"])
        reasons = ["current_value_adjudication_not_implemented"]
        reasons.append("source_review_not_authenticated" if source["reviewed_at"] else "source_review_missing")
        if _future_source(source, instant):
            reasons.append("source_evidence_from_future")
        if relation["status"] != "same":
            reasons.append("source_assertion_scope_" + relation["status"])
        for key in ("variant_key", "semantic", "unit", "conditions", "release_event_id"):
            if assertion[key] is None:
                reasons.append(key + "_unknown")
        if _unknown_value(assertion["asserted_value"]):
            reasons.append("asserted_value_unknown")
        comparisons = []
        for item in self._comparisons.values():
            if assertion_id not in item["assertion_ids"]:
                continue
            available = not any(_future_source(self._sources[self._assertions[key]["evidence_id"]], instant)
                                for key in item["assertion_ids"])
            comparisons.append({**item, "availability": "recorded_inputs_available" if available else "evidence_from_future"})
        if any(item["availability"] == "recorded_inputs_available" and item["assessment"]["status"] == "conflict"
               for item in comparisons):
            reasons.append("reported_conflict_unresolved")
        if any(item["availability"] == "evidence_from_future" for item in comparisons):
            reasons.append("comparison_evidence_from_future")
        candidate_links = [{**link, "record": self._catalog.record(link["record_key"]),
                            "field": self._catalog.field(link["record_key"], link["field_pointer"])}
                           for link in self._links.values() if link["assertion_id"] == assertion_id]
        start_key = "observed_effective_at" if assertion["observed_effective_at"] else "announced_effective_at"
        start = _time(assertion[start_key])
        end = _time(assertion["valid_until"])
        if end is not None and instant >= end:
            temporal_status = "expired"
        elif start is None:
            temporal_status = "start_unknown"
        elif instant < start:
            temporal_status = "not_yet_effective"
        else:
            temporal_status = "inside_declared_window" if end else "end_unknown"
        if temporal_status != "inside_declared_window":
            reasons.append("assertion_" + temporal_status)
        revisions = []
        for revision in self._revisions.values():
            if assertion_id not in revision["assertion_ids"]:
                continue
            revision_source = self._sources[revision["evidence_id"]]
            checks = [_scope_relation(revision["scope"], assertion["scope"]),
                      _scope_relation(revision["scope"], revision_source["scope"])]
            effective = _time(revision["effective_at"])
            if any(item["status"] == "different" for item in checks):
                state = "different_scope"
            elif _future_source(revision_source, instant):
                state = "evidence_from_future"
            elif effective is None:
                state = "effective_time_unknown"
            elif instant < effective:
                state = "scheduled"
            else:
                state = "due_scope_unknown" if any(item["status"] == "unknown" for item in checks) else "due_unadjudicated"
            if state in ("effective_time_unknown", "due_scope_unknown", "due_unadjudicated"):
                reasons.append("revision_" + state)
            revisions.append({"record": revision, "source": revision_source, "scope_relations": checks, "state": state})
        coverage = []
        for item in self._coverage.values():
            if assertion_id not in item["assertion_ids"]:
                continue
            coverage_source = self._sources[item["evidence_id"]]
            checks = [_scope_relation(item["scope"], assertion["scope"]),
                      _scope_relation(item["scope"], coverage_source["scope"])]
            if any(check["status"] == "different" for check in checks):
                state = "different_scope"
            elif _future_source(coverage_source, instant):
                state = "evidence_from_future"
            elif _time(item["checked_through"]) > instant:
                state = "check_boundary_from_future"
            elif any(check["status"] == "unknown" for check in checks):
                state = "scope_unknown"
            elif start is None:
                state = "assertion_start_unknown"
            elif _time(item["checked_from"]) > start:
                state = "initial_gap"
            elif _time(item["checked_through"]) < instant:
                state = "outdated"
            elif _time(item["checked_from"]) > instant:
                state = "not_yet_covered"
            else:
                state = "reported_interval_covers_query"
            coverage.append({"record": item, "source": coverage_source, "scope_relations": checks, "state": state})
        coverage_status = "reported_interval_covers_query" if any(
            item["state"] == "reported_interval_covers_query" for item in coverage) else "unknown_or_incomplete"
        reasons.append("revision_coverage_not_authenticated" if coverage_status == "reported_interval_covers_query"
                       else "revision_coverage_unknown_or_incomplete")
        binding = self._binding(client, assertion, instant) if client is not None else None
        reasons.extend(binding["reasons"] if binding else ["client_binding_missing"])
        return copy.deepcopy({"status": "blocked", "value": None, "decision_eligible": False, "at": at,
                              "assertion": assertion, "source": source, "source_scope_relation": relation,
                              "comparisons": comparisons, "candidate_links": candidate_links,
                              "temporal": {"status": temporal_status, "start_origin": start_key if start else "unknown",
                                           "end_is_exclusive": True, "effective_in_client": "not_established"},
                              "revisions": revisions, "coverage": coverage, "coverage_status": coverage_status,
                              "client_binding": binding, "reasons": list(dict.fromkeys(reasons))})

    def require_current_value(self, assertion_id: str, *, at: str, client: ClientBinding | None = None) -> Any:
        explanation = self.explain(assertion_id, at=at, client=client)
        raise EvidenceError("current_value_unavailable", "Evidence inputs do not authorize a current-client decision value.",
                            reasons=tuple(explanation["reasons"]))
