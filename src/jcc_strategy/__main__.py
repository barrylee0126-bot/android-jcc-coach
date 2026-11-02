"""Run a fictional offline demonstration. No input flags or device integration."""

import json

from .fixtures import fixture_case, next_fixture_frame


def main() -> None:
    case = fixture_case()
    engine = case.engine()
    admitted = engine.observe(case.state)
    decision = engine.evaluate(case.rule.rule_id)
    assert admitted.valid and decision.advice is not None
    before = engine.revalidate(decision.advice)
    changed = next_fixture_frame(case.state, context_changed=True)
    changed["phase"]["value"] = "combat"
    assert engine.observe(changed).valid
    after = engine.revalidate(decision.advice)
    print(json.dumps({
        "label": "完全虚构的合成离线演示；不读取游戏画面，不连接设备，不适用于真实对局。",
        "device_integration": False, "real_rules_verified": False,
        "decision": decision.to_dict(), "valid_before_change": before.to_dict(),
        "valid_after_scene_change": after.to_dict(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
