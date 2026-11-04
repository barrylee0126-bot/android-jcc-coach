"""Fictional data used only for transition-coach contract tests."""

from copy import deepcopy

from jcc_team_planner.fixtures import synthetic_request

from .core import REQUEST_FORMAT


def request() -> dict:
    snapshot = synthetic_request()
    # This fictional item makes the "carrier leaves board" boundary observable.
    snapshot["board"][1]["equipped_item_ids"] = ["fiction_item"]
    return {
        "format_version": REQUEST_FORMAT,
        "team_snapshot": snapshot,
        "target_rule_evidence": [
            {"target_id": "leaf-shot", "binding": deepcopy(snapshot["binding"]),
             "refs": ["synthetic:leaf-shot-route"]},
        ],
    }


def copied_request() -> dict:
    return deepcopy(request())
