"""Explicit private configuration runner. Input samples are always read-only."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import stat
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .probe import LexiconEntry, Rect, ShopLayout, probe_shop, SlotLayout


def _read_pinned(path: Path, digest: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("A pinned input changed; refusing to run")
    return raw


def _external(path: Path, project: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == project or project in resolved.parents:
        raise ValueError("Configuration and results must stay outside the project")
    return resolved


def _output_path(path: Path, project: Path) -> Path:
    # Do not resolve symlinks away before checking them component by component.
    path = path.expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Output must be an absolute path without parent traversal")
    if path == project or project in path.parents:
        raise ValueError("Results must stay outside the project")
    return path


@contextmanager
def _safe_output_parent(path: Path):
    """Walk existing POSIX ancestors by fd; never follow a symbolic link."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("Unsafe output ancestor ownership or write permissions")
        yield fd
    finally:
        os.close(fd)


def _must_be_new(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FileExistsError("Output must be a completely new directory")


@contextmanager
def _new_output(path: Path):
    with _safe_output_parent(path) as parent:
        _must_be_new(parent, path.name)
        # mkdir is exclusive, including when another process races the check.
        os.mkdir(path.name, 0o700, dir_fd=parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            yield fd
        finally:
            os.close(fd)


def _write_new(directory_fd: int, name: str, data: bytes) -> None:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("Output name must be one path component")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _viewer(results: list[dict], summary: dict, images: dict[str, bytes]) -> str:
    """Embed exactly the verified input bytes, never re-read a source file URI."""
    escape = lambda v: html.escape(str(v), quote=True)
    sections = []
    for i, result in enumerate(results, 1):
        rows = []
        for slot in result["slots"]:
            names = "/".join(o["name"] for o in slot["name_options"]) or "未知"
            costs = "/".join(map(str, slot["price"]["candidates"])) or "未知"
            reasons = sorted(set(slot["reasons"] + slot["name"]["reasons"] + slot["price"]["reasons"]))
            rows.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in (
                slot["slot"], names, costs, slot["status"],
                slot["name"]["status"], slot["price"]["status"], ", ".join(reasons)
            )) + "</tr>")
        original = images[result["source_sha256"]]
        if hashlib.sha256(original).hexdigest() != result["source_sha256"]:
            raise ValueError("Embedded image digest differs from its OCR frame")
        if not original.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Private snapshot viewer requires PNG inputs")
        image_uri = "data:image/png;base64," + base64.b64encode(original).decode("ascii")
        sections.append(f"<section><h2>开发帧 {i}</h2><code>{escape(result['frame_ref'])}</code>"
                        f'<img src="{image_uri}" alt="本机历史开发原帧 {i}">'
                        "<div class=scroll><table><thead><tr><th>槽</th><th>名称候选</th><th>费用候选</th>"
                        "<th>格状态</th><th>名称状态</th><th>费用状态</th><th>原因</th></tr></thead>"
                        "<tbody>" + "".join(rows) + "</tbody></table></div></section>")
    return ("<!doctype html><html lang=zh-CN><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>商店五格开发探针｜本机私有</title><style>"
            "body{background:#111820;color:#e1e7ef;font:16px/1.6 system-ui;margin:24px auto;max-width:1350px;padding:0 20px}"
            "section{margin:28px 0 50px}img{display:block;width:100%;margin:14px 0;border:1px solid #374657}"
            "table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:8px;text-align:left;border:1px solid #374657}"
            "code{overflow-wrap:anywhere;color:#b4cde4}.scroll{overflow-x:auto}p{color:#bcc8d7}</style>"
            "<h1>商店五格开发探针</h1><p>本机私有 · 已见历史开发画面 · 机器候选／未知。"
            "词典匹配不证明真实身份；empty_candidate 只是空槽线索，occluded_candidate 是覆盖线索。"
            "不代表人工标注、独立复核或准确率，不产生买卖操作。</p>"
            f"<p>四类状态与本轮计数：{escape(summary['status_counts'])}；"
            f"逐帧处理毫秒：{escape(summary['processing_ms_per_frame'])}</p>" + "".join(sections) + "</html>\n")


def run_private(config_path: Path, output: Path) -> dict:
    """Run only the explicitly pinned sample IDs, then prove inputs unchanged."""
    project = Path(__file__).resolve().parents[2]
    config_path, output = _external(config_path, project), _output_path(output, project)
    # Read-only preflight rejects existing outputs before OCR. The exclusive
    # mkdir/open checks are repeated at write time, with no path-based chmod.
    with _safe_output_parent(output) as parent:
        _must_be_new(parent, output.name)
    config_bytes = config_path.read_bytes()
    cfg = json.loads(config_bytes)
    if cfg.get("purpose") != "exposed_development_frames_only":
        raise ValueError("Explicit development-frame configuration is required")
    root = Path(cfg["sample_root"]).expanduser().resolve(strict=True)
    # Never write inside, or above, the source workspace.
    if output == root or root in output.parents or output in root.parents:
        raise ValueError("Output must be separate from the sample workspace")
    if output == config_path or output in config_path.parents:
        raise ValueError("Output must be a separate result directory")
    workspace_path = root / "workspace.json"
    manifest_path = (root / cfg["manifest_path"]).resolve(strict=True)
    if root not in manifest_path.parents:
        raise ValueError("Manifest must be inside the configured workspace")
    workspace_raw = _read_pinned(workspace_path, cfg["workspace_sha256"])
    manifest_raw = _read_pinned(manifest_path, cfg["manifest_sha256"])
    workspace = json.loads(workspace_raw)
    annotations = {s["sample_id"]: s for s in workspace["annotations"]}
    manifest = {s["sample_id"]: s for s in json.loads(manifest_raw)}
    ids = cfg["allowed_sample_ids"]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("An explicit, unique sample allowlist is required")
    source_inputs = []
    for sample_id in ids:
        sample = annotations[sample_id]
        if sample != manifest[sample_id]:
            raise ValueError("Workspace and frozen manifest differ")
        asset = workspace["assets"][sample_id]
        path = (root / asset["path"]).resolve(strict=True)
        if root not in path.parents:
            raise ValueError("Source image escaped the configured sample workspace")
        if asset["sha256"] != sample["asset"]["sha256"]:
            raise ValueError("Annotation and image digest differ")
        data = _read_pinned(path, asset["sha256"])
        source_inputs.append((path, data, sample, asset))
    vocab_path = Path(cfg["vocabulary_path"]).expanduser().resolve(strict=True)
    vocab_raw = _read_pinned(vocab_path, cfg["vocabulary_sha256"])
    vocabulary = tuple(LexiconEntry(
        key=str(i), name=e["name"], aliases=tuple(e.get("aliases", [])), reference_cost=e.get("cost")
    ) for i, e in enumerate(json.loads(vocab_raw)))
    l = cfg["layout"]
    layout = ShopLayout(
        layout_id=l["layout_id"], image_size=tuple(l["image_size"]),
        slots=tuple(SlotLayout(s["slot"], **{k: Rect(tuple(s[k])) for k in (
            "card", "name_zone", "price", "interior"
        )}) for s in l["slots"]),
        scales=tuple(l.get("scales", [1, 2])), min_confidence=l.get("min_confidence", 0.5),
        empty_std_max=l.get("empty_std_max", 8), empty_mean_max=l.get("empty_mean_max", 40),
        occlusion_terms=tuple(l.get("occlusion_terms", [])),
    )
    from jcc_device.vision_ocr import VisionOCR

    results, names = [], {e.key: e.name for e in vocabulary}
    with VisionOCR(timeout=cfg.get("ocr_request_timeout_seconds", 10)) as backend:
        for path, data, sample, asset in source_inputs:
            result = probe_shop(data, layout=layout, lexicon=vocabulary, ocr=backend,
                                expected_sha256=asset["sha256"])
            details = result.private_details()
            # Exact private binding, available for other offline demos to join.
            details["source_sha256"] = asset["sha256"]
            details["source"] = {
                "path": str(path), "sample_id": sample["sample_id"],
                "frame": sample["frame"], "stream_id": sample["stream_id"],
                "host_clock_id": sample["host_clock_id"], "asset_ref": asset["asset_ref"],
            }
            for s in details["slots"]:
                s["name_options"] = [{"key": k, "name": names[k]} for k in s["name"]["candidates"]]
            results.append(details)
    # No successful report if any source changed during recognition.
    for path, before, digest in (
        (workspace_path, workspace_raw, cfg["workspace_sha256"]),
        (manifest_path, manifest_raw, cfg["manifest_sha256"]),
        (vocab_path, vocab_raw, cfg["vocabulary_sha256"]),
    ):
        if before != _read_pinned(path, digest):
            raise ValueError("A pinned input changed during recognition")
    if config_bytes != config_path.read_bytes():
        raise ValueError("Private configuration changed during the run")
    for path, data, _, asset in source_inputs:
        if data != _read_pinned(path, asset["sha256"]):
            raise ValueError("A source image changed during recognition")
    slots = [s for r in results for s in r["slots"]]
    summary = {
        "format": "shop-probe-private-run-v1", "revision": 1,
        "run_id": uuid.uuid4().hex, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_workspace_revision": workspace.get("revision"),
        "source_workspace_sha256": cfg["workspace_sha256"],
        "source_manifest_sha256": cfg["manifest_sha256"],
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "frames": len(results), "slots": len(slots),
        "status_counts": dict(Counter(s["status"] for s in slots)),
        "name_status_counts": dict(Counter(s["name"]["status"] for s in slots)),
        "price_status_counts": dict(Counter(s["price"]["status"] for s in slots)),
        "ocr_requests": sum(r["ocr_requests"] for r in results),
        "processing_ms_per_frame": [r["elapsed_ms"] for r in results],
        "sample_inputs_unchanged": True, "accuracy": None, "human_annotations": 0,
        "independent_reviews": 0, "acceptance_samples": 0, "actionable": False,
        "candidate_only": True,
    }
    image_bytes = {asset["sha256"]: data for _, data, _, asset in source_inputs}
    payloads = {
        "results.private.json": _json_bytes({"revision": 1, "summary": summary, "frames": results}),
        "summary.json": _json_bytes(summary),
        "查看四帧五格.html": _viewer(results, summary, image_bytes).encode("utf-8"),
    }
    with _new_output(output) as directory:
        for name, payload in payloads.items():
            _write_new(directory, name, payload)
        # Consumers require this last-written marker; failed runs may leave
        # partial files, but never a misleading completed bundle.
        _write_new(directory, "COMPLETED.json", _json_bytes({
            "revision": 1, "run_id": summary["run_id"],
            "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
        }))
    # Readable terminal output is itself local development output.
    for i, r in enumerate(results, 1):
        print(f"Frame {i} {r['frame_ref']}")
        for s in r["slots"]:
            name = "/".join(o["name"] for o in s["name_options"]) or "?"
            price = "/".join(map(str, s["price"]["candidates"])) or "?"
            print(f"  {s['slot']}: {name} | cost {price} | {s['status']} "
                  f"(name={s['name']['status']}, price={s['price']['status']})")
    print(json.dumps(summary, ensure_ascii=False))
    print("Private output: " + str(output))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only local shop development probe; no device actions")
    parser.add_argument("--config", required=True, type=Path, help="Private pinned configuration outside the project")
    parser.add_argument("--output", required=True, type=Path, help="Private result directory outside source samples/project")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run_private(args.config, args.output)
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Probe stopped: {type(exc).__name__}. Check private input configuration and pins.\n")
    return 0
