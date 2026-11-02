"""Fictional, version-bound requests for planner tests only."""

from copy import deepcopy

from .core import REQUEST_FORMAT


def synthetic_request() -> dict:
    binding = {"game": "fiction-game", "region": "fiction-region", "mode": "fiction-mode",
               "season": "fiction-season", "patch": "fiction-patch", "rules_revision": "fiction-rules-1"}
    return {
        "format_version": REQUEST_FORMAT,
        "binding": binding,
        "evidence": {"kind": "synthetic", "refs": ["synthetic:team-planner-v1"]},
        "phase": "preparation", "level_capacity": 3, "bench_capacity": 9, "gold": 4,
        "trait_counting_rule": {"rule_id": "fiction-trait-counting", "binding": deepcopy(binding),
                                  "same_unit_id_counts_separately": True},
        "merge_rule": {"rule_id": "fiction-merge", "binding": deepcopy(binding),
                       "one_star_to_two_star_count": 3, "automatic_merge_on_purchase": True},
        "trait_catalog": [
            {"trait_id": "trait_leaf", "thresholds": [2, 3]},
            {"trait_id": "trait_guard", "thresholds": [2]},
            {"trait_id": "trait_shot", "thresholds": [2]},
        ],
        "unit_catalog": [
            {"unit_id": "unit_leaf_guard", "cost": 1, "traits": ["trait_leaf", "trait_guard"], "role_tags": ["frontline"]},
            {"unit_id": "unit_leaf_shot", "cost": 1, "traits": ["trait_leaf", "trait_shot"], "role_tags": ["backline"]},
            {"unit_id": "unit_guard", "cost": 2, "traits": ["trait_guard"], "role_tags": ["frontline"]},
            {"unit_id": "unit_shot", "cost": 2, "traits": ["trait_shot"], "role_tags": ["backline"]},
        ],
        "board": [
            {"instance_id": "own-a", "unit_id": "unit_leaf_guard", "star": 1, "is_core": True, "equipped_item_ids": []},
            {"instance_id": "own-b", "unit_id": "unit_guard", "star": 1, "is_core": False, "equipped_item_ids": []},
        ],
        "bench": [
            {"instance_id": "bench-c", "unit_id": "unit_leaf_shot", "star": 1, "is_core": False, "equipped_item_ids": []},
        ],
        "shop": [{"slot": 2, "unit_id": "unit_shot", "cost": 2}],
        "transition_targets": [{"target_id": "leaf-shot", "required_unit_ids": ["unit_leaf_guard", "unit_leaf_shot", "unit_shot"],
                                "target_traits": [{"trait_id": "trait_leaf", "threshold": 2}, {"trait_id": "trait_shot", "threshold": 2}]}],
    }


def copied_request() -> dict:
    return deepcopy(synthetic_request())
