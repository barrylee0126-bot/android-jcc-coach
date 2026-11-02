"""CLI for local candidate snapshots; source paths are always runtime inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog, CatalogError, FieldRef, KINDS, SCOPE_KEYS, SourceSpec, load_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read local game-reference candidates; no network or source writes.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--manifest", type=Path, help="Local schema_version=1 manifest; paths relative to its directory.")
    inputs.add_argument("--source", nargs=2, action="append", metavar=("KIND", "PATH"),
                        help="Repeat for hero, trait, item or augment. Missing provenance stays unknown.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("summary", help="Record and duplicate-ID counts, never completeness of game rules.")
    commands.add_parser("sources", help="Snapshot fingerprints, source claims and verification boundaries.")
    query = commands.add_parser("find", help="Search candidate metadata without printing full source records.")
    query.add_argument("--kind", choices=KINDS)
    query.add_argument("--name", help="Exact source name.")
    query.add_argument("--id", dest="external_id", help="Exact string source ID; all matching variants are returned.")
    query.add_argument("--text", help="Substring of source name or source aliases.")
    query.add_argument("--source-id")
    query.add_argument("--claimed", nargs=2, action="append", metavar=("DIMENSION", "VALUE"),
                       help="Exact supplier-claim filter only; never target-client applicability.")
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--offset", type=int, default=0)
    show = commands.add_parser("record", help="Metadata for one source-record key.")
    show.add_argument("record_key")
    field = commands.add_parser("field", help="One source field and its evidence; resolved value remains null.")
    field.add_argument("record_key")
    field.add_argument("pointer", help="RFC 6901 pointer relative to the source record, e.g. /stats/0/hp.")
    compare = commands.add_parser("compare", help="Compare explicitly selected raw field claims.")
    compare.add_argument("--field", nargs=2, action="append", required=True, metavar=("RECORD_KEY", "POINTER"))
    compare.add_argument("--relationship", choices=("unresolved", "same_fact"), default="unresolved")
    compare.add_argument("--reason", help="Required comparison hypothesis for same_fact; not an adjudication.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        specs = load_manifest(args.manifest) if args.manifest else [
            SourceSpec(source_id=kind + ":" + Path(path).name, kind=kind, path=path)
            for kind, path in args.source]
        catalog = Catalog(specs)
        if args.command == "summary":
            result = catalog.summary()
        elif args.command == "sources":
            result = {"sources": catalog.sources(), "decision_eligible": False}
        elif args.command == "record":
            result = catalog.record(args.record_key)
        elif args.command == "field":
            result = catalog.field(args.record_key, args.pointer)
        elif args.command == "compare":
            result = catalog.compare_fields([FieldRef(*pair) for pair in args.field],
                                            relationship=args.relationship, reason=args.reason)
        else:
            if not 1 <= args.limit <= 1000 or args.offset < 0:
                raise CatalogError("invalid_limit", "find requires limit 1..1000 and nonnegative offset.")
            scope = {}
            for key, value in args.claimed or []:
                if key not in SCOPE_KEYS or key in scope:
                    raise CatalogError("invalid_scope", "Use each supported supplier-claim dimension at most once.")
                scope[key] = value
            records = catalog.find(kind=args.kind, name=args.name, external_id=args.external_id,
                                   text=args.text, source_id=args.source_id, claimed_scope=scope)
            result = {"status": "candidate" if records else "unknown", "total": len(records),
                      "offset": args.offset, "limit": args.limit,
                      "records": records[args.offset:args.offset + args.limit], "decision_eligible": False}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except CatalogError as error:
        print(json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
