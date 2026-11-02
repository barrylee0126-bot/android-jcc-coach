"""Read-only source records and field evidence, deliberately without adjudication."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


KINDS = ("hero", "trait", "item", "augment")
SCOPE_KEYS = ("game", "region", "season", "season_theme", "mode", "patch", "hotfix")
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RECORDS = 100_000
_MISSING = object()


class CatalogError(ValueError):
    """An input/query error with a stable code and no raw source text in the message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    kind: str
    path: str | Path
    records_pointer: str = ""
    source_url: str | None = None
    retrieved_at: str | None = None
    claimed_scope: Mapping[str, str | None] = field(default_factory=dict)
    claims_path: str | Path | None = None
    claims_pointer: str = ""


@dataclass(frozen=True)
class FieldRef:
    record_key: str
    pointer: str


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("duplicate_json_key", "Duplicate object keys would lose source values.")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise CatalogError("invalid_json", "Non-finite numbers are not valid source JSON.")


def _read_json(path: str | Path, max_bytes: int) -> tuple[Any, dict[str, Any]]:
    try:
        local_path = Path(path).expanduser().resolve()
        if not local_path.is_file():
            raise CatalogError("unreadable_file", "Input must be a readable local regular file.")
        with local_path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        if isinstance(error, CatalogError):
            raise
        raise CatalogError("unreadable_file", "Could not read the local JSON input.") from error
    if len(data) > max_bytes:
        raise CatalogError("file_too_large", "Input exceeds the configured per-file byte limit.")
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_reject_pairs,
                           parse_constant=_reject_constant)
        # Also reject a syntactically finite exponent such as 1e999 that overflows to inf.
        _canonical(value).encode("utf-8")
    except CatalogError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise CatalogError("invalid_json", "Expected finite UTF-8 JSON with supported nesting.") from error
    return value, {"local_path": str(local_path), "sha256": hashlib.sha256(data).hexdigest(),
                   "size_bytes": len(data)}


def _tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise CatalogError("invalid_pointer", "Use an RFC 6901 pointer, with '' for the root.")
    if re.search(r"~(?![01])", pointer):
        raise CatalogError("invalid_pointer", "Only ~0 and ~1 are valid pointer escapes.")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")] if pointer else []


def _resolve(value: Any, pointer: str) -> Any:
    for token in _tokens(pointer):
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            # Bound before int conversion, including adversarially long indices.
            if len(token) > len(str(len(value))) or int(token) >= len(value):
                return _MISSING
            value = value[int(token)]
        else:
            return _MISSING
    return value


def _timestamp(value: str | None) -> None:
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone missing")
    except (AttributeError, TypeError, ValueError) as error:
        raise CatalogError("invalid_timestamp", "retrieved_at must be an ISO timestamp with a timezone.") from error


def _verification() -> dict[str, Any]:
    return {"evidence": "third_party_only", "project_review": "not_reviewed",
            "version_applicability": "unknown", "target_client_binding": "unknown",
            "decision_eligible": False}


def _validate_spec(spec: SourceSpec) -> None:
    if not isinstance(spec, SourceSpec):
        raise CatalogError("invalid_source", "Each input must be a SourceSpec.")
    if not isinstance(spec.source_id, str) or not spec.source_id.strip() or spec.kind not in KINDS:
        raise CatalogError("invalid_source", "A nonempty source_id and supported kind are required.")
    if not isinstance(spec.path, (str, Path)) or (isinstance(spec.path, str) and not spec.path.strip()):
        raise CatalogError("invalid_source", "path must name a local JSON file.")
    if spec.claims_path is not None and (not isinstance(spec.claims_path, (str, Path))
                                        or isinstance(spec.claims_path, str) and not spec.claims_path.strip()):
        raise CatalogError("invalid_source", "claims_path must name a local JSON file.")
    _tokens(spec.records_pointer)
    _tokens(spec.claims_pointer)
    _timestamp(spec.retrieved_at)
    if not isinstance(spec.claimed_scope, Mapping) or set(spec.claimed_scope) - set(SCOPE_KEYS):
        raise CatalogError("invalid_scope", "claimed_scope contains an unsupported dimension.")
    if any(value is not None and (not isinstance(value, str) or not value.strip())
           for value in spec.claimed_scope.values()):
        raise CatalogError("invalid_scope", "Scope claims must be nonempty strings or null.")
    if spec.source_url is not None:
        try:
            url = urlsplit(spec.source_url)
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                raise ValueError("unsupported URL")
        except (AttributeError, TypeError, ValueError) as error:
            raise CatalogError("invalid_source_url", "Use an HTTP(S) source locator without credentials.") from error
    if spec.claims_path is None and spec.claims_pointer:
        raise CatalogError("invalid_source", "claims_pointer requires claims_path.")


def load_manifest(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> list[SourceSpec]:
    """Read a local manifest. Relative paths are relative to the manifest, never the CWD."""
    data, snapshot = _read_json(path, max_bytes)
    if not isinstance(data, dict) or set(data) != {"schema_version", "sources"}:
        raise CatalogError("invalid_manifest", "Manifest requires only schema_version and sources.")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise CatalogError("invalid_manifest", "Only manifest schema_version 1 is supported.")
    if not isinstance(data["sources"], list) or not data["sources"]:
        raise CatalogError("invalid_manifest", "sources must be a nonempty list.")
    base = Path(snapshot["local_path"]).parent
    specs = []
    allowed = set(SourceSpec.__dataclass_fields__)
    for item in data["sources"]:
        if not isinstance(item, dict) or set(item) - allowed or not {"source_id", "kind", "path"} <= set(item):
            raise CatalogError("invalid_manifest", "Source entries have missing or unsupported keys.")
        entry = dict(item)
        for key in ("path", "claims_path"):
            if key == "path" and entry.get(key) is None:
                raise CatalogError("invalid_manifest", "path must be a nonempty string.")
            if key in entry and entry[key] is not None:
                if not isinstance(entry[key], str) or not entry[key].strip():
                    raise CatalogError("invalid_manifest", "File paths must be nonempty strings.")
                candidate = Path(entry[key]).expanduser()
                entry[key] = candidate if candidate.is_absolute() else base / candidate
        spec = SourceSpec(**entry)
        _validate_spec(spec)
        specs.append(spec)
    return specs


class Catalog:
    """An in-memory catalog of unverified candidates; all public results are copies.

    No method fetches a URL, modifies a source, stores a dataset, resolves a rule,
    or binds a claimed version to a target client.
    """

    def __init__(self, sources: Iterable[SourceSpec], *, max_file_bytes: int = DEFAULT_MAX_BYTES,
                 max_records: int = DEFAULT_MAX_RECORDS):
        if type(max_file_bytes) is not int or max_file_bytes < 1 or type(max_records) is not int or max_records < 1:
            raise CatalogError("invalid_limit", "Limits must be positive integers.")
        self._sources: dict[str, dict[str, Any]] = {}
        self._metadata: dict[str, Any] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._raw: dict[str, dict[str, Any]] = {}
        loaded_at = datetime.now(timezone.utc).isoformat()
        for spec in sources:
            _validate_spec(spec)
            self._load(spec, max_file_bytes, max_records, loaded_at)
        if not self._sources:
            raise CatalogError("empty_sources", "At least one source snapshot is required.")

    def _load(self, spec: SourceSpec, max_bytes: int, max_records: int, loaded_at: str) -> None:
        data, snapshot = _read_json(spec.path, max_bytes)
        records = _resolve(data, spec.records_pointer)
        if not isinstance(records, list):
            raise CatalogError("invalid_records", "records_pointer must locate an array of objects.")
        if len(self._records) + len(records) > max_records:
            raise CatalogError("too_many_records", "Inputs exceed the configured total record limit.")
        if any(not isinstance(record, dict) for record in records):
            raise CatalogError("invalid_record", "Every source record must be a JSON object; none are skipped.")
        metadata: dict[str, Any] = {}
        metadata_snapshot = None
        if spec.claims_path is not None:
            raw_metadata, metadata_snapshot = _read_json(spec.claims_path, max_bytes)
            metadata = _resolve(raw_metadata, spec.claims_pointer)
            if not isinstance(metadata, dict):
                raise CatalogError("invalid_claims", "claims_pointer must locate an object.")
            metadata_snapshot["pointer"] = spec.claims_pointer

        # Metadata and operator-supplied source claims remain distinct candidates.
        scope_evidence: dict[str, list[dict[str, Any]]] = {key: [] for key in SCOPE_KEYS}
        for key, value in spec.claimed_scope.items():
            scope_evidence[key].append({"value": value, "origin": "operator_supplied_source_claim"})
        for key in SCOPE_KEYS:
            metadata_key = "theme" if key == "season_theme" and key not in metadata else key
            if metadata_key in metadata:
                scope_evidence[key].append({"value": metadata[metadata_key], "origin": "supplier_metadata",
                                           "sha256": metadata_snapshot["sha256"],
                                           "pointer": spec.claims_pointer + "/" + metadata_key})
        claimed_scope: dict[str, str | None] = {}
        scope_conflicts = []
        for key, claims in scope_evidence.items():
            values = {_canonical(claim["value"]) for claim in claims if claim["value"] is not None}
            usable = [claim["value"] for claim in claims
                      if isinstance(claim["value"], str) and claim["value"].strip()]
            claimed_scope[key] = usable[0] if len(values) == 1 and usable else None
            if len(values) > 1:
                scope_conflicts.append(key)
        context = {"source_id": spec.source_id, "kind": spec.kind, "sha256": snapshot["sha256"],
                   "records_pointer": spec.records_pointer, "source_url": spec.source_url,
                   "retrieved_at": spec.retrieved_at, "claimed_scope_input": dict(spec.claimed_scope),
                   "metadata_sha256": metadata_snapshot["sha256"] if metadata_snapshot else None,
                   "claims_pointer": spec.claims_pointer}
        snapshot_id = "snapshot-" + _digest(context)
        if snapshot_id in self._sources:
            raise CatalogError("duplicate_snapshot", "The same source snapshot and context were supplied twice.")
        source = {"snapshot_id": snapshot_id, "source_id": spec.source_id, "kind": spec.kind,
                  "source_url": spec.source_url, "source_url_status": "unverified_locator",
                  "snapshot": {**snapshot, "retrieved_at": spec.retrieved_at, "loaded_at": loaded_at,
                               "retrieved_at_origin": "operator_supplied" if spec.retrieved_at else "unknown"},
                  "records_pointer": spec.records_pointer, "record_count": len(records),
                  "metadata_snapshot": metadata_snapshot, "claimed_scope": claimed_scope,
                  "scope_evidence": scope_evidence, "scope_claim_conflicts": scope_conflicts,
                  "verification": _verification()}
        self._sources[snapshot_id] = source
        self._metadata[snapshot_id] = metadata
        for index, raw in enumerate(records):
            pointer = spec.records_pointer + "/" + str(index)
            record_key = "record-" + _digest([snapshot_id, pointer])
            external_id = raw.get("id")
            if type(external_id) not in (str, int) or external_id == "":
                external_id = None
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                name = None
            aliases = raw.get("aliases", [])
            aliases = [alias for alias in aliases if isinstance(alias, str)] if isinstance(aliases, list) else []
            self._records[record_key] = {
                "record_key": record_key, "snapshot_id": snapshot_id, "source_id": spec.source_id,
                "kind": spec.kind, "external_id": external_id, "name": name, "aliases": aliases,
                "record_pointer": pointer, "claimed_scope": claimed_scope,
                "scope_claim_conflicts": scope_conflicts, "status": "candidate",
                "verification": _verification(), "available_fields": sorted(raw),
                "identity_status": "unresolved_source_record", "variant_relation": "unknown"}
            self._raw[record_key] = raw

    def sources(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._sources.values()))

    def source_metadata(self, snapshot_id: str) -> dict[str, Any]:
        """Explicit local access to unchanged supplier claims, never project verification."""
        if snapshot_id not in self._metadata:
            raise CatalogError("unknown_snapshot", "Snapshot not found.")
        return copy.deepcopy(self._metadata[snapshot_id])

    def _record(self, record_key: str) -> dict[str, Any]:
        try:
            return self._records[record_key]
        except (KeyError, TypeError) as error:
            raise CatalogError("unknown_record", "Record key not found.") from error

    def record(self, record_key: str) -> dict[str, Any]:
        return copy.deepcopy(self._record(record_key))

    def raw_record(self, record_key: str) -> dict[str, Any]:
        """Return an explicit copy of the original local record, without normalizing values."""
        self._record(record_key)
        return copy.deepcopy(self._raw[record_key])

    def find(self, *, kind: str | None = None, name: str | None = None,
             external_id: str | int | None = None, text: str | None = None,
             source_id: str | None = None, claimed_scope: Mapping[str, str | None] | None = None
             ) -> list[dict[str, Any]]:
        """Exact identity filters, optional name/alias substring, no entity merge or fallback.

        claimed_scope filters supplier claims only; null matches unknown, not any version.
        """
        if kind is not None and kind not in KINDS:
            raise CatalogError("invalid_kind", "Unsupported kind.")
        if external_id is not None and type(external_id) not in (str, int):
            raise CatalogError("invalid_external_id", "external_id must be a string or integer.")
        if any(value is not None and not isinstance(value, str) for value in (name, text, source_id)):
            raise CatalogError("invalid_query", "name, text and source_id must be strings.")
        if claimed_scope is not None:
            if not isinstance(claimed_scope, Mapping) or set(claimed_scope) - set(SCOPE_KEYS):
                raise CatalogError("invalid_scope", "Unsupported scope filter.")
            if any(value is not None and not isinstance(value, str) for value in claimed_scope.values()):
                raise CatalogError("invalid_scope", "Scope filters must be strings or null.")
        result = []
        for record in self._records.values():
            if kind is not None and record["kind"] != kind:
                continue
            if name is not None and record["name"] != name:
                continue
            if external_id is not None and _canonical(record["external_id"]) != _canonical(external_id):
                continue
            if source_id is not None and record["source_id"] != source_id:
                continue
            if text is not None and not any(text.casefold() in value.casefold()
                                            for value in [record["name"] or "", *record["aliases"]]):
                continue
            if claimed_scope is not None and any(record["claimed_scope"][key] != value
                                                 for key, value in claimed_scope.items()):
                continue
            result.append(copy.deepcopy(record))
        return result

    def field(self, record_key: str, pointer: str) -> dict[str, Any]:
        """Read a candidate value and its exact JSON location; resolved value stays null."""
        record = self._record(record_key)
        source = self._sources[record["snapshot_id"]]
        value = _resolve(self._raw[record_key], pointer)
        if value is _MISSING:
            state = "missing"
        elif value is None:
            state = "null"
        elif isinstance(value, str) and not value.strip():
            state = "empty_text"
        elif isinstance(value, (list, dict)) and not value:
            state = "empty_container"
        else:
            state = "present"
        status = "unknown" if state in ("missing", "null", "empty_text") else "candidate"
        evidence = {
            "assertion_id": "assertion-" + _digest([record_key, pointer]),
            "record_key": record_key, "field_pointer": pointer,
            "source_value": None if value is _MISSING else copy.deepcopy(value),
            "value_state": state, "status": status,
            "origin": {"snapshot_id": record["snapshot_id"], "source_id": source["source_id"],
                       "source_url": source["source_url"], **copy.deepcopy(source["snapshot"]),
                       "record_pointer": record["record_pointer"],
                       "json_pointer": record["record_pointer"] + pointer,
                       "locator_exists": state != "missing"},
            "source_claimed_scope": copy.deepcopy(source["claimed_scope"]),
            "scope_evidence": copy.deepcopy(source["scope_evidence"]),
            "scope_claim_conflicts": list(source["scope_claim_conflicts"]),
            "conditions": "unknown", "unit": None, "semantic_interpretation": "not_performed",
            "verification": _verification()}
        return {"status": status, "value": None, "candidates": [evidence],
                "reason": "no_independent_field_verification" if status == "candidate" else "source_" + state,
                "decision_eligible": False}

    def compare_fields(self, references: Sequence[FieldRef], *, relationship: str = "unresolved",
                       reason: str | None = None) -> dict[str, Any]:
        """Compare raw claims, without deriving semantic equality or selecting a winner.

        'same_fact' is the caller's explicit comparison hypothesis and requires a reason.
        Default differences remain possible variant/scope differences, not factual conflicts.
        """
        if relationship not in ("unresolved", "same_fact"):
            raise CatalogError("invalid_relationship", "Use unresolved or same_fact.")
        if relationship == "same_fact" and (not isinstance(reason, str) or not reason.strip()):
            raise CatalogError("missing_comparison_reason", "same_fact requires an explicit comparison reason.")
        if not references or any(not isinstance(ref, FieldRef) for ref in references):
            raise CatalogError("invalid_comparison", "At least one FieldRef is required.")
        for ref in references:
            if not isinstance(ref.record_key, str):
                raise CatalogError("invalid_comparison", "Each FieldRef needs a string record key.")
            _tokens(ref.pointer)
        if len({(ref.record_key, ref.pointer) for ref in references}) != len(references):
            raise CatalogError("duplicate_reference", "Repeated references are not independent evidence.")
        candidates = [self.field(ref.record_key, ref.pointer)["candidates"][0] for ref in references]
        present = [candidate for candidate in candidates if candidate["status"] == "candidate"]
        values = {_canonical(candidate["source_value"]) for candidate in present}
        scopes = [candidate["source_claimed_scope"] for candidate in candidates]
        different_scope = any(len({scope[key] for scope in scopes if scope[key] is not None}) > 1
                              for key in SCOPE_KEYS)
        scope_status = "different_claimed_scope" if different_scope else (
            "unknown" if any(None in scope.values() for scope in scopes)
            or any(candidate["scope_claim_conflicts"] for candidate in candidates) else "same_claimed_scope")
        if not present:
            status, comparison = "unknown", "no_usable_candidate"
        elif different_scope:
            status, comparison = "candidate", "different_claimed_scope"
        elif len(values) <= 1:
            status = "candidate"
            comparison = "agreeing_candidates" if len(present) > 1 else "single_candidate"
        elif relationship == "same_fact":
            status, comparison = "conflict", "raw_value_disagreement"
        else:
            status, comparison = "candidate", "variant_or_scope_difference"
        return {"status": status, "value": None, "candidates": candidates, "comparison": comparison,
                "relationship": relationship, "comparison_reason": reason, "scope_status": scope_status,
                "completeness": "incomplete" if len(present) < len(candidates) else "source_values_present",
                "semantic_contradiction": "not_determined", "project_resolution": "unresolved",
                "decision_eligible": False}

    def require_verified_value(self, record_key: str, pointer: str) -> Any:
        """Fail closed: v1 has no adjudicated fields or current-client applicability proof."""
        self.field(record_key, pointer)
        raise CatalogError("unverified_field", "Candidate evidence is not an applicable verified decision value.")

    def summary(self) -> dict[str, Any]:
        snapshots = []
        for source in self._sources.values():
            records = [record for record in self._records.values() if record["snapshot_id"] == source["snapshot_id"]]
            identities = Counter(_canonical(record["external_id"]) for record in records if record["external_id"] is not None)
            snapshots.append({"snapshot_id": source["snapshot_id"], "source_id": source["source_id"],
                              "kind": source["kind"], "record_count": len(records),
                              "distinct_external_ids": len(identities),
                              "duplicate_id_groups": sum(count > 1 for count in identities.values()),
                              "extra_records_with_duplicate_id": sum(count - 1 for count in identities.values()),
                              "records_without_external_id": sum(record["external_id"] is None for record in records),
                              "records_without_name": sum(record["name"] is None for record in records),
                              "claimed_scope": copy.deepcopy(source["claimed_scope"]),
                              "scope_claim_conflicts": list(source["scope_claim_conflicts"])})
        return {"source_snapshots": len(self._sources), "total_records": len(self._records),
                "by_kind": dict(Counter(record["kind"] for record in self._records.values())),
                "snapshots": snapshots, "independently_verified_fields": 0,
                "decision_eligible_records": 0, "target_client_binding": "unknown"}
