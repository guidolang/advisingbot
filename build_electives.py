#!/usr/bin/env python3
"""
Rebuild the elective course list from the Quinnipiac catalog.

The catalog changes about once a year, so this is deliberately a separate step
from the daily seat check: run it when a new catalog goes live, commit the
result, and `advisingbot.py` picks it up from then on.

    python3 build_electives.py            # rewrite electives.toml
    python3 build_electives.py --dry-run  # show what would change first
    python3 build_electives.py --diff     # compare against the current file

Courses the catalog states as a rule rather than a list ("Any BAN course",
"any 200 level or above CIS class") cannot be resolved mechanically. They are
written into electives.toml as comments and printed at the end of the run, so
you can cover them with a subject-wide watch or add the courses by hand under
[manual] in watchlist.toml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import catalog
from catalog import CatalogError, Elective, ProgramElectives

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "electives.toml"


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
    )
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def collect(
    programs: list[ProgramElectives],
) -> tuple[dict[tuple[str, str], dict], list[tuple[str, str]]]:
    """Fold the per-program results into one course list and one rule list."""
    courses: dict[tuple[str, str], dict] = {}
    rules: list[tuple[str, str]] = []

    for program in programs:
        for elective, cross_listed in (
            [(e, False) for e in program.electives]
            + [(e, True) for e in program.cross_listings]
        ):
            key = (elective.subject, elective.number)
            entry = courses.setdefault(
                key,
                {
                    "subject": elective.subject,
                    "number": elective.number,
                    "title": elective.title,
                    "programs": [],
                    "cross_listed": cross_listed,
                },
            )
            if program.name not in entry["programs"]:
                entry["programs"].append(program.name)
            # A course listed outright anywhere is not merely a cross-listing.
            if not cross_listed:
                entry["cross_listed"] = False
            if len(elective.title) > len(entry["title"]):
                entry["title"] = elective.title

        for rule in program.rules:
            rules.append((program.name, rule.text))

    return courses, rules


def render(
    programs: list[ProgramElectives],
    courses: dict[tuple[str, str], dict],
    rules: list[tuple[str, str]],
    *,
    today: str,
) -> str:
    lines = [
        "# Elective courses harvested from the Quinnipiac catalog.",
        "#",
        "# GENERATED FILE - do not edit by hand. Rerun:",
        "#     python3 build_electives.py",
        "#",
        "# To add a course the catalog misses, put it under [manual] in",
        "# watchlist.toml instead. Edits here are lost on the next rebuild.",
        "",
        f'generated = "{today}"',
        "",
        "# Programs read, in order:",
    ]
    for program in programs:
        lines.append(f"#   {program.name}")
        lines.append(f"#     {program.url}")
    lines.append("")

    for key in sorted(courses):
        entry = courses[key]
        lines.append("[[course]]")
        lines.append(f'subject = {_toml_string(entry["subject"])}')
        lines.append(f'number = {_toml_string(entry["number"])}')
        lines.append(f'title = {_toml_string(entry["title"])}')
        lines.append(f'programs = {_toml_array(entry["programs"])}')
        if entry["cross_listed"]:
            lines.append("cross_listed = true")
        lines.append("")

    lines.append("# ---------------------------------------------------------")
    lines.append("# Elective requirements the catalog states as a rule rather")
    lines.append("# than a course list. These are NOT covered by the courses")
    lines.append("# above - cover them with a subject watch in watchlist.toml,")
    lines.append("# or add the specific courses under [manual].")
    lines.append("# ---------------------------------------------------------")
    if rules:
        for program_name, text in rules:
            lines.append(f"#   [{program_name}]")
            lines.append(f"#     {text}")
    else:
        lines.append("#   (none)")
    lines.append("")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_electives",
        description="Rebuild electives.toml from the Quinnipiac catalog.",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Where to write (default: electives.toml).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the file instead of writing it.",
    )
    parser.add_argument(
        "--diff", action="store_true",
        help="Summarise how the catalog changed since the current file.",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds between catalog page fetches (default: 0.5).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress progress output."
    )
    return parser


def _existing_codes(path: Path) -> set[str] | None:
    """The course codes already in electives.toml, for --diff."""
    if not path.exists():
        return None
    import tomllib

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return None
    return {
        f"{c.get('subject')} {c.get('number')}"
        for c in data.get("course") or []
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet

    def progress(name: str, url: str) -> None:
        if verbose:
            print(f"  reading {name} ...", file=sys.stderr)

    if verbose:
        print("Reading the catalog ...", file=sys.stderr)

    try:
        programs = catalog.scrape(delay=args.delay, progress=progress)
    except CatalogError as exc:
        print(f"\nCatalog error: {exc}", file=sys.stderr)
        return 2

    courses, rules = collect(programs)
    if not courses:
        print(
            "\nNo elective courses found at all. The catalog's page layout has "
            "probably changed - check catalog.py against a curriculum page.",
            file=sys.stderr,
        )
        return 2

    before = _existing_codes(args.output) if args.diff else None

    text = render(
        programs, courses, rules,
        today=dt.date.today().isoformat(),
    )

    if args.dry_run:
        print(text)
    else:
        args.output.write_text(text)

    # -- summary ----------------------------------------------------------
    by_subject: dict[str, list[str]] = {}
    for subject, number in sorted(courses):
        by_subject.setdefault(subject, []).append(number)

    print(f"\n{len(courses)} elective course(s) across {len(programs)} program(s):")
    for subject, numbers in sorted(by_subject.items()):
        print(f"  {subject:<5} {', '.join(numbers)}")

    if args.diff and before is not None:
        now = {f"{s} {n}" for s, n in courses}
        added, removed = sorted(now - before), sorted(before - now)
        print("\nSince the current electives.toml:")
        if not (added or removed):
            print("  no change.")
        for code in added:
            print(f"  ADDED    {code}")
        for code in removed:
            print(f"  REMOVED  {code}")

    if rules:
        print(
            f"\n{len(rules)} elective requirement(s) are stated as rules and "
            "could not be\nturned into a course list. Cover these yourself:"
        )
        for program_name, rule_text in rules:
            print(f"  [{program_name}]")
            print(f"    {rule_text}")

    if not args.dry_run:
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
