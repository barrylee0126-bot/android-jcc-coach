"""Test-only bulk assembly of a NEW synthetic workspace, never real imports.

Avoids the existing one-by-one import's quadratic scans while constructing a
benchmark. Product writes still exclusively use inherited SampleStore methods.
Every image uses generated texture/text; every label remains unknown.
"""

import io
import random
import uuid

from PIL import Image, ImageDraw
from src.jcc_samples import SampleStore
from src.jcc_samples import workflow as wf


def make_scale_fixture(root, *, count=100, size=(3200, 2000), progress=None):
    if not 1 <= count <= 256 or min(size) <= 0 or size[0] * size[1] > wf.MAX_PIXELS:
        raise ValueError("Invalid synthetic fixture dimensions or count")
    store = SampleStore.create(root, dataset_id="synthetic-ui-scale-v1")
    rng = random.Random(1106)
    texture_size = (min(size[0], 640), min(size[1], 400))
    texture = Image.frombytes("L", texture_size, rng.randbytes(texture_size[0] * texture_size[1]))
    base = texture.resize(size, Image.Resampling.BILINEAR).convert("RGB")
    byte_sizes, ids = [], []
    with store._locked():
        data = store._load()
        for index in range(count):
            image = base.copy()
            draw = ImageDraw.Draw(image)
            draw.rectangle((0,0,1,1),fill=(index,110,6))
            w, h = size
            draw.rectangle((w*.02,h*.03,w*.98,h*.13), fill="#142c42")
            draw.text((w*.04,h*.05), f"SYNTHETIC SOFTWARE FIXTURE {index:03d} / NO GAME CONTENT", fill="white", font_size=max(12,w//60))
            for j, text in enumerate(("SCENE battle","PHASE preparation","ROUND 3-2","HP 78","GOLD 32","LEVEL 6")):
                x=w*(.04+(j%3)*.32); y=h*(.21+(j//3)*.18)
                draw.rectangle((x,y,x+w*.27,y+h*.12), fill="#24435a")
                draw.text((x+w*.012,y+h*.025), text, fill="white", font_size=max(10,w//75))
            out=io.BytesIO();image.save(out,format="PNG",compress_level=3)
            raw=out.getvalue();info=wf._image_info(raw)
            sid, ref="sample-"+uuid.uuid4().hex,"asset-"+uuid.uuid4().hex
            path="assets/"+ref+".png"
            wf._write_new(store._path(path),raw)
            record=wf._new_annotation(data,info,sample_kind="synthetic",sample_id=sid,asset_ref=ref,
                session=f"synthetic-session-{index//10}",dedupe_group=f"synthetic-group-{index}",
                state_group=f"synthetic-state-{index}",annotator="synthetic-fixture",redaction="none")
            data["annotations"].append(record)
            data["assets"][sid]={**info,"asset_ref":ref,"path":path,"sample_kind":"synthetic"}
            data["revision"]+=1
            data["events"].append({"revision":data["revision"],"action":"import","sample_ids":[sid],
                                   "time_basis":"host_local_import_decode"})
            byte_sizes.append(len(raw));ids.append(sid)
            if progress is not None and ((index+1)%20==0 or index+1==count): progress(index+1)
        wf._check_annotations(data["annotations"])
        wf._atomic_save(store.root,data)
    return {"sample_ids":ids,"images":count,"width_px":size[0],"height_px":size[1],
            "total_encoded_bytes":sum(byte_sizes),"min_image_bytes":min(byte_sizes),"max_image_bytes":max(byte_sizes)}
