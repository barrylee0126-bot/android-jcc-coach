"""Explicitly fictional fixtures; no real game identifiers or recipes."""

from copy import deepcopy

from .core import REQUEST_FORMAT


def synthetic_request() -> dict:
    binding = {"game": "fiction_game", "region": "fiction_region", "mode": "fiction_mode",
               "season": "fiction_season", "patch": "fiction_patch", "rules_revision": "fiction_rules_v1"}
    evidence = {"kind": "synthetic", "refs": ["synthetic:recipe-candidate-v1"]}
    return {
        "format_version": REQUEST_FORMAT,
        "version": {"status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence)},
        "inventory": {
            "status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence),
            "availability": "unequipped_components", "complete": True,
            "entries": [{"component_id": "piece_x", "count": 2}, {"component_id": "piece_y", "count": 1}],
        },
        "catalog": {
            "status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence),
            "component_ids": ["piece_x", "piece_y", "piece_z"],
            "recipes": [
                {"item_id": "fiction_xx", "components": ["piece_x", "piece_x"]},
                {"item_id": "fiction_xy", "components": ["piece_x", "piece_y"]},
                {"item_id": "fiction_yy", "components": ["piece_y", "piece_y"]},
            ],
        },
    }
