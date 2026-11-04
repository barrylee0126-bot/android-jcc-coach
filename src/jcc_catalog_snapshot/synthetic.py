"""Explicitly fictional snapshot fixture for adapter contract tests."""

from .core import REQUEST_FORMAT


def synthetic_request() -> dict:
    return {"format_version": REQUEST_FORMAT,
            "source": {"status": "known", "binding": {"game": "fiction_game", "region": "fiction_region", "mode": "fiction_mode", "season": "fiction_season", "version": "fiction_version"},
                       "provenance": {"kind": "synthetic", "source_url": "https://example.invalid/catalog.json", "retrieved_at": "2026-11-04T00:00:00Z", "sha256": "a" * 64}},
            "traits": [{"trait_key": "trait_leaf", "display_name": "虚构叶系", "thresholds": [2, 4]}, {"trait_key": "trait_guard", "display_name": "虚构护卫", "thresholds": [2, 3]}],
            "heroes": [{"hero_key": "hero_leaf_guard", "display_name": "虚构守叶", "trait_keys": ["trait_leaf", "trait_guard"]}, {"hero_key": "hero_leaf_guard_variant", "display_name": "虚构守叶", "trait_keys": ["trait_leaf"]}],
            "items": [{"item_key": "piece_x", "display_name": "虚构散件X", "component_keys": []}, {"item_key": "piece_y", "display_name": "虚构散件Y", "component_keys": []}, {"item_key": "item_xy", "display_name": "虚构成品XY", "component_keys": ["piece_x", "piece_y"]}]}
