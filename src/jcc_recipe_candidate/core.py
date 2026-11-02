"""Validate caller data, then call the pinned original JS recipe function.

This module never loads a game catalog, device state, private material, or a
default inventory. It does not implement a second recipe feasibility algorithm.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import unicodedata


REQUEST_FORMAT = "jcc-recipe-candidate-request/v1"
RESULT_FORMAT = "jcc-recipe-candidate/v1"
MAX_COUNT = 2**53 - 1
MAX_COMPONENT_IDS = 256
MAX_RECIPES = 4096
UPSTREAM_COMMIT = "8380b6dbeffe9edade7b198d8063bc3e18917b6d"
UPSTREAM_EXCERPT_SHA256 = "ba5620cb0e1ca229fb0dbc5aa173ef9ba9a4e0cbfe5b795a37b10d5dff7c6c59"
_BINDING_FIELDS = {"game", "region", "mode", "season", "patch", "rules_revision"}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}")
_UNKNOWN_VERSION_TOKENS = {
    "unknown", "unverified", "unconfirmed", "pending", "latest", "current",
    "unspecified", "none", "null", "missing", "tbd", "any", "all",
}
# The original function counts with {}. Never allow inherited property names
# to enter that unmodified code as component IDs (or any other entity IDs).
_JS_RESERVED = {
    "__proto__", "constructor", "toString", "toLocaleString", "valueOf",
    "hasOwnProperty", "isPrototypeOf", "propertyIsEnumerable",
    "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
}


def _issue(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message_zh": message}


def _unknown(issues: list[dict]) -> dict:
    return {
        "format_version": RESULT_FORMAT,
        "status": "unknown",
        "candidate_semantics": "independently_feasible_alternatives",
        "candidate_order": "input_recipe_order_not_rank",
        "catalog_scope": "caller_supplied_recipes_only",
        "simultaneous_feasibility": "not_evaluated",
        "inventory_consumed": False,
        "strategy_recommendation": False,
        "execution_eligible": False,
        "input_verification": "caller_declared_only",
        "data_kind": None,
        "binding": None,
        "evidence": None,
        "inventory_snapshot": None,
        "candidates": None,
        "issues": issues,
        "notes_zh": [
            "每项只表示从同一份起始库存单独检查该配方；候选可能争用散件。",
            "未计算同时合成方案、合成次数、装备强弱、合成位置或操作合法性。",
            "unknown 的候选为 null；只有成功检查已提供配方后才可能返回空列表。",
            "调用方须信任 PATH 选中的 Node、bridge.mjs、本包文件及宿主环境；源码摘要只核对算法片段，不认证这些依赖或计算结果。",
        ],
        "algorithm": {"project": "ndh0408/tft-advisor", "commit": UPSTREAM_COMMIT,
                      "function": "craftableFrom", "reuse": "unchanged_js_excerpt"},
    }


def _shape(value: Any, keys: set[str], path: str, issues: list) -> bool:
    if type(value) is not dict or set(value) != keys:
        issues.append(_issue("shape_invalid", path, "对象缺少必需字段、含未知字段或类型不正确。"))
        return False
    return True


def _known_block(value: Any, keys: set[str], path: str, issues: list) -> bool:
    if type(value) is dict and value.get("status") != "known":
        issues.append(_issue("input_not_known", path + ".status", "输入未显式确认为已知；不补默认值。"))
        return False
    return _shape(value, keys | {"status", "binding", "evidence"}, path, issues)


def _identifier(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None and value not in _JS_RESERVED


def _reference(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 1024 and value == value.strip()
            and not any(unicodedata.category(c) in {"Cc", "Cs"} for c in value))


def _binding(value: Any, path: str, issues: list) -> dict | None:
    if not _shape(value, _BINDING_FIELDS, path, issues):
        return None
    if not all(type(v) is str and _VERSION_TOKEN.fullmatch(v)
               and v.lower() not in _UNKNOWN_VERSION_TOKENS for v in value.values()):
        issues.append(_issue("version_invalid", path, "版本各字段须为非空、明确的版本标识；不猜测补丁或规则修订。"))
        return None
    return dict(value)


def _evidence(value: Any, path: str, issues: list) -> dict | None:
    if type(value) is not dict or type(value.get("kind")) is not str or value["kind"] not in {"synthetic", "verified"}:
        issues.append(_issue("evidence_insufficient", path, "仅接收显式合成或经核验的数据声明及来源。"))
        return None
    keys = {"kind", "refs"} | ({"verification_ref"} if value["kind"] == "verified" else set())
    if not _shape(value, keys, path, issues):
        return None
    refs = value["refs"]
    if (type(refs) is not list or not 1 <= len(refs) <= 32
            or not all(_reference(ref) for ref in refs) or len(set(refs)) != len(refs)
            or (value["kind"] == "verified" and not _reference(value["verification_ref"]))):
        issues.append(_issue("evidence_insufficient", path, "须提供非空、不重复的来源；经核验声明另需核验记录引用。"))
        return None
    result = {"kind": value["kind"], "refs": list(refs)}
    if value["kind"] == "verified":
        result["verification_ref"] = value["verification_ref"]
    return result


def _validate(request: Any) -> tuple[dict | None, list[dict]]:
    issues: list[dict] = []
    if not _shape(request, {"format_version", "version", "inventory", "catalog"}, "$", issues):
        return None, issues
    if request["format_version"] != REQUEST_FORMAT:
        issues.append(_issue("format_unsupported", "$.format_version", "请求格式版本不受支持。"))
    bindings, evidence, blocks = {}, {}, {}
    for name, extra in (("version", set()),
                        ("inventory", {"availability", "complete", "entries"}),
                        ("catalog", {"component_ids", "recipes"})):
        block = request[name]
        if _known_block(block, extra, "$." + name, issues):
            blocks[name] = block
            bindings[name] = _binding(block["binding"], "$." + name + ".binding", issues)
            evidence[name] = _evidence(block["evidence"], "$." + name + ".evidence", issues)
    if len(bindings) == 3 and all(v is not None for v in bindings.values()):
        if any(v != bindings["version"] for v in bindings.values()):
            issues.append(_issue("version_mismatch", "$.binding", "版本、库存及配方的游戏/地区/模式/赛季/补丁/规则修订必须完全一致。"))
    if len(evidence) == 3 and all(v is not None for v in evidence.values()):
        if len({v["kind"] for v in evidence.values()}) != 1:
            issues.append(_issue("evidence_kind_mismatch", "$.evidence", "不能混用合成数据与经核验数据作为同一份局面。"))

    component_ids: set[str] = set()
    recipes = []
    if "catalog" in blocks:
        catalog = blocks["catalog"]
        ids = catalog["component_ids"]
        if type(ids) is not list or len(ids) > MAX_COMPONENT_IDS:
            issues.append(_issue("component_ids_invalid", "$.catalog.component_ids", "散件 ID 须为显式列表，最多 256 项。"))
        else:
            for index, item in enumerate(ids):
                path = f"$.catalog.component_ids[{index}]"
                if not _identifier(item):
                    issues.append(_issue("id_invalid", path, "ID 格式无效或与 JavaScript 原型属性冲突。"))
                elif item in component_ids:
                    issues.append(_issue("component_id_duplicate", path, "散件目录 ID 重复；不自动去重。"))
                else:
                    component_ids.add(item)
        rows = catalog["recipes"]
        if type(rows) is not list or len(rows) > MAX_RECIPES:
            issues.append(_issue("recipes_invalid", "$.catalog.recipes", "配方须为显式列表，最多 4096 项。"))
        else:
            seen_items, seen_pairs = set(), set()
            for index, row in enumerate(rows):
                path = f"$.catalog.recipes[{index}]"
                if not _shape(row, {"item_id", "components"}, path, issues):
                    continue
                item, pair = row["item_id"], row["components"]
                if not _identifier(item):
                    issues.append(_issue("id_invalid", path + ".item_id", "成品 ID 缺失或不合法。"))
                    continue
                if item in seen_items:
                    issues.append(_issue("recipe_id_duplicate", path + ".item_id", "成品 ID 重复或冲突；不采用最后一条。"))
                seen_items.add(item)
                if item in component_ids:
                    issues.append(_issue("item_component_overlap", path + ".item_id", "成品与散件 ID 必须区分；本接口不处理链式合成。"))
                if (type(pair) is not list or len(pair) != 2
                        or not all(_identifier(c) and c in component_ids for c in pair)):
                    issues.append(_issue("recipe_pair_invalid", path + ".components", "配方必须恰含两份目录内合法散件；空配方也无效。"))
                    continue
                key = tuple(sorted(pair))
                if key in seen_pairs:
                    issues.append(_issue("recipe_pair_duplicate", path + ".components", "同一无序散件对出现多次或对应不同成品；需先由调用方解决。"))
                seen_pairs.add(key)
                recipes.append({"item_id": item, "components": list(pair)})

    entries = []
    if "inventory" in blocks:
        inventory = blocks["inventory"]
        if inventory["complete"] is not True:
            issues.append(_issue("inventory_incomplete", "$.inventory.complete", "闲置散件库存须完整已知；部分识别不等于空库存。"))
        if inventory["availability"] != "unequipped_components":
            issues.append(_issue("inventory_unavailable", "$.inventory.availability", "仅接收调用方确认可用的闲置散件；不猜测已装备或位置不明的物品。"))
        rows = inventory["entries"]
        if type(rows) is not list or len(rows) > MAX_COMPONENT_IDS:
            issues.append(_issue("inventory_invalid", "$.inventory.entries", "库存须为显式列表，最多 256 项；未知不能写成空列表。"))
        else:
            seen = set()
            for index, row in enumerate(rows):
                path = f"$.inventory.entries[{index}]"
                if not _shape(row, {"component_id", "count"}, path, issues):
                    continue
                item, count = row["component_id"], row["count"]
                if not _identifier(item) or item not in component_ids:
                    issues.append(_issue("inventory_id_invalid", path + ".component_id", "库存 ID 必须属于所提供散件目录。"))
                    continue
                if item in seen:
                    issues.append(_issue("inventory_id_duplicate", path, "同类库存须显式给总数量；重复行不相加，以免重复计数。"))
                seen.add(item)
                if type(count) is not int or not 0 <= count <= MAX_COUNT:
                    issues.append(_issue("count_invalid", path + ".count", "数量须为 0 至 2^53-1 的整数；未知、布尔值、负数或文本无效。"))
                    continue
                entries.append({"component_id": item, "count": count})
    if issues:
        return None, issues
    return {"binding": bindings["version"], "evidence": evidence,
            "data_kind": evidence["version"]["kind"], "entries": entries, "recipes": recipes}, []


class _UpstreamError(Exception):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _run_upstream(data: dict) -> list:
    package = Path(__file__).resolve().parent
    try:
        source = (package / "vendor" / "craftableFrom.mjs").read_bytes()
    except OSError as exc:
        raise _UpstreamError("upstream_source_unavailable") from exc
    if hashlib.sha256(source).hexdigest() != UPSTREAM_EXCERPT_SHA256:
        raise _UpstreamError("upstream_source_integrity")
    node = shutil.which("node")
    if node is None:
        raise _UpstreamError("upstream_runtime_unavailable")
    # A legal recipe needs exactly two tokens, so >2 copies of one token cannot
    # change its individual feasibility. Preserve full counts in the snapshot.
    held = [entry["component_id"] for entry in data["entries"]
            for _ in range(min(entry["count"], 2))]
    recipes = {row["item_id"]: {"name": row["item_id"], "components": row["components"]}
               for row in data["recipes"]}
    # No inherited account/configuration variables or Node preload options.
    env = {"PATH": os.defpath}
    try:
        # Bind relative PATH results to the caller's cwd before changing the
        # child's cwd. An absolute path does not authenticate the executable.
        node = os.path.abspath(node)
        completed = subprocess.run(
            [node, str(package / "bridge.mjs")], cwd=package,
            input=json.dumps({"heldComponents": held, "recipes": recipes}),
            text=True, encoding="utf-8", capture_output=True, timeout=5, env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _UpstreamError("upstream_timeout") from exc
    except OSError as exc:
        raise _UpstreamError("upstream_runtime_unavailable") from exc
    if completed.returncode != 0:
        raise _UpstreamError("upstream_failed")
    try:
        rows = _json(completed.stdout)
    except (ValueError, TypeError, RecursionError) as exc:
        raise _UpstreamError("upstream_output_invalid") from exc
    if type(rows) is not list:
        raise _UpstreamError("upstream_output_invalid")
    seen, order = set(), []
    indices = {row["item_id"]: i for i, row in enumerate(data["recipes"])}
    for row in rows:
        if (type(row) is not dict or set(row) != {"item", "name", "components"}
                or type(row["item"]) is not str or row["item"] not in recipes
                or row["item"] in seen or row["name"] != row["item"]
                or row["components"] != recipes[row["item"]]["components"]):
            raise _UpstreamError("upstream_output_invalid")
        seen.add(row["item"])
        order.append(indices[row["item"]])
    if order != sorted(order):
        raise _UpstreamError("upstream_output_invalid")
    return rows


def analyze_request(request: Any) -> dict:
    """Return independent candidates or unknown; never mutate the caller input.

    Validation is local and checks declarations, not the truth of external
    evidence. Only fully valid input invokes the vendored original JS function.
    """
    data, issues = _validate(request)
    result = _unknown(issues)
    if data is None:
        return result
    try:
        rows = _run_upstream(data)
    except _UpstreamError as exc:
        result["issues"] = [_issue(str(exc), "$.algorithm", "固定算法未成功运行或输出无效；不解释为没有可合成项。")]
        return result
    candidates = []
    for row in rows:
        required = {}
        for component in row["components"]:
            required[component] = required.get(component, 0) + 1
        candidates.append({
            "item_id": row["item"], "components": list(row["components"]),
            "required_counts": [{"component_id": key, "count": value} for key, value in required.items()],
            "feasibility": "individually_feasible",
        })
    result.update(status="evaluated", data_kind=data["data_kind"], binding=data["binding"],
                  evidence=data["evidence"], inventory_snapshot=data["entries"], candidates=candidates)
    return result


def analyze_json(text: Any) -> dict:
    """Strict JSON entry point; duplicate keys and non-finite values fail closed."""
    try:
        if type(text) is not str:
            raise ValueError("JSON text required")
        request = _json(text)
    except (ValueError, TypeError, RecursionError):
        return _unknown([_issue("json_invalid", "$", "JSON 无效、含重复键或非有限数；未推断库存。")])
    return analyze_request(request)
