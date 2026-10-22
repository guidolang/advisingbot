#!/usr/bin/env python3
"""
AdvisingBot - local web app.

Paste a Stellic audit link; get back the courses that student still needs,
grouped the way Stellic groups them, with the ones that have open seats in
Self-Service marked online / on-ground / both.

    python3 app.py            then open http://127.0.0.1:8765

Runs on the loopback interface only, and deliberately so: it reads a student's
degree audit through your authenticated Stellic session. That is a protected
education record. Do not expose this port, and do not deploy this anywhere.
Audits are held in memory for the length of one request and never written to
disk; only public course and seat data is cached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import eligibility
import opentabs as ot
import seatcache
import stellic
from selfservice import Section

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# One browser session and one cache, shared across requests. A lock keeps two
# browser tabs from being driven at once.
_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress_message = "Waiting to start"
_session: ot.OpenTabs | None = None
_cache: seatcache.SeatCache | None = None
_update_available = os.environ.get("ADVISINGBOT_UPDATE_AVAILABLE") == "1"
_update_prompt = os.environ.get("ADVISINGBOT_UPDATE_PROMPT") or ""


def session() -> ot.OpenTabs:
    global _session
    if _session is None:
        _session = ot.OpenTabs()
        _session.connect()
    return _session


def cache() -> seatcache.SeatCache:
    global _cache
    if _cache is None:
        _cache = seatcache.SeatCache()
    return _cache


def _set_progress(message: str) -> None:
    """Expose a coarse, non-student-specific stage to the local page."""
    global _progress_message
    with _progress_lock:
        _progress_message = message


def _progress() -> str:
    with _progress_lock:
        return _progress_message


def friendly_error(exc: Exception) -> str:
    """Keep connection failures useful without exposing implementation detail."""
    if isinstance(exc, ot.OpenTabsError):
        text = str(exc).lower()
        if "token" in text or "authorization" in text:
            return (
                "AdvisingBot needs its secure Stellic connection. Ask Codex to "
                "set up OpenTabs for AdvisingBot, then try again."
            )
        if "reach opentabs" in text or "connection" in text:
            return (
                "AdvisingBot could not reach its secure Stellic connection. "
                "Ask Codex to start or repair OpenTabs, then try again."
            )
        return (
            "AdvisingBot could not use its secure Stellic connection. Ask Codex "
            "to check OpenTabs, then try again."
        )
    if isinstance(exc, stellic.AuditError):
        text = str(exc).lower()
        if "sign" in text or "login" in text:
            return "Sign in to Stellic in Chrome, then try again."
        return "Stellic could not read this audit. Refresh it in Stellic, then try again."
    return "AdvisingBot could not complete that check. Try again, or ask Codex for help."


def delivery_of(sections: list[Section]) -> str:
    """How a course can be taken, across the sections that have seats."""
    online = any(s.is_online for s in sections)
    ground = any(s.is_on_ground for s in sections)
    if online and ground:
        return "both"
    if online:
        return "online"
    if ground:
        return "on-ground"
    return "none"


def blocker(code: str, entry: dict, audit: stellic.Audit) -> dict | None:
    """Why this student should not be shown this course, if there is a reason.

    Three reasons, in the order an adviser would reach them: they have it
    already, they cannot take it yet, or it was written for somebody else's
    programme. Anything the catalogue states unclearly is not a reason - see
    eligibility.py.
    """
    if code in audit.completed:
        return {"reason": "taken", "detail": "Already taken or under way"}

    missing = eligibility.missing_prerequisites(
        entry.get("requisites") or [], audit.prerequisite_completed
    )
    if missing:
        return {
            "reason": "prerequisite",
            "detail": "Needs " + ", ".join(missing),
        }

    # A business course is written for business students, so its description
    # has nothing to say about whether this one belongs in it.
    if code.split("-")[0] not in eligibility.BUSINESS_SUBJECTS:
        restricted = eligibility.restriction(
            entry.get("description") or "", audit.programs
        )
        if restricted:
            return {"reason": "programme", "detail": restricted}

    return None


def add_course_type_candidates(audit: stellic.Audit, store: seatcache.SeatCache) -> None:
    """Add Self-Service Course Type matches to attribute-based requirements.

    Stellic says a requirement carries ``UC_FA`` or ``UC_HU``; Self-Service is
    the source that maps that attribute to its concrete course catalogue.
    """
    type_courses: dict[str, list[tuple[str, str]]] = {}
    for requirement in audit.requirements:
        if requirement.satisfied or stellic._is_university_total(requirement.name):
            continue
        for course_type in stellic.course_types_named_in_rules(requirement.rules):
            if course_type not in type_courses:
                type_courses[course_type] = store.courses_of_type(course_type)
            known = {course.code for course in requirement.needed}
            for code, title in type_courses[course_type]:
                display_code = code.replace("-", " ", 1)
                if code not in audit.completed and display_code not in known:
                    known.add(display_code)
                    requirement.needed.append(
                        stellic.NeededCourse(
                            code=display_code, title=title, candidate=True
                        )
                    )
        requirement.needed.sort(key=lambda course: (course.candidate, course.code))


def check(url: str, progress: Callable[[str], None] | None = None) -> dict:
    """The whole job: audit -> needed courses -> seats -> who may take them."""
    update = progress or (lambda _message: None)
    update("Reading and processing data from Stellic and Self-Service. This may take a minute...")
    # Stellic expansion is limited inside stellic.fetch() to unmet, bounded
    # leaf requirements with a concrete course-set rule. That supplies named
    # elective choices such as BAN 600-level ranges without expanding a
    # parent concentration or an indeterminate free-credit requirement.
    audit = stellic.fetch(url, session=session(), expand=True)

    update("Finding courses that can satisfy each requirement")
    store = cache()
    add_course_type_candidates(audit, store)
    update("Checking availability in Self-Service")
    before_live, before_hits = store.live_lookups, store.cache_hits
    known_terms = {t["code"]: t for t in store.terms()}
    seats = store.lookup(audit.all_codes)

    # One verdict per course, not per requirement: a course that shows up
    # under three requirements is the same course to register for.
    update("Checking prerequisites and programme restrictions")
    blocked = {
        code: blocker(code, entry, audit) for code, entry in seats.items()
    }

    requirements = []
    for requirement in audit.requirements:
        courses = []
        for course in requirement.needed:
            entry = seats.get(course.lookup_code) or {}
            sections = [
                s for s in seatcache.sections_of(entry) if s.has_open_seats
            ]
            courses.append(
                {
                    "code": course.code,
                    "title": course.title or entry.get("title") or "",
                    "units": course.units,
                    "candidate": course.candidate,
                    "blocked": blocked.get(course.lookup_code),
                    "checked_on": entry.get("checked_on"),
                    "sections": [
                        {
                            "name": s.name,
                            "term": s.term_code,
                            "seats": s.available,
                            "capacity": s.capacity,
                            "display": s.seats_display,
                            "delivery": s.delivery,
                            "location": s.location,
                            "meets": s.meeting_times,
                            "instructor": s.faculty,
                            "credits": s.credits,
                        }
                        for s in sections
                    ],
                }
            )
        requirements.append(
            {
                "name": requirement.name,
                "depth": requirement.depth,
                "satisfied": requirement.satisfied,
                "slots": requirement.slots,
                "rules": requirement.rules,
                "open_units": requirement.open_units,
                "units_total": requirement.units_total,
                "units_remaining": requirement.units_remaining,
                "review": requirement.review,
                "courses": courses,
            }
        )

    # Offer only terms this student actually has open sections in, so the
    # dropdown never contains a dead option.
    present = {
        s.term_code
        for entry in seats.values()
        for s in seatcache.sections_of(entry)
        if s.has_open_seats
    }
    terms = [
        known_terms.get(code, {"code": code, "description": code,
                               "start": "", "end": ""})
        for code in present
    ]
    terms.sort(key=lambda t: (t.get("start") or "9999", t["code"]))

    update("Preparing the course tree")
    return {
        "student": {
            "name": audit.student_name,
            "level": audit.level,
            "programs": audit.programs,
            "online_only": audit.online_only,
        },
        "terms": terms,
        "requirements": requirements,
        "cache": {
            "live": store.live_lookups - before_live,
            "hits": store.cache_hits - before_hits,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AdvisingBot"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (self.path or ""):
            sys.stderr.write(f"  {self.command} {self.path.split('?')[0]}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be embedded or cached by anything.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(
            status, json.dumps(payload).encode("utf-8"), "application/json"
        )

    def do_GET(self) -> None:
        route = urlparse(self.path)
        path = route.path

        if path in ("/", "/index.html"):
            page = STATIC / "index.html"
            if not page.exists():
                self._send(500, b"static/index.html is missing", "text/plain")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/check":
            url = (parse_qs(route.query).get("url") or [""])[0]
            if not url.strip():
                self._json(400, {"error": "Paste a Stellic audit link first."})
                return
            # One browser at a time.
            with _lock:
                try:
                    self._json(200, check(url, progress=_set_progress))
                except (stellic.AuditError, ot.OpenTabsError) as exc:
                    self._json(502, {"error": friendly_error(exc)})
                except Exception as exc:  # noqa: BLE001 - keep failures private
                    traceback.print_exc()
                    self._json(500, {"error": friendly_error(exc)})
            return

        if path == "/api/progress":
            self._json(200, {"message": _progress()})
            return

        if path == "/api/update":
            self._json(200, {
                "available": _update_available,
                "prompt": _update_prompt if _update_available else "",
            })
            return

        self._send(404, b"Not found", "text/plain")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="advisingbot",
        description="Local advising app: Stellic audit -> open seats.",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    # 127.0.0.1 only. This handles protected student records.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AdvisingBot running at http://127.0.0.1:{args.port}")
    print("Loopback only - do not expose this port. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
