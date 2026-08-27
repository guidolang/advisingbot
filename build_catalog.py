#!/usr/bin/env python3
"""
Rebuild the program curricula from the Quinnipiac catalog.

The catalog turns over once a year, so this is deliberately separate from the
seat check: run it when a new edition goes live, commit programs.json, and
everything downstream reads from that file instead of hitting the catalog.

    python3 build_catalog.py             # rewrite programs.json
    python3 build_catalog.py --diff      # what changed since the current file
    python3 build_catalog.py --dry-run   # don't write anything

Requirements the catalog states as prose rather than a course list ("Any BAN
course", "Take one 600-level MK course") can't be resolved mechanically. They
are kept on their group as `rules`, shown at the end of every run, and
surfaced in the web UI so nobody mistakes them for courses that don't exist.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import catalog
from catalog import CatalogError, Program

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "programs.json"


def to_json(programs: list[Program], *, today: str) -> dict:
    return {
        "generated": today,
        "source": "https://catalog.qu.edu/",
        "programs": [
            {
                "name": program.name,
                "url": program.url,
                "level": program.level,
                "category": program.category,
                "groups": [
                    {
                        "name": group.name,
                        "credits": group.credits,
                        "is_elective": group.is_elective,
                        "notes": group.notes,
                        "rules": group.rules,
                        "courses": [
                            {
                                "subject": course.subject,
                                "number": course.number,
                                "title": course.title,
                                **({"alternative": True} if course.alternative else {}),
                                **({"cross_listed": True} if course.cross_listed else {}),
                            }
                            for course in group.courses
                        ],
                    }
                    for group in program.groups
                ],
            }
            for program in programs
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_catalog",
        description="Rebuild programs.json from the Quinnipiac catalog.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--dry-run", action="store_true", help="Don't write the file."
    )
    parser.add_argument(
        "--diff", action="store_true",
        help="Summarise how the catalog changed since the current file.",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds between catalog page fetches (default: 0.5).",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def _previous(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet

    def progress(label: str, url: str, index: int, total: int) -> None:
        if verbose:
            print(f"  [{index:>2}/{total}] {label}", file=sys.stderr)

    if verbose:
        print("Reading the catalog ...", file=sys.stderr)

    try:
        programs = catalog.scrape(delay=args.delay, progress=progress)
    except CatalogError as exc:
        print(f"\nCatalog error: {exc}", file=sys.stderr)
        return 2

    if not programs:
        print(
            "\nNo programs parsed at all. The catalog's page layout has "
            "probably changed - check catalog.py against a curriculum page.",
            file=sys.stderr,
        )
        return 2

    before = _previous(args.output) if args.diff else None
    payload = to_json(programs, today=dt.date.today().isoformat())

    if not args.dry_run:
        args.output.write_text(json.dumps(payload, indent=2) + "\n")

    # -- summary ----------------------------------------------------------
    courses = {(c.subject, c.number) for p in programs for c in p.courses}
    subjects = sorted({s for _, s in ((c[0], c[0]) for c in courses)})
    groups = sum(len(p.groups) for p in programs)
    rules = [(p.name, g, r) for p in programs for g, r in p.rules]

    print(
        f"\n{len(programs)} program(s), {groups} group(s), "
        f"{len(courses)} distinct course(s) across {len(subjects)} subject(s)."
    )
    by_category: dict[str, int] = {}
    for program in programs:
        by_category[f"{program.level} / {program.category}"] = (
            by_category.get(f"{program.level} / {program.category}", 0) + 1
        )
    for key, count in sorted(by_category.items()):
        print(f"  {count:>3}  {key}")

    if args.diff and before is not None:
        old = {p["name"] for p in before.get("programs") or []}
        new = {p.name for p in programs}
        print("\nSince the current programs.json:")
        if old == new:
            print("  same programs (course lists may still differ).")
        for name in sorted(new - old):
            print(f"  ADDED    {name}")
        for name in sorted(old - new):
            print(f"  REMOVED  {name}")

    if rules:
        print(
            f"\n{len(rules)} requirement(s) are stated as rules rather than "
            "course lists.\nThey are kept in programs.json and shown in the "
            "web UI:"
        )
        for program_name, group_name, text in rules[:12]:
            print(f"  [{program_name[:38]} / {group_name[:24]}]")
            print(f"    {text[:96]}")
        if len(rules) > 12:
            print(f"  ... and {len(rules) - 12} more")

    if not args.dry_run:
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
