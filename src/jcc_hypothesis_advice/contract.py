"""Small closed input contract; it does not accept game-state-v1 snapshots.

All source and review classifications are caller reports. Checking these
records is not source authentication, recognition validation, or game-rule
verification. No locator is opened by this module.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any


MAX_INTEGER = 2**53 - 1
SCHEMA = "conditional-advice-input-v1"
TIME_BASIS = "offline_review_monotonic"
SCOPE_KEYS = ("game", "region", "season", "patch", "hotfix", "mode")
CLAIM_ROLES = {
    "scene": "scene_context",
    "phase": "phase_context",
    "perspective": "self_perspective",
    "gold": "self_gold",
    "scope_binding": "scope_hypothesis",
    "hypothetical_spend": "single_spend_hypothesis",
    "reserve": "reserve_hypothesis",
}
HYPOTHESES = frozenset({"scope_binding", "hypothetical_spend", "reserve"})
NUMBERS = frozenset({"gold", "hypothetical_spend", "reserve"})
BLOCKING_STATES = frozenset({"unknown", "conflict", "stale", "not_applicable"})
BINDING_KEYS = frozenset({
    "stream_id", "frame_id", "frame_sha256", "sequence", "snapshot_revision",
    "context_epoch", "review_clock_id", "received_review_ms", "assembled_review_ms",
})
ROOT_KEYS = frozenset({
    "schema_version", "purpose", "sample_kind", "time_basis", "source", "binding", "scope", "claims",
})
CLAIM_KEYS = frozenset({
    "status", "value", "role", "unit", "visibility", "binding", "observed_review_ms",
    "completed_review_ms", "expires_review_ms", "basis",
})
BASIS_KEYS = frozenset({
    "evidence_id", "status", "provenance", "scope", "binding", "asserted_value", "unit",
    "source_ref", "source_sha256", "source_pointer", "basis_note", "source_time",
    "review_ref", "assessed_review_ms", "expires_review_ms",
})


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def valid_ms(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value <= MAX_INTEGER and math.isfinite(value)


def integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_INTEGER


def issue(code: str, message: str, path: str = "") -> dict:
    return {"code": code, "message": message, "path": path}


class InputProblem(ValueError):
    def __init__(self, code: str, message: str, path: str = ""):
        super().__init__(code)
        self.reason = issue(code, message, path)


def _fail(path: str, message: str = "输入结构或类型不符合隔离合同。") -> None:
    raise InputProblem("input_malformed", message, path)


def _object(value: Any, path: str, required, optional=()) -> None:
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional)):
        _fail(path, "缺少结构成员，或包含合同未定义的成员；核验布尔开关不被消费。")


def _text(value: Any, path: str, *, limit: int = 256) -> None:
    if (type(value) is not str or not value or value != value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail(path)


def _identifier(value: Any, path: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        _fail(path)


def _ref(value: Any, path: str) -> None:
    if type(value) is not str or not re.fullmatch(r"(?:synthetic|private):[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        _fail(path, "来源使用 synthetic: 或 private: 的不透明引用，不接收路径、网址或设备参数。")


def _sha(value: Any, path: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail(path, "缺少规范的内容摘要声明。摘要格式正确不证明原始来源已核实。")


def _time(value: Any, path: str, *, nullable=False) -> None:
    if not (nullable and value is None) and not valid_ms(value):
        _fail(path, "时间必须为有限非负数；不能把布尔值或墙钟时间当作回放单调时间。")


def _wall_time(value: Any, path: str) -> None:
    if value is None:
        return
    _text(value, path, limit=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail(path)
    except (ValueError, OverflowError):
        _fail(path, "历史采集与来源日期若填写，须保留带时区的时间；未知可为空。")


def _scope(scope: Any, path: str) -> None:
    _object(scope, path, SCOPE_KEYS)
    for key in SCOPE_KEYS:
        if scope[key] is not None:
            _text(scope[key], path + "/" + key, limit=128)


def _binding(binding: Any, path: str) -> None:
    _object(binding, path, BINDING_KEYS)
    for name in ("stream_id", "frame_id", "review_clock_id"):
        _identifier(binding[name], path + "/" + name)
    _sha(binding["frame_sha256"], path + "/frame_sha256")
    for name in ("sequence", "snapshot_revision", "context_epoch"):
        if not integer(binding[name]) or (name == "snapshot_revision" and binding[name] == 0):
            _fail(path + "/" + name)
    for name in ("received_review_ms", "assembled_review_ms"):
        _time(binding[name], path + "/" + name)


def validate_shape(request: Any) -> dict:
    """Validate a JSON copy; semantic gaps remain explicit rather than filled in."""
    # Reject live and production envelopes before interpreting their contents.
    if type(request) is dict:
        if request.get("sample_kind") in ("live", "live_candidate"):
            raise InputProblem("live_input_forbidden", "隔离原型不接收实时输入。")
        if request.get("schema_version") == "game-state-v1":
            raise InputProblem("production_input_forbidden", "本入口不接收或改写正式局面。")
    _object(request, "", ROOT_KEYS)
    if request["schema_version"] != SCHEMA or request["purpose"] != "hypothesis_review_only":
        raise InputProblem("purpose_mismatch", "必须显式声明假设复盘用途及独立输入版本。")
    if request["sample_kind"] not in ("synthetic_fixture", "replay_candidate"):
        raise InputProblem("sample_kind_forbidden", "只能使用合成夹具或隔离回放候选，不能重标正式输入。")
    if request["time_basis"] != TIME_BASIS:
        raise InputProblem("time_basis_mismatch", "只消费显式的离线复盘时间；不比较历史采集时间与当前时钟。")
    _object(request["source"], "/source", {"kind", "recording_ref", "capture_ref", "historical_capture_time"})
    source = request["source"]
    if source["kind"] not in ("generated_fixture", "constructed_replay_template", "private_replay_candidate"):
        _fail("/source/kind")
    for key in ("recording_ref", "capture_ref"):
        _ref(source[key], "/source/" + key)
    _wall_time(source["historical_capture_time"], "/source/historical_capture_time")
    _binding(request["binding"], "/binding")
    _scope(request["scope"], "/scope")
    _object(request["claims"], "/claims", (), CLAIM_ROLES)
    for name, claim in request["claims"].items():
        path = "/claims/" + name
        _object(claim, path, CLAIM_KEYS, {"machine_confidence"})
        if claim["status"] not in {"candidate", "hypothesis", *BLOCKING_STATES}:
            raise InputProblem("claim_status_forbidden", "候选不能通过 recognized、verified 等标签升格。", path)
        for key in ("role", "unit", "visibility"):
            if claim[key] is not None:
                _text(claim[key], path + "/" + key)
        if claim["visibility"] not in ("clear", "occluded", "unknown", "not_visual"):
            _fail(path + "/visibility")
        _binding(claim["binding"], path + "/binding")
        blocked = claim["status"] in BLOCKING_STATES
        for key in ("observed_review_ms", "completed_review_ms", "expires_review_ms"):
            _time(claim[key], path + "/" + key, nullable=blocked)
        if blocked:
            if claim["value"] is not None or claim["expires_review_ms"] is not None:
                _fail(path, "未知、冲突、过期或不适用字段不能携带候选值和新有效期。")
        elif name in NUMBERS:
            if not integer(claim["value"]):
                _fail(path + "/value", "金额只接受非负安全整数，不纠正原始文字或猜测数字。")
        elif name == "scope_binding":
            _scope(claim["value"], path + "/value")
        else:
            _text(claim["value"], path + "/value", limit=128)
        if "machine_confidence" in claim:
            score = claim["machine_confidence"]
            if not valid_ms(score) or score > 1:
                _fail(path + "/machine_confidence")
        if type(claim["basis"]) is not list or len(claim["basis"]) > 16:
            _fail(path + "/basis")
        for index, basis in enumerate(claim["basis"]):
            bp = path + "/basis/" + str(index)
            _object(basis, bp, BASIS_KEYS)
            _identifier(basis["evidence_id"], bp + "/evidence_id")
            if basis["status"] not in ("candidate", "unknown", "conflict", "revoked", "stale"):
                raise InputProblem("evidence_status_forbidden", "依据也只保留候选或阻断状态，没有核验开关。", bp)
            if basis["provenance"] not in ("synthetic_fixture", "machine_candidate", "caller_assumption", "review_recorded"):
                _fail(bp + "/provenance")
            _scope(basis["scope"], bp + "/scope")
            _binding(basis["binding"], bp + "/binding")
            _text(basis["unit"], bp + "/unit")
            _ref(basis["source_ref"], bp + "/source_ref")
            _sha(basis["source_sha256"], bp + "/source_sha256")
            _text(basis["source_pointer"], bp + "/source_pointer")
            if not basis["source_pointer"].startswith("/") or re.search(r"~(?![01])", basis["source_pointer"]):
                _fail(bp + "/source_pointer")
            _text(basis["basis_note"], bp + "/basis_note", limit=1024)
            _wall_time(basis["source_time"], bp + "/source_time")
            if basis["review_ref"] is not None:
                _ref(basis["review_ref"], bp + "/review_ref")
            for key in ("assessed_review_ms", "expires_review_ms"):
                _time(basis[key], bp + "/" + key, nullable=basis["status"] != "candidate")
    try:
        return json.loads(canonical(request))
    except (ValueError, TypeError, RecursionError, OverflowError):
        _fail("", "输入必须是可完整复制的有限 JSON 数据。")


def semantic_issues(case: dict, *, review_clock_id: str, now: float) -> tuple[list[dict], list[dict], list[float]]:
    """Return hard refusals, explanatory gaps, and original expiry ceilings."""
    refused: list[dict] = []
    gaps: list[dict] = []
    deadlines: list[float] = []
    binding, scope = case["binding"], case["scope"]
    if binding["review_clock_id"] != review_clock_id:
        refused.append(issue("clock_domain_mismatch", "输入与复盘时钟异域，禁止比较时间。", "/binding/review_clock_id"))
        # Do not inspect or compare timestamps across clock domains.
        return refused, gaps, deadlines
    if binding["received_review_ms"] > binding["assembled_review_ms"] or binding["assembled_review_ms"] > now:
        refused.append(issue("frame_time_invalid", "复盘收帧、组装时间次序错误，或完成时点尚未发生。", "/binding"))
    fixture = case["sample_kind"] == "synthetic_fixture"
    allowed_sources = {"generated_fixture"} if fixture else {"constructed_replay_template", "private_replay_candidate"}
    prefix = "synthetic:" if fixture else "private:"
    if case["source"]["kind"] not in allowed_sources or any(
            not case["source"][key].startswith(prefix) for key in ("recording_ref", "capture_ref")):
        refused.append(issue("source_kind_mismatch", "素材性质与来源引用不匹配；不能将回放重标为合成。", "/source"))
    for name in SCOPE_KEYS:
        if scope[name] is None:
            gaps.append(issue("scope_unknown", "地区、赛季、补丁、热修复或模式仍未知；不使用通配符。", "/scope/" + name))
    seen_evidence: set[str] = set()
    for name, role in CLAIM_ROLES.items():
        path = "/claims/" + name
        claim = case["claims"].get(name)
        if claim is None:
            gaps.append(issue("field_missing", "缺少本例依赖的字段，不能从其他区域补猜。", path))
            continue
        if claim["binding"] != binding:
            refused.append(issue("field_binding_mismatch", "字段不是当前完整帧、图像摘要、修订或上下文的候选。", path))
            continue
        if claim["role"] != role:
            gaps.append(issue("field_role_mismatch", "字段角色与要求不一致，观察者或对手信息不能当作己方。", path))
        expected_status = "hypothesis" if name in HYPOTHESES else "candidate"
        if claim["status"] in BLOCKING_STATES:
            gaps.append(issue("field_" + claim["status"], "必要字段未知、冲突、过期或不适用。", path))
            continue
        if claim["status"] != expected_status:
            refused.append(issue("hypothesis_kind_mismatch", "视觉候选与显式假设须区分，基础费用也不能冒充本次支出。", path))
        expected_visibility = "not_visual" if name in HYPOTHESES else "clear"
        if claim["visibility"] != expected_visibility:
            gaps.append(issue("field_visibility_unestablished", "字段被遮挡、可见性未知，或把非视觉假设冒充读图。", path))
        expected_unit = "gold" if name in NUMBERS else "scope" if name == "scope_binding" else "context"
        if claim["unit"] != expected_unit:
            gaps.append(issue("field_unit_mismatch", "字段单位未知或不同，不做单位或语义的隐式转换。", path))
        observed, completed, expiry = (claim[key] for key in
                                       ("observed_review_ms", "completed_review_ms", "expires_review_ms"))
        if (observed != binding["received_review_ms"] or not observed <= completed <= binding["assembled_review_ms"]
                or expiry <= completed):
            refused.append(issue("field_time_invalid", "字段必须保留同帧观察起点、完成时间及原截止时间。", path))
        elif now >= expiry:
            gaps.append(issue("field_expired", "必要字段已到原截止时间，重读不会续期。", path))
        deadlines.append(expiry)
        if name == "scope_binding" and claim["value"] != scope:
            refused.append(issue("scope_binding_mismatch", "模式/版本假设的断言与本案例作用域不完全相等。", path))
        if not claim["basis"]:
            gaps.append(issue("evidence_missing", "字段或假设没有可追溯的候选依据。", path))
        for index, basis in enumerate(claim["basis"]):
            bp = path + "/basis/" + str(index)
            if basis["evidence_id"] in seen_evidence:
                refused.append(issue("evidence_id_duplicate", "断言引用重复；不能按相同 ID 静默合并或投票。", bp))
            seen_evidence.add(basis["evidence_id"])
            if basis["binding"] != binding:
                refused.append(issue("evidence_binding_mismatch", "依据与当前帧、摘要、流、修订或时钟不匹配。", bp))
                continue
            if basis["scope"] != scope:
                refused.append(issue("evidence_scope_mismatch", "依据的游戏、地区、赛季、补丁、热修复或模式不匹配。", bp))
            if ((basis["provenance"] == "synthetic_fixture") != fixture
                    or not basis["source_ref"].startswith(prefix)):
                refused.append(issue("evidence_material_mismatch", "合成依据与回放候选不能互相改标签。", bp))
            if not fixture and name in HYPOTHESES and basis["provenance"] not in ("caller_assumption", "review_recorded"):
                refused.append(issue("hypothesis_basis_required", "费用、保留线及范围必须显式列为假设。", bp))
            if basis["provenance"] == "review_recorded" and basis["review_ref"] is None:
                gaps.append(issue("review_reference_missing", "自报审阅记录缺少引用；即使有引用也不表示已核验。", bp))
            if basis["status"] != "candidate":
                gaps.append(issue("evidence_" + basis["status"], "依据未知、冲突、撤销或过期，不能选择其中一个。", bp))
                continue
            if canonical(basis["asserted_value"]) != canonical(claim["value"]):
                gaps.append(issue("evidence_value_conflict", "依据断言与字段候选不同；高置信度或多源一致不能裁决。", bp))
            if basis["unit"] != expected_unit:
                gaps.append(issue("evidence_unit_mismatch", "依据单位与当前假设的计算单位不同。", bp))
            assessed, end = basis["assessed_review_ms"], basis["expires_review_ms"]
            if not binding["received_review_ms"] <= assessed <= binding["assembled_review_ms"] or end <= assessed:
                refused.append(issue("evidence_time_invalid", "依据复盘检查时点与原有效期不合法。", bp))
            elif now >= end:
                gaps.append(issue("evidence_expired", "候选依据已到原截止时间，不能沿用或刷新。", bp))
            deadlines.append(end)
    return refused, gaps, deadlines
