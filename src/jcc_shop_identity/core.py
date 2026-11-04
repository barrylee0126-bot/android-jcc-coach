"""Match existing shop text to one validated, caller-supplied catalog snapshot.

The result is a lexical development candidate, not a verified unit identity.
Neither a content hash nor a caller-declared freshness status authenticates a
frame, a catalog, a client version, or the currently visible game.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from jcc_catalog_snapshot import analyze_request as analyze_catalog
from jcc_shop_probe import display_text_binding_sha256, display_text_slot_binding_sha256
from jcc_shop_probe.probe import _normalize

REQUEST_FORMAT = "jcc-shop-identity-request/v1"
RESULT_FORMAT = "jcc-shop-identity-result/v1"
_FRAME = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MIN_RAW_SCORE = 0.5
_SAFETY_FORMAT = "shop-display-text-sanitization-evidence/v1"
_SAFETY_POLICY = "NFKC_then_remove_whitespace_after_rejecting_Cc_Cf_Cs"
_REQUEST_FIELDS = {"format_version", "display_text", "catalog_request", "catalog_namespace",
                   "catalog_context", "frame_context"}


def catalog_namespace(catalog_request: Any) -> str:
    """Hash exactly the caller-supplied JSON request, not its claimed source.

    Stable keys are scoped by this complete request, which prevents the same
    identifier from silently merging across different submitted snapshots.
    A hash is a content binding, not authentication or proof of completeness.
    """
    try:
        encoded = json.dumps(catalog_request, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("catalog request is not strict JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_text(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 256
            and not any(unicodedata.category(char) in {"Cc", "Cs", "Cf"} for char in value))


def _normalized(value: Any) -> str | None:
    if not _safe_text(value):
        return None
    normalized = _normalize(value)
    return normalized if _safe_text(normalized) else None


def _unknown_slot(number: int, reason: str) -> dict[str, Any]:
    return {"slot": number, "catalog_match_status": "unknown", "candidate_ref": None,
            "match_count": None, "identity_status": "unknown", "reasons": [reason]}


def _result(slots: list[dict[str, Any]], *, frame_ref: str | None = None,
            namespace: str | None = None, frame_scope: str | None = None,
            frame_sample_kind: str | None = None, catalog_data_kind: str | None = None,
            issues: list[str] | None = None) -> dict[str, Any]:
    return {
        "format_version": RESULT_FORMAT,
        "usage": "offline_development_only",
        "frame_ref": frame_ref,
        "frame_scope": frame_scope,
        "frame_sample_kind": frame_sample_kind,
        "catalog_data_kind": catalog_data_kind,
        "sample_kind_validation": "caller_declaration_only",
        "catalog_namespace": namespace,
        "slots": slots,
        "issues": issues or [],
        "identity_status": "unknown",
        "source_authentication": "not_authenticated",
        "sanitization_evidence_authentication": "not_authenticated",
        "target_client_binding": "unknown",
        "source_version_applicability": "unknown",
        "freshness_validation": "caller_declaration_only",
        "catalog_coverage_validation": "caller_declaration_only",
        "catalog_conflict_validation": "caller_declaration_only",
        "decision_eligible": False,
        "action_advice": None,
        "accuracy": None,
        "human_annotations": 0,
        "independent_reviews": 0,
    }


def _failure(reason: str) -> dict[str, Any]:
    return _result([_unknown_slot(i, reason) for i in range(1, 6)], issues=[reason])


def _score_ok(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and _MIN_RAW_SCORE <= value <= 1
    except (TypeError, OverflowError):
        return False


def _display_candidate(slot: Any) -> tuple[str | None, str | None]:
    if type(slot) is not dict:
        return None, "invalid_slot"
    if slot.get("status") != "candidate":
        return None, "display_text_unknown"
    if (slot.get("display_kind") != "unclassified" or slot.get("identity_status") != "unknown"
            or slot.get("price_status") != "unknown" or slot.get("buyability") != "unknown"
            or slot.get("trait_binding") != "unknown" or slot.get("reasons") not in ([], ())):
        return None, "display_contract_invalid"
    raw = slot.get("display_text_normalized_candidate")
    text = _normalized(raw)
    if text is None or text != raw:
        return None, "display_text_invalid"
    views = slot.get("variants")
    if type(views) not in (list, tuple) or len(views) != 2:
        return None, "variant_contract_invalid"
    scales = []
    for view in views:
        if type(view) is not dict or type(view.get("scale")) is not int:
            return None, "variant_contract_invalid"
        scales.append(view["scale"])
        if (view.get("status") != "candidate" or view.get("reason_codes") not in ([], ())
                or not _score_ok(view.get("raw_ocr_score"))):
            return None, "variant_low_or_unknown"
    if sorted(scales) != [1, 2]:
        return None, "variant_contract_invalid"
    return text, None


def _safety_slots(display: dict, evidence: Any) -> tuple[dict[int, dict] | None, str | None]:
    """Check the separately versioned safety wrapper against the whole summary.

    The wrapper is still a caller-supplied declaration, not source proof.
    """
    if evidence is None:
        return None, "sanitization_evidence_missing"
    fields = {"format", "usage", "display_summary_format", "display_summary_binding_sha256",
              "frame_ref", "source_sha256", "layout_id", "source_authentication",
              "normalization_policy", "slots", "accuracy", "human_annotations",
              "independent_reviews", "actionable"}
    if type(evidence) is not dict or set(evidence) != fields:
        return None, "sanitization_evidence_invalid"
    if (evidence["format"] != _SAFETY_FORMAT or evidence["usage"] != "offline_development_only"
            or evidence["display_summary_format"] != display["format"]
            or evidence["frame_ref"] != display["frame_ref"]
            or evidence["source_sha256"] != display["source_sha256"]
            or not _safe_text(display.get("layout_id")) or not display["layout_id"].strip()
            or evidence["layout_id"] != display["layout_id"]
            or evidence["source_authentication"] != "not_authenticated"
            or evidence["normalization_policy"] != _SAFETY_POLICY
            or evidence["accuracy"] is not None or evidence["actionable"] is not False
            or type(evidence["human_annotations"]) is not int or evidence["human_annotations"] != 0
            or type(evidence["independent_reviews"]) is not int or evidence["independent_reviews"] != 0):
        return None, "sanitization_evidence_invalid"
    try:
        binding = display_text_binding_sha256(display)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None, "sanitization_binding_invalid"
    if evidence["display_summary_binding_sha256"] != binding:
        return None, "sanitization_binding_mismatch"
    rows = evidence["slots"]
    if (type(rows) is not list or len(rows) != 5
            or any(type(row) is not dict or type(row.get("slot")) is not int for row in rows)
            or sorted(row["slot"] for row in rows) != [1, 2, 3, 4, 5]):
        return None, "sanitization_slots_invalid"
    return {row["slot"]: row for row in rows}, None


def _safe_views(display: dict, slot: int, evidence: dict) -> str | None:
    """Reject unless both named views explicitly declare safe raw text."""
    if set(evidence) != {"slot", "display_slot_binding_sha256", "variants"}:
        return "sanitization_slot_invalid"
    try:
        expected = display_text_slot_binding_sha256(display, slot)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "sanitization_slot_binding_invalid"
    if evidence["display_slot_binding_sha256"] != expected:
        return "sanitization_slot_binding_mismatch"
    views = evidence["variants"]
    if type(views) is not list or len(views) != 2:
        return "sanitization_variants_invalid"
    scales = []
    for view in views:
        if (type(view) is not dict or set(view) != {"scale", "raw_text_safety", "contains_control_or_format"}
                or type(view["scale"]) is not int):
            return "sanitization_variants_invalid"
        scales.append(view["scale"])
        if view["raw_text_safety"] != "safe" or view["contains_control_or_format"] is not False:
            return "sanitization_view_not_safe"
    if sorted(scales) != [1, 2]:
        return "sanitization_variants_invalid"
    return None


def _conflict_exclusions(value: Any, namespace: str, adapted: dict) -> set[str] | None:
    """Validate declared scoped conflicts and return affected hero keys.

    This checks only the declared ledger's structure and references; it cannot
    establish completeness or determine whether a conflict is really non-name.
    """
    if value == "none_declared":
        return set()
    if type(value) is not dict or set(value) != {"status", "catalog_namespace", "records"}:
        return None
    records = value["records"]
    if (value["status"] != "declared_scoped" or value["catalog_namespace"] != namespace
            or type(records) is not list or not 1 <= len(records) <= 256):
        return None
    hero_traits = adapted["hero_traits"]
    valid_keys = {"hero": hero_traits, "trait": adapted["trait_thresholds"],
                  "item": adapted["item_components"]}
    excluded: set[str] = set()
    for conflict in records:
        if type(conflict) is not dict or set(conflict) != {"entity_kind", "key", "field_scope"}:
            return None
        kind, key = conflict["entity_kind"], conflict["key"]
        if (type(kind) is not str or kind not in valid_keys or type(key) is not str
                or key not in valid_keys[kind] or conflict["field_scope"] != "not_display_name_or_alias"):
            return None
        if kind == "hero":
            excluded.add(key)
        elif kind == "trait":
            excluded.update(hero for hero, traits in hero_traits.items() if key in traits)
    return excluded


def resolve(request: Any) -> dict[str, Any]:
    """Return per-slot lexical candidates under explicit, limited conditions.

    The 25C request is revalidated instead of trusting a caller-modified result.
    Neither a matching namespace nor `fresh` validates the underlying evidence.
    """
    if type(request) is not dict or set(request) not in (
            _REQUEST_FIELDS, _REQUEST_FIELDS | {"sanitization_evidence"}):
        return _failure("request_invalid")
    if request["format_version"] != REQUEST_FORMAT:
        return _failure("format_unsupported")
    context = request["frame_context"]
    if type(context) is not dict or set(context) != {"frame_ref", "freshness", "scope", "sample_kind"}:
        return _failure("frame_context_invalid")
    scope = context["scope"]
    freshness = context["freshness"]
    sample_kind = context["sample_kind"]
    if scope not in ("current_frame", "historical_frame"):
        return _failure("frame_scope_invalid")
    if freshness not in ("fresh", "stale", "unknown"):
        return _failure("freshness_invalid")
    if sample_kind not in ("synthetic", "replay", "unknown"):
        return _failure("frame_sample_kind_invalid")
    if sample_kind == "replay" and scope != "historical_frame":
        return _failure("frame_kind_scope_conflict")
    if scope == "current_frame" and freshness != "fresh":
        return _failure("freshness_not_declared_fresh")
    frame_ref = context["frame_ref"]
    if type(frame_ref) is not str or _FRAME.fullmatch(frame_ref) is None:
        return _failure("frame_ref_invalid")

    display = request["display_text"]
    if (type(display) is not dict or display.get("format") != "shop-display-text-probe-v1"
            or display.get("usage") != "offline_development_only"
            or display.get("frame_ref") != frame_ref
            or display.get("source_sha256") != frame_ref.removeprefix("sha256:")
            or display.get("source_authentication") != "not_authenticated"
            or display.get("actionable") is not False or display.get("issues") not in ([], ())):
        return _failure("display_contract_invalid")
    entries = display.get("slots")
    if type(entries) not in (list, tuple) or len(entries) != 5:
        return _failure("slots_invalid")
    if any(type(row) is not dict or type(row.get("slot")) is not int for row in entries):
        return _failure("slots_invalid")
    if sorted(row["slot"] for row in entries) != [1, 2, 3, 4, 5]:
        return _failure("slots_invalid")
    safety, safety_issue = _safety_slots(display, request.get("sanitization_evidence"))
    if safety_issue is not None:
        return _failure(safety_issue)

    catalog_context = request["catalog_context"]
    if (type(catalog_context) is not dict
            or set(catalog_context) != {"coverage", "conflicts"}):
        return _failure("catalog_context_invalid")
    conflicts = catalog_context["conflicts"]
    if conflicts != "none_declared" and type(conflicts) is not dict:
        return _failure("catalog_conflicts_unknown_or_present")
    if catalog_context["coverage"] != "declared_complete":
        return _failure("catalog_coverage_unknown")

    catalog = request["catalog_request"]
    try:
        adapted = analyze_catalog(catalog)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _failure("catalog_invalid")
    if adapted.get("status") != "candidate":
        return _failure("catalog_unknown")
    try:
        calculated_namespace = catalog_namespace(catalog)
    except ValueError:
        return _failure("catalog_invalid")
    if request["catalog_namespace"] != calculated_namespace:
        return _failure("catalog_namespace_mismatch")
    excluded = _conflict_exclusions(conflicts, calculated_namespace, adapted)
    if excluded is None:
        return _failure("catalog_conflicts_not_safely_scoped")

    index: dict[str, list[str]] = {}
    for display_name, keys in adapted["name_lookup"]["heroes"].items():
        normalized = _normalized(display_name)
        if normalized is None:
            return _failure("catalog_name_invalid")
        index.setdefault(normalized, []).extend(keys)

    results = []
    for slot in sorted(entries, key=lambda row: row["slot"]):
        number = slot["slot"]
        issue = _safe_views(display, number, safety[number])
        if issue is not None:
            results.append(_unknown_slot(number, issue))
            continue
        text, issue = _display_candidate(slot)
        if issue is not None:
            results.append(_unknown_slot(number, issue))
            continue
        matches = index.get(text, [])
        if not matches:
            results.append(_unknown_slot(number, "no_catalog_match"))
        elif len(matches) != 1:
            result = _unknown_slot(number, "catalog_match_ambiguous")
            result["match_count"] = len(matches)
            results.append(result)
        elif matches[0] in excluded:
            results.append(_unknown_slot(number, "catalog_conflict_affects_match"))
        else:
            results.append({"slot": number, "catalog_match_status": "candidate",
                            "candidate_ref": {"catalog_namespace": calculated_namespace,
                                              "hero_key": matches[0]},
                            "match_count": 1, "identity_status": "unknown",
                            "reasons": ["source_and_current_version_unverified"]})
    return _result(results, frame_ref=frame_ref, namespace=calculated_namespace, frame_scope=scope,
                   frame_sample_kind=sample_kind, catalog_data_kind=adapted["data_kind"])
