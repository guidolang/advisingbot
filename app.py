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
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import opentabs as ot
import seatcache
import stellic
from selfservice import Section

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# One browser session and one cache, shared across requests. A lock keeps two
# browser tabs from being driven at once.
_lock = threading.Lock()
_session: ot.OpenTabs | None = None
_cache: seatcache.SeatCache | None = None


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


def check(url: str) -> dict:
    """The whole job: audit -> needed courses -> seats."""
    audit = stellic.fetch(url, session=session())

    store = cache()
    before_live, before_hits = store.live_lookups, store.cache_hits
    known_terms = {t["code"]: t for t in store.terms()}
    seats = store.lookup(audit.all_codes)

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
                    "checked_on": entry.get("checked_on"),
                    "checked_at": entry.get("checked_at"),
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

    # The oldest reading among the courses we looked up: seat counts are only
    # as fresh as the least recently checked course on the page.
    stamps = [
        e.get("checked_at") for e in seats.values() if e.get("checked_at")
    ]

    return {
        "as_of": min(stamps) if stamps else None,
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
                    self._json(200, check(url))
                except (stellic.AuditError, ot.OpenTabsError) as exc:
                    self._json(502, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001 - surface, don't crash
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
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
