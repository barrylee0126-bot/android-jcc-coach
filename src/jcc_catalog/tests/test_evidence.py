"""Synthetic evidence only: no game assets, live services or device dependency."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jcc_catalog import Catalog, CatalogError, FieldRef, SourceSpec
from jcc_catalog.evidence import (
    ClientBinding, EvidenceError, EvidenceIndex, FieldAssertion, RevisionCoverage,
    RevisionEvent, SourceEvidence,
)


T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T01:00:00Z"
T2 = "2026-01-01T02:00:00Z"
T3 = "2026-01-01T03:00:00Z"
SCOPE = {"game": "demo-game", "region": "demo-region", "season": "DEMO",
         "season_theme": "示例主题", "mode": "demo-mode", "patch": "demo-patch",
         "hotfix": "demo-base-event"}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.json"
        self.records = [{"id": "shared", "name": "示例", "gold": 4, "a/b": {"~x": 2}},
                        {"id": "shared", "name": "示例+", "gold": 6},
                        {"id": "shared", "name": "示例+", "gold": 8}]
        self.path.write_text(json.dumps(self.records, ensure_ascii=False), encoding="utf-8")
        self.before = self.path.read_bytes()
        self.catalog = Catalog([SourceSpec("demo-candidates", "augment", self.path,
                                           claimed_scope=SCOPE, source_url="https://example.invalid/candidates",
                                           retrieved_at=T0)])
        self.refs = [FieldRef(item["record_key"], "/gold") for item in self.catalog.find()]
        self.index = EvidenceIndex(self.catalog)
        self.notice = SourceEvidence("notice", "official_documented",
                                     {"url": "https://example.invalid/notice/image-2", "section": "图2：中部奖励行"},
                                     scope=SCOPE, source_published_at=T0, retrieved_at=T0,
                                     reviewed_at=T0, review_note="合成：发布者和参数的人工读取记录")
        self.index.add_source(self.notice)

    def assertion(self, assertion_id="a", evidence_id="notice", value=4, **changes):
        data = FieldAssertion(assertion_id, evidence_id, "demo-subject", "/reward/gold", value,
                              scope=SCOPE, variant_key="base", semantic="initial_gold_reward",
                              unit="gold", conditions={"round": "demo-3-2"},
                              interpretation_note="合成：只读取指定奖励字段，不提升完整效果",
                              release_event_id="demo-release", announced_effective_at=T1)
        return replace(data, **changes)

    def add(self, assertion_id="a", evidence_id="notice", value=4, **changes):
        return self.index.add_assertion(self.assertion(assertion_id, evidence_id, value, **changes))

    def observation(self, evidence_id="observation", **changes):
        source = SourceEvidence(evidence_id, "client_observed", {"observation_id": "demo-frame-1"},
                                scope=SCOPE, observed_at=T1, retrieved_at=T1,
                                reviewed_at=T1, review_note="合成：读取当前上下文的观察记录")
        self.index.add_source(replace(source, **changes))

    def binding(self, **changes):
        return replace(ClientBinding("demo-context", SCOPE, ("observation",), ("demo-release",), T1, T3), **changes)

    def coverage(self, coverage_id="coverage", evidence_id="notice", **changes):
        record = RevisionCoverage(coverage_id, evidence_id, ("a",), T1, T2,
                                  "合成：记录渠道、读取区间和失败项", scope=SCOPE)
        return self.index.add_coverage(replace(record, **changes))

    def assert_error(self, code, operation):
        with self.assertRaises(CatalogError) as raised:
            operation()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_exact_record_and_field_links_preserve_shared_id_variants(self):
        self.add("base", value=4)
        self.add("plus", value=6, variant_key="plus")
        self.index.link_field(self.refs[0], "base", reason="合成基础变体定位")
        self.index.link_field(self.refs[1], "plus", reason="合成加号变体定位")
        traces = [self.index.trace_field(ref) for ref in self.refs]
        self.assertEqual(["示例", "示例+", "示例+"], [item["record"]["name"] for item in traces])
        self.assertEqual([["base"], ["plus"], []],
                         [[link["assertion_id"] for link in item["links"]] for item in traces])
        self.assertEqual(3, len({item["record"]["record_key"] for item in traces}))
        raw = traces[0]["field"]["candidates"][0]
        self.assertEqual("/0/gold", raw["origin"]["json_pointer"])
        self.assertEqual(hashlib.sha256(self.before).hexdigest(), raw["origin"]["sha256"])
        self.assertEqual(T0, raw["origin"]["retrieved_at"])
        backlinks = self.index.explain("base", at=T2)["candidate_links"]
        self.assertEqual(self.refs[0].record_key, backlinks[0]["record"]["record_key"])
        self.assertEqual("/0/gold", backlinks[0]["field"]["candidates"][0]["origin"]["json_pointer"])
        self.assertEqual(self.before, self.path.read_bytes())

    def test_source_location_review_and_assertion_interpretation_are_separate(self):
        self.add(value=[12, 18], unit="percent_of_demo_attribute", semantic="demo_bonus_coefficient",
                 conditions=None)
        self.index.link_field(self.refs[0], "a", reason="合成：字段只提供关联线索", relationship="same_fact")
        trace = self.index.trace_field(self.refs[0])
        link = trace["links"][0]
        self.assertEqual(4, trace["field"]["candidates"][0]["source_value"])
        self.assertEqual([12, 18], link["assertion"]["asserted_value"])
        self.assertEqual("different", link["raw_value_relation"])
        self.assertEqual("not_determined", link["semantic_consistency"])
        self.assertEqual("图2：中部奖励行", link["source"]["locator"]["section"])
        self.assertEqual("review_recorded", link["source"]["authenticity"])
        self.assertFalse(link["source"]["authenticity_authenticated"])
        self.assertIsNone(link["assertion"]["conditions"])
        self.assertIsNone(trace["value"])

    def test_official_statement_never_becomes_client_observation(self):
        self.add()
        result = self.index.explain("a", at=T2)
        self.assertEqual("official_documented", result["source"]["evidence_kind"])
        self.assertIsNone(result["source"]["observed_at"])
        self.assertEqual("announced_effective_at", result["temporal"]["start_origin"])
        self.assertEqual("not_established", result["temporal"]["effective_in_client"])
        self.assertIn("client_binding_missing", result["reasons"])
        self.assertIn("source_review_not_authenticated", result["reasons"])
        self.assert_error("not_an_observation", lambda: self.add("bad", observed_effective_at=T1))
        self.assert_error("not_an_observation", lambda: self.index.add_source(
            replace(self.notice, evidence_id="bad-source", observed_at=T1)))

    def test_observation_keeps_its_own_source_and_effective_time(self):
        self.observation()
        self.add(evidence_id="observation", observed_effective_at=T2)
        result = self.index.explain("a", at=T2)
        self.assertEqual(T1, result["source"]["observed_at"])
        self.assertEqual(T2, result["assertion"]["observed_effective_at"])
        self.assertEqual(T1, result["assertion"]["announced_effective_at"])
        self.assertEqual("observed_effective_at", result["temporal"]["start_origin"])
        self.assertFalse(result["decision_eligible"])

    def test_source_and_client_scope_unknown_are_not_wildcards(self):
        self.index.add_source(SourceEvidence("unknown", "unknown", {"local_path": "/synthetic/report.txt"}))
        self.add(evidence_id="unknown")
        result = self.index.explain("a", at=T2, client=ClientBinding("partial"))
        self.assertEqual("unknown", result["source_scope_relation"]["status"])
        self.assertIn("source_assertion_scope_unknown", result["reasons"])
        self.assertIn("client_scope_unknown", result["reasons"])
        self.assertEqual(list(SCOPE), result["source_scope_relation"]["unknown_dimensions"])
        self.assertTrue(all(value is None for value in result["source"]["scope"].values()))

    def test_each_different_scope_axis_prevents_same_fact_conflict(self):
        self.add("a", value=4)
        for dimension in SCOPE:
            with self.subTest(dimension=dimension):
                other_scope = {**SCOPE, dimension: "other-demo-value"}
                source_id = "source-" + dimension
                assertion_id = "assertion-" + dimension
                self.index.add_source(replace(self.notice, evidence_id=source_id, scope=other_scope))
                self.add(assertion_id, source_id, 6, scope=other_scope)
                result = self.index.compare_assertions("a", assertion_id, relationship="same_fact", reason="合成跨域对照")
                self.assertEqual("different_scope", result["comparison"])
                self.assertEqual("candidate", result["status"])
                self.assertEqual([dimension], result["scope_relation"]["different_dimensions"])
                self.assertIsNone(result["value"])

    def test_same_patch_or_name_does_not_transfer_tft_evidence(self):
        other = {**SCOPE, "game": "demo-tft"}
        self.index.add_source(replace(self.notice, evidence_id="tft-notice", scope=other))
        self.add("a", value=4)
        self.add("b", "tft-notice", 6, scope=SCOPE)
        result = self.index.compare_assertions("a", "b", relationship="same_fact", reason="合成同名同补丁")
        self.assertEqual("different_scope", result["comparison"])
        explanation = self.index.explain("b", at=T2)
        self.assertIn("source_assertion_scope_different", explanation["reasons"])
        self.assertEqual("different", explanation["source_scope_relation"]["status"])

    def test_assertion_comparison_requires_explicit_hypothesis_and_retains_conflict(self):
        self.add("a", value=4)
        self.add("b", value=6)
        result = self.index.compare_assertions("a", "b")
        self.assertEqual("relationship_unresolved", result["comparison"])
        self.assertEqual("different", result["raw_value_relation"])
        self.assert_error("invalid_text", lambda: self.index.compare_assertions("a", "b", relationship="same_fact"))
        record = self.index.record_comparison("conflict-1", "a", "b", reason="合成：两行应描述同一奖励")
        self.assertEqual("conflict", record["assessment"]["status"])
        self.assertEqual([4, 6], [item["asserted_value"] for item in record["assessment"]["assertions"]])
        self.assertEqual("not_determined", record["assessment"]["semantic_contradiction"])
        result = self.index.explain("a", at=T2)
        self.assertEqual("conflict-1", result["comparisons"][0]["comparison_id"])
        self.assertIn("reported_conflict_unresolved", result["reasons"])
        self.assertIsNone(result["value"])

    def test_different_variants_or_subjects_are_not_combined(self):
        self.add("a", value=4)
        self.add("plus", value=6, variant_key="plus")
        self.add("other", value=6, subject_key="another-demo-subject")
        for other in ("plus", "other"):
            result = self.index.compare_assertions("a", other, relationship="same_fact", reason="合成对照")
            self.assertEqual("different_subject_or_variant", result["comparison"])
            self.assertEqual("candidate", result["status"])
        self.assertEqual(["plus"], [item["assertion_id"] for item in self.index.find_assertions(variant_key="plus")])

    def test_unknown_variant_semantics_or_scope_cannot_establish_contradiction(self):
        self.add("a", value=4)
        for key, value in (("variant_key", None), ("semantic", None), ("unit", None),
                           ("conditions", None), ("scope", {})):
            other = "unknown-" + key
            self.add(other, value=6, **{key: value})
            result = self.index.compare_assertions("a", other, relationship="same_fact", reason="合成缺项")
            self.assertEqual("identity_meaning_or_scope_unknown", result["comparison"])
            self.assertEqual("different", result["raw_value_relation"])
            self.assertEqual("candidate", result["status"])

    def test_units_conditions_and_meaning_are_not_silently_normalized(self):
        self.add("a", value=4)
        for key, value in (("semantic", "absolute_demo_damage"), ("unit", "percent"),
                           ("conditions", {"round": "demo-4-2"}), ("field_key", "/reward/other")):
            other = "different-" + key
            self.add(other, value=6, **{key: value})
            result = self.index.compare_assertions("a", other, relationship="same_fact", reason="合成语义对照")
            self.assertEqual("different_meaning_unit_or_conditions", result["comparison"])
            self.assertEqual("candidate", result["status"])

    def test_agreement_is_not_verification_or_independent_vote(self):
        self.add("a", value=4)
        self.add("b", value=4)
        result = self.index.compare_assertions("a", "b", relationship="same_fact", reason="合成两行同值")
        self.assertEqual("agreeing_assertions", result["comparison"])
        self.assertEqual("not_determined", result["source_independence"])
        self.assertFalse(result["decision_eligible"])
        self.assert_error("duplicate_reference", lambda: self.index.compare_assertions("a", "a"))

    def test_null_zero_false_and_numeric_types_remain_distinct(self):
        self.add("null", value=None)
        self.add("zero", value=0)
        self.add("bool", value=False)
        result = self.index.compare_assertions("zero", "bool", relationship="same_fact", reason="合成类型差异")
        self.assertEqual("conflict", result["status"])
        self.assertEqual("unknown", self.index.compare_assertions("null", "zero")["status"])
        self.assertIn("asserted_value_unknown", self.index.explain("null", at=T2)["reasons"])
        self.assertNotIn("asserted_value_unknown", self.index.explain("zero", at=T2)["reasons"])

    def test_field_pointer_escapes_missing_and_non_normalized_raw_values(self):
        self.add()
        escaped = FieldRef(self.refs[0].record_key, "/a~1b/~0x")
        missing = FieldRef(self.refs[0].record_key, "/not-present")
        self.index.link_field(escaped, "a", reason="合成转义字段")
        self.index.link_field(missing, "a", reason="合成缺失字段定位")
        self.assertEqual("/0/a~1b/~0x", self.index.trace_field(escaped)["field"]["candidates"][0]["origin"]["json_pointer"])
        result = self.index.trace_field(missing)
        self.assertEqual("missing", result["field"]["candidates"][0]["value_state"])
        self.assertEqual("unknown", result["links"][0]["raw_value_relation"])
        self.assert_error("invalid_pointer", lambda: self.index.link_field(
            FieldRef(escaped.record_key, "/bad~2escape"), "a", reason="合成坏路径"))

    def test_effective_start_and_exclusive_expiry_are_timezone_aware(self):
        self.add(valid_until=T3)
        for at, expected in ((T0, "not_yet_effective"), (T1, "inside_declared_window"),
                             ("2026-01-01T02:59:59.999999Z", "inside_declared_window"),
                             (T3, "expired"), ("2026-01-01T11:00:00+08:00", "expired")):
            with self.subTest(at=at):
                result = self.index.explain("a", at=at)
                self.assertEqual(expected, result["temporal"]["status"])
                self.assertFalse(result["decision_eligible"])
        self.assert_error("invalid_interval", lambda: self.add("bad", valid_until=T1))

    def test_unknown_start_and_unknown_end_are_not_infinite_validity(self):
        self.add("a")
        self.add("no-start", announced_effective_at=None)
        self.assertEqual("end_unknown", self.index.explain("a", at=T2)["temporal"]["status"])
        self.assertEqual("start_unknown", self.index.explain("no-start", at=T2)["temporal"]["status"])
        self.assertIn("revision_coverage_unknown_or_incomplete", self.index.explain("a", at=T2)["reasons"])

    def test_scheduled_revision_only_becomes_due_at_effective_boundary(self):
        self.add("a", value=4)
        self.add("replacement", value=6, announced_effective_at=T3)
        self.index.add_revision(RevisionEvent("rev", "notice", ("a",), "合成后续修订", SCOPE, T3, ("replacement",)))
        before = self.index.explain("a", at=T2)
        due = self.index.explain("a", at=T3)
        self.assertEqual("scheduled", before["revisions"][0]["state"])
        self.assertNotIn("revision_due_unadjudicated", before["reasons"])
        self.assertEqual("due_unadjudicated", due["revisions"][0]["state"])
        self.assertIn("revision_due_unadjudicated", due["reasons"])
        self.assertEqual(4, due["assertion"]["asserted_value"])
        self.assertEqual(["replacement"], due["revisions"][0]["record"]["replacement_ids"])
        self.assertIsNone(due["value"])

    def test_unknown_revision_time_or_scope_is_explicit(self):
        self.add()
        self.index.add_revision(RevisionEvent("unknown-time", "notice", ("a",), "合成未知生效时间", SCOPE))
        self.index.add_revision(RevisionEvent("unknown-scope", "notice", ("a",), "合成未知范围", {}, T2))
        result = self.index.explain("a", at=T2)
        self.assertEqual(["effective_time_unknown", "due_scope_unknown"], [item["state"] for item in result["revisions"]])
        self.assertIn("revision_effective_time_unknown", result["reasons"])
        self.assertIn("revision_due_scope_unknown", result["reasons"])

    def test_cross_game_revision_never_replaces_jcc_assertion(self):
        self.add()
        other = {**SCOPE, "game": "demo-tft"}
        self.index.add_source(replace(self.notice, evidence_id="foreign", scope=other))
        self.index.add_revision(RevisionEvent("foreign-rev", "foreign", ("a",), "合成跨游戏线索", other, T2))
        result = self.index.explain("a", at=T2)
        self.assertEqual("different_scope", result["revisions"][0]["state"])
        self.assertEqual(4, result["assertion"]["asserted_value"])
        self.assertNotIn("revision_due_unadjudicated", result["reasons"])

    def test_future_source_evidence_cannot_be_used_in_replay_time(self):
        self.index.add_source(replace(self.notice, evidence_id="future", retrieved_at=T3, reviewed_at=T3))
        self.add("a", "future")
        self.index.add_revision(RevisionEvent("future-rev", "future", ("a",), "合成迟到公告", SCOPE, T1))
        self.coverage(evidence_id="future")
        result = self.index.explain("a", at=T2)
        self.assertIn("source_evidence_from_future", result["reasons"])
        self.assertEqual("evidence_from_future", result["revisions"][0]["state"])
        self.assertEqual("evidence_from_future", result["coverage"][0]["state"])
        self.assertEqual("unknown_or_incomplete", result["coverage_status"])

    def test_future_comparison_evidence_stays_visible_without_backdating_conflict(self):
        self.add("a", value=4)
        self.index.add_source(replace(self.notice, evidence_id="future", retrieved_at=T3, reviewed_at=T3))
        self.add("b", "future", 6)
        self.index.record_comparison("future-comparison", "a", "b", reason="合成迟到反证")
        before = self.index.explain("a", at=T2)
        self.assertEqual("evidence_from_future", before["comparisons"][0]["availability"])
        self.assertIn("comparison_evidence_from_future", before["reasons"])
        self.assertNotIn("reported_conflict_unresolved", before["reasons"])
        after = self.index.explain("a", at=T3)
        self.assertEqual("recorded_inputs_available", after["comparisons"][0]["availability"])
        self.assertIn("reported_conflict_unresolved", after["reasons"])

    def test_coverage_closed_endpoint_and_future_or_expired_boundary(self):
        self.add()
        self.coverage()
        result = self.index.explain("a", at=T1)
        self.assertEqual("check_boundary_from_future", result["coverage"][0]["state"])
        exact = self.index.explain("a", at=T2)
        self.assertEqual("reported_interval_covers_query", exact["coverage_status"])
        self.assertIn("revision_coverage_not_authenticated", exact["reasons"])
        after = self.index.explain("a", at="2026-01-01T02:00:00.000001Z")
        self.assertEqual("outdated", after["coverage"][0]["state"])
        self.assertEqual("unknown_or_incomplete", after["coverage_status"])
        self.assertFalse(exact["decision_eligible"])

    def test_coverage_gaps_or_different_scopes_do_not_imply_completeness(self):
        self.add()
        self.coverage("gap", checked_from="2026-01-01T01:00:00.000001Z")
        self.coverage("different", scope={**SCOPE, "mode": "demo-other-mode"})
        self.coverage("unknown", scope={})
        result = self.index.explain("a", at=T2)
        self.assertEqual(["initial_gap", "different_scope", "scope_unknown"], [item["state"] for item in result["coverage"]])
        self.assertEqual("unknown_or_incomplete", result["coverage_status"])
        self.assert_error("invalid_interval", lambda: self.coverage("bad", checked_from=T3, checked_through=T2))

    def test_client_expiry_and_release_scope_have_separate_blockers(self):
        self.add()
        self.observation()
        result = self.index.explain("a", at=T2, client=self.binding())
        self.assertNotIn("client_binding_missing", result["reasons"])
        self.assertIn("client_binding_not_authenticated", result["reasons"])
        for binding, reason in ((self.binding(expires_at=T2), "client_binding_expired"),
                                (self.binding(expires_at=None), "client_binding_expiry_unknown"),
                                (self.binding(release_event_ids=("other-event",)), "client_release_event_unbound"),
                                (self.binding(scope={**SCOPE, "mode": "other-mode"}), "client_scope_different"),
                                (self.binding(observed_at=T3, expires_at=None), "client_observation_from_future"),
                                (self.binding(evidence_ids=("notice",)), "client_evidence_is_not_an_observation")):
            with self.subTest(reason=reason):
                self.assertIn(reason, self.index.explain("a", at=T2, client=binding)["reasons"])
        self.assert_error("invalid_interval", lambda: self.index.explain("a", at=T2, client=self.binding(expires_at=T1)))

    def test_even_all_reported_prerequisites_cannot_unlock_current_value(self):
        self.add(valid_until=T3)
        self.observation()
        self.coverage()
        result = self.index.explain("a", at=T2, client=self.binding())
        self.assertEqual("same", result["source_scope_relation"]["status"])
        self.assertEqual("same", result["client_binding"]["scope_relation"]["status"])
        self.assertEqual("reported_interval_covers_query", result["coverage_status"])
        self.assertEqual("inside_declared_window", result["temporal"]["status"])
        error = self.assert_error("current_value_unavailable", lambda: self.index.require_current_value("a", at=T2, client=self.binding()))
        self.assertIn("current_value_adjudication_not_implemented", error.reasons)
        self.assertFalse(result["decision_eligible"])
        self.assertIsNone(result["value"])
        self.assert_error("unverified_field", lambda: self.catalog.require_verified_value(self.refs[0].record_key, "/gold"))

    def test_verified_flags_are_not_a_supported_authority(self):
        for constructor, kwargs in ((SourceEvidence, {"evidence_id": "x", "evidence_kind": "official_documented", "locator": {}}),
                                    (FieldAssertion, {"assertion_id": "x", "evidence_id": "notice", "subject_key": "x", "field_key": "/x", "asserted_value": 4}),
                                    (ClientBinding, {"context_id": "x"})):
            with self.subTest(constructor=constructor.__name__), self.assertRaises(TypeError):
                constructor(**kwargs, verified=True)
        self.assert_error("invalid_locator", lambda: self.index.add_source(replace(
            self.notice, evidence_id="x", locator={"url": "https://example.invalid/notice", "verified": True})))
        self.add(value={"verified": True, "value": 4})
        self.assert_error("current_value_unavailable", lambda: self.index.require_current_value("a", at=T2))
        with self.assertRaises(TypeError):
            self.index.require_current_value("a", at=T2, verified=True)

    def test_input_and_output_mutation_cannot_rewrite_evidence(self):
        scope = dict(SCOPE)
        conditions = {"rounds": ["demo-3-2"]}
        self.index.add_assertion(self.assertion(scope=scope, conditions=conditions, asserted_value=[4, 6]))
        self.index.link_field(self.refs[0], "a", reason="合成引用")
        scope["patch"] = "mutated"
        conditions["rounds"].append("mutated")
        result = self.index.explain("a", at=T2)
        result["assertion"]["asserted_value"].append(100)
        result["source"]["authenticity_authenticated"] = True
        self.index.trace_field(self.refs[0])["links"][0]["assertion"]["conditions"]["rounds"].clear()
        again = self.index.explain("a", at=T2)
        self.assertEqual([4, 6], again["assertion"]["asserted_value"])
        self.assertEqual(["demo-3-2"], again["assertion"]["conditions"]["rounds"])
        self.assertEqual(SCOPE["patch"], again["assertion"]["scope"]["patch"])
        self.assertFalse(again["source"]["authenticity_authenticated"])
        self.assertEqual(self.before, self.path.read_bytes())

    def test_duplicate_and_dangling_references_fail_without_overwrite(self):
        self.add()
        self.assert_error("duplicate_evidence_id", lambda: self.add(value=99))
        self.assert_error("duplicate_evidence_id", lambda: self.index.add_source(self.notice))
        self.index.link_field(self.refs[0], "a", reason="合成引用")
        self.assert_error("duplicate_evidence_id", lambda: self.index.link_field(self.refs[0], "a", reason="重复引用"))
        self.assert_error("unknown_assertion", lambda: self.index.link_field(self.refs[1], "missing", reason="合成悬空引用"))
        self.assert_error("unknown_record", lambda: self.index.link_field(FieldRef("missing", "/gold"), "a", reason="合成悬空记录"))
        self.assert_error("unknown_evidence", lambda: self.add("bad", "missing"))
        self.assert_error("unknown_assertion", lambda: self.index.add_revision(RevisionEvent("bad", "notice", ("missing",), "合成")))
        self.assert_error("duplicate_reference", lambda: self.index.add_revision(RevisionEvent("bad", "notice", ("a", "a"), "合成")))
        self.assert_error("self_revision", lambda: self.index.add_revision(RevisionEvent("bad", "notice", ("a",), "合成", replacement_ids=("a",))))
        self.assertEqual(4, self.index.assertion("a")["asserted_value"])

    def test_invalid_structures_times_and_untrusted_metadata_fail_closed(self):
        self.assert_error("invalid_catalog", lambda: EvidenceIndex({}))
        self.assert_error("invalid_record_type", lambda: self.index.add_source({"verified": True}))
        self.assert_error("invalid_scope", lambda: self.index.add_source(replace(self.notice, evidence_id="x", scope={"balance_patch": "demo"})))
        self.assert_error("invalid_text", lambda: self.add(scope={"game": True}))
        self.assert_error("invalid_source_url", lambda: self.index.add_source(replace(
            self.notice, evidence_id="x", locator={"url": "https://user:synthetic-secret@example.invalid"})))
        self.assert_error("invalid_digest", lambda: self.index.add_source(replace(self.notice, evidence_id="x", locator={"url": "https://example.invalid", "sha256": "bad"})))
        self.assert_error("missing_locator", lambda: self.index.add_source(replace(self.notice, evidence_id="x", locator={})))
        self.assert_error("incomplete_review", lambda: self.index.add_source(replace(self.notice, evidence_id="x", reviewed_at=None)))
        self.assert_error("invalid_timestamp", lambda: self.index.add_source(replace(self.notice, evidence_id="x", retrieved_at="2026-01-01")))
        self.assert_error("invalid_conditions", lambda: self.add(conditions="unknown"))
        self.assert_error("invalid_pointer", lambda: self.add(field_key="gold"))
        for value in (float("nan"), float("inf"), {1: "non-string key"}, object(), "\ud800"):
            with self.subTest(kind=type(value).__name__):
                self.assert_error("invalid_json_value", lambda: self.add(value=value))
        self.add()
        self.assert_error("invalid_timestamp", lambda: self.index.explain("a", at="2026-01-01T01:00:00"))
        self.assert_error("invalid_field_reference", lambda: self.index.trace_field({"record_key": "x", "pointer": "/x"}))

    def test_find_scope_is_exact_and_serializable_without_implicit_fallback(self):
        self.add("a")
        self.add("unknown", scope={})
        self.assertEqual(["unknown"], [item["assertion_id"] for item in self.index.find_assertions(scope={"patch": None})])
        self.assertEqual(["a"], [item["assertion_id"] for item in self.index.find_assertions(scope={"patch": SCOPE["patch"]})])
        self.assertEqual([], self.index.find_assertions(release_event_id="other"))
        json.dumps(self.index.explain("a", at=T2), ensure_ascii=False, allow_nan=False)
        json.dumps(self.index.trace_field(self.refs[0]), ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
