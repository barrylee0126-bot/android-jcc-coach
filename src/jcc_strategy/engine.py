"""Conservative, offline strategy session with current-time and revocation guards."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
import hashlib
import hmac
import re
import secrets
import threading
import uuid
from typing import Any, Iterable

from jcc_device.game_state_contract import ContractViolation, make_validator, validate_state

from .evidence import Citation, EvidencePort, EvidenceQuery, EvidenceReport, RejectingEvidencePort
from .model import (
    Advice, Clock, ClockTick, Decision, EvidenceValue, FieldValue, GuardResult, Literal,
    Reason, Rule, Scope, at_pointer, canonical, digest, valid_ms,
)


def _refused(rule_id: str, *reasons: Reason) -> Decision:
    return Decision("refused", rule_id, tuple(reasons))


def _observations(state: dict) -> dict[str, dict]:
    """Only observation positions defined by game-state-v1 can be dependencies."""
    def escape(key):
        return key.replace("~", "~0").replace("/", "~1")

    result = {"/" + key: state[key] for key in
              ("scene", "phase", "round", "mode", "game_version", "hp", "gold", "level")}
    for index, slot in enumerate(state["shop"]):
        result[f"/shop/{index}/content"] = slot["content"]
        for key, value in slot.get("extensions", {}).items():
            result[f"/shop/{index}/extensions/{escape(key)}"] = value
    for group in ("affordances", "extensions"):
        for key, value in state.get(group, {}).items():
            result[f"/{group}/{escape(key)}"] = value
    return result


class StrategyEngine:
    """One stream/clock per session. Empty rulebook and unresolved evidence by default.

    observe() accepts published contract snapshots; invalidate() is the explicit
    ingress/disconnect barrier before replacement recognition is available.
    Evaluation never performs device, network, file-write or command operations.
    """

    def __init__(self, *, clock: Clock, rules: Iterable[Rule] = (), evidence: EvidencePort | None = None):
        self._clock = clock
        self._evidence = evidence if evidence is not None else RejectingEvidencePort()
        self._rules: dict[str, Rule] = {}
        for rule in rules:
            if not isinstance(rule, Rule) or rule.rule_id in self._rules:
                raise ValueError("Rule IDs must be unique and rules must use the declarative contract")
            self._rules[rule.rule_id] = rule
        self._validator = make_validator()
        self._lock = threading.RLock()
        self._engine_id = "strategy-" + uuid.uuid4().hex
        self._issuance_key = secrets.token_bytes(32)
        self._generation = 0
        self._current: str | None = None
        self._previous: str | None = None
        self._last_tick: ClockTick | None = None
        self._clock_failure: Reason | None = None
        self._unavailable = Reason("state_unavailable", "尚未接纳局面，不能生成建议。")

    def rules(self) -> list[dict]:
        return [rule.to_dict() for rule in self._rules.values()]

    def _tag(self, advice: Advice) -> str:
        body = asdict(advice)
        body.pop("issuance_tag")
        return hmac.new(self._issuance_key, canonical(body).encode("utf-8"), hashlib.sha256).hexdigest()

    def _now(self) -> tuple[ClockTick | None, Reason | None]:
        with self._lock:
            if self._clock_failure:
                return None, self._clock_failure
            try:
                tick = self._clock.now()
                if not isinstance(tick, ClockTick) or not valid_ms(tick.monotonic_ms):
                    raise ValueError("Invalid clock reading")
            except Exception:
                self._clock_failure = Reason("clock_unavailable", "评估时钟不可用；须重建时钟会话。")
                return None, self._clock_failure
            if self._last_tick:
                if tick.host_clock_id != self._last_tick.host_clock_id:
                    self._clock_failure = Reason("clock_domain_changed", "时钟域已改变；须重建会话，不能比较时间。")
                elif tick.monotonic_ms < self._last_tick.monotonic_ms:
                    self._clock_failure = Reason("clock_regressed", "评估时钟倒退；须重建会话。")
                if self._clock_failure:
                    return None, self._clock_failure
            self._last_tick = tick
            return tick, None

    def invalidate(self, reason: str = "画面变化或断连，等待新的可信局面。") -> GuardResult:
        """Call on ingress/change/disconnect even before the next state is ready."""
        with self._lock:
            self._generation += 1
            self._current = None
            self._unavailable = Reason("state_invalidated", reason)
            return GuardResult(False, (self._unavailable,))

    def observe(self, state: dict) -> GuardResult:
        """Validate a defensive copy; rejected new input also revokes old advice."""
        with self._lock:
            try:
                encoded = canonical(state)
                candidate = json.loads(encoded)
                # Re-reading the same accepted snapshot is idempotent, not a new
                # publication. After invalidation, the old revision cannot revive.
                duplicate = encoded == self._current and self._current is not None
                previous = json.loads(self._previous) if self._previous and not duplicate else None
                validate_state(candidate, previous=previous, validator=self._validator)
            except (ValueError, TypeError, OverflowError, RecursionError, ContractViolation):
                self.invalidate("新的局面不符合局面契约；旧建议已撤销。")
                return GuardResult(False, (Reason("invalid_state", "结构、来源、顺序或时间契约检查失败。"),))
            tick, problem = self._now()
            if problem:
                self.invalidate(problem.message)
                return GuardResult(False, (problem,))
            if tick.host_clock_id != candidate["host_clock_id"]:
                self.invalidate("局面与评估时钟不在同一时钟域。")
                return GuardResult(False, (Reason("clock_domain_mismatch", "禁止比较局面与评估器的异域时间。"),))
            if candidate["assembled_host_monotonic_ms"] > tick.monotonic_ms:
                self.invalidate("局面组装时间晚于评估当下。")
                return GuardResult(False, (Reason("state_from_future", "局面时间尚未发生，不能消费。"),))
            if not duplicate:
                self._generation += 1
                self._current = self._previous = encoded
            return GuardResult(True)

    def _field_checks(self, rule: Rule, observations: dict, tick: ClockTick) -> list[Reason]:
        problems = []
        for pointer in rule.dependencies:
            field = observations.get(pointer)
            if field is None:
                problems.append(Reason("field_missing", "缺少所需观察封套，或路径不是受支持的字段。", pointer))
            elif field["applicability"] != "applicable":
                problems.append(Reason("field_not_applicable", "关键字段的适用性尚未成立。", pointer))
            elif field["status"] != "recognized":
                problems.append(Reason("field_" + field["status"], "关键字段不是可信的当前识别值。", pointer))
            elif tick.monotonic_ms >= field["expires_host_monotonic_ms"]:
                problems.append(Reason("field_expired", "按评估当下检查，关键字段已到期。", pointer))
        return problems

    def _evidence_checks(self, query: EvidenceQuery, report: Any, tick: ClockTick) -> list[Reason]:
        def fail(code: str, message: str) -> list[Reason]:
            return [Reason(code, message, reference_id=query.reference_id)]

        if not isinstance(report, EvidenceReport):
            return fail("evidence_malformed", "依据接口未返回约定的证据报告；布尔核验标记不被接纳。")
        if report.reference_id != query.reference_id or report.purpose != query.purpose:
            return fail("evidence_reference_mismatch", "返回的依据引用或用途不匹配。")
        if report.status != "available":
            code = {"unknown": "evidence_unknown", "candidate": "evidence_candidate",
                    "conflict": "evidence_conflict", "stale": "evidence_stale",
                    "missing": "evidence_missing", "unverifiable": "evidence_unverifiable"}.get(
                        report.status, "evidence_malformed")
            return fail(code, "所需依据缺失、未核验、冲突、过期或无法核验，不能生成建议。")
        if report.material_kind != "synthetic_fixture":
            return fail("evidence_not_fixture", "v1 只消费明确标记的离线合成依据，未接通真实规则核验。")
        if report.scope != query.scope:
            return fail("evidence_scope_mismatch", "依据的地区、赛季、补丁或模式不完全匹配。")
        if report.stream_id != query.stream_id:
            return fail("evidence_binding_mismatch", "依据未绑定本次流，不能沿用旧流的版本核验。")
        if report.host_clock_id != tick.host_clock_id:
            return fail("evidence_clock_mismatch", "依据与评估不在同一时钟域，不能校验有效期。")
        assessed, expiry = report.assessed_host_monotonic_ms, report.expires_host_monotonic_ms
        if not valid_ms(assessed) or not valid_ms(expiry) or expiry <= assessed or assessed > tick.monotonic_ms:
            return fail("evidence_time_invalid", "依据有效区间缺失、不合法或评估时间尚未发生。")
        if tick.monotonic_ms >= expiry:
            return fail("evidence_expired", "所需依据已在当前评估时间到期。")
        if (not isinstance(report.value, Literal) or report.value.value() is None
                or type(report.conditions) is not str or not report.conditions.strip()
                or not isinstance(report.citations, tuple) or not report.citations):
            return fail("evidence_incomplete", "依据缺少断言值、适用条件或可定位的引用。")
        for citation in report.citations:
            if (not isinstance(citation, Citation)
                    or any(type(value) is not str or not value.strip() for value in
                           (citation.snapshot_id, citation.record_key, citation.field_pointer,
                            citation.locator, citation.review_ref))
                    or type(citation.source_sha256) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", citation.source_sha256)):
                return fail("evidence_incomplete", "依据定位或内容摘要不完整，不能仅凭核验标签消费。")
        if query.purpose == "runtime_binding" and report.value != Literal(query.scope.to_dict()):
            return fail("runtime_binding_mismatch", "版本核验引用并未确认本局面的完整版本与模式。")
        if query.purpose == "rule_basis" and report.value != Literal({
            "rule_id": query.rule_id, "revision": query.rule_revision, "rule_digest": query.rule_digest,
        }):
            return fail("rule_basis_mismatch", "规则依据没有绑定当前规则的修订与内容。")
        return []

    def evaluate(self, rule_id: str) -> Decision:
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return _refused(rule_id, Reason("rule_unknown", "规则容器中没有这条规则。"))
            if self._current is None:
                return _refused(rule_id, self._unavailable)
            encoded, generation = self._current, self._generation
        state = json.loads(encoded)
        tick, problem = self._now()
        if problem:
            return _refused(rule_id, problem)
        if tick.host_clock_id != state["host_clock_id"]:
            return _refused(rule_id, Reason("clock_domain_mismatch", "局面与评估时钟域不匹配。"))
        if state["sample_kind"] != "synthetic":
            return _refused(rule_id, Reason("real_integration_unavailable",
                                           "v1 只有合成离线规则；真实画面和回放均不能触发建议。"))
        if rule.material_kind != "synthetic_fixture":
            return _refused(rule_id, Reason("rule_unreviewed", "未审查的规则不能借合成运行自动获得核验。"))
        observations = _observations(state)
        problems = self._field_checks(rule, observations, tick)
        if problems:
            return _refused(rule_id, *problems)
        version = state["game_version"]["value"]
        scope = Scope(version["region"], version["season"], version["patch"], state["mode"]["value"])
        if scope.mode != rule.scope.mode:
            return _refused(rule_id, Reason("mode_mismatch", "规则与当前局面模式不一致。", "/mode"))
        if scope != rule.scope:
            return _refused(rule_id, Reason("version_mismatch", "规则与局面的地区、赛季或补丁不一致。",
                                           "/game_version"))

        try:
            evidence_revision = self._evidence.revision()
            if type(evidence_revision) is not str or not evidence_revision.strip():
                raise ValueError("Missing evidence revision barrier")
        except Exception:
            return _refused(rule_id, Reason("evidence_unavailable", "依据接口缺少可检查的一致性屏障。"))
        rule_digest = digest(rule.to_dict())
        if version["verification_ref"] in {need.reference_id for need in rule.evidence}:
            return _refused(rule_id, Reason("ambiguous_evidence_reference",
                                           "运行版本核验与规则依据不能复用同一个引用标识。"))
        queries = [EvidenceQuery(version["verification_ref"], "runtime_binding", scope,
                                 state["stream_id"], state["sample_kind"], rule_id, rule.revision, rule_digest)]
        queries.extend(EvidenceQuery(need.reference_id, need.purpose, scope, state["stream_id"],
                                     state["sample_kind"], rule_id, rule.revision, rule_digest)
                       for need in rule.evidence)
        reports = []
        evidence_values = {}
        for query in queries:
            try:
                report = self._evidence.resolve(query, at=tick)
                problems = self._evidence_checks(query, report, tick)
            except Exception:
                return _refused(rule_id, Reason("evidence_unavailable", "依据接口失败，未采纳其内容。",
                                               reference_id=query.reference_id))
            if problems:
                return _refused(rule_id, *problems)
            reports.append(report)
            evidence_values[query.reference_id] = report.value.value()

        def value(operand):
            if isinstance(operand, Literal):
                return operand.value()
            if isinstance(operand, EvidenceValue):
                return evidence_values[operand.reference_id]
            return at_pointer(observations[operand.pointer]["value"], operand.value_pointer)

        matched = []
        for condition in rule.conditions:
            try:
                left, right = value(condition.left), value(condition.right)
            except (KeyError, IndexError, ValueError, TypeError):
                return _refused(rule_id, Reason("field_value_missing", "关键观察值缺少规则要求的成员。",
                                               condition.left.pointer))
            if condition.operator == "eq":
                satisfied = canonical(left) == canonical(right)
            elif (type(left) not in (int, float) or type(right) not in (int, float)):
                return _refused(rule_id, Reason("condition_type_mismatch", "数值条件要求数值，布尔值不能当作数字。",
                                               condition.left.pointer))
            else:
                satisfied = {"ge": lambda: left >= right, "gt": lambda: left > right,
                             "le": lambda: left <= right, "lt": lambda: left < right}[condition.operator]()
            if not satisfied:
                return Decision("not_applicable", rule_id, (
                    Reason("condition_not_met", "条件未满足：" + condition.explanation, condition.left.pointer),))
            matched.append({"condition": condition.explanation, "field": condition.left.pointer,
                            "operator": condition.operator, "observed": left, "required": right})
        try:
            target_value = value(rule.target)
        except (KeyError, IndexError, ValueError, TypeError):
            return _refused(rule_id, Reason("target_unresolved", "建议对象尚未明确。", rule.target.pointer))

        expiry = min(tick.monotonic_ms + rule.max_age_ms,
                     *(observations[p]["expires_host_monotonic_ms"] for p in rule.dependencies),
                     *(r.expires_host_monotonic_ms for r in reports))
        finished, problem = self._now()
        if problem:
            return _refused(rule_id, problem)
        if finished.monotonic_ms >= expiry:
            return _refused(rule_id, Reason("expired_during_evaluation", "评估完成前局面、依据或建议已到期。"))
        # Recheck every report with the completion time, never the old assembly
        # time. A response arriving in a different clock domain cannot revive it.
        for query, report in zip(queries, reports):
            problems = self._evidence_checks(query, report, finished)
            if problems:
                return _refused(rule_id, *problems)
        try:
            if self._evidence.revision() != evidence_revision:
                return _refused(rule_id, Reason("evidence_changed_during_evaluation",
                                               "评估期间依据发生变化；需要重新评估。"))
        except Exception:
            return _refused(rule_id, Reason("evidence_unavailable", "无法在评估结束时复核依据修订。"))
        with self._lock:
            if self._generation != generation or self._current != encoded:
                return _refused(rule_id, Reason("evaluation_superseded", "评估期间已出现新局面或失效屏障。"))
            basis = [report.to_dict() for report in reports]
            evidence_binding = {"revision": evidence_revision, "reports": basis}
            payload = {
                "label": "仅供合成离线测试：虚构素材，不代表游戏战术，不得用于实机。",
                "sample_kind": "synthetic", "status": "conditional_fixture",
                "rule_id": rule_id, "rule_revision": rule.revision, "category": rule.category,
                "intent": rule.intent, "target": {"field": rule.target.pointer,
                                                "label": rule.target_label, "value": target_value},
                "scope": scope.to_dict(), "applicability": rule.applicability,
                "conditions_satisfied": matched, "rationale": rule.rationale, "basis": basis,
                "evidence_revision": evidence_revision,
                "state_binding": {key: state[key] for key in ("schema_version", "sample_kind", "stream_id",
                                  "host_clock_id", "frame_id", "snapshot_revision", "context_epoch",
                                  "freshness_policy_id", "assembled_host_monotonic_ms")},
                "field_dependencies": [{"pointer": pointer, "observation": observations[pointer]}
                                       for pointer in rule.dependencies],
                "invalidates_when": [
                    "任一新帧、快照修订、场景代数、模式或版本变化。",
                    "关键字段未知、冲突、不适用、到期或失去来源。",
                    "依据撤销、内容变化、无法核验、时钟域改变或到期。",
                    "收到画面变化、断连等显式失效通知，或建议自身到期。",
                    *rule.invalidates_when,
                ],
            }
            frozen_payload = Literal(payload)
            # Include serialization and adapter work in freshness, rather than
            # treating the earlier completion checkpoint as publication time.
            published, problem = self._now()
            if problem:
                return _refused(rule_id, problem)
            if published.monotonic_ms >= expiry:
                return _refused(rule_id, Reason("expired_during_evaluation", "发布前检查发现建议或依赖已到期。"))
            advice = Advice(self._engine_id, generation, rule_id, rule_digest, digest(state), digest(evidence_binding),
                            tick.host_clock_id, published.monotonic_ms, expiry, frozen_payload)
            advice = replace(advice, issuance_tag=self._tag(advice))
            try:
                if self._evidence.revision() != evidence_revision:
                    return _refused(rule_id, Reason("evidence_changed_during_evaluation",
                                                   "封装输出期间依据变化，撤销本次结果。"))
            except Exception:
                return _refused(rule_id, Reason("evidence_unavailable", "发布前无法复核依据修订。"))
            final_tick, problem = self._now()
            if problem:
                return _refused(rule_id, problem)
            if final_tick.monotonic_ms >= expiry:
                return _refused(rule_id, Reason("expired_during_evaluation", "封装输出期间建议或依赖已到期。"))
            if self._generation != generation or self._current != encoded:
                return _refused(rule_id, Reason("evaluation_superseded", "发布前局面或失效屏障已更新。"))
            return Decision("conditional_fixture", rule_id, (), advice)

    def revalidate(self, advice: Advice) -> GuardResult:
        """Use on every display read/tick and state change; never renew old TTL."""
        if not isinstance(advice, Advice):
            return GuardResult(False, (Reason("advice_malformed", "不是本接口生成的建议对象。"),))
        if advice.engine_id != self._engine_id:
            return GuardResult(False, (Reason("session_changed", "建议属于另一个策略会话。"),))
        try:
            if not hmac.compare_digest(advice.issuance_tag, self._tag(advice)):
                raise ValueError("Changed advice")
        except Exception:
            return GuardResult(False, (Reason("advice_modified", "建议内容或有效期已被改写，不能继续使用。"),))
        tick, problem = self._now()
        if problem:
            return GuardResult(False, (problem,))
        if advice.host_clock_id != tick.host_clock_id:
            return GuardResult(False, (Reason("clock_domain_mismatch", "建议与评估时钟域不一致。"),))
        if tick.monotonic_ms >= advice.expires_host_monotonic_ms:
            return GuardResult(False, (Reason("advice_expired", "建议到期；复核不会延长旧建议有效期。"),))
        with self._lock:
            if advice.generation != self._generation or self._current is None:
                return GuardResult(False, (Reason("state_changed", "局面或失效屏障已改变，撤销旧建议。"),))
        decision = self.evaluate(advice.rule_id)
        if decision.advice is None:
            return GuardResult(False, decision.reasons)
        current = decision.advice
        if (advice.rule_digest != current.rule_digest or advice.snapshot_digest != current.snapshot_digest
                or advice.evidence_digest != current.evidence_digest or advice.payload != current.payload):
            return GuardResult(False, (Reason("dependency_changed", "规则、局面或所需依据内容已变化。"),))
        with self._lock:
            tick, problem = self._now()
            if problem:
                return GuardResult(False, (problem,))
            if tick.monotonic_ms >= advice.expires_host_monotonic_ms:
                return GuardResult(False, (Reason("advice_expired", "复核完成时旧建议已经到期。"),))
            if advice.generation != self._generation or self._current is None:
                return GuardResult(False, (Reason("state_changed", "复核期间局面已变化。"),))
            return GuardResult(True)
