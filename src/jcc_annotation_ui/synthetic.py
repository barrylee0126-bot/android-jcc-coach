"""Explicit, generated-pixel fixtures; labels start completely unknown."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw
from src.jcc_samples import SampleStore
from src.jcc_samples import workflow as wf


def make_synthetic(private_dir: Path | str) -> dict:
    store = SampleStore.create(private_dir, dataset_id="synthetic-annotation-ui-v1")
    for index in range(2):
        image = Image.new("RGB", (1000, 600), "#15273a")
        draw = ImageDraw.Draw(image)
        draw.text((35, 25), "SYNTHETIC SOFTWARE FIXTURE / NOT A GAME SCREEN", fill="#9ebcce", font_size=25)
        values = (("SCENE battle", (35, 100, 340, 160)),
                  ("PHASE preparation" if index == 0 else "PHASE combat", (375, 100, 960, 160)),
                  ("ROUND 3-2", (35, 190, 280, 260)), ("HP 78", (320, 190, 510, 260)),
                  ("GOLD 32", (550, 190, 740, 260)), ("LEVEL 6", (780, 190, 970, 260)))
        for text, bounds in values:
            draw.rounded_rectangle(bounds, radius=9, fill="#24435a", outline="#5a819b", width=2)
            draw.text((bounds[0] + 15, bounds[1] + 17), text, fill="#e3edf3", font_size=25)
        draw.text((35, 330), "Shop, mode and game version: intentionally unspecified", fill="#b4c5cf", font_size=24)
        draw.text((35, 400), "All stored labels begin UNKNOWN. Draw evidence regions manually.", fill="#b4c5cf", font_size=23)
        if index == 1:
            draw.rectangle((560, 198, 735, 250), fill="#8e3855")
            draw.text((575, 211), "COVERED", fill="white", font_size=23)
        output = io.BytesIO()
        image.save(output, format="PNG")
        source = store.root / "synthetic-ui-input.png"
        wf._write_new(source, output.getvalue())
        try:
            store.import_image(source, sample_kind="synthetic", recording_session_id=f"synthetic-session-{index}",
                               dedupe_group_id=f"synthetic-group-{index}", state_group_id=f"synthetic-state-{index}",
                               annotator_id="synthetic-import")
        finally:
            source.unlink(missing_ok=True)
    return store.validate()
