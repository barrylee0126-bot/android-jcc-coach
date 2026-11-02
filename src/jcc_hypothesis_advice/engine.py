"""Atomic offline hypotheses with withdrawal and immutable review deadlines.

This module deliberately imports no strategy, device, catalog or overlay code.
Its explanations cannot be sent to those production interfaces as advice.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import threading
import time
from typing import Protocol

from .contract import (
    BLOCKING_STATES, CLAIM_ROLES, SCOPE_KEYS, InputProblem, canonical, digest, integer, issue, semantic_issues,
    valid_ms, validate_shape,
)


LABEL = "假设／仅供复盘 · 未核验 · 不构成实战指令"
RULE = {
    "rule_id": "hypothesis:single-spend-reserve",
    "revision": "arithmetic-v1",
    "semantics": "if context matches and G >= P and G-P >= R, the assumed budget condition holds",
    "parameters": ["gold_candidate_G", "single_spend_hypothesis_P", "reserve_hypothesis_R"],
    "game_rule": False,
}
UNKNOWNS = [
    issue("recognition_not_validated", "画面字段、己方身份及遮挡声明仍是候选，未证明读图正确。"),
    issue("scope_not_authenticated", "模式、地区、赛季、补丁与热修复只是有来源的范围假设，未核实适用于本局。"),
    issue("actual_price_unknown", "单笔支出是显式假设；未证明实际价格、折扣、额外费用或特殊限制。"),
    issue("value_not_established", "对象、合星、阵容、装备、机会成本及收益未知；预算通过不能推出值得购买。"),
    issue("source_not_authenticated", "来源定位、摘要及审阅记录均为输入声明，本模块未读取原图或认证来源。"),
]
COUNTEREXAMPLES = [
    {"code": "not_enough_candidate_balance", "condition": "G < P",
     "effect": "单笔支出超出候选余额；不计算负余额，也不推断按钮是否可用。"},
    {"code": "reserve_breached", "condition": "G >= P and G-P < R",
     "effect": "即使单笔扣款算术可计算，也未达到显式假设保留线。"},
    {"code": "budget_is_not_value", "condition": "预算条件成立，但对象价值或实际费用假设不成立",
     "effect": "预算条件不是购买的充分条件；不能据此生成买卖结论。"},
    {"code": "binding_or_evidence_lost", "condition": "新帧、遮挡、模式/版本变化、冲突、撤销或到期",
     "effect": "撤回旧推论；重新提供同帧候选和相应依据。"},
]
EVENTS = {
    "frame_changed": "收到新画面，旧假设推论已撤回。",
    "disconnected": "回放输入中断，旧假设推论已撤回。",
    "occluded": "发现遮挡，旧假设推论已撤回。",
    "evidence_changed": "依据改变或撤销，旧假设推论已撤回。",
    "context_changed": "场景、模式或版本改变，旧假设推论已撤回。",
}


@dataclass(frozen=True)
class ClockTick:
    review_clock_id: str
    review_ms: float


class Clock(Protocol):
    def now(self) -> ClockTick: ...


class ReplayReviewClock:
    """Explicit offline coordinate plus actual elapsed processing time.

    This clock does not establish historical game freshness. initial_review_ms
    is a user-selected replay review cutoff, not input assembled time or wall
    time. The input adapter must have created this offline coordinate system.
    """

    def __init__(self, review_clock_id: str, initial_review_ms: float):
        if type(review_clock_id) is not str or not review_clock_id or not valid_ms(initial_review_ms):
            raise ValueError("Invalid offline review clock")
        self.review_clock_id = review_clock_id
        self._initial = initial_review_ms
        self._started = time.monotonic_ns()

    def now(self) -> ClockTick:
        return ClockTick(self.review_clock_id, self._initial + (time.monotonic_ns() - self._started) / 1_000_000)


def _base(status: str, reasons=()) -> dict:
    return {
        "schema_version": "conditional-advice-result-v1",
        "purpose": "hypothesis_review_only",
        "label": LABEL,
        "status": status,
        "sample_kind": None,
        "material_origin": None,
        "source": None,
        "time_basis": "offline_review_monotonic",
        "production_eligible": False,
        "execution_eligible": False,
        "real_rules_verified": False,
        "historical_game_freshness_established": False,
        "advice": None,
        "action": None,
        "analysis": None,
        "evidence_updates": [],
        "reasons": list(reasons),
        "unknowns": deepcopy(UNKNOWNS),
        "counterexamples": deepcopy(COUNTEREXAMPLES),
        "binding": None,
        "scope": None,
        "input_digest": None,
        "rule": {**RULE, "digest": digest(RULE)},
        "evaluated_review_ms": None,
        "expires_review_ms": None,
    }


def _explain(case: dict) -> dict:
    claims = case["claims"]
    g, p, r = (claims[name]["value"] for name in ("gold", "hypothetical_spend", "reserve"))
    within_balance = g >= p
    after = g - p if within_balance else None
    reserve_met = after >= r if after is not None else None
    checks = [
        {"field": "scene", "expected": "battle", "satisfied": claims["scene"]["value"] == "battle"},
        {"field": "phase", "expected": "preparation", "satisfied": claims["phase"]["value"] == "preparation"},
        {"field": "perspective", "expected": "self", "satisfied": claims["perspective"]["value"] == "self"},
        {"field": "gold", "condition": "G >= P", "satisfied": within_balance},
        {"field": "reserve", "condition": "G-P >= R", "satisfied": reserve_met},
    ]
    holds = all(check["satisfied"] is True for check in checks)
    if not all(check["satisfied"] for check in checks[:3]):
        statement = "假设／仅供复盘：本例所需的己方准备阶段条件不成立，不展示支出推论。"
    elif not within_balance:
        statement = f"假设／仅供复盘：单笔假设支出 {p} 超出候选余额 {g}，预算条件不成立。"
    elif not reserve_met:
        statement = f"假设／仅供复盘：候选余额 {g}，单笔假设支出 {p} 后余 {after}，低于假设保留线 {r}。"
    else:
        statement = f"假设／仅供复盘：候选余额 {g}，单笔假设支出 {p} 后余 {after}，达到假设保留线 {r}。"
    context_holds = all(check["satisfied"] for check in checks[:3])
    return {
        "conditions_hold": holds,
        "statement_zh": statement + " 预算条件不能推出应购买。",
        "rationale": "仅根据显式输入检查 G>=P 以及 G-P>=R；R 是本例假设，既不是实际规则，也不代表经济最优。",
        "checks": checks,
        "arithmetic": {
            "candidate_gold": g if context_holds else None,
            "hypothetical_spend": p if context_holds else None,
            "hypothetical_reserve": r if context_holds else None,
            "after_gold": after if context_holds else None,
            "within_candidate_balance": within_balance if context_holds else None,
            "reserve_met": reserve_met if context_holds else None,
            "interest": None,
        },
        "assumptions": [
            {"field": name, "value": claim["value"], "status": claim["status"],
             "trust": "unverified_caller_report", "role": claim["role"], "unit": claim["unit"],
             "evidence": deepcopy(claim["basis"])}
            for name, claim in claims.items()
        ],
    }


class HypothesisSession:
    """One offline replay stream/clock, immutable frame metadata, no old revival.

    submit() is an atomic replacement, including every source and assertion.
    Any non-duplicate attempt withdraws the previous explanation first. Call
    view() on each presentation read/tick, and invalidate() on ingress/change
    before replacement candidates are available. No automatic display, game
    subscription, or background timer is installed.
    """

    def __init__(self, *, clock: Clock, max_frame_age_ms: int, max_result_ms: int):
        for value in (max_frame_age_ms, max_result_ms):
            if not integer(value) or not 0 < value <= 30_000:
                raise ValueError("Explicit engineering review deadlines must be in 1..30000 ms")
        self._clock = clock
        self._max_frame_age_ms = max_frame_age_ms
        self._max_result_ms = max_result_ms
        self._lock = threading.RLock()
        self._last_tick: ClockTick | None = None
        self._reading_clock = False
        self._evidence_event_during_read = False
        self._clock_failure: dict | None = None
        self._previous: dict | None = None
        self._highest_revision = 0
        self._initial_revision_watermarks: dict[tuple[str, str], int] = {}
        self._seen_frames: set[str] = set()
        self._field_limits: dict[str, dict] = {}
        self._evidence_limits: dict[tuple[str, ...], dict] = {}
        self._barriers: dict[str, dict] = {}
        self._seen_evidence: dict[tuple[str, str], dict] = {}
        self._retired_evidence: dict[tuple[str, str], dict] = {}
        self._pending_evidence: dict[str, dict] = {}
        self._evidence_updates: dict[tuple[str, str], dict] = {}
        self._source_assertions: dict[tuple[str, str], str] = {}
        self._known_context: dict[str, tuple[str, int]] = {}
        self._lost_context: set[str] = set()
        self._fact_deadlines: dict[str, float] = {}
        self._source_result_limits: dict[tuple[str, str], float] = {}
        self._generation = 0
        self._current_digest: str | None = None
        self._result = _base("insufficient_evidence", [issue("input_unavailable", "尚无隔离回放候选。")])

    def _now(self) -> ClockTick | None:
        if self._clock_failure is not None:
            return None
        try:
            self._reading_clock = True
            try:
                tick = self._clock.now()
            finally:
                self._reading_clock = False
            if (type(tick) is not ClockTick or type(tick.review_clock_id) is not str
                    or not tick.review_clock_id or not valid_ms(tick.review_ms)):
                raise ValueError("Invalid clock")
        except Exception:
            self._clock_failure = issue("clock_unavailable", "复盘时钟不可用；须重建会话。")
            return None
        if self._last_tick:
            if tick.review_clock_id != self._last_tick.review_clock_id:
                self._clock_failure = issue("clock_domain_changed", "复盘时钟域改变；须重建会话。")
            elif tick.review_ms < self._last_tick.review_ms:
                self._clock_failure = issue("clock_regressed", "复盘时钟倒退；须重建会话。")
        self._last_tick = tick
        if self._evidence_event_during_read and self._clock_failure is None:
            self._evidence_event_during_read = False
            for (name, _), record in list(self._seen_evidence.items()):
                self._retire_evidence(name, record, tick.review_ms, "evidence_changed")
        return None if self._clock_failure else tick

    def _replace(self, status: str, reasons=(), *, case: dict | None = None) -> dict:
        self._result = _base(status, reasons)
        self._current_digest = None
        if case is not None:
            self._result.update(
                sample_kind=case["sample_kind"], material_origin=case["source"]["kind"],
                source=deepcopy(case["source"]),
                binding=deepcopy(case["binding"]), scope=deepcopy(case["scope"]), input_digest=digest(case),
            )
        return deepcopy(self._result)

    def invalidate(self, event: str) -> dict:
        with self._lock:
            self._generation += 1
            if type(event) is not str or event not in EVENTS:
                if self._previous is not None:
                    self._barriers["frame_changed"] = self._previous
                return self._replace("refused", [issue("invalidation_event_unknown", "失效事件不符合合同，旧推论已撤回。")])
            if self._previous is not None:
                self._barriers[event] = self._previous
                if event == "evidence_changed":
                    # A clock may notify invalidation during its own read.
                    # Mark it now; _now advances the boundary to its completed
                    # tick before any later submission can recover evidence.
                    if self._reading_clock:
                        self._evidence_event_during_read = True
                    tick = self._last_tick if self._reading_clock else self._now()
                    if tick is None:
                        return self._replace("refused", [self._clock_failure])
                    at = tick.review_ms
                    for (name, _), record in list(self._seen_evidence.items()):
                        self._retire_evidence(name, record, at, event)
            return self._replace("invalidated", [issue(event, EVENTS[event])])

    @staticmethod
    def _new_capture(case: dict, old: dict) -> bool:
        current, previous = case["binding"], old["binding"]
        return (current["frame_id"] != previous["frame_id"]
                and current["sequence"] > previous["sequence"]
                and current["received_review_ms"] > previous["received_review_ms"]
                and case["source"]["capture_ref"] != old["source"]["capture_ref"])

    def _barrier_issues(self, case: dict) -> list[dict]:
        problems = []
        for event, old in self._barriers.items():
            if event == "evidence_changed":
                changed = all(
                    name in case["claims"] and case["claims"][name]["basis"] and all(
                        basis["source_sha256"] not in {prior["source_sha256"] for prior in claim["basis"]}
                        for basis in case["claims"][name]["basis"])
                    for name, claim in old["claims"].items())
                if not changed:
                    problems.append(issue("evidence_refresh_required", "依据失效后须提供新内容来源，修订号或重绑帧不能恢复旧依据。"))
            elif event == "context_changed":
                if (not self._new_capture(case, old)
                        or case["binding"]["context_epoch"] <= old["binding"]["context_epoch"]):
                    problems.append(issue("context_refresh_required", "上下文失效后须有新采集和更大的上下文代数。"))
            elif not self._new_capture(case, old):
                problems.append(issue("frame_refresh_required", "画面、遮挡或断连屏障须由新采集解除，修订号不能代替新帧。"))
        return problems

    @staticmethod
    def _evidence_key(name: str, basis: dict) -> tuple[str, ...]:
        # Aliasing an ID does not create another copy of the same source.
        return (name, basis["source_sha256"])

    @staticmethod
    def _locator(basis: dict) -> dict:
        return {key: basis[key] for key in (
            "evidence_id", "source_ref", "source_sha256", "source_pointer",
            "review_ref", "assessed_review_ms",
        )}

    def _retire_evidence(self, name: str, record: dict, at: float, reason: str) -> None:
        key = (name, record["source_sha256"])
        self._retired_evidence[key] = {**record, "retired_review_ms": at, "reason": reason}
        pending = self._pending_evidence.setdefault(name, {
            "after_ms": at, "versions": set(), "previous": {},
        })
        pending["after_ms"] = max(pending["after_ms"], at)
        pending["versions"].update(sha for field, sha in self._seen_evidence if field == name)
        pending["versions"].add(record["source_sha256"])
        pending["previous"][record["source_sha256"]] = self._retired_evidence[key]

    def _record_retirements(self, case: dict, at: float) -> bool:
        changed = False
        for name, claim in case["claims"].items():
            for basis in claim["basis"]:
                if (basis["status"] != "candidate" and claim["binding"] == case["binding"]
                        and basis["binding"] == case["binding"] and basis["scope"] == case["scope"]):
                    self._retire_evidence(name, self._locator(basis), at, "evidence_" + basis["status"])
                    changed = True
        return changed

    def _evidence_history(self, case: dict) -> tuple[list[dict], dict[tuple[str, str], dict]]:
        problems, updates = [], {}
        assertions = dict(self._source_assertions)
        for name, claim in case["claims"].items():
            for basis in claim["basis"]:
                location, content = self._assertion_identity(name, basis)
                if basis["status"] == "candidate" and canonical(basis["asserted_value"]) == canonical(claim["value"]):
                    if assertions.setdefault(location, content) != content:
                        problems.append(issue("evidence_content_changed", "同一来源内容摘要和字段定位不能承载不同断言、单位、范围或来源性质。", "/claims/" + name))
                if basis["status"] == "candidate" and (name, basis["source_sha256"]) in self._retired_evidence:
                    problems.append(issue("evidence_version_retired", "已撤销或失效的来源内容不能通过改状态、ID、定位或帧重新使用。", "/claims/" + name))
            pending = self._pending_evidence.get(name)
            if pending is None:
                continue
            # A new hash plus a later, newly referenced review is required.
            # These remain caller declarations, never source authentication.
            replacements = [basis for basis in claim["basis"] if (
                basis["status"] == "candidate" and basis["source_sha256"] not in pending["versions"]
                and basis["review_ref"] is not None
                and basis["review_ref"] not in {record["review_ref"] for record in pending["previous"].values()}
                and basis["assessed_review_ms"] > pending["after_ms"])]
            if replacements:
                for basis in replacements:
                    updates[(name, basis["source_sha256"])] = {
                        "field": name, "previous": list(pending["previous"].values()),
                        "replacement": self._locator(basis), "trust": "unverified_caller_report",
                    }
            elif any(basis["status"] == "candidate" for basis in claim["basis"]):
                problems.append(issue("evidence_update_required", "恢复须有不同来源内容、晚于失效的检查时间及新的审阅引用；改修订号不构成新事实。", "/claims/" + name))
        return problems, updates

    @staticmethod
    def _assertion_identity(name: str, basis: dict) -> tuple[tuple[str, str], str]:
        location = (basis["source_sha256"], basis["source_pointer"])
        content = digest({"field": name, **{key: basis[key] for key in (
            "asserted_value", "unit", "scope", "provenance",
        )}})
        return location, content

    @staticmethod
    def _loss_metadata_current(case: dict, claim: dict, now: float) -> bool:
        # Blocking states have nullable times and no consumable value. Check
        # their supplied metadata explicitly; semantic_issues skips their basis.
        binding = case["binding"]
        observed, completed = claim["observed_review_ms"], claim["completed_review_ms"]
        if ((observed is not None and observed != binding["received_review_ms"])
                or (completed is not None and not binding["received_review_ms"] <= completed <= binding["assembled_review_ms"])):
            return False
        fixture = case["sample_kind"] == "synthetic_fixture"
        prefix = "synthetic:" if fixture else "private:"
        for basis in claim["basis"]:
            if (basis["binding"] != binding or basis["scope"] != case["scope"]
                    or basis["unit"] != claim["unit"]
                    or ((basis["provenance"] == "synthetic_fixture") != fixture)
                    or not basis["source_ref"].startswith(prefix)
                    or (basis["provenance"] == "review_recorded" and basis["review_ref"] is None)
                    or (not fixture and claim["role"] == CLAIM_ROLES["scope_binding"]
                        and basis["provenance"] not in ("caller_assumption", "review_recorded"))):
                return False
            assessed, end = basis["assessed_review_ms"], basis["expires_review_ms"]
            if ((assessed is not None and not binding["received_review_ms"] <= assessed <= binding["assembled_review_ms"])
                    or (end is not None and (end <= now or (assessed is not None and end <= assessed)))):
                return False
        return True

    def _context_frame_current(self, case: dict, problems, tick: ClockTick) -> bool:
        return (not any(problem["code"] in {"frame_time_invalid", "source_kind_mismatch", "clock_domain_mismatch"}
                        for problem in problems)
                and tick.review_ms < case["binding"]["received_review_ms"] + self._max_frame_age_ms)

    def _context_values(self, case: dict, problems, tick: ClockTick) -> dict[str, str | None]:
        # An omitted entry means untrusted input, not a declaration of loss.
        # Even valid entries are caller candidates/assumptions, never facts.
        values = {}
        for name in ("scope_binding", "scene", "phase", "perspective"):
            path = "/claims/" + name
            paths = ["/scope/" + key for key in SCOPE_KEYS] if name == "scope_binding" else [path]
            claim = case["claims"].get(name)
            if claim is None:
                values.update(dict.fromkeys(paths))
                continue
            unit = "scope" if name == "scope_binding" else "context"
            if (claim["binding"] != case["binding"] or claim["role"] != CLAIM_ROLES[name]
                    or claim["unit"] != unit):
                continue
            if claim["status"] in BLOCKING_STATES:
                if self._loss_metadata_current(case, claim, tick.review_ms):
                    values.update(dict.fromkeys(paths))
                continue
            expected_status = "hypothesis" if name == "scope_binding" else "candidate"
            if (claim["status"] != expected_status
                    or (name == "scope_binding" and claim["value"] != case["scope"])):
                continue
            if any((problem["path"] == path or problem["path"].startswith(path + "/"))
                   and problem["code"] != "field_visibility_unestablished" for problem in problems):
                continue
            visibility = "not_visual" if name == "scope_binding" else "clear"
            if claim["visibility"] != visibility:
                values.update(dict.fromkeys(paths))
            elif name == "scope_binding":
                values.update({"/scope/" + key: case["scope"][key] for key in SCOPE_KEYS})
            else:
                values[path] = claim["value"]
        return values

    def _remember_context(self, case: dict, values: dict, *, losses_only: bool = False) -> None:
        for path, value in values.items():
            if value is None:
                if path in self._known_context:
                    self._lost_context.add(path)
            elif not losses_only:
                self._known_context[path] = (value, case["binding"]["context_epoch"])
                self._lost_context.discard(path)

    @staticmethod
    def _fact_identity(case: dict) -> str:
        # Labels, revision/assembly counters, review metadata, source aliases,
        # duplicate assertions and their order are not new factual material.
        return digest({
            "frame": {key: value for key, value in case["binding"].items()
                      if key not in ("snapshot_revision", "assembled_review_ms")},
            "source": case["source"], "scope": case["scope"],
            "claims": {name: {
                "value": claim["value"], "role": claim["role"], "unit": claim["unit"],
                "source_versions": sorted({basis["source_sha256"] for basis in claim["basis"]}),
            } for name, claim in case["claims"].items()},
        })

    def _ordering(self, case: dict) -> list[dict]:
        old_case = self._previous
        binding = case["binding"]
        if old_case is None:
            key = (binding["stream_id"], binding["review_clock_id"])
            if binding["snapshot_revision"] <= self._initial_revision_watermarks.get(key, 0):
                return [issue("snapshot_not_new", "旧修订、被拒绝的修订或失效前修订不能复活推论。", "/binding/snapshot_revision")]
            return []
        old = old_case["binding"]
        if binding["stream_id"] != old["stream_id"] or binding["review_clock_id"] != old["review_clock_id"]:
            return [issue("review_session_changed", "流或时钟域改变，须重建隔离会话。", "/binding")]
        if (case["sample_kind"] != old_case["sample_kind"] or any(
                case["source"][key] != old_case["source"][key] for key in ("kind", "recording_ref"))):
            return [issue("source_changed", "素材种类或原始来源改变，须重建隔离会话。", "/source")]
        if binding["snapshot_revision"] <= self._highest_revision:
            return [issue("snapshot_not_new", "旧修订、被拒绝的修订或失效前修订不能复活推论。", "/binding/snapshot_revision")]
        if binding["sequence"] < old["sequence"] or binding["context_epoch"] < old["context_epoch"]:
            return [issue("frame_order_regressed", "帧顺序或上下文代数倒退。", "/binding")]
        if binding["received_review_ms"] < old["received_review_ms"]:
            return [issue("frame_time_regressed", "复盘收帧起点不能倒退。", "/binding")]
        if binding["frame_id"] == old["frame_id"]:
            if case["source"] != old_case["source"]:
                return [issue("frame_source_changed", "同帧的原始采集引用及历史采集时间不可重写。", "/source")]
            for key in binding.keys() - {"snapshot_revision", "assembled_review_ms"}:
                if binding[key] != old[key]:
                    return [issue("frame_identity_changed", "同帧内容摘要、来源时点、序号及上下文不可重写。", "/binding/" + key)]
            if binding["assembled_review_ms"] < old["assembled_review_ms"]:
                return [issue("assembly_time_regressed", "同帧的新组装时间不能倒退。", "/binding")]
            for name, claim in case["claims"].items():
                previous = self._field_limits.get(name)
                if previous is None:
                    continue
                for key in ("observed_review_ms", "expires_review_ms"):
                    before, after = previous[key], claim[key]
                    if before is not None and after is not None and (after != before if key.startswith("observed") else after > before):
                        return [issue("field_deadline_extended", "同帧重读不能重写观察起点或延长字段原有效期。", "/claims/" + name)]
                for basis in claim["basis"]:
                    prior = self._evidence_limits.get(self._evidence_key(name, basis))
                    if prior and prior["expires_review_ms"] is not None and basis["expires_review_ms"] is not None:
                        if (basis["expires_review_ms"] > prior["expires_review_ms"]
                                or basis["assessed_review_ms"] != prior["assessed_review_ms"]):
                            return [issue("evidence_deadline_extended", "相同依据不能重写检查起点或延长原有效期。", "/claims/" + name)]
        elif binding["sequence"] <= old["sequence"] or binding["frame_id"] in self._seen_frames:
            return [issue("frame_identity_reused", "新帧须有更大序号及未复用的帧引用。", "/binding")]
        return []

    def _context_issues(self, case: dict, values: dict) -> list[dict]:
        for path, value in values.items():
            previous = self._known_context.get(path)
            if (previous is not None and value is not None
                    and (value != previous[0] or path in self._lost_context)
                    and case["binding"]["context_epoch"] <= previous[1]):
                return [issue("context_change_unannounced", "上下文变化或恢复须跨过最后已知上下文的代数；未知态不会清除该边界。", "/binding/context_epoch")]
        return []

    def _remember(self, case: dict) -> None:
        if self._previous is None or case["binding"]["frame_id"] != self._previous["binding"]["frame_id"]:
            self._field_limits.clear()
            self._evidence_limits.clear()
        for name, claim in case["claims"].items():
            limit = self._field_limits.setdefault(name, {"observed_review_ms": None, "expires_review_ms": None})
            for key in limit:
                value = claim[key]
                if value is not None:
                    limit[key] = value if limit[key] is None else min(limit[key], value)
            for basis in claim["basis"]:
                key = self._evidence_key(name, basis)
                if claim["binding"] == case["binding"] and basis["binding"] == case["binding"]:
                    self._seen_evidence.setdefault((name, basis["source_sha256"]), self._locator(basis))
                    if (basis["status"] == "candidate" and basis["scope"] == case["scope"]
                            and canonical(basis["asserted_value"]) == canonical(claim["value"])):
                        location, content = self._assertion_identity(name, basis)
                        self._source_assertions.setdefault(location, content)
                previous = self._evidence_limits.get(key)
                if basis["expires_review_ms"] is not None:
                    self._evidence_limits[key] = {
                        "assessed_review_ms": basis["assessed_review_ms"],
                        "expires_review_ms": basis["expires_review_ms"] if previous is None
                        else min(previous["expires_review_ms"], basis["expires_review_ms"]),
                    }
        self._previous = case
        self._highest_revision = max(self._highest_revision, case["binding"]["snapshot_revision"])
        self._seen_frames.add(case["binding"]["frame_id"])

    def submit(self, request: dict) -> dict:
        with self._lock:
            tick = self._now()
            if tick is None:
                return self._replace("refused", [self._clock_failure])
            try:
                case = validate_shape(request)
            except InputProblem as problem:
                self._generation += 1
                return self._replace("refused", [problem.reason])
            except (ValueError, TypeError, RecursionError, OverflowError):
                self._generation += 1
                return self._replace("refused", [issue("input_malformed", "输入结构或数值不符合隔离合同。")])
            fingerprint = digest(case)
            if fingerprint == self._current_digest:
                return self.view()  # Idempotent read, never recompute or extend the result.
            self._generation += 1
            generation = self._generation
            self._replace("insufficient_evidence")
            ordering = self._ordering(case)
            if self._previous is None and case["binding"]["review_clock_id"] == tick.review_clock_id:
                # A rejected first frame must consume its local revision, not
                # establish a trusted frame/epoch or affect another stream.
                key = (case["binding"]["stream_id"], case["binding"]["review_clock_id"])
                self._initial_revision_watermarks[key] = max(
                    self._initial_revision_watermarks.get(key, 0), case["binding"]["snapshot_revision"])
            elif self._previous is not None and all(
                    case["binding"][key] == self._previous["binding"][key]
                    for key in ("stream_id", "review_clock_id")):
                # A failed ordering attempt also consumes its revision without
                # replacing the last coherent frame/TTL metadata.
                self._highest_revision = max(self._highest_revision, case["binding"]["snapshot_revision"])
            if ordering:
                return self._replace("refused", ordering, case=case)
            refused, gaps, deadlines = semantic_issues(case, review_clock_id=tick.review_clock_id, now=tick.review_ms)
            if case["binding"]["review_clock_id"] != tick.review_clock_id:
                return self._replace("refused", refused, case=case)
            barriers = self._barrier_issues(case)
            history, updates = self._evidence_history(case)
            frame_current = self._context_frame_current(case, refused, tick)
            context = self._context_values(case, refused + gaps + history, tick) if frame_current else {}
            context_ordering = self._context_issues(case, context)
            if context_ordering:
                return self._replace("refused", context_ordering, case=case)
            # Context ordering, like frame ordering, precedes history mutation.
            if frame_current and self._record_retirements(case, tick.review_ms):
                history, updates = self._evidence_history(case)
            refused.extend(history)
            # A valid declaration of loss survives an unrelated refusal, but
            # rejected candidates cannot replace/clear a prior context anchor.
            self._remember_context(case, context, losses_only=bool(refused or barriers))
            if barriers:
                return self._replace("refused", barriers, case=case)
            # The revision watermark was consumed above. Only a current frame
            # may replace coherent metadata, including its context epoch.
            if frame_current:
                self._remember(case)
            if refused:
                return self._replace("refused", refused + gaps, case=case)
            frame_end = case["binding"]["received_review_ms"] + self._max_frame_age_ms
            if tick.review_ms >= frame_end:
                gaps.append(issue("frame_expired", "已到显式复盘帧年龄上限；这不是当前对局新鲜度。", "/binding"))
            if gaps:
                return self._replace("insufficient_evidence", gaps, case=case)
            facts = self._fact_identity(case)
            freshest = max(self._source_result_limits.setdefault(
                (case["binding"]["frame_id"], basis["source_sha256"]), tick.review_ms + self._max_result_ms)
                for claim in case["claims"].values() for basis in claim["basis"])
            end = min(freshest, frame_end, *deadlines)
            end = min(end, self._fact_deadlines.get(facts, end))
            self._fact_deadlines[facts] = end
            if tick.review_ms >= end:
                return self._replace("expired", [issue("fact_result_expired", "同一帧与来源事实的结果期限已用尽，修改修订、标签或定位不能续期。")], case=case)
            analysis = _explain(case)
            status = "hypothesis_only" if analysis["conditions_hold"] else "conditions_not_met"
            self._replace(status, case=case)
            current_updates = {key: value for key, value in self._evidence_updates.items()
                               if any(key == (name, basis["source_sha256"])
                                      for name, claim in case["claims"].items() for basis in claim["basis"])}
            self._result["evidence_updates"] = list({**current_updates, **updates}.values())
            self._result.update(analysis=analysis, evaluated_review_ms=tick.review_ms,
                                expires_review_ms=end)
            # Include calculation, source copying and result serialization in TTL.
            published_copy = deepcopy(self._result)
            canonical(published_copy)
            completed = self._now()
            if completed is None:
                return self._replace("refused", [self._clock_failure], case=case)
            if generation != self._generation:
                return self._replace("refused", [issue("evaluation_superseded", "评估期间发生失效事件，撤回本次推论。")], case=case)
            if completed.review_ms >= self._result["expires_review_ms"]:
                return self._replace("expired", [issue("expired_during_evaluation", "评估或封装耗时已用尽原有效期。")], case=case)
            self._barriers.clear()
            self._evidence_updates.update(updates)
            for name, _ in updates:
                self._pending_evidence.pop(name, None)
            self._current_digest = fingerprint
            return published_copy

    def view(self) -> dict:
        with self._lock:
            tick = self._now()
            if tick is None:
                return self._replace("refused", [self._clock_failure])
            end = self._result["expires_review_ms"]
            if end is not None and tick.review_ms >= end:
                # Remove all candidate values, rationale and source payloads on
                # withdrawal. Keep only a controlled reason and the warning label.
                return self._replace("expired", [issue("result_expired", "假设推论已到期；原修订不会因重读而恢复。")])
            return deepcopy(self._result)
