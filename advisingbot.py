#!/usr/bin/env python3
"""
AdvisingBot - find Quinnipiac courses with open seats.

Walks the same path an advisor clicks through in Self-Service - pick a subject,
tick "Open Sections Only" and a term, then open "View Available Sections" for
each course - and reports every section that still has a seat.

    python3 advisingbot.py                     # current term, watchlist.toml
    python3 advisingbot.py --term "Spring 2027"
    python3 advisingbot.py --subject CIS --levels UG
    python3 advisingbot.py --csv seats.csv --json seats.json
    python3 advisingbot.py --diff              # what changed since last run

The catalog is public, so no login or credentials are involved anywhere.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import tomllib

ELECTIVE_PROGRAMS: list[str] = []
from dataclasses import asdict
from pathlib import Path

from selfservice import (
    Course,
    SelfServiceClient,
    SelfServiceError,
    Term,
)

HERE = Path(__file__).resolve().parent
DEFAULT_WATCHLIST = HERE / "watchlist.toml"
DEFAULT_ELECTIVES = HERE / "programs.json"
SNAPSHOT_PATH = HERE / ".last_run.json"

LEVEL_NAMES = {
    "UG": "Undergraduate",
    "GR": "Graduate",
    "DL": "Doctorate",
    "LW": "Law",
}


# -- watchlist -------------------------------------------------------------


class Watch:
    def __init__(self, raw: dict, index: int) -> None:
        subject = (raw.get("subject") or "").strip().upper()
        if not subject:
            raise ValueError(f"watch #{index} is missing a `subject`")
        self.subject = subject
        self.levels = [str(x).strip().upper() for x in raw.get("levels") or []]
        self.courses = {str(x).strip().upper() for x in raw.get("courses") or []}
        self.exclude = {str(x).strip().upper() for x in raw.get("exclude") or []}
        self.label = raw.get("label") or subject

    def wants(self, course: Course) -> bool:
        """Apply the optional course-number include/exclude lists."""
        number = course.number.upper()
        full = course.code.upper()
        if self.exclude and (number in self.exclude or full in self.exclude):
            return False
        if self.courses:
            return number in self.courses or full in self.courses
        return True


def load_watchlist(path: Path) -> tuple[list[Watch], list[tuple[str, str]]]:
    """Returns the [[watch]] blocks and any [manual] course additions."""
    if not path.exists():
        raise SystemExit(
            f"No watchlist at {path}.\n"
            "Create one (see watchlist.toml in the repo) or pass "
            "--subject to run a one-off search."
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    entries = data.get("watch") or []
    watches = [Watch(raw, i) for i, raw in enumerate(entries, start=1)]

    manual_raw = (data.get("manual") or {}).get("courses") or []
    manual = [_split_code(str(c), source=str(path)) for c in manual_raw]

    global ELECTIVE_PROGRAMS
    ELECTIVE_PROGRAMS = [
        str(x) for x in (data.get("electives") or {}).get("programs") or []
    ]

    if not watches and not manual:
        raise SystemExit(f"{path} has no [[watch]] blocks and no [manual] courses.")
    return watches, manual


def _split_code(value: str, *, source: str) -> tuple[str, str]:
    """Accept "MK 321", "MK-321" or "MK321" and split into subject + number."""
    match = re.fullmatch(
        r"\s*([A-Za-z]{2,4})[\s\-_]*([0-9]{3}[A-Za-z]?)\s*", value
    )
    if not match:
        raise SystemExit(
            f'{source}: "{value}" is not a course code. '
            'Write them like "MK 321".'
        )
    return match.group(1).upper(), match.group(2).upper()


def load_electives(path: Path) -> tuple[list[tuple[str, str]], str | None]:
    """Elective courses from the catalog, narrowed to the programs we advise.

    programs.json holds every School of Business curriculum. The CLI only
    wants electives from the programs named under [electives] in
    watchlist.toml - without that filter, "electives" would mean every
    elective in the school.
    """
    if not path.exists():
        return [], None
    data = json.loads(path.read_text())

    needles = [p.lower() for p in ELECTIVE_PROGRAMS]
    courses: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for program in data.get("programs") or []:
        if needles and not any(n in program["name"].lower() for n in needles):
            continue
        for group in program.get("groups") or []:
            if not group.get("is_elective"):
                continue
            for course in group.get("courses") or []:
                key = (
                    str(course["subject"]).upper(),
                    str(course["number"]).upper(),
                )
                if key not in seen:
                    seen.add(key)
                    courses.append(key)

    return courses, data.get("generated")


def elective_watches(
    catalog_courses: list[tuple[str, str]],
    manual_courses: list[tuple[str, str]],
) -> list[Watch]:
    """One watch per subject, so each subject is searched only once."""
    from_catalog: dict[str, set[str]] = {}
    for subject, number in catalog_courses:
        from_catalog.setdefault(subject, set()).add(number)

    from_manual: dict[str, set[str]] = {}
    for subject, number in manual_courses:
        from_manual.setdefault(subject, set()).add(number)

    watches: list[Watch] = []
    for subject in sorted(set(from_catalog) | set(from_manual)):
        catalog_numbers = from_catalog.get(subject, set())
        manual_numbers = from_manual.get(subject, set())
        if manual_numbers - catalog_numbers and catalog_numbers:
            label = f"Electives - {subject} (catalog + manual)"
        elif manual_numbers - catalog_numbers:
            label = f"Electives - {subject} (added by hand)"
        else:
            label = f"Electives - {subject} (from catalog)"
        watches.append(
            Watch(
                {
                    "subject": subject,
                    "courses": sorted(catalog_numbers | manual_numbers),
                    "label": label,
                },
                0,
            )
        )
    return watches


# -- term selection --------------------------------------------------------


def _warn_if_stale(generated: str | None, verbose: bool) -> None:
    """The catalog turns over yearly; a year-old harvest is probably behind."""
    if not generated or not verbose:
        return
    built = _parse_date(generated)
    if built and (dt.date.today() - built).days > 365:
        print(
            f"Note: programs.json was built {built.isoformat()}, over a year "
            "ago.\n      Rerun `python3 build_electives.py` to pick up the "
            "new catalog.",
            file=sys.stderr,
        )


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None


def pick_term(terms: list[Term], requested: str | None) -> list[Term]:
    """Resolve --term into actual terms, defaulting to the current semester."""
    if not terms:
        raise SystemExit(
            "Self-Service is not listing any terms right now. Try again later."
        )

    if requested and requested.lower() == "all":
        return terms

    today = dt.date.today()

    def current() -> Term | None:
        # More than one term can be in session at once - Fall 2026 and Law
        # Fall 2026 overlap - so prefer the one with the most courses, which
        # is the main term rather than a parallel school calendar.
        in_session = [
            t for t in terms
            if (_parse_date(t.start_date) or dt.date.max) <= today
            <= (_parse_date(t.end_date) or dt.date.min)
        ]
        if not in_session:
            return None
        return max(in_session, key=lambda t: t.course_count)

    def upcoming() -> Term | None:
        future = [
            t for t in terms
            if (_parse_date(t.start_date) or dt.date.min) > today
        ]
        if not future:
            return None
        earliest = min(_parse_date(t.start_date) for t in future)
        starting_then = [
            t for t in future if _parse_date(t.start_date) == earliest
        ]
        return max(starting_then, key=lambda t: t.course_count)

    if not requested or requested.lower() == "current":
        chosen = current() or upcoming() or terms[0]
        return [chosen]

    if requested.lower() in ("next", "upcoming"):
        chosen = upcoming()
        if not chosen:
            raise SystemExit(
                "No upcoming term is published yet. Available: "
                + ", ".join(str(t) for t in terms)
            )
        return [chosen]

    needle = requested.strip().lower()
    matches = [
        t for t in terms
        if needle == t.code.lower() or needle in t.description.lower()
    ]
    if not matches:
        raise SystemExit(
            f"No term matches {requested!r}. Available: "
            + ", ".join(str(t) for t in terms)
        )
    return matches


# -- the run ---------------------------------------------------------------


def collect(
    client: SelfServiceClient,
    watches: list[Watch],
    term_codes: list[str],
    *,
    include_closed: bool,
    verbose: bool,
) -> list[dict]:
    """For each watch: search, then open the sections for each hit."""
    results: list[dict] = []
    unpublished: list[str] = []
    # A course can be reached by more than one watch - BAN 629 is both a BAN
    # course and an MS elective. Report it once, under the first watch that
    # claims it, and don't fetch its sections twice.
    seen_courses: set[str] = set()
    empty_watches: list[str] = []

    for watch in watches:
        found_any = False
        fully_covered = False
        if verbose:
            levels = "/".join(watch.levels) if watch.levels else "all levels"
            print(f"  searching {watch.subject} ({levels}) ...", file=sys.stderr)

        # Deliberately NOT using Self-Service's own "Open Sections Only"
        # filter. It drops sections that genuinely have seats - as of this
        # writing CIS*101-01 has a free seat but is excluded by it - so we
        # pull every section in the term and judge the seat counts ourselves.
        courses, _ = client.search(
            subjects=[watch.subject],
            terms=term_codes,
            academic_levels=watch.levels,
            open_only=False,
        )
        courses = [c for c in courses if watch.wants(c)]
        matched = len(courses)
        courses = [c for c in courses if c.course_id not in seen_courses]
        # Every course this watch wanted was already reported under an earlier
        # one - the BAN electives all sit inside the BAN subject watch. That is
        # full coverage, not an empty result, so don't flag it as nothing open.
        fully_covered = matched > 0 and not courses
        seen_courses.update(c.course_id for c in courses)
        courses.sort(key=lambda c: (c.subject, c.number))

        for course in courses:
            course.sections = client.fetch_sections(course)
            in_term = [
                s for s in course.sections if s.term_code in term_codes
            ]
            unpublished.extend(
                f"{course.code} {s.name}"
                for s in in_term
                if s.seats_unpublished
            )
            sections = (
                in_term
                if include_closed
                else [s for s in in_term if s.has_open_seats]
            )
            if not sections:
                continue
            found_any = True
            results.append(
                {
                    "watch": watch.label,
                    "subject": course.subject,
                    "number": course.number,
                    "code": course.code,
                    "title": course.title,
                    "course_id": course.course_id,
                    "sections": [asdict(s) for s in sections],
                }
            )

        if not found_any and not fully_covered:
            empty_watches.append(watch.label)

    return results, unpublished, empty_watches


# -- output ----------------------------------------------------------------


def print_report(
    results: list[dict],
    terms: list[Term],
    unpublished: list[str],
    empty_watches: list[str],
    *,
    include_closed: bool = False,
) -> None:
    label = "All sections" if include_closed else "Open seats"
    heading = f"{label} - " + ", ".join(str(t) for t in terms)
    print()
    print(heading)
    print("=" * len(heading))

    if not results:
        print("\nNo sections with open seats matched the watchlist.")
        _print_empty(empty_watches)
        _print_unpublished(unpublished)
        return

    by_watch: dict[str, list[dict]] = {}
    for entry in results:
        by_watch.setdefault(entry["watch"], []).append(entry)

    total_sections = 0
    total_seats = 0

    for label, entries in by_watch.items():
        print(f"\n{label}")
        print("-" * len(label))
        for entry in entries:
            print(f"\n  {entry['code']}  {entry['title']}")
            for section in entry["sections"]:
                total_sections += 1
                total_seats += section["available"] or 0
                if section["unlimited_seats"]:
                    seats = "unlimited seats"
                elif not section["capacity"]:
                    seats = "no seat count published"
                elif not section["available"]:
                    seats = f"FULL (0 of {section['capacity']})"
                else:
                    seats = (
                        f"{section['available']} of "
                        f"{section['capacity']} open"
                    )
                waitlist = section["waitlisted"] or 0
                if waitlist:
                    seats += f", {waitlist} waitlisted"
                bits = [
                    section["meeting_times"],
                    section["location"],
                    section["faculty"],
                ]
                detail = " | ".join(b for b in bits if b)
                print(f"    {section['name']:<16} {seats}")
                print(f"    {'':<16} {detail}")

    print(
        f"\n{total_sections} section(s) with open seats, "
        f"{total_seats} seat(s) total, across {len(results)} course(s)."
    )
    _print_empty(empty_watches)
    _print_unpublished(unpublished)


def _print_empty(empty_watches: list[str]) -> None:
    """An absent heading reads as "not checked". Say it was checked."""
    if not empty_watches:
        return
    print(
        "\nChecked, nothing open (courses are full, or not offered this "
        "term):"
    )
    for label in empty_watches:
        print(f"  {label}")


def _print_unpublished(unpublished: list[str]) -> None:
    """Never silently drop a section we could not judge."""
    if not unpublished:
        return
    print(
        f"\nAlso worth a look - {len(unpublished)} section(s) publish no seat "
        "count at all\n(Self-Service shows 0 / 0 / 0). Not counted above; "
        "check these by hand:"
    )
    for name in sorted(set(unpublished)):
        print(f"  {name}")


def write_csv(results: list[dict], path: Path) -> None:
    columns = [
        "watch", "subject", "number", "code", "title", "section",
        "term", "available", "capacity", "waitlisted", "status",
        "credits", "meeting_times", "location", "faculty", "seats_display",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for entry in results:
            for section in entry["sections"]:
                writer.writerow(
                    {
                        "watch": entry["watch"],
                        "subject": entry["subject"],
                        "number": entry["number"],
                        "code": entry["code"],
                        "title": entry["title"],
                        "section": section["name"],
                        "term": section["term_description"]
                        or section["term_code"],
                        "available": section["available"],
                        "capacity": section["capacity"],
                        "waitlisted": section["waitlisted"],
                        "status": section["status"],
                        "credits": section["credits"],
                        "meeting_times": section["meeting_times"],
                        "location": section["location"],
                        "faculty": section["faculty"],
                        "seats_display": section["seats_display"],
                    }
                )


def _seat_map(results: list[dict]) -> dict[str, tuple[str, int | None]]:
    out: dict[str, tuple[str, int | None]] = {}
    for entry in results:
        for section in entry["sections"]:
            label = f"{entry['code']} {section['name']}"
            out[section["section_id"]] = (label, section["available"])
    return out


def print_diff(results: list[dict], snapshot: Path) -> None:
    if not snapshot.exists():
        print("\n(no previous run to compare against - saving this one)")
        return

    try:
        previous = json.loads(snapshot.read_text())
    except (json.JSONDecodeError, OSError):
        print("\n(previous run could not be read - skipping diff)")
        return

    old = _seat_map(previous.get("results") or [])
    new = _seat_map(results)

    opened = [new[k] for k in new.keys() - old.keys()]
    closed = [old[k] for k in old.keys() - new.keys()]
    changed = [
        (new[k][0], old[k][1], new[k][1])
        for k in new.keys() & old.keys()
        if old[k][1] != new[k][1]
    ]

    when = previous.get("generated_at", "the previous run")
    print(f"\nChanges since {when}")
    print("-" * 40)
    if not (opened or closed or changed):
        print("No change.")
        return
    for label, seats in sorted(opened):
        print(f"  NEWLY OPEN   {label}  ({seats} seat(s))")
    for label, before, after in sorted(changed):
        arrow = "up" if (after or 0) > (before or 0) else "down"
        print(f"  {arrow.upper():<12} {label}  {before} -> {after}")
    for label, seats in sorted(closed):
        print(f"  NOW FULL     {label}")


# -- cli -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisingbot",
        description="Find Quinnipiac courses with open seats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--term",
        help='Term to check: "current" (default), "next", "all", a code like '
             '26/FA, or a name like "Fall 2026".',
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST,
        help="Path to the watchlist TOML (default: watchlist.toml).",
    )
    parser.add_argument(
        "--subject",
        action="append",
        metavar="CODE",
        help="One-off search for this subject, ignoring the watchlist. "
             "Repeatable.",
    )
    parser.add_argument(
        "--levels",
        nargs="*",
        metavar="LVL",
        default=None,
        help="Academic levels for --subject (UG GR DL LW). Default: all.",
    )
    parser.add_argument(
        "--electives",
        type=Path,
        default=DEFAULT_ELECTIVES,
        help="Catalog curricula to draw electives from "
             "(default: programs.json).",
    )
    parser.add_argument(
        "--no-electives",
        action="store_true",
        help="Check only the [[watch]] blocks, ignoring catalog electives.",
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Also list sections with no seats left.",
    )
    parser.add_argument("--csv", type=Path, help="Write results to a CSV file.")
    parser.add_argument("--json", type=Path, help="Write results to a JSON file.")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Compare against the previous run and show what changed.",
    )
    parser.add_argument(
        "--list-terms", action="store_true",
        help="Show the terms Self-Service currently offers, then exit.",
    )
    parser.add_argument(
        "--list-subjects", action="store_true",
        help="Show all subject codes with course counts, then exit.",
    )
    parser.add_argument(
        "--delay", type=float, default=0.4,
        help="Seconds between requests (default: 0.4).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress progress output."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet
    client = SelfServiceClient(delay=args.delay)

    try:
        if args.list_subjects:
            _, payload = client.search(
                open_only=False, page_size=1, complete=False
            )
            print("\nSubject codes currently offering courses:\n")
            for item in payload.get("Subjects") or []:
                print(f"  {item['Value']:<6} {item['Description']:<40} "
                      f"{item['Count']:>4} course(s)")
            return 0

        if verbose:
            print("Reading terms from Self-Service ...", file=sys.stderr)
        terms = client.discover_terms()

        if args.list_terms:
            print("\nTerms currently offered:\n")
            today = dt.date.today()
            current_code = pick_term(terms, "current")[0].code
            for term in terms:
                start, end = _parse_date(term.start_date), _parse_date(term.end_date)
                span = (
                    f"{start.isoformat()} .. {end.isoformat()}"
                    if start and end else "dates not published"
                )
                marker = "  <- current" if term.code == current_code else ""
                print(
                    f"  {term.code:<8} {term.description:<20} {span:<26}"
                    f"{term.course_count:>5} course(s){marker}"
                )
            return 0

        chosen = pick_term(terms, args.term)
        term_codes = [t.code for t in chosen]

        if args.subject:
            watches = [
                Watch(
                    {
                        "subject": code,
                        "levels": args.levels or [],
                        "label": f"{code.upper()} (one-off search)",
                    },
                    i,
                )
                for i, code in enumerate(args.subject, start=1)
            ]
        else:
            watches, manual = load_watchlist(args.watchlist)
            if args.no_electives:
                catalog_courses, generated = [], None
                manual = []
            else:
                catalog_courses, generated = load_electives(args.electives)
                if not catalog_courses and verbose:
                    print(
                        f"No electives found in {args.electives} - run "
                        "`python3 build_catalog.py` to read them from the "
                        "catalog, and check [electives] in watchlist.toml.",
                        file=sys.stderr,
                    )
                _warn_if_stale(generated, verbose)
            watches += elective_watches(catalog_courses, manual)

        if verbose:
            print(
                f"Checking {', '.join(str(t) for t in chosen)} "
                f"across {len(watches)} watch(es) ...",
                file=sys.stderr,
            )

        results, unpublished, empty_watches = collect(
            client,
            watches,
            term_codes,
            include_closed=args.include_closed,
            verbose=verbose,
        )

    except SelfServiceError as exc:
        print(f"\nSelf-Service error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    print_report(
        results,
        chosen,
        unpublished,
        empty_watches,
        include_closed=args.include_closed,
    )

    if args.diff:
        print_diff(results, SNAPSHOT_PATH)

    snapshot = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "terms": [asdict(t) for t in chosen],
        "results": results,
        "sections_without_published_seats": sorted(set(unpublished)),
        "watches_with_nothing_open": empty_watches,
    }
    try:
        SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2))
    except OSError:
        pass

    if args.json:
        args.json.write_text(json.dumps(snapshot, indent=2))
        print(f"\nWrote {args.json}")
    if args.csv:
        write_csv(results, args.csv)
        print(f"Wrote {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
