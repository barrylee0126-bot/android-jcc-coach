"""Convert local source identifiers into opaque catalog keys without making rules.

This module is deliberately a pre-adapter.  It has no I/O, no network access,
and accepts only an explicitly ``unknown`` source state.  It may preserve a
private raw-id to canonical-key table for a caller, but cannot authenticate the
origin, version, mode, patch, or applicability of those records.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any
import unicodedata
import re

REQUEST_FORMAT = "jcc-catalog-pre-adapter-request/v1"
RESULT_FORMAT = "jcc-catalog-pre-adapter-result/v1"
_KINDS = ("traits", "heroes", "items")
_MAX_ROWS = {"traits": 256, "heroes": 512, "items": 1024}
_MAX_ID = 256
_MAX_NAME = 256
_MAX_REFS = 32
_MAX_ITEM_COMPONENTS = 8  # Matches jcc_catalog_snapshot.MAX_COMPONENTS.
_SINGULAR = {"traits": "trait", "heroes": "hero", "items": "item"}
_NAMESPACE = re.compile(r"sha256:[0-9a-f]{64}")


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message_zh": message}


def _unknown(issues: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "format_version": RESULT_FORMAT,
        "status": "unknown",
        "source_status": "unknown",
        "snapshot_namespace": None,
        "decision_eligible": False,
        "action_advice": None,
        "private_key_map": None,
        "records": None,
        "coverage": None,
        "issues": issues,
        "notes_zh": [
            "本模块只转换调用方明确提供的本地记录，不读取网络或认证来源。",
            "来源、版本、模式、地区、补丁和当前对局适用性均保持 unknown。",
            "输出不能直接喂给需要已知来源绑定的 catalog adapter，更不能产生行动建议。",
        ],
    }


def _shape(value: Any, keys: set[str], path: str, issues: list[dict[str, str]]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "对象缺少必需字段、含未知字段或类型不正确。"))
        return False
    return True


def _text(value: Any, maximum: int) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def canonical_key(kind: str, source_id: str, snapshot_namespace: str) -> str:
    """Return an opaque ASCII key scoped to one caller-declared local snapshot."""
    if kind not in _KINDS or not _text(source_id, _MAX_ID) or type(snapshot_namespace) is not str or not _NAMESPACE.fullmatch(snapshot_namespace):
        raise ValueError("kind, source_id, or snapshot_namespace invalid")
    material = snapshot_namespace + "\x00" + kind + ":" + source_id
    return f"src-{_SINGULAR[kind]}-{sha256(material.encode('utf-8')).hexdigest()}"


def _source(value: Any, issues: list[dict[str, str]]) -> str | None:
    if not _shape(value, {"status", "snapshot_namespace"}, "$.source", issues):
        return None
    if value["status"] != "unknown":
        issues.append(_issue("source_must_be_unknown", "$.source.status", "预适配转换不得把调用方声明升级为已认证来源。"))
        return None
    namespace = value["snapshot_namespace"]
    if type(namespace) is not str or _NAMESPACE.fullmatch(namespace) is None:
        issues.append(_issue("snapshot_namespace_invalid", "$.source.snapshot_namespace", "快照命名空间须是调用方声明的 sha256: 小写摘要；它不认证来源。"))
        return None
    return namespace


def _raw_rows(request: dict[str, Any], kind: str, issues: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows = request[kind]
    if type(rows) is not list or len(rows) > _MAX_ROWS[kind]:
        issues.append(_issue("rows_invalid", f"$.{kind}", "记录必须是有界列表。"))
        return []
    wanted = {
        "traits": {"source_id", "display_name", "thresholds"},
        "heroes": {"source_id", "display_name", "trait_source_ids"},
        "items": {"source_id", "display_name", "component_source_ids"},
    }[kind]
    parsed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if type(row) is not dict or set(row) != wanted:
            # A malformed row must not erase the valid rows around it.
            partial_id = row.get("source_id") if type(row) is dict and _text(row.get("source_id"), _MAX_ID) else None
            parsed.append({"index": index, "source_id": partial_id, "display_name": None,
                           "refs": [], "reasons": ["invalid_record"]})
            continue
        source_id, display_name = row["source_id"], row["display_name"]
        refs = row["thresholds"] if kind == "traits" else row["trait_source_ids" if kind == "heroes" else "component_source_ids"]
        reasons: list[str] = []
        safe_source_id = source_id if _text(source_id, _MAX_ID) else None
        safe_name = display_name if _text(display_name, _MAX_NAME) else None
        raw_refs = list(refs) if type(refs) is list and len(refs) <= _MAX_REFS else []
        safe_refs = raw_refs if kind == "traits" or all(_text(ref, _MAX_ID) for ref in raw_refs) else []
        if safe_source_id is None or safe_name is None:
            reasons.append("invalid_record")
        if type(refs) is not list or len(refs) > _MAX_REFS:
            reasons.append("reference_invalid")
        if kind == "traits":
            if not safe_refs:
                reasons.append("thresholds_missing")
            elif (not all(type(value) is int and not isinstance(value, bool) and 1 <= value <= 99 for value in safe_refs)
                  or safe_refs != sorted(set(safe_refs))):
                reasons.append("thresholds_invalid")
        else:
            if type(refs) is list and not all(_text(ref, _MAX_ID) for ref in raw_refs):
                reasons.append("reference_invalid")
            if kind == "items" and len(raw_refs) > _MAX_ITEM_COMPONENTS:
                reasons.append("component_references_too_many")
            if kind == "heroes" and not safe_refs:
                reasons.append("trait_references_empty")
            if kind == "heroes" and all(_text(ref, _MAX_ID) for ref in safe_refs) and len(set(safe_refs)) != len(safe_refs):
                reasons.append("trait_references_duplicate")
        parsed.append({"index": index, "source_id": safe_source_id, "display_name": safe_name,
                       "refs": safe_refs, "reasons": list(dict.fromkeys(reasons))})
    return parsed


def _conflicts(value: Any, issues: list[dict[str, str]]) -> tuple[dict[tuple[str, str], list[str]], list[dict[str, Any]]]:
    if type(value) is not list or len(value) > 256:
        issues.append(_issue("conflicts_invalid", "$.conflicts", "冲突记录必须是有界列表。"))
        return {}, []
    parsed: list[dict[str, Any]] = []
    singular = {"trait": "traits", "hero": "heroes", "item": "items"}
    for index, row in enumerate(value):
        entity_kind = row.get("entity_kind") if type(row) is dict else None
        source_id = row.get("source_id") if type(row) is dict else None
        target = (singular[entity_kind], source_id) if type(entity_kind) is str and entity_kind in singular and _text(source_id, _MAX_ID) else None
        valid_shape = type(row) is dict and set(row) == {"conflict_id", "entity_kind", "source_id", "field"}
        conflict_id = row.get("conflict_id") if type(row) is dict else None
        field = row.get("field") if type(row) is dict else None
        valid = valid_shape and _text(conflict_id, _MAX_ID) and target is not None and _text(field, _MAX_NAME)
        if not valid:
            output: dict[str, Any] = {"input_index": index, "state": "blocked", "reasons": ["conflict_invalid"], "target": target}
            if _text(conflict_id, _MAX_ID):
                output["conflict_id"] = conflict_id
            parsed.append(output)
            continue
        parsed.append({"input_index": index, "conflict_id": conflict_id, "entity_kind": entity_kind,
                       "source_id": source_id, "field": field, "state": "retained_unresolved", "reasons": [], "target": target})
    counts: dict[str, int] = {}
    for row in parsed:
        if row.get("conflict_id"):
            counts[row["conflict_id"]] = counts.get(row["conflict_id"], 0) + 1
    attached: dict[tuple[str, str], list[str]] = {}
    for row in parsed:
        if row.get("conflict_id") and counts[row["conflict_id"]] > 1:
            row["state"] = "blocked"
            row["reasons"].append("duplicate_conflict_id")
            attached.setdefault(row["target"], []).append(row["conflict_id"])
        elif row.get("conflict_id"):
            attached.setdefault(row["target"], []).append(row["conflict_id"])
        elif row.get("target"):
            # A malformed conflict still names this entity. Do not let the
            # missing/invalid conflict ID turn that entity into consumable data.
            attached.setdefault(row["target"], []).append(f"invalid-conflict-{row['input_index']}")
        row.pop("target", None)
    return attached, parsed


def _add_reason(row: dict[str, Any], reason: str) -> None:
    if reason not in row["reasons"]:
        row["reasons"].append(reason)
    row["state"] = "blocked"


def _cycle_ids(items: list[dict[str, Any]]) -> set[str]:
    """Iteratively find actual SCC cycle members in the complete safe graph."""
    counts: dict[str, int] = {}
    for row in items:
        if row.get("source_id"):
            counts[row["source_id"]] = counts.get(row["source_id"], 0) + 1
    nodes = {row["source_id"]: row for row in items
             if row.get("source_id") and counts[row["source_id"]] == 1 and "reference_invalid" not in row["reasons"]}
    graph = {node: [ref for ref in row["source_refs"] if ref in nodes] for node, row in nodes.items()}
    reverse = {node: [] for node in nodes}
    for node, refs in graph.items():
        for ref in refs:
            reverse[ref].append(node)

    # Iterative Kosaraju: avoids RecursionError for the contractual 1024-item chain.
    seen: set[str] = set()
    finish: list[str] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finish.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for ref in reversed(sorted(graph[node])):
                if ref not in seen:
                    stack.append((ref, False))

    assigned: set[str] = set()
    cycles: set[str] = set()
    for start in reversed(finish):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for ref in reverse[node]:
                if ref not in assigned:
                    assigned.add(ref)
                    stack.append(ref)
        if len(component) > 1 or start in graph[start]:
            cycles.update(component)
    return cycles


def analyze_request(request: Any) -> dict[str, Any]:
    """Return a conservative, private raw-ID/key table and explicit coverage ledger."""
    issues: list[dict[str, str]] = []
    if not _shape(request, {"format_version", "source", "traits", "heroes", "items", "conflicts"}, "$", issues):
        return _unknown(issues)
    if request["format_version"] != REQUEST_FORMAT:
        issues.append(_issue("format_unsupported", "$.format_version", "请求格式版本不受支持。"))
    snapshot_namespace = _source(request["source"], issues)
    parsed = {kind: _raw_rows(request, kind, issues) for kind in _KINDS}
    conflicts, conflict_ledger = _conflicts(request["conflicts"], issues)
    if issues or snapshot_namespace is None:
        return _unknown(issues)

    # Duplicate raw IDs and a theoretical digest collision both block every affected record.
    raw_counts = {kind: {} for kind in _KINDS}
    generated: dict[str, str] = {}
    for kind in _KINDS:
        for entry in parsed[kind]:
            source_id = entry["source_id"]
            if source_id is None:
                continue
            raw_counts[kind][source_id] = raw_counts[kind].get(source_id, 0) + 1
            key = canonical_key(kind, source_id, snapshot_namespace)
            prior = generated.get(key)
            if prior is None:
                generated[key] = f"{kind}:{source_id}"
            elif prior != f"{kind}:{source_id}":
                generated[key] = "__collision__"

    records: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _KINDS}
    by_id: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in _KINDS}
    for kind in _KINDS:
        for entry in parsed[kind]:
            source_id = entry["source_id"]
            if source_id is None:
                output = {"input_index": entry["index"], "state": "blocked", "reasons": entry["reasons"]}
            else:
                reasons = list(entry["reasons"])
                if raw_counts[kind].get(source_id, 0) > 1:
                    reasons.append("duplicate_source_id")
                key = canonical_key(kind, source_id, snapshot_namespace)
                if generated.get(key) == "__collision__":
                    reasons.append("canonical_key_collision")
                if (kind, source_id) in conflicts:
                    reasons.append("unresolved_conflict")
                output = {
                    "input_index": entry["index"], "source_id": source_id, "canonical_key": key,
                    "display_name": entry["display_name"], "source_refs": list(entry["refs"]),
                    "state": "blocked" if reasons else "candidate", "reasons": reasons,
                }
            records[kind].append(output)
            if output.get("source_id") and output["source_id"] not in by_id[kind]:
                by_id[kind][output["source_id"]] = output

    # Establish cycles from the complete safe graph before a missing component
    # blocks an item, so an overlapping cycle remains diagnosable.
    cycle_ids = _cycle_ids(records["items"])
    for output in records["items"]:
        if output.get("source_id") in cycle_ids:
            _add_reason(output, "recipe_cycle")

    # Missing references are local blockers. Then reference-blocking propagates
    # to a fixed point, independent of input order.
    for kind, target in (("heroes", "traits"), ("items", "items")):
        for output in records[kind]:
            if "source_refs" not in output:
                continue
            missing = [ref for ref in output["source_refs"] if ref not in by_id[target]]
            blocked = [ref for ref in output["source_refs"]
                       if ref in by_id[target] and by_id[target][ref]["state"] == "blocked"]
            if missing:
                _add_reason(output, "reference_missing")
            if blocked:
                _add_reason(output, "reference_blocked")

    changed = True
    while changed:
        changed = False
        for kind, target in (("heroes", "traits"), ("items", "items")):
            for output in records[kind]:
                if "source_refs" not in output:
                    continue
                if ("reference_blocked" not in output["reasons"]
                        and any(ref in by_id[target] and by_id[target][ref]["state"] == "blocked" for ref in output["source_refs"])):
                    _add_reason(output, "reference_blocked")
                    changed = True

    coverage = {}
    for kind in _KINDS:
        output = records[kind]
        candidates = sum(row["state"] == "candidate" for row in output)
        reason_counts: dict[str, int] = {}
        for row in output:
            for reason in row["reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        primary_counts: dict[str, int] = {}
        for row in output:
            row["primary_block_reason"] = None if row["state"] == "candidate" else row["reasons"][0]
            if row["primary_block_reason"] is not None:
                primary = row["primary_block_reason"]
                primary_counts[primary] = primary_counts.get(primary, 0) + 1
        coverage[kind] = {"input_total": len(output), "candidate_count": candidates,
                          "blocked_count": len(output) - candidates,
                          "primary_block_reason_counts": dict(sorted(primary_counts.items())),
                          "reason_occurrence_counts": dict(sorted(reason_counts.items()))}

    return {
        "format_version": RESULT_FORMAT,
        "status": "candidate",
        "source_status": "unknown",
        "snapshot_namespace": snapshot_namespace,
        "decision_eligible": False,
        "action_advice": None,
        "private_key_map": {kind: {row["source_id"]: row["canonical_key"] for row in records[kind]
                                    if row["state"] == "candidate" and row.get("source_id") and row.get("canonical_key")}
                            for kind in _KINDS},
        "records": deepcopy(records),
        "coverage": coverage,
        "conflicts": conflict_ledger,
        "issues": [],
        "notes_zh": [
            "raw source ID 与 canonical key 的可逆表仅应保存在调用方私有目录；不得提交到公开仓库。",
            "candidate 只表示本地结构转换成功，不认证来源、版本、模式、地区、补丁或当前对局适用性。",
            "所有被阻断记录和未解决冲突保留在账本中；不得静默删除后把 partial 覆盖称为全量。",
        ],
    }
