"""Private JSONL latency accounting; this command never reads or controls a tablet."""

import argparse
import json
import os
from pathlib import Path
import sys

from .audit import audit_attempts, read_attempts


def _outside_project(path):
    if path.name in {"", ".", ".."}:
        raise ValueError("report_name")
    root = Path(__file__).resolve().parents[2]
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("report_parent") from error
    if not parent.is_dir() or parent == root or root in parent.parents:
        raise ValueError("report_must_be_outside_project")
    # Deliberately retain the final component without resolving it: resolving
    # a dangling final symlink would follow its target before O_NOFOLLOW gets
    # a chance to reject it.
    return parent / path.name


def _write_new_private_report(path, payload):
    path = _outside_project(path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("new_private_report_required")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ValueError("new_private_report_required") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except Exception:
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="全分母时延审计；只读私有JSONL，不访问平板。")
    parser.add_argument("--attempts", required=True, type=Path, help="私有机会日志JSONL")
    parser.add_argument("--report", required=True, type=Path, help="项目外新建的私有汇总JSON")
    args = parser.parse_args(argv)
    try:
        report = audit_attempts(read_attempts(args.attempts))
        _write_new_private_report(args.report, report)
    except (OSError, ValueError):
        print("时延审计未通过；没有访问平板或输出原始机会内容。", file=sys.stderr)
        return 1
    print(json.dumps({"opportunities": report["opportunities"], "outcomes": report["outcomes"],
                      "all_supplied_rows_accounted": report["all_supplied_rows_accounted"],
                      "input_opportunity_completeness": report["input_opportunity_completeness"]},
                     ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
