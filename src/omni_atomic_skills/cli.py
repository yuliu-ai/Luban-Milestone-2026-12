"""Command-line inspection for atomic-skill catalogs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .contracts import SkillKind
from .registry import CatalogValidationError, SkillRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omni-atomic-skills",
        description="Validate and inspect an Luban atomic-skill catalog.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="strictly validate catalog structure, schemas, references, and cycles",
    )
    validate_parser.add_argument(
        "catalog", type=Path, nargs="+", help="one or more catalog JSON files to merge"
    )
    validate_parser.add_argument("--json", action="store_true", dest="as_json")

    list_parser = subparsers.add_parser("list", help="list catalog contracts")
    list_parser.add_argument("catalog", type=Path, nargs="+")
    list_parser.add_argument("--kind", choices=[kind.value for kind in SkillKind])
    list_parser.add_argument("--category")
    list_parser.add_argument("--status")
    list_parser.add_argument("--capability")
    list_parser.add_argument(
        "--latest",
        action="store_true",
        help="show only the highest version of each skill ID",
    )
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    graph_parser = subparsers.add_parser(
        "graph", help="render the resolved dependency graph"
    )
    graph_parser.add_argument("catalog", type=Path, nargs="+")
    graph_parser.add_argument(
        "--format", choices=("text", "dot", "json"), default="text"
    )
    graph_parser.add_argument("--include-optional", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        registry = SkillRegistry.load_json_files(arguments.catalog)
        if arguments.command == "validate":
            return _validate_command(registry, arguments.as_json)
        if arguments.command == "list":
            return _list_command(registry, arguments)
        if arguments.command == "graph":
            return _graph_command(
                registry, arguments.format, arguments.include_optional
            )
        parser.error(f"unknown command {arguments.command!r}")
    except CatalogValidationError as exc:
        print(
            "INVALID: " + ", ".join(str(path) for path in arguments.catalog),
            file=sys.stderr,
        )
        for issue in exc.issues:
            print(f"- {issue}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    return 0


def _validate_command(registry: SkillRegistry, as_json: bool) -> int:
    summary = registry.summary()
    if as_json:
        print(json.dumps({"valid": True, **summary}, ensure_ascii=False, indent=2))
        return 0
    print(
        "VALID "
        f"catalog={summary['catalog_version']} "
        f"contracts={summary['contract_versions']} "
        f"skill_ids={summary['unique_skill_ids']} "
        f"atomic_ids={summary['atomic_skill_ids']} "
        f"edges={summary['dependency_edges']} cycles=0"
    )
    print("kinds: " + _format_counts(summary["kinds"]))
    print("statuses: " + _format_counts(summary["statuses"]))
    return 0


def _list_command(registry: SkillRegistry, arguments: argparse.Namespace) -> int:
    contracts = list(
        registry.find(
            kind=arguments.kind,
            category=arguments.category,
            status=arguments.status,
            capability=arguments.capability,
        )
    )
    if arguments.latest:
        contracts = [
            contract
            for contract in contracts
            if contract.version == registry.versions(contract.skill_id)[0]
        ]
    if arguments.as_json:
        print(
            json.dumps(
                [contract.to_dict() for contract in contracts],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    columns = ("ID@VERSION", "KIND", "CATEGORY", "STATUS", "NAME")
    rows = [
        (
            str(contract.ref),
            contract.kind.value,
            contract.category,
            contract.status,
            f"{contract.name} / {contract.name_zh}",
        )
        for contract in contracts
    ]
    print(_render_table(columns, rows))
    return 0


def _graph_command(
    registry: SkillRegistry, output_format: str, include_optional: bool
) -> int:
    if output_format == "dot":
        print(registry.graph.to_dot(include_optional=include_optional))
    elif output_format == "json":
        print(
            json.dumps(
                registry.graph.to_dict(include_optional=include_optional),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(registry.graph.to_text(include_optional=include_optional))
    return 0


def _render_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    if not rows:
        return "(no matching skills)"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    header_line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    separator = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([header_line, separator, *body])


def _format_counts(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
