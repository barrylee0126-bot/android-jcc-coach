"""Experimental, read-only scene classification using a local Ollama model."""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import urllib.request
from pathlib import Path

from PIL import Image

from .stream import TabletStream


SCENES = ("lobby", "mode_select", "battle", "loading", "settlement", "history", "unknown")
CONFIDENCE = ("low", "medium", "high")
SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "enum": list(SCENES)},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
    },
    "required": ["scene", "confidence"],
    "additionalProperties": False,
}


def classify(image_bytes: bytes, model: str, timeout: float = 180.0) -> dict:
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((1024, 1024))
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=85)
    prompt = (
        "判断图片是否是《金铲铲之战》的界面。只选一个 scene: "
        "lobby=游戏大厅，mode_select=选择游戏模式，battle=对局中，"
        "loading=加载中，settlement=刚结束的结算页面，history=历史对局记录页面，"
        "unknown=无法确定或不是游戏。"
        "confidence 选择 low、medium、high。只返回要求的 JSON，不要猜测。"
    )
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "num_predict": 128},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(encoded.getvalue()).decode("ascii")]}],
    }
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Always use the local service directly, regardless of proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    with opener.open(request, timeout=timeout) as response:
        body = json.load(response)
    elapsed = time.monotonic() - started
    try:
        result = json.loads(body["message"]["content"])
        if result["scene"] not in SCENES or result["confidence"] not in CONFIDENCE:
            raise ValueError("Unexpected scene or confidence value")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Local model did not return valid scene JSON") from error
    return {
        "scene": result["scene"],
        "confidence": result["confidence"],
        "model": model,
        "model_seconds": round(elapsed, 2),
        "model_load_seconds": round(body.get("load_duration", 0) / 1e9, 2),
        "note": "实验性识别；不要据此自动操作或判断对局状态",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="使用本地视觉模型只读判断画面场景")
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    parser.add_argument("--image", type=Path, help="使用本地图片代替读取平板")
    args = parser.parse_args()
    if args.image:
        image = args.image.read_bytes()
    else:
        with TabletStream(sample_fps=1) as stream:
            image = stream.read_frame()
    print(json.dumps(classify(image, args.model), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
