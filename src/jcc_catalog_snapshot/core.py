"""Import explicit local catalog snapshots without deciding client applicability.

This adapter deliberately has no network capability. It accepts caller-supplied
structured records, checks their shape, and creates stable local lookup maps.
A source's version claim never binds it to a tablet client or a live game.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

REQUEST_FORMAT = "jcc-catalog-snapshot-request/v1"
RESULT_FORMAT = "jcc-catalog-snapshot/v1"
MAX_HEROES, MAX_TRAITS, MAX_ITEMS = 512, 256, 1024
MAX_TRAITS_PER_HERO, MAX_THRESHOLDS, MAX_COMPONENTS = 32, 32, 8
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_BINDING = ("game", "region", "mode", "season", "version")
_UNKNOWN = {"unknown", "unverified", "unconfirmed", "latest", "current", "any", "none", "null", "tbd", "n/a",
            "未知", "待核实", "未核实", "最新", "当前", "待定", "不明", "最新版本", "当前版本", "待核验"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _issue(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message_zh": message}


def _unknown(issues: list[dict]) -> dict:
    return {"format_version": RESULT_FORMAT, "status": "unknown", "data_kind": None,
            "source_binding": None, "source_provenance": None, "name_lookup": None,
            "hero_traits": None, "trait_thresholds": None, "item_components": None,
            "issues": issues, "source_version_applicability": "unknown",
            "target_client_binding": "unknown", "decision_eligible": False, "action_advice": None,
            "notes_zh": ["不能读取、下载或证明官方数据；输入必须由调用方在本地明确提供。",
                         "来源的模式、赛季和版本只是来源声明，不能据此认定平板客户端或对局实际适用。",
                         "任何规则绑定、来源或结构不完整时不输出映射，更不会输出行动建议。"]}


def _shape(value: Any, keys: set[str], path: str, issues: list[dict]) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "对象缺少必需字段、含未知字段或类型不正确。"))
        return False
    return True


def _key(value: Any) -> bool:
    return type(value) is str and _KEY.fullmatch(value) is not None


def _text(value: Any, maximum: int = 256) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def _timestamp(value: Any) -> bool:
    if not _text(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _binding(value: Any, path: str, issues: list[dict]) -> dict | None:
    if not _shape(value, set(_BINDING), path, issues):
        return None
    if not all(_text(value[field], 128) and value[field].casefold() not in _UNKNOWN for field in _BINDING):
        issues.append(_issue("binding_invalid", path, "来源游戏、地区、模式、赛季和版本均须为明确字符串；不以 unknown/latest 代替。"))
        return None
    return {field: value[field] for field in _BINDING}


def _provenance(value: Any, path: str, issues: list[dict]) -> dict | None:
    if not _shape(value, {"kind", "source_url", "retrieved_at", "sha256"}, path, issues):
        return None
    source_url = value["source_url"]
    try:
        url = urlsplit(source_url) if _text(source_url, 2048) and not any(char.isspace() for char in source_url) else None
        port = url.port if url is not None else None
        url_valid = (url is not None and url.scheme == "https" and bool(url.netloc) and bool(url.hostname)
                     and url.username is None and url.password is None
                     and (port is None or 1 <= port <= 65535))
    except (AttributeError, TypeError, ValueError):
        url_valid = False
    if (type(value["kind"]) is not str or value["kind"] not in {"synthetic", "official_candidate"} or not url_valid
            or not _timestamp(value["retrieved_at"]) or type(value["sha256"]) is not str
            or _SHA256.fullmatch(value["sha256"]) is None):
        issues.append(_issue("provenance_invalid", path, "来源须有类型、HTTPS URL、带时区采集时间和小写 SHA-256；这些都是调用方声明。"))
        return None
    return dict(value)


def _index_by_name(rows: list[dict], key_field: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["display_name"], []).append(row[key_field])
    return {name: sorted(keys) for name, keys in sorted(grouped.items())}


def _validate(request: Any) -> tuple[dict | None, list[dict]]:
    issues: list[dict] = []
    if not _shape(request, {"format_version", "source", "heroes", "traits", "items"}, "$", issues):
        return None, issues
    if request["format_version"] != REQUEST_FORMAT:
        issues.append(_issue("format_unsupported", "$.format_version", "请求格式版本不受支持。"))
    source_binding = source_provenance = None
    source = request["source"]
    if type(source) is not dict or source.get("status") != "known":
        issues.append(_issue("source_not_known", "$.source.status", "来源、版本声明和摘要须显式为已知；不能从文件名或客户端版本推断。"))
    elif _shape(source, {"status", "binding", "provenance"}, "$.source", issues):
        source_binding = _binding(source["binding"], "$.source.binding", issues)
        source_provenance = _provenance(source["provenance"], "$.source.provenance", issues)

    traits: dict[str, dict] = {}
    rows = request["traits"]
    if type(rows) is not list or not rows or len(rows) > MAX_TRAITS:
        issues.append(_issue("traits_invalid", "$.traits", "羁绊须为非空、有界列表。"))
    else:
        for index, row in enumerate(rows):
            path = f"$.traits[{index}]"
            if not _shape(row, {"trait_key", "display_name", "thresholds"}, path, issues):
                continue
            key, name, thresholds = row["trait_key"], row["display_name"], row["thresholds"]
            if not _key(key) or key in traits or not _text(name) or type(thresholds) is not list or not thresholds or len(thresholds) > MAX_THRESHOLDS:
                issues.append(_issue("trait_invalid", path, "羁绊键须唯一，显示名和至少一个档位须明确。"))
            elif (not all(type(x) is int and not isinstance(x, bool) and 1 <= x <= 99 for x in thresholds)
                  or thresholds != sorted(set(thresholds))):
                issues.append(_issue("trait_thresholds_invalid", path + ".thresholds", "羁绊档位须为严格递增的正整数列表。"))
            else:
                traits[key] = {"trait_key": key, "display_name": name, "thresholds": list(thresholds)}

    heroes: dict[str, dict] = {}
    rows = request["heroes"]
    if type(rows) is not list or not rows or len(rows) > MAX_HEROES:
        issues.append(_issue("heroes_invalid", "$.heroes", "英雄须为非空、有界列表。"))
    else:
        for index, row in enumerate(rows):
            path = f"$.heroes[{index}]"
            if not _shape(row, {"hero_key", "display_name", "trait_keys"}, path, issues):
                continue
            key, name, trait_keys = row["hero_key"], row["display_name"], row["trait_keys"]
            if (not _key(key) or key in heroes or not _text(name) or type(trait_keys) is not list or not trait_keys
                    or len(trait_keys) > MAX_TRAITS_PER_HERO or not all(_key(x) and x in traits for x in trait_keys)
                    or len(set(trait_keys)) != len(trait_keys)):
                issues.append(_issue("hero_invalid", path, "英雄键、显示名和已存在的羁绊键须完整且唯一。"))
            else:
                heroes[key] = {"hero_key": key, "display_name": name, "trait_keys": list(trait_keys)}

    items: dict[str, dict] = {}
    rows = request["items"]
    if type(rows) is not list or not rows or len(rows) > MAX_ITEMS:
        issues.append(_issue("items_invalid", "$.items", "装备须为非空、有界列表。"))
    else:
        for index, row in enumerate(rows):
            path = f"$.items[{index}]"
            if not _shape(row, {"item_key", "display_name", "component_keys"}, path, issues):
                continue
            key, name, components = row["item_key"], row["display_name"], row["component_keys"]
            if (not _key(key) or key in items or not _text(name) or type(components) is not list
                    or len(components) > MAX_COMPONENTS or not all(_key(x) for x in components)):
                issues.append(_issue("item_invalid", path, "装备键、显示名和配方键须为合法有界值。"))
            else:
                items[key] = {"item_key": key, "display_name": name, "component_keys": list(components)}
        for index, row in enumerate(rows):
            if type(row) is dict and _key(row.get("item_key")) and row.get("item_key") in items:
                if any(key not in items for key in items[row["item_key"]]["component_keys"]):
                    issues.append(_issue("item_component_missing", f"$.items[{index}].component_keys", "配方组件键必须在同一快照的装备表中出现。"))
        unresolved = set(items)
        while True:
            removable = [key for key in unresolved if not (set(items[key]["component_keys"]) & unresolved)]
            if not removable:
                break
            unresolved.difference_update(removable)
        if unresolved:
            issues.append(_issue("item_recipe_cycle", "$.items", "装备配方不能包含自引用或多件循环引用；同一散件重复两次仍可合法。"))
    if issues:
        return None, issues
    return {"binding": source_binding, "provenance": source_provenance, "traits": traits, "heroes": heroes, "items": items}, []


def analyze_request(request: Any) -> dict:
    """Build candidate lookup maps from one local, caller-declared snapshot."""
    data, issues = _validate(request)
    if data is None:
        return _unknown(issues)
    hero_rows = [data["heroes"][key] for key in sorted(data["heroes"])]
    trait_rows = [data["traits"][key] for key in sorted(data["traits"])]
    item_rows = [data["items"][key] for key in sorted(data["items"])]
    return {"format_version": RESULT_FORMAT, "status": "candidate", "data_kind": data["provenance"]["kind"],
            "source_binding": deepcopy(data["binding"]), "source_provenance": deepcopy(data["provenance"]),
            "name_lookup": {"heroes": _index_by_name(hero_rows, "hero_key"), "traits": _index_by_name(trait_rows, "trait_key"), "items": _index_by_name(item_rows, "item_key")},
            "hero_traits": {row["hero_key"]: list(row["trait_keys"]) for row in hero_rows},
            "trait_thresholds": {row["trait_key"]: list(row["thresholds"]) for row in trait_rows},
            "item_components": {row["item_key"]: list(row["component_keys"]) for row in item_rows},
            "issues": [], "source_version_applicability": "unknown", "target_client_binding": "unknown",
            "decision_eligible": False, "action_advice": None,
            "notes_zh": ["稳定键只在调用方这一份快照中稳定；相同显示名保留多个候选键，不自动选一个。",
                         "来源 binding 是调用方关于资料的声明，未证明当前平板、对局模式、补丁或热修实际一致。",
                         "本模块只适合给后续识别和规则管线提供候选映射，不能单独产生购买、装备、出售、转型或站位建议。"]}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def analyze_json(text: Any) -> dict:
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未推断规则。")])
    return analyze_request(value)
