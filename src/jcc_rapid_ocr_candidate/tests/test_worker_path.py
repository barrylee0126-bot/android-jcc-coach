"""Synthetic backends through the real worker loop and framed pipe protocol."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from jcc_rapid_ocr_candidate import CropRequest, FixedROI
from jcc_rapid_ocr_candidate.contract import blank_png, normalize_raw
from jcc_rapid_ocr_candidate import worker


@dataclass
class TextRecOutput:
    txts: object = ("synthetic",)
    scores: object = (0.9,)
    boxes: object = None


class WorkerPathTests(unittest.TestCase):
    def setUp(self):
        self.crop = CropRequest(FixedROI("synthetic.worker", 96, 48), blank_png())

    def run_worker(self, output, *, warmup_only=False):
        image = Mock(format="PNG", mode="RGB", size=(96, 48), n_frames=1)
        image.getexif.return_value = {}
        image_context = MagicMock()
        image_context.__enter__.return_value = image
        image_module = SimpleNamespace(DecompressionBombWarning=RuntimeWarning,
                                       open=Mock(return_value=image_context))
        engine = Mock()
        engine.text_det = engine.text_cls = None
        engine.text_rec.session.session.get_providers.return_value = ["CPUExecutionProvider"]
        engine.side_effect = [output] if warmup_only else [TextRecOutput(), output, output]
        requests = [{"id": 1, "op": "warmup"}]
        if not warmup_only:
            # The third request must remain unread after a malformed result.
            requests.extend({"id": i, "op": "transcribe", "roi": self.crop.roi.to_dict(),
                             "input_sha256": self.crop.sha256,
                             "png_base64": base64.b64encode(self.crop.png).decode("ascii")}
                            for i in (2, 3))
        frames = bytearray()
        for request in requests:
            data = json.dumps(request).encode()
            frames.extend(struct.pack(">I", len(data)) + data)
        source, destination = io.BytesIO(frames), io.BytesIO()
        module = ModuleType("rapidocr.ch_ppocr_rec.typings")
        module.TextRecOutput = TextRecOutput
        with patch.object(worker, "initialize", return_value=(engine, image_module, {"status": "ready"})), \
                patch.dict(sys.modules, {module.__name__: module}), \
                patch.object(worker.sys, "stdin", SimpleNamespace(buffer=source)), \
                patch.object(worker.sys, "stdout", SimpleNamespace(buffer=destination)):
            code = worker.main()
        destination.seek(0)
        responses = []
        while header := destination.read(4):
            size = struct.unpack(">I", header)[0]
            responses.append(json.loads(destination.read(size)))
        return code, responses, engine.call_count, source.tell() < len(frames)

    def test_malformed_values_are_rejected_before_worker_serialization(self):
        malformed = [
            TextRecOutput(scores=[True]), TextRecOutput(scores=[False]),
            TextRecOutput(scores=["0.99"]), TextRecOutput(txts="X"),
            TextRecOutput(txts={"X": 1}), TextRecOutput(txts=iter(["X"])),
            TextRecOutput(scores="1"), TextRecOutput(scores=iter([0.9])),
            TextRecOutput(txts=[None]), TextRecOutput(txts=None),
            TextRecOutput(scores=None), TextRecOutput(scores=[complex(0.9)]),
            TextRecOutput(scores=[float("nan")]), TextRecOutput(scores=[float("inf")]),
            TextRecOutput(scores=[-0.1]), TextRecOutput(scores=[1.1]),
            TextRecOutput(txts=["X", "Y"], scores=[0.9]),
        ]
        for index, output in enumerate(malformed):
            with self.subTest(case=index):
                code, responses, calls, unread = self.run_worker(output)
                self.assertEqual(code, 1)
                self.assertEqual(responses[-1], {"id": 2, "status": "error", "reason": "invalid_backend_output"})
                self.assertEqual(calls, 2)
                self.assertTrue(unread)
                self.assertNotIn("raw", responses[-1])

    def test_convertible_objects_are_not_coerced(self):
        score = Mock()
        score.__float__ = Mock(return_value=0.99)
        _, responses, _, _ = self.run_worker(TextRecOutput(scores=[score]))
        self.assertEqual(responses[-1].get("reason"), "invalid_backend_output")
        score.__float__.assert_not_called()

    def test_matching_class_name_is_not_a_backend_type_certificate(self):
        impostor = type("TextRecOutput", (), {"txts": ["X"], "scores": [0.9], "boxes": None})()
        _, responses, _, _ = self.run_worker(impostor)
        self.assertEqual(responses[-1].get("reason"), "invalid_backend_output")

    def test_malformed_warmup_cannot_mark_worker_ready_for_transcription(self):
        code, responses, calls, _ = self.run_worker(TextRecOutput(scores=[True]), warmup_only=True)
        self.assertEqual(code, 1)
        self.assertEqual(responses[-1], {"id": 1, "status": "error", "reason": "invalid_backend_output"})
        self.assertEqual(calls, 1)

    def test_list_and_tuple_results_keep_exact_text_and_no_authorization(self):
        for texts, scores in (([" X "], [0.9]), ((" X ",), (1,))):
            with self.subTest(container=type(texts).__name__):
                code, responses, _, _ = self.run_worker(TextRecOutput(texts, scores))
                self.assertEqual(code, 0)
                response = responses[2]
                self.assertEqual(response["status"], "result")
                result = normalize_raw(self.crop, response["raw"], 0.5)
                self.assertEqual(result.status, "development_candidate")
                self.assertEqual(result.transcription, " X ")
                self.assertEqual(result.to_dict()["evidence_kind"], "transcription_only")
                self.assertIsNone(result.to_dict()["text_boxes"])
                for key in ("geometry_evidence", "eligible_for_geometry_gate", "eligible_for_policy",
                            "eligible_for_execution", "live_use_authorized"):
                    self.assertIs(result.to_dict()[key], False)

    def test_unexpected_boxes_never_reach_the_pipe(self):
        code, responses, _, _ = self.run_worker(TextRecOutput(boxes=[[0, 0, 96, 48]]))
        self.assertEqual(code, 1)
        self.assertNotIn("raw", responses[-1])
        self.assertEqual(responses[-1].get("reason"), "invalid_backend_output")


if __name__ == "__main__":
    unittest.main()
