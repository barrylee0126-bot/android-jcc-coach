"""Content-free command output; all images, labels and edits stay private."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .workflow import SampleStore, WorkflowError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors echo user arguments, potentially filenames
        # containing personal information. Help is explicit and content-free.
        self.exit(2, '{"error":{"code":"invalid_arguments","message":"Invalid or missing arguments; use --help for command syntax."}}\n')


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="本地样本导入与标注；不联网、不连接设备、不输出图像或标签原文。")
    parser.add_argument("--private-dir", required=True, type=Path, help="显式指定绝对私有目录；项目、Git 与已知云同步目录禁止。")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    init = commands.add_parser("init", help="创建新的私有工作区；不覆盖已有目录。")
    init.add_argument("--dataset-id", required=True)
    add = commands.add_parser("import", help="导入调用方提供的本地 PNG/JPEG；全部标签初始未知。")
    add.add_argument("--image", required=True, type=Path)
    add.add_argument("--sample-kind", required=True, choices=("synthetic", "real"))
    for key in ("recording-session-id", "dedupe-group-id", "state-group-id", "annotator-id"):
        add.add_argument("--" + key, required=True)
    add.add_argument("--redaction", choices=("none", "redacted"), default="none", help="调用方声明；工具不自动脱敏。")
    validate = commands.add_parser("validate", help="校验契约、分集、私有映射及图片字节/像素/尺寸。")
    validate.add_argument("--manifest", help="可选：工作区内已有清单的相对路径。")
    export = commands.add_parser("export-manifest", help="向私有 manifests/ 目录写不可覆盖的标注数组。")
    export.add_argument("--out", required=True)
    template = commands.add_parser("edit-template", help="向私有 edits/ 目录写出可编辑标签及修订号。")
    template.add_argument("--sample-id", required=True)
    template.add_argument("--out", required=True)
    annotate = commands.add_parser("annotate", help="应用人工编辑文件；重置复核，不改变分集。")
    annotate.add_argument("--edit-file", required=True)
    annotate.add_argument("--annotator-id", required=True)
    review = commands.add_parser("review", help="记录不同人员对当前修订的复核；不改变标签或分集。")
    review.add_argument("--sample-id", required=True)
    review.add_argument("--reviewer-id", required=True)
    review.add_argument("--evidence-ref", required=True, help="私有复核依据的无个人信息代号；不是 OCR 原文。")
    review.add_argument("--status", choices=("independently_reviewed", "adjudicated"), default="independently_reviewed")
    review.add_argument("--expected-revision", required=True, type=int)
    split = commands.add_parser("split", help="按完整录制场次及去重连通组分集；不会自动标注或复核。")
    split.add_argument("--recording-session-id", required=True, action="append")
    split.add_argument("--target", required=True, choices=("quarantine", "development", "acceptance"))
    split.add_argument("--expected-revision", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            result = SampleStore.create(args.private_dir, dataset_id=args.dataset_id).validate()
        else:
            store = SampleStore(args.private_dir)
            if args.command == "import":
                result = store.import_image(args.image, sample_kind=args.sample_kind,
                                            recording_session_id=args.recording_session_id,
                                            dedupe_group_id=args.dedupe_group_id, state_group_id=args.state_group_id,
                                            annotator_id=args.annotator_id, redaction=args.redaction)
            elif args.command == "validate":
                result = store.validate(args.manifest)
            elif args.command == "export-manifest":
                result = store.export_manifest(args.out)
            elif args.command == "edit-template":
                result = store.edit_template(args.sample_id, args.out)
            elif args.command == "annotate":
                result = store.annotate(args.edit_file, annotator_id=args.annotator_id)
            elif args.command == "review":
                result = store.review(args.sample_id, reviewer_id=args.reviewer_id,
                                      expected_revision=args.expected_revision, evidence_ref=args.evidence_ref, status=args.status)
            else:
                result = store.assign_split(args.recording_session_id, split=args.target, expected_revision=args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except WorkflowError as error:
        result = {"error": {"code": error.code, "message": str(error)}}
    except OSError:
        result = {"error": {"code": "local_io_failed", "message": "A local read or write failed; check private directory availability and permissions. No source content was printed."}}
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
