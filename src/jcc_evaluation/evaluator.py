"""Score declared, frame-aligned observations against sample-annotation-v1.

This module does not open images, import recognition/transport modules, install
dependencies, or treat its synthetic fixtures as evidence of real accuracy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
import math
import platform
from typing import Any

from src.jcc_device.game_state_contract import (
    ContractViolation,
    SCHEMA_PATH,
    make_validator,
    validate_annotations,
    validate_state,
)


EVALUATOR_VERSION = "evaluation-v1.0.0"
CORE_FIELDS = ("scene", "phase", "round", "hp", "gold", "level")
SHOP_FIELDS = tuple(f"shop.{slot}" for slot in range(1, 6))
FIELDS = CORE_FIELDS + SHOP_FIELDS
TARGET_CORE_FIELDS = ("scene", "round", "hp", "gold", "level")
OUTCOMES = ("correct", "error", "unknown", "stale", "conflict", "unverifiable")
COHORTS = ("real_acceptance", "synthetic")
METADATA_KEYS = (
    "annotation_revision", "split_revision", "dedupe_revision", "review_evidence_ref",
    "recognizer_revision", "model_revision", "config_revision",
)
EVENTS = (
    "scene_transition", "popup", "occlusion", "disconnect_reconnect",
    "expiry", "late_result",
)


class EvaluationInputError(ValueError):
    """Invalid evaluation input; no scores may be issued for this run."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require_json(value: Any) -> None:
    """The Python API, like the CLI, accepts actual JSON data types only."""
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _require_json(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _require_json(child)
        return
    raise ValueError("Not a finite JSON value")


def _same_value(left: Any, right: Any) -> bool:
    """Exact JSON equality: numbers compare numerically, booleans stay distinct."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def _atomic(container: dict, field: str) -> dict:
    if field in CORE_FIELDS:
        return container[field]
    return container["shop"][int(field.split(".")[1]) - 1]["content"]


def _metadata(value: dict | None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(METADATA_KEYS):
        raise EvaluationInputError("invalid_metadata", "Run metadata contains unsupported keys.")
    if any(item is not None and (not isinstance(item, str) or not item.strip()) for item in value.values()):
        raise EvaluationInputError("invalid_metadata", "Run revision values must be nonempty text or null.")
    return {key: value.get(key) for key in METADATA_KEYS}


def _check_inputs(annotations: Any, predictions: Any) -> tuple[Any, Any]:
    # Detach from caller mutation and require actual JSON values for stable hashes.
    # The CLI also rejects duplicate keys and non-finite numbers at parse time.
    try:
        _require_json(annotations)
        _require_json(predictions)
        _canonical(annotations)
        _canonical(predictions)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EvaluationInputError("non_json_input", "Inputs must be finite, serializable JSON values.") from None
    annotation_validator = make_validator(definition="sample_annotation")
    try:
        validate_annotations(annotations, validator=annotation_validator)
    except ContractViolation as error:
        raise EvaluationInputError(
            "invalid_annotations", "Annotation contract validation failed; no scores were produced.\n" + str(error)
        ) from None
    if len({record["dataset_id"] for record in annotations}) > 1:
        raise EvaluationInputError("mixed_dataset_revisions", "Use one frozen dataset_id per evaluation run.")
    targets = [(r["stream_id"], r["host_clock_id"], r["frame"]["frame_id"]) for r in annotations]
    if len(set(targets)) != len(targets):
        raise EvaluationInputError("duplicate_target_frame", "One source frame cannot be scored as multiple samples.")
    if not isinstance(predictions, list):
        raise EvaluationInputError("invalid_predictions", "Predictions must be a list of sample_id/state records.")
    for item in predictions:
        if (not isinstance(item, dict) or set(item) != {"sample_id", "state"}
                or not isinstance(item["sample_id"], str) or not item["sample_id"]):
            raise EvaluationInputError(
                "invalid_prediction_binding", "Every prediction requires only a nonempty sample_id and state (or null)."
            )
    return annotation_validator, make_validator()


def _exclusion(record: dict, cohort: str) -> str | None:
    if cohort == "synthetic":
        return None if record["sample_kind"] == "synthetic" else "real_excluded_from_synthetic"
    if record["sample_kind"] != "real":
        return "synthetic_excluded_from_real"
    if record["split"] != "acceptance":
        return "not_acceptance_split"
    if (record["annotation_method"] != "human"
            or record["review_status"] not in ("independently_reviewed", "adjudicated")
            or not record["reviewer_id"] or record["reviewer_id"] == record["annotator_id"]):
        return "independent_human_review_missing"
    return None


def _prediction(record: dict, entries: list, validator) -> tuple[dict | None, list[str]]:
    if not entries or entries == [None]:
        return None, ["missing_prediction"]
    if len(entries) != 1:
        return None, ["duplicate_predictions"]
    state = entries[0]
    try:
        validate_state(state, validator=validator)
    except ContractViolation:
        # Deliberately reject the whole snapshot. Partial salvage can reward a
        # malformed object whose time/provenance relationships are ambiguous.
        return None, ["invalid_game_state"]
    issues = []
    expected_kinds = ("synthetic",) if record["sample_kind"] == "synthetic" else ("live", "replay")
    for key in ("stream_id", "host_clock_id"):
        if state[key] != record[key]:
            issues.append(key + "_mismatch")
    if state["sample_kind"] not in expected_kinds:
        issues.append("sample_kind_mismatch")
    if state["frame_id"] != record["frame"]["frame_id"]:
        issues.append("anchor_frame_mismatch")
    registered = [frame for frame in state["frames"] if frame["frame_id"] == record["frame"]["frame_id"]]
    if len(registered) != 1 or not _same_value(registered[0], record["frame"]):
        issues.append("target_frame_metadata_mismatch")
    return state, issues


def _classify(label: dict, observation: dict | None, issues: list[str], target_frame: str) -> dict:
    issues = list(issues)
    if not issues and observation is not None:
        if observation["source_frame_id"] != target_frame:
            issues.append("field_source_frame_mismatch")
        if any(candidate["source_frame_id"] != target_frame for candidate in observation.get("candidates", [])):
            issues.append("candidate_source_frame_mismatch")
    applicability = label["applicability"]
    if applicability != "applicable":
        outcome = applicability
    elif issues:
        outcome = "unknown"
    elif observation["status"] != "recognized":
        outcome = observation["status"]
    elif label["truth_status"] == "unknown":
        outcome = "unverifiable"
    else:
        outcome = "correct" if _same_value(observation["value"], label["value"]) else "error"
    return {
        "outcome": outcome,
        "truth_status": label["truth_status"],
        "label_reason": label["reason"],
        "prediction_status": observation["status"] if observation is not None else None,
        "prediction_reason": observation.get("reason") if observation is not None else None,
        "protocol_issues": issues,
    }


def _counts(rows: list[dict]) -> dict:
    outcomes = Counter(row["outcome"] for row in rows)
    applicable = sum(outcomes[key] for key in OUTCOMES)
    result = {"applicable": applicable, **{key: outcomes[key] for key in OUTCOMES}}
    result.update(
        known_answers=sum(row["truth_status"] == "known" for row in rows if row["outcome"] in OUTCOMES),
        not_applicable=outcomes["not_applicable"], undetermined=outcomes["undetermined"],
        protocol_errors=sum(bool(row["protocol_issues"]) for row in rows if row["outcome"] in OUTCOMES),
        protocol_errors_outside_denominator=sum(bool(row["protocol_issues"]) for row in rows if row["outcome"] not in OUTCOMES),
    )
    result["unknown_answers"] = applicable - result["known_answers"]
    result["rates"] = {key: result[key] / applicable if applicable else None for key in OUTCOMES}
    result["rates"]["protocol_errors"] = result["protocol_errors"] / applicable if applicable else None
    result["rates"]["worst_case_error"] = (result["error"] + result["unverifiable"]) / applicable if applicable else None
    result["observed_error_is_partial"] = result["unverifiable"] > 0
    result["not_applicable_reasons"] = dict(sorted(Counter(
        row["label_reason"] for row in rows if row["outcome"] == "not_applicable").items()))
    result["undetermined_reasons"] = dict(sorted(Counter(
        row["label_reason"] for row in rows if row["outcome"] == "undetermined").items()))
    result["unknown_truth_reasons"] = dict(sorted(Counter(
        row["label_reason"] for row in rows
        if row["outcome"] in OUTCOMES and row["truth_status"] == "unknown").items()))
    if applicable != sum(result[key] for key in OUTCOMES):
        raise AssertionError("Applicable denominator does not partition into the six outcomes.")
    return result


def _scope_group(record: dict, name: str) -> str:
    label = record["labels"][name]
    return _canonical({key: label[key] for key in ("applicability", "truth_status", "value", "reason")}).decode("utf-8")


def _coverage(records: list[dict], fields: dict) -> dict:
    complete_shops = []
    for record in records:
        labels = [slot["content"] for slot in record["labels"]["shop"]]
        if all(label["applicability"] == "applicable" and label["truth_status"] == "known" for label in labels):
            complete_shops.append([label["value"] for label in labels])
    # Use contract equality (including 1 == 1.0), rather than lexical hashes,
    # so alternate number spellings cannot inflate state counts.
    distinct_shops: list = []
    for shop in complete_shops:
        if not any(_same_value(shop, existing) for existing in distinct_shops):
            distinct_shops.append(shop)
    actual = {
        "recording_sessions": len({r["recording_session_id"] for r in records}),
        "deduplicated_frames": len({r["dedupe_group_id"] for r in records}),
        "distinct_state_groups": len({r["state_group_id"] for r in records}),
        "applicable_shop_slots": sum(fields[name]["applicable"] for name in SHOP_FIELDS),
        "known_complete_shop_states": len(distinct_shops),
    }
    required = {"recording_sessions": 3, "deduplicated_frames": 100, "distinct_state_groups": 20,
                "applicable_shop_slots": 100, "known_complete_shop_states": 20}
    return {
        "sample_count": len(records), "actual": actual, "first_round_targets": required,
        "shortfalls": {key: max(0, required[key] - actual[key]) for key in required},
        "core_applicable_shortfalls": {name: max(0, 100 - fields[name]["applicable"]) for name in TARGET_CORE_FIELDS},
        "phase_minimum_not_specified": True,
        "shop_state_count_basis": "distinct, fully known five-slot canonical contract values; partial shops do not qualify",
        "dedupe_basis": "declared frozen dedupe groups; image similarity and assets are not independently checked",
    }


def _rate_target(counts: dict, correct_percent: int, minimum: int) -> dict:
    # Integer arithmetic preserves inclusive 95%/90% and 2% boundaries exactly.
    a = counts["applicable"]
    met = (100 * counts["correct"] >= correct_percent * a
           and 100 * (counts["error"] + counts["unverifiable"]) <= 2 * a) if a else None
    return {"minimum_applicable": minimum, "applicable_shortfall": max(0, minimum - a),
            "correct_rate_minimum": correct_percent / 100, "worst_case_error_maximum": 0.02,
            "observed_rates_meet_target": met,
            "status": "insufficient_data" if a < minimum else "met" if met else "not_met"}


def _targets(fields: dict, shop: dict, coverage: dict, cohort: str) -> dict:
    core = {name: _rate_target(fields[name], 95, 100) for name in TARGET_CORE_FIELDS}
    slots = {name: _rate_target(fields[name], 90, 1) for name in SHOP_FIELDS}
    shop_target = _rate_target(shop, 90, 100)
    sample_ok = all(coverage["shortfalls"][key] == 0 for key in
                    ("recording_sessions", "deduplicated_frames", "distinct_state_groups"))
    shop_ok = (coverage["shortfalls"]["known_complete_shop_states"] == 0
               and shop_target["status"] == "met" and all(t["status"] == "met" for t in slots.values()))
    if not sample_ok or any(t["status"] == "insufficient_data" for t in core.values()):
        m1 = "insufficient_data"
    else:
        m1 = "met" if all(t["status"] == "met" for t in core.values()) else "not_met"
    if not sample_ok or shop_target["status"] == "insufficient_data" or coverage["shortfalls"]["known_complete_shop_states"]:
        m2_shop = "insufficient_data"
    else:
        m2_shop = "met" if shop_ok else "not_met"
    return {
        "real_acceptance_verdict": None if cohort == "synthetic" else {"m1_numerical_targets": m1, "m2_shop_numerical_targets": m2_shop},
        "synthetic_comparisons_are_not_acceptance": cohort == "synthetic",
        "core": core, "shop_slots": slots, "shop_all": shop_target,
        "phase": {"status": "report_only", "reason": "No numerical acceptance target specified for phase."},
        "scope": "Numerical screening on supplied annotations; this is not full M1 acceptance or evidence of live reliability.",
    }


def evaluate(annotations: list[dict], predictions: list[dict], *, cohort: str = "real_acceptance",
             run_metadata: dict | None = None) -> dict:
    """Return an offline report or raise EvaluationInputError without any scores.

    Validate the complete manifest before cohort selection, including development
    and quarantine records. Predictions are [{"sample_id": ..., "state": ...}].
    Duplicate bindings invalidate that sample; absent/null/invalid snapshots are
    counted, never dropped. Each run has exactly one cohort and dataset revision.
    """
    if cohort not in COHORTS:
        raise EvaluationInputError("invalid_cohort", "Choose real_acceptance or synthetic; cohorts cannot be merged.")
    metadata = _metadata(run_metadata)
    _, state_validator = _check_inputs(annotations, predictions)
    annotations, predictions = deepcopy(annotations), deepcopy(predictions)
    selected, exclusions = [], Counter()
    for record in annotations:
        reason = _exclusion(record, cohort)
        if reason is None:
            selected.append(record)
        else:
            exclusions[reason] += 1
    by_sample: dict[str, list] = defaultdict(list)
    for prediction in predictions:
        by_sample[prediction["sample_id"]].append(prediction["state"])
    all_ids = {record["sample_id"] for record in annotations}
    selected_ids = {record["sample_id"] for record in selected}
    unbound = [item for item in predictions if item["sample_id"] not in all_ids]
    details = []
    method_counts: Counter = Counter()
    for record in sorted(selected, key=lambda r: r["sample_id"]):
        state, issues = _prediction(record, by_sample.get(record["sample_id"], []), state_validator)
        fields = {}
        for name in FIELDS:
            observation = _atomic(state, name) if state is not None else None
            fields[name] = _classify(_atomic(record["labels"], name), observation, issues, record["frame"]["frame_id"])
            if observation is not None:
                for item in (observation, *observation.get("candidates", [])):
                    method_counts[_canonical(item["method"]).decode("utf-8")] += 1
        details.append({"sample_id": record["sample_id"], "sample_protocol_issues": issues, "fields": fields})
    field_counts = {name: _counts([row["fields"][name] for row in details]) for name in FIELDS}
    shop_counts = _counts([row["fields"][name] for row in details for name in SHOP_FIELDS])
    coverage = _coverage(selected, field_counts)
    detail_index = {row["sample_id"]: row for row in details}

    def group_summary(records: list[dict]) -> dict:
        rows = [detail_index[r["sample_id"]] for r in records]
        return {"sample_count": len(records), "fields": {name: _counts([r["fields"][name] for r in rows]) for name in FIELDS}}

    conditions = sorted({condition for record in selected for condition in record["conditions"]})
    grouped_conditions = {condition: group_summary([r for r in selected if condition in r["conditions"]]) for condition in conditions}
    grouped_scopes = {}
    for key in ("mode", "game_version"):
        groups: dict[str, list] = defaultdict(list)
        for record in selected:
            groups[_scope_group(record, key)].append(record)
        grouped_scopes[key] = [{"label": json.loads(label), **group_summary(records)} for label, records in sorted(groups.items())]
    warnings = [
        "Offline frame scoring only. Images, bytes/hashes, human identities, dedupe quality and actual producer pixel access are not verified.",
        "All freshness checks use snapshot assembly time, never the evaluator's current clock; streaming inheritance is not evaluated.",
        "Condition groups overlap and must not be added together. Five shop slots are not five independent frames.",
        "No event log, cold/warm labels, visible-suggestion timestamps or trusted device/host clock mapping was supplied.",
    ]
    if cohort == "synthetic":
        warnings.insert(0, "SYNTHETIC ONLY — TOOL SELF-TEST. NOT REAL ACCURACY, ACCEPTANCE OR LATENCY RESULTS.")
    if not selected:
        warnings.insert(0, "No eligible samples: all rates are null and no accuracy result is available.")
    elif not any(field_counts[name]["applicable"] for name in FIELDS):
        warnings.insert(0, "No applicable scored labels: all rates are null and no accuracy result is available.")
    missing_metadata = [key for key, value in metadata.items() if value is None]
    if missing_metadata:
        warnings.append("Missing run provenance: " + ", ".join(missing_metadata) + ".")
    if unbound:
        warnings.append("Predictions without an annotation binding were recorded as run protocol issues; they cannot create denominator samples.")
    return {
        "schema_version": "evaluation-report-v1", "evaluator_version": EVALUATOR_VERSION,
        "cohort": cohort,
        "result_kind": ("SYNTHETIC_TOOL_TEST_NOT_REAL_RESULTS" if cohort == "synthetic"
                        else "REVIEWED_REAL_ACCEPTANCE_OFFLINE" if any(field_counts[name]["applicable"] for name in FIELDS)
                        else "NO_REAL_ACCURACY_RESULT"),
        "accuracy_result_available": any(field_counts[name]["applicable"] for name in FIELDS),
        "real_result_available": cohort == "real_acceptance" and any(field_counts[name]["applicable"] for name in FIELDS),
        "full_m1_acceptance": "not_assessed",
        "warnings": warnings,
        "reproducibility": {
            "dataset_id": annotations[0]["dataset_id"] if annotations else None,
            "canonical_annotations_sha256": _sha(annotations), "canonical_predictions_sha256": _sha(predictions),
            "canonical_run_metadata_sha256": _sha(metadata), "contract_schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
            "python_version": platform.python_version(), "jsonschema_version": version("jsonschema"),
            "run_metadata": metadata, "missing_run_metadata": missing_metadata,
        },
        "inventory": {"input_samples": len(annotations), "selected_samples": len(selected),
                      "exclusions": dict(sorted(exclusions.items())),
                      "input_by_kind": dict(sorted(Counter(r["sample_kind"] for r in annotations).items())),
                      "input_by_split": dict(sorted(Counter(r["split"] for r in annotations).items()))},
        "prediction_inventory": {
            "input_records": len(predictions), "unbound_prediction_records": len(unbound),
            "ignored_records_for_excluded_samples": sum(p["sample_id"] in all_ids - selected_ids for p in predictions),
            "missing_selected_predictions": sum(not by_sample.get(r["sample_id"]) or by_sample[r["sample_id"]] == [None] for r in selected),
            "duplicate_selected_bindings": sum(len(by_sample[r["sample_id"]]) > 1 for r in selected),
            "samples_with_protocol_issues": sum(any(f["protocol_issues"] for f in row["fields"].values()) for row in details),
        },
        "methods": [{"method": json.loads(method), "observation_occurrences": count} for method, count in sorted(method_counts.items())],
        "fields": field_counts, "shop_all": shop_counts, "coverage": coverage,
        "targets": _targets(field_counts, shop_counts, coverage, cohort),
        "conditions": {"groups_overlap": True, "groups": grouped_conditions,
                       "untagged_sample_count": sum(not r["conditions"] for r in selected)},
        "applicability_context": grouped_scopes,
        "latency": {"status": "not_measured", "cold_start": None, "warmed_median_ms": None,
                    "warmed_p95_ms": None, "device_change_to_visible_ms": None,
                    "reason": "This input contract does not establish an eligible cold/warm event cohort or end-to-end timestamps."},
        "event_coverage": {
            "status": "not_measured", "applicable_events": None, "success": None, "timeouts": None,
            "no_result": None, "errors": None, "interruptions": None, "recording_sessions": None,
            "first_round_targets": {"recording_sessions": 3, "applicable_events": 100},
            "boundary_events": {name: {"status": "not_measured", "count": None} for name in EVENTS},
            "reason": "Frames and field statuses are not independent state-change events or evidence of continuous gate testing.",
        },
        "samples": details,
    }


def render_markdown(report: dict) -> str:
    """Render the counts and gaps without reproducing assets or predicted values."""
    synthetic = report["cohort"] == "synthetic"
    lines = ["# " + ("合成自测报告：非真实识别成绩" if synthetic else "人工复核验收标注的离线测评"), "",
             "**" + report["result_kind"] + "**", "",
             "仅评价声明的逐帧结果；完整 JSON 含逐条理由、版本、条件分组及冻结输入摘要。", "",
             "| 字段 | 适用 A | 已知答案 | 正确 C | 错误 E | unknown | stale | conflict | 不可核验 V | 协议错误¹ | C/A | E/A | (E+V)/A |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]

    def percent(value):
        return "null（无适用样本）" if value is None else f"{value:.2%}"

    for name, counts in [*report["fields"].items(), ("shop_all（格次）", report["shop_all"])]:
        values = [name, *[str(counts[key]) for key in ("applicable", "known_answers", *OUTCOMES, "protocol_errors")],
                  *[percent(counts["rates"][key]) for key in ("correct", "error", "worst_case_error")]]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "¹ 协议错误为适用格次中的重叠计数，已归入 unknown；它不增加 A。",
              "", "| 字段 | 不适用 | 适用性未定 | 分母外协议错误 |", "| --- | ---: | ---: | ---: |"]
    for name, counts in report["fields"].items():
        lines.append(f"| {name} | {counts['not_applicable']} | {counts['undetermined']} | {counts['protocol_errors_outside_denominator']} |")
    lines += ["", "## 样本与缺口", "",
              f"输入 {report['inventory']['input_samples']} 条，纳入 {report['inventory']['selected_samples']} 条。", ""]
    coverage = report["coverage"]
    for key, actual in coverage["actual"].items():
        lines.append(f"- {key}：{actual} / {coverage['first_round_targets'][key]}；缺 {coverage['shortfalls'][key]}。")
    lines += ["", "核心字段适用标注缺口：" + "；".join(f"{key} 缺 {value}" for key, value in coverage["core_applicable_shortfalls"].items()) + "。",
              "", "验收判断：" + ("合成数据无真实验收判断。" if synthetic else json.dumps(report["targets"]["real_acceptance_verdict"], ensure_ascii=False)),
              "", "时延、冷启动、预热后 P50/P95、端到端响应及 100 个状态变化事件：**未测**。", "",
              "## 限制与来源", ""]
    lines.extend("- " + warning for warning in report["warnings"])
    provenance = report["reproducibility"]
    lines += ["", f"- 工具：`{report['evaluator_version']}`；Python `{provenance['python_version']}`；jsonschema `{provenance['jsonschema_version']}`。",
              f"- annotations SHA-256（canonical）：`{provenance['canonical_annotations_sha256']}`。",
              f"- predictions SHA-256（canonical）：`{provenance['canonical_predictions_sha256']}`。", ""]
    return "\n".join(lines)
