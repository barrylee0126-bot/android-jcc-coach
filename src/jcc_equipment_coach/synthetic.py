"""Fictional requests used only for contract tests and local demonstrations."""

from copy import deepcopy

from jcc_recipe_candidate.synthetic import synthetic_request as recipe_request

from .core import REQUEST_FORMAT


def synthetic_request() -> dict:
    recipe = recipe_request()
    binding = deepcopy(recipe["version"]["binding"])
    evidence = deepcopy(recipe["version"]["evidence"])
    return {
        "format_version": REQUEST_FORMAT,
        "recipe_request": recipe,
        "carrier_state": {"status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence),
                          "instances": [
                              {"instance_id": "unit_a", "unit_id": "fiction_front", "equipped_item_ids": [], "item_slot_capacity": 3},
                              {"instance_id": "unit_b", "unit_id": "fiction_back", "equipped_item_ids": ["old_item"], "item_slot_capacity": 3},
                              {"instance_id": "unit_full", "unit_id": "fiction_full", "equipped_item_ids": ["a", "b", "c"], "item_slot_capacity": 3},
                          ]},
        "carrier_fit": {"status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence),
                        "complete_for_catalog": False,
                        "rules": [
                            {"item_id": "fiction_xx", "carrier_instance_ids": ["unit_a", "unit_full"], "condition_zh": "虚构前排条件。"},
                            {"item_id": "fiction_xy", "carrier_instance_ids": ["unit_b"], "condition_zh": "虚构后排条件。"},
                        ]},
        "reservations": {"status": "known", "binding": deepcopy(binding), "evidence": deepcopy(evidence),
                         "plans": [
                             {"plan_id": "fiction_future_xy", "required_components": [{"component_id": "piece_x", "count": 1}, {"component_id": "piece_y", "count": 1}],
                              "condition_zh": "虚构转型条件成立时保留。"},
                         ]},
    }
