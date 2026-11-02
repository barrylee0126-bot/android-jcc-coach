"""Entirely fictional offline material. No fixture is a current-game tactic.

Names, version, rules, prices, thresholds, scopes and clock times are invented.
No fixture data is loaded automatically by the core engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import uuid

from .engine import StrategyEngine
from .evidence import Citation, EvidenceQuery, EvidenceReport
from .model import (
    ClockTick, Condition, EvidenceNeed, EvidenceValue, FieldValue, Literal, Rule, Scope, digest,
)


SCOPE = Scope("fixture-region", "fixture-season", "fixture-patch-0", "fixture-mode")
RUNTIME_REF = "fixture:runtime-binding"
UNIT = {"dataset_id": "fixture-catalog-v0", "entity_kind": "unit",
        "entity_id": "fictional-paper-square", "variant": "fixture-only"}
RULE_ID = "fixture:paper-square-buy"


class FixtureClock:
    """Controllable offline clock, unrelated to the host or device's real time."""

    def __init__(self, ms: float = 1100, host_clock_id: str = "fixture-clock"):
        self.ms = ms
        self.host_clock_id = host_clock_id

    def now(self) -> ClockTick:
        return ClockTick(self.host_clock_id, self.ms)

    def advance(self, delta_ms: float) -> None:
        if delta_ms < 0:
            raise ValueError("FixtureClock.advance cannot go backwards")
        self.ms += delta_ms


class FixtureEvidencePort:
    """Mutable test store with revocation barrier; never accepts live/replay use."""

    def __init__(self, reports: tuple[EvidenceReport, ...]):
        self._lock = threading.RLock()
        self._reports = {(report.reference_id, report.purpose): report for report in reports}
        self._identity = "synthetic-fixture-port-" + uuid.uuid4().hex
        self._revision = 0

    def revision(self) -> str:
        with self._lock:
            return f"{self._identity}:{self._revision}"

    def resolve(self, query: EvidenceQuery, *, at: ClockTick) -> EvidenceReport:
        with self._lock:
            if query.sample_kind != "synthetic":
                return EvidenceReport(query.reference_id, query.purpose, "unverifiable",
                                      reason="Fictional fixtures cannot support real gameplay")
            return self._reports.get((query.reference_id, query.purpose), EvidenceReport(
                query.reference_id, query.purpose, "missing", reason="No such synthetic fixture"))

    def get(self, reference_id: str, purpose: str) -> EvidenceReport:
        with self._lock:
            return self._reports[reference_id, purpose]

    def set_report(self, report: EvidenceReport) -> None:
        with self._lock:
            self._reports[report.reference_id, report.purpose] = report
            self._revision += 1

    def drop(self, reference_id: str, purpose: str) -> None:
        with self._lock:
            self._reports.pop((reference_id, purpose), None)
            self._revision += 1


def fixture_rule() -> Rule:
    return Rule(
        rule_id=RULE_ID, revision="fixture-rule-v1", category="trade", scope=SCOPE,
        intent="consider_buy", target=FieldValue("/shop/1/content"),
        target_label="第 2 格的虚构纸片小方",
        conditions=(
            Condition(FieldValue("/scene"), "eq", Literal("battle"), "合成局面处于对局场景。"),
            Condition(FieldValue("/phase"), "eq", Literal("preparation"), "合成局面处于准备阶段。"),
            Condition(FieldValue("/affordances/shop_purchase"), "eq", Literal(True), "合成素材明确给出商店可买。"),
            Condition(FieldValue("/shop/1/content", "/kind"), "eq", Literal("unit"), "第 2 格确认为棋子。"),
            Condition(FieldValue("/shop/1/content", "/catalog_ref"), "eq",
                      EvidenceValue("fixture:unit-mapping"), "对象引用与合成图鉴映射完全一致。"),
            Condition(FieldValue("/shop/1/extensions/fixture:price"), "eq",
                      EvidenceValue("fixture:price"), "画面合成价格与虚构断言一致。"),
            Condition(FieldValue("/gold"), "ge", EvidenceValue("fixture:minimum-gold"),
                      "达到玩具规则自定的 7 金币门槛；这个门槛没有实战含义。"),
            Condition(FieldValue("/extensions/fixture:held-copies"), "eq", Literal(1),
                      "合成素材中已有 1 个同类对象；未实现真实备战席识别。"),
            Condition(FieldValue("/extensions/fixture:free-bench-slots"), "ge", Literal(1),
                      "合成素材中至少有 1 个空位；未实现真实容量判断。"),
        ),
        evidence=(
            EvidenceNeed("fixture:rule-basis", "rule_basis"),
            EvidenceNeed("fixture:unit-mapping", "entity_mapping"),
            EvidenceNeed("fixture:price", "price"),
            EvidenceNeed("fixture:minimum-gold", "toy_threshold"),
        ),
        applicability="只适用于 fixture-mode / fixture-patch-0 的离线合成纸片例子。",
        rationale="在本例虚构对象、价格、预算、持有数与空位条件全部成立时，条件性考虑第 2 格。",
        invalidates_when=("纸片对象、虚构预算或空位前提改变即失效；不推断合成、强度或最优阵容。",),
        max_age_ms=300,
    )


def fixture_state() -> dict:
    """A full game-state-v1 snapshot generated without a file or a real image."""
    def observation(value, *, scope="context", region=None):
        return {
            "status": "recognized", "value": value, "applicability": "applicable", "scope": scope,
            "source_frame_id": "fixture-frame-0", "observed_host_monotonic_ms": 1000,
            "completed_host_monotonic_ms": 1020, "expires_host_monotonic_ms": 2000,
            "method": {"kind": "manual", "name": "fictional-fixture", "version": "fixture-v1"},
            "region": region, "reason": None,
        }

    shop = []
    for index in range(5):
        box = {"x_min": 0.02 + index * 0.19, "y_min": 0.72,
               "x_max": 0.19 + index * 0.19, "y_max": 0.96}
        content = {"kind": "unit", "display_name": "虚构纸片小方", "catalog_ref": dict(UNIT)} \
            if index == 1 else {"kind": "empty"}
        shop.append({"slot": index + 1, "content": observation(content, region=box), "extensions": (
            {"fixture:price": observation(2, region=box)} if index == 1 else {})})
    return {
        "schema_version": "game-state-v1", "sample_kind": "synthetic",
        "stream_id": "fixture-stream", "host_clock_id": "fixture-clock",
        "snapshot_revision": 1, "context_epoch": 0, "frame_id": "fixture-frame-0",
        "assembled_host_monotonic_ms": 1030, "freshness_policy_id": "fixture-ttl-not-calibrated",
        "frames": [{
            "frame_id": "fixture-frame-0", "sequence": 0, "context_epoch": 0,
            "host_received_monotonic_ms": 1000, "device_capture_time": None,
            "image": {"width_px": 1280, "height_px": 800, "orientation": "landscape",
                      "rotation_applied_degrees": 0, "coordinate_space": "decoded_frame_normalized_top_left",
                      "game_viewport": {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                      "calibration_id": "fictional-fixture-layout"},
        }],
        "scene": observation("battle"), "phase": observation("preparation"),
        "round": observation({"stage": 3, "step": 2}), "hp": observation(66),
        "gold": observation(9), "level": observation(4), "mode": observation(SCOPE.mode, scope="stream"),
        "game_version": observation({"region": SCOPE.region, "season": SCOPE.season, "patch": SCOPE.patch,
                                     "verification_ref": RUNTIME_REF}, scope="stream"),
        "shop": shop, "affordances": {"shop_purchase": observation(True)},
        "extensions": {"fixture:held-copies": observation(1), "fixture:free-bench-slots": observation(1)},
    }


def fixture_evidence(rule: Rule | None = None) -> FixtureEvidencePort:
    rule = rule or fixture_rule()
    values = (
        (RUNTIME_REF, "runtime_binding", rule.scope.to_dict(), None),
        ("fixture:rule-basis", "rule_basis", {"rule_id": rule.rule_id, "revision": rule.revision,
                                            "rule_digest": digest(rule.to_dict())}, None),
        ("fixture:unit-mapping", "entity_mapping", UNIT, None),
        ("fixture:price", "price", 2, "fictional_gold"),
        ("fixture:minimum-gold", "toy_threshold", 7, "fictional_gold"),
    )
    reports = []
    for reference_id, purpose, value, unit in values:
        # This hash names generated synthetic assertion content, not a downloaded
        # source or a physical game screenshot.
        assertion = {"material_kind": "synthetic_fixture", "reference_id": reference_id,
                     "purpose": purpose, "value": value, "scope": rule.scope.to_dict()}
        reports.append(EvidenceReport(
            reference_id, purpose, "available", "synthetic_fixture", rule.scope,
            "fixture-stream", "fixture-clock", 1000, 1800, Literal(value),
            (Citation("fixture-snapshot-v1", reference_id, "/value", digest(assertion),
                      "synthetic://jcc_strategy/" + reference_id,
                      "fixture-only:no-real-review"),),
            "纯虚构离线素材，不适用于任何真实游戏版本或模式。", unit,
        ))
    return FixtureEvidencePort(tuple(reports))


@dataclass
class FixtureCase:
    clock: FixtureClock
    state: dict
    rule: Rule
    evidence: FixtureEvidencePort

    def engine(self) -> StrategyEngine:
        return StrategyEngine(clock=self.clock, rules=(self.rule,), evidence=self.evidence)


def fixture_case() -> FixtureCase:
    rule = fixture_rule()
    return FixtureCase(FixtureClock(), fixture_state(), rule, fixture_evidence(rule))


def next_fixture_frame(state: dict, *, context_changed: bool = False) -> dict:
    """Test helper only: advances a fictional frame and re-observes every field."""
    import json
    from .engine import _observations

    updated = json.loads(json.dumps(state))
    old_frame = updated["frames"][-1]
    new_id = "fixture-frame-" + str(old_frame["sequence"] + 1)
    next_epoch = updated["context_epoch"] + int(context_changed)
    received = old_frame["host_received_monotonic_ms"] + 10
    updated.update(frame_id=new_id, snapshot_revision=updated["snapshot_revision"] + 1,
                   context_epoch=next_epoch, assembled_host_monotonic_ms=received + 30)
    frame = dict(old_frame)
    frame.update(frame_id=new_id, sequence=old_frame["sequence"] + 1,
                 context_epoch=next_epoch, host_received_monotonic_ms=received)
    updated["frames"] = [frame]
    for field in _observations(updated).values():
        field.update(source_frame_id=new_id, observed_host_monotonic_ms=received,
                     completed_host_monotonic_ms=received + 20,
                     expires_host_monotonic_ms=2000 if field["status"] == "recognized" else None)
    return updated
