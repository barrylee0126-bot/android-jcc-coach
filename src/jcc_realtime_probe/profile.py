"""Private explicit input layouts. No real game coordinates are shipped."""

from dataclasses import dataclass
import json
from pathlib import Path

from jcc_device.recognition import PixelROI
from jcc_device.roi_recognition import FieldROI, ImageGeometry, ROILayout
from jcc_device.scene_evidence import CueSpec, LayoutKey, SceneLayout


PROJECT = Path(__file__).resolve().parents[2]


def outside_project(path):
    path = Path(path).expanduser().resolve()
    if path == PROJECT or PROJECT in path.parents:
        raise ValueError("Private inputs and outputs must stay outside the project")
    return path


@dataclass(frozen=True)
class Profile:
    image_size: tuple
    raw_boxes: tuple
    dependencies: tuple
    key: LayoutKey
    scene_layout: SceneLayout | None
    numeric_layouts: tuple


def load_profile(data, source_kind):
    geometry = ImageGeometry(tuple(data["image_size"]))
    key = LayoutKey("experiment-explicit-layout-v1", geometry, source_kind, "video_frame")
    boxes = tuple(tuple(b) for b in data["input_boxes"])
    for i, box in enumerate(boxes):
        PixelROI(f"raw_{i}", geometry.image_size, box).validate(geometry.image_size)
    cues = tuple(CueSpec(c["cue_id"], PixelROI(f"cue_{i}", geometry.image_size, tuple(c["box"])),
                         tuple(c.get("exact_texts", ())), c.get("scale", 1))
                 for i, c in enumerate(data.get("cues", ())))
    fields = tuple(FieldROI(c["field_name"], PixelROI(f"number_{i}", geometry.image_size, tuple(c["box"])),
                            scale=c.get("scale", 1), syntax=c.get("syntax", "bare"))
                   for i, c in enumerate(data.get("numeric_fields", ())))
    dependencies = tuple(dict.fromkeys((*boxes, *(c.roi.box for c in cues), *(c.roi.box for c in fields))))
    if not dependencies or len(dependencies) > 32:
        raise ValueError("Provide one to 32 OCR inputs")
    return Profile(geometry.image_size, boxes, dependencies, key, SceneLayout(key, cues) if cues else None,
                   (ROILayout(key.layout_id, geometry, source_kind, fields),) if fields else ())


def read_profile(path, source_kind):
    path = outside_project(path)
    if path.stat().st_size > 65_536:
        raise ValueError("Profile too large")
    return load_profile(json.loads(path.read_text()), source_kind)
