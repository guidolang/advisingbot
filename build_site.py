#!/usr/bin/env python3
"""
Build the data file the web page reads.

Combines programs.json (curricula, from the catalog) with live section data
from Self-Service, and writes docs/data.json: every course in every School of
Business program that currently has an open seat, grouped exactly the way its
program groups it.

    python3 build_site.py
    python3 build_site.py --programs "Applied AI"   # a subset, for testing

Self-Service is queried as lightly as the data allows:

  * One search covers every subject at once and every term at once - the
    endpoint accepts a list of subjects, and omitting the term filter makes
    sections come back bucketed by term. Terms are separated afterwards
    rather than by re-running the whole scrape per term.
  * Courses are deduplicated across programs first. SB 488 appears in a dozen
    curricula and is fetched once.
  * Courses the search says have no sections are skipped entirely; roughly
    half of any catalog is not offered in a given year.

That leaves one request per offered course, paced by SelfServiceClient. A
failure on one course is recorded and the run continues.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from selfservice import Course, SelfServiceClient, SelfServiceError

HERE = Path(__file__).resolve().parent
DEFAULT_PROGRAMS = HERE / "programs.json"
DEFAULT_OUTPUT = HERE / "docs" / "data.json"

EASTERN = dt.timezone(dt.timedelta(hours=-4), "ET")


def load_programs(path: Path, patterns: list[str] | None) -> dict:
    if not path.exists():
        raise SystemExit(
            f"No curricula at {path}. Run `python3 build_catalog.py` first."
        )
    data = json.loads(path.read_text())
    if patterns:
        needles = [p.lower() for p in patterns]
        data["programs"] = [
            p for p in data["programs"]
            if any(n in p["name"].lower() for n in needles)
        ]
        if not data["programs"]:
            raise SystemExit(f"No program name matches {patterns}.")
    return data


def wanted_courses(programs: dict) -> dict[str, dict]:
    """Every distinct course any program names, keyed "SUBJ-NUM"."""
    out: dict[str, dict] = {}
    for program in programs["programs"]:
        for group in program["groups"]:
            for course in group["courses"]:
                code = f"{course['subject']}-{course['number']}"
                entry = out.setdefault(
                    code,
                    {
                        "subject": course["subject"],
                        "number": course["number"],
                        "title": course.get("title") or "",
                    },
                )
                # Catalog titles vary in how much they truncate; keep the
                # fullest one we see.
                if len(course.get("title") or "") > len(entry["title"]):
                    entry["title"] = course["title"]
    return out


def section_payload(section) -> dict:
    return {
        "id": section.section_id,
        "name": section.name,
        "term": section.term_code,
        "seats": section.available,
        "capacity": section.capacity,
        "waitlisted": section.waitlisted,
        "seats_display": section.seats_display,
        "seats_unknown": section.seats_unpublished,
        "unlimited": section.unlimited_seats,
        "status": section.status,
        "delivery": section.delivery,
        "location": section.location,
        "meets": section.meeting_times,
        "instructor": section.faculty,
        "credits": section.credits,
    }


def collect(
    client: SelfServiceClient,
    needed: dict[str, dict],
    *,
    verbose: bool,
) -> tuple[dict[str, dict], list[dict], list[str]]:
    """Fetch sections for every needed course that is actually offered."""
    subjects = sorted({c["subject"] for c in needed.values()})

    if verbose:
        print(
            f"  one search across {len(subjects)} subject(s), all terms ...",
            file=sys.stderr,
        )
    offered, payload = client.search(
        subjects=subjects, terms=[], open_only=False
    )

    terms = [
        {
            "code": t["Value"],
            "description": t["Description"],
        }
        for t in payload.get("TermFilters") or []
        if t.get("Value")
    ]
    dated = {
        t.get("Code"): t for t in payload.get("ActivePlanTerms") or []
    }
    for term in terms:
        extra = dated.get(term["code"], {})
        term["start"] = (extra.get("StartDate") or "")[:10]
        term["end"] = (extra.get("EndDate") or "")[:10]
    terms.sort(key=lambda t: (t["start"] or "9999", t["code"]))

    by_code: dict[str, Course] = {c.code: c for c in offered}

    # Only courses that are both wanted and actually have sections.
    targets = [
        (code, by_code[code])
        for code in sorted(needed)
        if code in by_code and by_code[code].matching_section_ids
    ]
    if verbose:
        print(
            f"  {len(needed)} course(s) in the curricula; {len(targets)} are "
            f"offered and need a section lookup",
            file=sys.stderr,
        )

    courses: dict[str, dict] = {}
    failures: list[str] = []

    for index, (code, course) in enumerate(targets, start=1):
        if verbose and index % 25 == 0:
            print(f"    {index}/{len(targets)} ...", file=sys.stderr)
        try:
            sections = client.fetch_sections(course)
        except SelfServiceError as exc:
            # One bad course must not cost us the whole run.
            failures.append(f"{code}: {exc}")
            continue

        interesting = [
            s for s in sections if s.has_open_seats or s.seats_unpublished
        ]
        if not interesting:
            continue

        courses[code] = {
            "subject": course.subject,
            "number": course.number,
            # Self-Service titles are the authoritative ones; fall back to the
            # catalog's when a course is somehow absent from the search.
            "title": course.title or needed[code]["title"],
            "sections": [section_payload(s) for s in interesting],
        }

    return courses, terms, failures


def build(programs: dict, courses: dict[str, dict], terms: list[dict]) -> dict:
    """Assemble the page's data file, keeping the catalog's own grouping."""
    out_programs = []
    for program in programs["programs"]:
        groups = []
        for group in program["groups"]:
            codes = [
                f"{c['subject']}-{c['number']}" for c in group["courses"]
            ]
            open_codes = [c for c in codes if c in courses]
            # Keep a group if it has open courses OR a rule worth showing -
            # a rule with nothing under it is exactly what an adviser needs
            # to see, because it means "go look this up yourself".
            if not open_codes and not group["rules"]:
                continue
            groups.append(
                {
                    "name": group["name"],
                    "credits": group.get("credits") or "",
                    "is_elective": group.get("is_elective", False),
                    "notes": group.get("notes") or [],
                    "rules": group.get("rules") or [],
                    "courses": open_codes,
                    "listed": len(codes),
                }
            )
        if not groups:
            continue
        out_programs.append(
            {
                "name": program["name"],
                "level": program["level"],
                "category": program["category"],
                "url": program["url"],
                "groups": groups,
            }
        )

    now = dt.datetime.now(dt.timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.astimezone(EASTERN).strftime(
            "%B %-d, %Y at %-I:%M %p Eastern"
        ),
        "catalog_generated": programs.get("generated"),
        "terms": terms,
        "courses": courses,
        "programs": out_programs,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_site",
        description="Build docs/data.json for the web page.",
    )
    parser.add_argument("--programs-file", type=Path, default=DEFAULT_PROGRAMS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--programs", nargs="*", metavar="TEXT",
        help="Only programs whose name contains one of these (for testing).",
    )
    parser.add_argument(
        "--delay", type=float, default=0.4,
        help="Seconds between Self-Service requests (default: 0.4).",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet

    programs = load_programs(args.programs_file, args.programs)
    needed = wanted_courses(programs)

    if verbose:
        print(
            f"{len(programs['programs'])} program(s), "
            f"{len(needed)} distinct course(s) to check.",
            file=sys.stderr,
        )

    client = SelfServiceClient(delay=args.delay)
    try:
        courses, terms, failures = collect(client, needed, verbose=verbose)
    except SelfServiceError as exc:
        print(f"\nSelf-Service error: {exc}", file=sys.stderr)
        return 2

    payload = build(programs, courses, terms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")

    sections = sum(len(c["sections"]) for c in courses.values())
    size = args.output.stat().st_size // 1024
    print(
        f"\n{len(courses)} course(s) with open seats, {sections} section(s), "
        f"across {len(payload['programs'])} program(s)."
    )
    print("Terms: " + ", ".join(f"{t['description']}" for t in terms))
    if failures:
        print(f"\n{len(failures)} course(s) could not be read:")
        for line in failures[:10]:
            print(f"  {line}")
    print(f"\nWrote {args.output} ({size} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
