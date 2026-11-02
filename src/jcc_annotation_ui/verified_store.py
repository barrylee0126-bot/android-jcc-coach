"""UI-only verification adapter; fresh file bytes are always the evidence.

SampleStore exposes no decode hook. This local subclass mirrors its _verify
checks and retains inherited locking, envelope, schema, writes and events.
Only the expensive derivation of image info is memoized by freshly hashed bytes.
Inventory reads may defer decoding untouched assets, and explicitly report that
scope. Inherited annotate/review/validate always use full verification.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import threading

from PIL import Image
from src.jcc_samples import SampleStore
from src.jcc_samples import workflow as wf


class VerifiedUIStore(SampleStore):
    MAX_PROOFS = 256

    def __init__(self, private_dir):
        super().__init__(private_dir)
        self._proofs: OrderedDict[tuple, dict] = OrderedDict()
        self._proof_lock = threading.RLock()
        self._context = threading.local()

    @staticmethod
    def _policy() -> tuple:
        # Decoder policy changes must never reuse results produced under an old
        # limit/function. Pillow's implementation is fixed for a process lifetime.
        return (wf.MAX_PIXELS, Image.MAX_IMAGE_PIXELS, id(wf._image_info))

    def _proof(self, actual_sha: str) -> dict | None:
        key = (actual_sha, self._policy())
        with self._proof_lock:
            value = self._proofs.get(key)
            if value is not None:
                self._proofs.move_to_end(key)
                return dict(value)
        return None

    def _remember(self, actual_sha: str, info: dict) -> None:
        with self._proof_lock:
            self._proofs[(actual_sha, self._policy())] = dict(info)
            while len(self._proofs) > self.MAX_PROOFS:
                self._proofs.popitem(last=False)

    def clear_proofs(self) -> None:
        with self._proof_lock:
            self._proofs.clear()

    @contextmanager
    def _scope(self, targets):
        previous = getattr(self._context, "scope", None)
        stats = {"scope": "full_content" if targets is None else "inventory_bytes" if not targets else "selected_content",
                 "assets": 0, "byte_checked_assets": 0, "decoded_assets": 0, "fresh_decodes": 0,
                 "reused_decodes": 0, "bytes_hashed": 0}
        self._context.scope = (targets, stats)
        try:
            yield stats
        finally:
            self._context.scope = previous

    def read_snapshot(self, *, target: str | None = None, full: bool = False) -> tuple[dict, dict]:
        """Caller holds SampleStore's lock. No labels or file bytes are cached."""
        with self._scope(None if full else frozenset() if target is None else frozenset((target,))) as stats:
            data = self._load()
            return data, dict(stats)

    def _verify(self, workspace: dict, records: list[dict]) -> None:
        # Keep the checks in sync with SampleStore._verify. Read scopes only
        # defer pixel derivation; default scope (all inherited writes) is full.
        context = getattr(self._context, "scope", None)
        targets, stats = context if context is not None else (None, None)
        wf._check_annotations(records)
        ids = {record["sample_id"] for record in workspace["annotations"]}
        if set(workspace["assets"]) != ids:
            wf._fail("asset_mapping", "Each stored sample needs exactly one private asset-index entry.")
        paths, digests, pixels = set(), set(), set()
        for record in records:
            sample_id = record["sample_id"]
            if wf.SAMPLE_TOKEN.fullmatch(sample_id) is None or sample_id not in workspace["assets"]:
                wf._fail("asset_mapping", "Manifest sample is not present in this private workspace.")
            for key in ("recording_session_id", "dedupe_group_id", "state_group_id", "annotator_id"):
                wf._token(record[key], key)
            if record["reviewer_id"] is not None:
                wf._token(record["reviewer_id"], "reviewer_id")
            if record["dataset_id"] != workspace["dataset_id"]:
                wf._fail("dataset_mismatch", "Manifest and workspace dataset revisions disagree.")
            asset = workspace["assets"][sample_id]
            expected_keys = {"asset_ref", "path", "sample_kind", "sha256", "pixel_sha256", "width_px", "height_px", "format"}
            if not isinstance(asset, dict) or set(asset) != expected_keys:
                wf._fail("asset_mapping", "Private asset metadata is invalid.")
            ref = asset["asset_ref"]
            if not isinstance(ref, str) or wf.ASSET_TOKEN.fullmatch(ref) is None or asset["format"] not in ("PNG", "JPEG"):
                wf._fail("asset_mapping", "Private asset reference or format is invalid.")
            suffix = ".png" if asset["format"] == "PNG" else ".jpg"
            if asset["path"] != "assets/" + ref + suffix or asset["sample_kind"] != record["sample_kind"]:
                wf._fail("asset_mapping", "Asset path or sample-kind binding disagrees.")
            if any(not isinstance(asset[key], str) or wf.SHA256.fullmatch(asset[key]) is None for key in ("sha256", "pixel_sha256")):
                wf._fail("asset_mapping", "Private asset hashes are invalid.")
            if any(type(asset[k]) is not int or asset[k] <= 0 for k in ("width_px", "height_px")):
                wf._fail("asset_mapping", "Private asset dimensions are invalid.")

            # Open anew, enforce ownership/mode/nlink/no symlinks, bound size,
            # read the complete bytes, then hash. mtime/size/inode/revision or a
            # previous successful scan NEVER stand in for this evidence.
            raw = wf._read_file(self._path(asset["path"]), wf.MAX_IMAGE_BYTES, private=True)
            actual_sha = hashlib.sha256(raw).hexdigest()
            if actual_sha != asset["sha256"]:
                wf._fail("asset_integrity", "Stored image bytes changed.")
            info = self._proof(actual_sha)
            derived_now = False
            if info is None and (targets is None or sample_id in targets):
                info = wf._image_info(raw)
                derived_now = True
            if info is not None and any(info[k] != asset[k] for k in info):
                wf._fail("asset_integrity", "Stored pixels, dimensions or format disagree.")
            image = record["frame"]["image"]
            if any(image[k] != asset[k] for k in ("width_px", "height_px")) or image["rotation_applied_degrees"] != 0:
                wf._fail("asset_geometry", "Annotation dimensions or rotation disagree.")
            if record["asset"] is not None and (record["asset"]["asset_ref"] != ref or record["asset"]["sha256"] != actual_sha):
                wf._fail("asset_binding", "Annotation asset binding disagrees.")
            if asset["path"] in paths or actual_sha in digests or asset["pixel_sha256"] in pixels:
                wf._fail("duplicate_image", "Manifest contains repeated image bytes or decoded pixels.")
            paths.add(asset["path"])
            digests.add(actual_sha)
            pixels.add(asset["pixel_sha256"])
            if derived_now:
                self._remember(actual_sha, info)
            if stats is not None:
                stats["assets"] += 1
                stats["byte_checked_assets"] += 1
                stats["bytes_hashed"] += len(raw)
                stats["decoded_assets"] += info is not None
                stats["fresh_decodes"] += derived_now
                stats["reused_decodes"] += info is not None and not derived_now
