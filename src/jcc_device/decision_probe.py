"""Verify the upstream coach can call a local model using a synthetic state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from src.decision_llm import DecisionContext, DecisionLLM
from src.schema import WorldState


async def probe(model: str) -> dict:
    # The upstream client uses httpx's proxy environment; force loopback direct.
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    state = WorldState(stage="pvp", round="3-2", hp=75, gold=50, level=6,
                       exp="4/36", timestamp=time.time())
    coach = DecisionLLM(base_url="http://127.0.0.1:11434/v1", model=model,
                        timeout=90, use_guided_json=False)
    started = time.monotonic()
    advice = await coach.decide(state, DecisionContext(kind="level"))
    fallback = "降级" in advice.reasoning
    return {
        "synthetic_state": True,
        "kind": advice.kind,
        "model_response_not_fallback": not fallback,
        "seconds": round(time.monotonic() - started, 2),
        "note": "合成局面仅验证模型调用，不代表实际对局建议可靠",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="用合成局面验证本地教练模型调用")
    parser.add_argument("--model", default="qwen3-vl:4b-instruct")
    args = parser.parse_args()
    result = asyncio.run(probe(args.model))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["model_response_not_fallback"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
