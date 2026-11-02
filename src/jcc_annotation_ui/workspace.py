"""Narrow adapter: SampleStore remains the only label/review source of truth.

SampleStore has no public snapshot/image read API. The private calls used here
are deliberately confined to this module and always use its lock and verifier.
Writes go through SampleStore.annotate/review, never a second workspace writer.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
from typing import Any
import uuid

from src.jcc_samples import WorkflowError
from src.jcc_samples import workflow as wf
from .verified_store import VerifiedUIStore


EDITABLE_FIELDS = ("scene", "phase", "round", "hp", "gold", "level")
OCCLUSION_PREFIX = "jcc_ui:occlusion:"


def fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def digest(value: Any) -> str:
    return hashlib.sha256(wf._json_bytes(value)).hexdigest()


def box(value: Any) -> dict:
    keys = ("x_min", "y_min", "x_max", "y_max")
    if not isinstance(value, dict) or set(value) != set(keys):
        fail("invalid_region", "区域需要完整的整帧归一化坐标。")
    if any(type(value[k]) not in (float, int) or not math.isfinite(value[k]) or not 0 <= value[k] <= 1 for k in keys):
        fail("invalid_region", "区域坐标必须是 0 到 1 之间的有限数字。")
    if value["x_min"] >= value["x_max"] or value["y_min"] >= value["y_max"]:
        fail("invalid_region", "区域必须有正面积。")
    return {key: value[key] for key in keys}


def intersects(a: dict, b: dict) -> bool:
    return (max(a["x_min"], b["x_min"]) < min(a["x_max"], b["x_max"])
            and max(a["y_min"], b["y_min"]) < min(a["y_max"], b["y_max"]))


def split_conditions(conditions: list) -> tuple[list[str], list[dict]]:
    ordinary, occlusions = [], []
    for condition in conditions:
        if condition.startswith(OCCLUSION_PREFIX):
            try:
                coordinates = wf._parse_json(condition[len(OCCLUSION_PREFIX):].encode())
                if not isinstance(coordinates, list) or len(coordinates) != 4:
                    raise ValueError
                occlusions.append(box(dict(zip(("x_min", "y_min", "x_max", "y_max"), coordinates))))
            except (WorkflowError, ValueError, TypeError, UnicodeError):
                fail("invalid_occlusion", "存储中的遮挡区域无效；请用原工作流检查条件记录。")
        else:
            ordinary.append(condition)
    if len(occlusions) > 32:
        fail("invalid_occlusion", "单张画面的遮挡区域最多 32 个。")
    return ordinary, occlusions


def join_conditions(ordinary: Any, occlusions: Any) -> list[str]:
    if (not isinstance(ordinary, list) or len(ordinary) > 64
            or any(not isinstance(c, str) or not c.strip() or len(c) > 160
                   or any(ord(ch) < 32 for ch in c) or c.startswith(OCCLUSION_PREFIX) for c in ordinary)
            or len(set(ordinary)) != len(ordinary)):
        fail("invalid_conditions", "画面条件须是互不重复的简短文本，不能写入保留的遮挡前缀。")
    if not isinstance(occlusions, list) or len(occlusions) > 32:
        fail("invalid_occlusion", "单张画面的遮挡区域最多 32 个。")
    encoded = []
    for item in occlusions:
        b = box(item)
        encoded.append(OCCLUSION_PREFIX + json.dumps(list(b.values()), separators=(",", ":"), allow_nan=False))
    if len(set(encoded)) != len(encoded):
        fail("invalid_occlusion", "遮挡区域重复。")
    return ordinary + encoded


def guard_regions(record: dict) -> None:
    _, occlusions = split_conditions(record["conditions"])
    labels = record["labels"]
    entries = [(name, labels[name]) for name in wf.CORE_FIELDS]
    entries += [("shop", slot["content"]) for slot in labels["shop"]]
    for name, label in entries:
        region = label["region"]
        if region is not None:
            box(region)
        if label["truth_status"] == "known":
            if name in EDITABLE_FIELDS and region is None:
                fail("known_region_required", "六个可编辑字段的已知答案都须有当前图像的证据区域。")
            if region is not None and any(intersects(region, covered) for covered in occlusions):
                fail("occluded_known_field", "已知字段的证据区域与遮挡重叠；须保留未知或重新核实可见证据。")


class AnnotationWorkspace:
    """Explicit single-kind workspace; signed views are ephemeral, not label state."""

    def __init__(self, private_dir: Path | str, *, sample_kind: str):
        if sample_kind not in ("real", "synthetic"):
            fail("sample_kind_boundary", "启动时须明确选择真实或合成样本模式。")
        self.store = VerifiedUIStore(private_dir)
        self.sample_kind = sample_kind
        self._secret = secrets.token_bytes(32)
        self._mutex = threading.RLock()
        with self.store._locked():
            self._load(read=True)

    def _load(self, *, read: bool = False, target: str | None = None) -> dict:
        data, verification = self.store.read_snapshot(target=target, full=not read)
        if any(r["sample_kind"] != self.sample_kind for r in data["annotations"]):
            fail("sample_kind_boundary", "工作区含其他类型样本；本界面拒绝混用真实与合成流程。")
        self._last_verification = verification
        return data

    def _view_token(self, data: dict, record: dict) -> str:
        material = {"revision": data["revision"], "record": record,
                    "image_sha256": data["assets"][record["sample_id"]]["sha256"], "kind": self.sample_kind}
        return hmac.new(self._secret, wf._json_bytes(material), hashlib.sha256).hexdigest()

    def _current(self, data: dict, payload: dict) -> dict:
        self.store._revision(data, payload["expected_revision"])
        if payload["sample_kind"] != self.sample_kind:
            fail("sample_kind_boundary", "页面声明的样本类型与本实例不一致。")
        if not isinstance(payload["sample_id"], str) or wf.SAMPLE_TOKEN.fullmatch(payload["sample_id"]) is None:
            fail("invalid_identifier", "样本代号无效。")
        record = self.store._record(data, payload["sample_id"])
        token = payload["view_token"]
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None or not hmac.compare_digest(token, self._view_token(data, record)):
            fail("view_expired", "当前画面或标签已变化；请重新载入并检查，不能复用旧复核。")
        return record

    @staticmethod
    def _keys(payload: Any, expected: set[str]) -> None:
        if not isinstance(payload, dict) or set(payload) != expected:
            fail("invalid_request", "请求字段不符合本界面契约；来源、身份、组别和分集不能编辑。")

    def samples(self) -> dict:
        with self._mutex, self.store._locked():
            data = self._load(read=True)
            return {"revision": data["revision"], "dataset_id": data["dataset_id"], "sample_kind": self.sample_kind,
                    "samples": [{"sample_id": r["sample_id"], "recording_session_id": r["recording_session_id"],
                                 "review_status": r["review_status"], "split": r["split"],
                                 "known_fields": sum(r["labels"][k]["truth_status"] == "known" for k in EDITABLE_FIELDS)}
                                for r in data["annotations"]], "accuracy": None,
                    "verification": dict(self._last_verification)}

    def verify(self) -> dict:
        """Explicit read-only full preflight. It never changes store revision."""
        with self._mutex, self.store._locked():
            data = self._load()
            return {"status": "content_verified", "revision": data["revision"],
                    "verification": dict(self._last_verification)}

    def close(self) -> None:
        self.store.clear_proofs()

    @staticmethod
    def _review_binding(data: dict, record: dict) -> dict:
        result = {"current": False, "revision": None, "evidence_ref": None}
        if record["review_status"] == "draft":
            return result
        for event in reversed(data["events"]):
            if not isinstance(event, dict) or record["sample_id"] not in event.get("sample_ids", []):
                continue
            if event.get("action") == "split":
                continue
            if event.get("action") == "review":
                result = {"current": (event.get("labels_sha256") == digest(record["labels"])
                                      and event.get("reviewer_id") == record["reviewer_id"]),
                          "revision": event.get("revision"), "evidence_ref": event.get("evidence_ref")}
            return result
        return result

    def _view(self, data: dict, record: dict) -> dict:
        ordinary, occlusions = split_conditions(record["conditions"])
        warnings = []
        try:
            guard_regions(record)
        except WorkflowError as error:
            warnings.append(error.code)
        binding = self._review_binding(data, record)
        if record["review_status"] != "draft" and not binding["current"]:
            warnings.append("review_binding_stale")
        return {"revision": data["revision"], "sample_kind": self.sample_kind, "record": deepcopy(record),
                "view_token": self._view_token(data, record), "labels_sha256": digest(record["labels"]),
                "image_sha256": data["assets"][record["sample_id"]]["sha256"],
                "conditions": ordinary, "occlusions": occlusions, "editable_fields": list(EDITABLE_FIELDS),
                "review_binding": binding, "warnings": warnings,
                "verification": dict(self._last_verification)}

    def sample(self, sample_id: str) -> dict:
        if not isinstance(sample_id, str) or wf.SAMPLE_TOKEN.fullmatch(sample_id) is None:
            fail("invalid_identifier", "样本代号无效。")
        with self._mutex, self.store._locked():
            data = self._load(read=True, target=sample_id)
            return self._view(data, self.store._record(data, sample_id))

    def image(self, payload: dict) -> tuple[bytes, str]:
        self._keys(payload, {"sample_id", "sample_kind", "expected_revision", "view_token"})
        if not isinstance(payload["sample_id"], str) or wf.SAMPLE_TOKEN.fullmatch(payload["sample_id"]) is None:
            fail("invalid_identifier", "样本代号无效。")
        with self._mutex, self.store._locked():
            data = self._load(read=True, target=payload["sample_id"])
            record = self._current(data, payload)
            asset = data["assets"][record["sample_id"]]
            raw = wf._read_file(self.store._path(asset["path"]), wf.MAX_IMAGE_BYTES, private=True)
            # The selected asset was fully verified just above. Re-read the
            # exact response bytes and compare their hash; no second pixel
            # decode and no cached bytes are used for delivery.
            if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
                fail("asset_integrity", "画面在校验后变化；本次拒绝显示或提交。")
            return raw, "image/png" if asset["format"] == "PNG" else "image/jpeg"

    def annotate(self, payload: dict) -> dict:
        self._keys(payload, {"sample_id", "sample_kind", "expected_revision", "view_token", "fields",
                             "conditions", "occlusions", "annotator_id"})
        wf._token(payload["annotator_id"], "annotator_id")
        if not isinstance(payload["fields"], dict) or set(payload["fields"]) != set(EDITABLE_FIELDS):
            fail("unsupported_fields", "仅可编辑场景、阶段、回合、血量、金币和等级，且不能省略字段。")
        relative = "edits/ui-" + uuid.uuid4().hex + ".json"
        created = False
        with self._mutex:
            try:
                with self.store._locked():
                    data = self._load()
                    record = self._current(data, payload)
                    if record["split"] == "acceptance":
                        fail("acceptance_frozen", "验收样本已冻结；须先通过原工作流将关联场次移回隔离区。")
                    candidate = deepcopy(record)
                    candidate["labels"].update(deepcopy(payload["fields"]))
                    candidate.update(conditions=join_conditions(payload["conditions"], payload["occlusions"]),
                                     annotator_id=payload["annotator_id"], reviewer_id=None, review_status="draft")
                    records = [candidate if r["sample_id"] == record["sample_id"] else r for r in data["annotations"]]
                    wf._check_annotations(records)
                    guard_regions(candidate)
                    edit = {"sample_id": record["sample_id"], "expected_revision": data["revision"],
                            "labels": candidate["labels"], "conditions": candidate["conditions"]}
                    self.store._new_document(relative, edit, "edits")
                    created = True
                # Reacquires SampleStore's lock and rechecks revision. Any writer
                # between these two locks causes a conflict, never lost updates.
                result = self.store.annotate(relative, annotator_id=payload["annotator_id"])
            finally:
                if created:
                    self.store._path(relative).unlink(missing_ok=True)
        return {"status": "annotation_saved", "revision": result["revision"], "review_status": "draft"}

    def review(self, payload: dict) -> dict:
        self._keys(payload, {"sample_id", "sample_kind", "expected_revision", "view_token", "reviewer_id",
                             "evidence_ref", "status", "confirmation"})
        wf._token(payload["reviewer_id"], "reviewer_id")
        wf._token(payload["evidence_ref"], "evidence_ref")
        confirmation = "different_human_checked" if self.sample_kind == "real" else "synthetic_software_check"
        if payload["confirmation"] != confirmation:
            fail("review_confirmation_required", "须由实际使用者明确确认本次复核的人员及类型边界。")
        with self._mutex:
            with self.store._locked():
                data = self._load()
                record = self._current(data, payload)
                guard_regions(record)
            result = self.store.review(payload["sample_id"], reviewer_id=payload["reviewer_id"],
                                       expected_revision=payload["expected_revision"], evidence_ref=payload["evidence_ref"],
                                       status=payload["status"])
        return {"status": "review_saved", "revision": result["revision"], "sample_kind": self.sample_kind}
