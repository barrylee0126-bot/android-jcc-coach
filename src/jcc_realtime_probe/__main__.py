"""Explicit modes only; importing or running tests never connects a device."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

from .assets import read_recording
from .profile import outside_project, read_profile
from .runtime import run_live
from .timeline import run_timeline, synthetic_profile, synthetic_rows


def _new_private_output(value):
    """Create once at owner-only permissions; never chmod or unlink a path.

    Resolve/validate the parent, then pin it by descriptor. Parent aliases are
    allowed only when their resolved directory is outside the project. Keep
    the final component unresolved: existing files and all final symlinks,
    including dangling links, must fail O_EXCL rather than redirect creation.
    Failed runs retain any newly created, possibly partial 0600 output.
    """
    path = Path(value).expanduser()
    parent = outside_project(path.parent)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only pixel freshness experiment; no formal game advice")
    sub = parser.add_subparsers(dest="mode", required=True)
    synthetic = sub.add_parser("synthetic", help="generated RGB and virtual clock; no device/OCR")
    synthetic.add_argument("--case", choices=("static", "outside_motion", "inside_aba", "sequence_gap", "receive_stall"),
                           default="outside_motion")
    replay = sub.add_parser("replay", help="private received pixels + virtual simulated processing time")
    replay.add_argument("--manifest", required=True)
    replay.add_argument("--profile", required=True)
    tablet = sub.add_parser("tablet", help="explicit read-only default-tablet capture + real Apple Vision")
    tablet.add_argument("--profile", required=True)
    tablet.add_argument("--duration", type=float, default=20)
    tablet.add_argument("--max-frames", type=int, default=80)
    tablet.add_argument("--sample-fps", type=int, default=4)
    tablet.add_argument("--record-private", help="new directory OUTSIDE this project")
    for command in (synthetic, replay):
        command.add_argument("--processing-ms", type=int, default=1450,
                             help="simulated work duration; never measured OCR latency")
    for command in (synthetic, replay, tablet):
        command.add_argument("--events", help="new anonymous JSONL file OUTSIDE this project")
        command.add_argument("--report", help="new aggregate report OUTSIDE this project")
    args = parser.parse_args(argv)
    event_file = report_file = None
    try:
        with ExitStack() as outputs:
            if args.events:
                event_file = outputs.enter_context(_new_private_output(args.events))
            if args.report:
                report_file = outputs.enter_context(_new_private_output(args.report))
            if args.mode == "synthetic":
                result = run_timeline(synthetic_rows(args.case), synthetic_profile(),
                                      processing_ns=args.processing_ms * 1_000_000, events_file=event_file)
                result["synthetic_case"] = args.case
            elif args.mode == "replay":
                result = run_timeline(read_recording(args.manifest), read_profile(args.profile, "replay"),
                                      source_kind="replay", processing_ns=args.processing_ms * 1_000_000,
                                      events_file=event_file)
            else:
                result = run_live(read_profile(args.profile, "live"), duration_seconds=args.duration,
                                  max_frames=args.max_frames, events_file=event_file,
                                  recording_dir=args.record_private, sample_fps=args.sample_fps)
            output = json.dumps(result, ensure_ascii=False, indent=2)
            if report_file:
                report_file.write(output + "\n")
        print(output)
        return 1 if (result.get("error_types") or result.get("worker_alive")
                     or result.get("status", "completed") != "completed") else 0
    except Exception as error:
        # Paths, device IDs, OCR text and subprocess logs must not escape.
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
