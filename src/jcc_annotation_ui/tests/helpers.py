from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from src.jcc_samples import SampleStore, WorkflowError
from src.jcc_annotation_ui.workspace import AnnotationWorkspace, EDITABLE_FIELDS


REGION = {"x_min": 0.1, "y_min": 0.2, "x_max": 0.3, "y_max": 0.4}


class PrivateFixture(unittest.TestCase):
    """Even records declared real below use generated pixels and fictional codes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jcc-ui-synthetic-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "store"
        self.store = SampleStore.create(self.root, dataset_id="synthetic-ui-tests")
        self.sample_id = self.add_image()
        self.ui = AnnotationWorkspace(self.root, sample_kind="synthetic")

    def add_image(self, kind="synthetic", color=(20, 50, 80)):
        source = self.base / "generated-input.png"
        Image.new("RGB", (100, 60), color).save(source)
        index = self.store.validate()["samples"]
        return self.store.import_image(source, sample_kind=kind, recording_session_id=f"synthetic-session-{index}",
            dedupe_group_id=f"synthetic-group-{index}", state_group_id=f"synthetic-state-{index}",
            annotator_id="synthetic-a")["sample_id"]

    def data(self):
        return json.loads((self.root / "workspace.json").read_bytes())

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def view(self):
        return self.ui.sample(self.sample_id)

    def anchor(self, view=None):
        v = view or self.view()
        return {"sample_id": v["record"]["sample_id"], "sample_kind": v["sample_kind"],
                "expected_revision": v["revision"], "view_token": v["view_token"]}

    def edit(self, view=None):
        v = view or self.view()
        return {**self.anchor(v), "fields": {k: deepcopy(v["record"]["labels"][k]) for k in EDITABLE_FIELDS},
                "conditions": deepcopy(v["conditions"]), "occlusions": deepcopy(v["occlusions"]),
                "annotator_id": "synthetic-a"}

    def review(self, view=None):
        return {**self.anchor(view), "reviewer_id": "synthetic-b", "evidence_ref": "synthetic-software-check",
                "status": "independently_reviewed", "confirmation": "synthetic_software_check"}

    def known(self, payload, name="gold", value=32, region=REGION):
        payload["fields"][name] = {"applicability":"applicable", "truth_status":"known", "value":value,
                                    "reason":None, "region":deepcopy(region)}
        return payload

    def rejects(self, code, operation, *, unchanged=True):
        before = self.snapshot()
        with self.assertRaises(WorkflowError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        if unchanged:
            self.assertEqual(before, self.snapshot())

    def real_fixture(self):
        # Wire-format exercise only. No real game image or human is represented.
        self.root = self.base / "real-shape-store"
        self.store = SampleStore.create(self.root, dataset_id="synthetic-real-shape-test")
        self.sample_id = self.add_image(kind="real")
        self.ui = AnnotationWorkspace(self.root, sample_kind="real")
