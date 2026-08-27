"""
Thin client for Quinnipiac's Ellucian Colleague Self-Service course catalog.

Self-Service is a single-page app whose UI actions map onto a small JSON API:

    GET  /student/Courses/Search?subjects=BAN   -> HTML page; sets the session
                                                   cookie and carries the
                                                   antiforgery token
    POST /student/Courses/PostSearchCriteria    -> the search + all the filters
                                                   ("Open Sections Only",
                                                   term, academic level)
    POST /student/Courses/Sections              -> "View Available Sections
                                                   for XXX-YYY", incl. seats

Talking to those endpoints does exactly what clicking through the UI does, but
without a browser. Everything here is anonymous: the catalog requires no login.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

BASE_URL = "https://selfservice.quinnipiac.edu"
SEARCH_PAGE = "/student/Courses/Search"
POST_SEARCH_CRITERIA = "/student/Courses/PostSearchCriteria"
POST_SECTIONS = "/student/Courses/Sections"

USER_AGENT = (
    "AdvisingBot/1.0 (course-availability checker; "
    "contact: Quinnipiac advising)"
)

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"'
)


class SelfServiceError(RuntimeError):
    """Raised when Self-Service answers with something we can't use."""


@dataclass
class Term:
    code: str                 # e.g. "26/FA"
    description: str          # e.g. "Fall 2026"
    start_date: str | None = None
    end_date: str | None = None
    sequence: int | None = None
    course_count: int = 0

    def __str__(self) -> str:
        return f"{self.description} ({self.code})"


@dataclass
class Section:
    """One offered section, with its seat counts."""

    section_id: str
    name: str                 # "BAN*610-02"
    number: str               # "02"
    term_code: str
    term_description: str
    available: int | None     # None => unlimited / not published
    capacity: int | None
    waitlisted: int | None
    status: str               # "Open" / "Closed" / "Waitlisted"
    unlimited_seats: bool
    faculty: str
    location: str
    meeting_times: str
    credits: str
    seats_display: str        # "15 / 35 / 0" exactly as Self-Service shows it

    @property
    def seats_unpublished(self) -> bool:
        """Self-Service shows "0 / 0 / 0" - no capacity is configured.

        These turn up on some distance-education sections. Colleague reports
        them as Closed, but because no cap is set the count tells you nothing,
        so we surface them separately instead of counting or hiding them.
        """
        return not self.unlimited_seats and not self.capacity

    @property
    def has_open_seats(self) -> bool:
        if self.unlimited_seats:
            return True
        return (self.available or 0) > 0


@dataclass
class Course:
    course_id: str
    subject: str
    number: str
    title: str
    academic_levels: list[str] = field(default_factory=list)
    matching_section_ids: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    @property
    def code(self) -> str:
        return f"{self.subject}-{self.number}"

    @property
    def open_sections(self) -> list[Section]:
        return [s for s in self.sections if s.has_open_seats]

    @property
    def total_open_seats(self) -> int:
        return sum(s.available or 0 for s in self.open_sections)


class SelfServiceClient:
    """Holds the session cookie + antiforgery token and speaks the JSON API."""

    def __init__(self, *, delay: float = 0.4, timeout: int = 45) -> None:
        self.delay = delay
        self.timeout = timeout
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._token: str | None = None
        self._last_request_at = 0.0

    # -- plumbing ---------------------------------------------------------

    def _throttle(self) -> None:
        """Stay well inside anything a human clicking the site would do."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_at = time.monotonic()

    def _open(self, request: urllib.request.Request) -> bytes:
        self._throttle()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise SelfServiceError(
                f"{exc.code} from {request.full_url}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SelfServiceError(
                f"Could not reach {request.full_url}: {exc.reason}"
            ) from exc

    def _ensure_session(self) -> str:
        """Load the search page once to pick up the cookie + CSRF token.

        This is the equivalent of opening
        https://selfservice.quinnipiac.edu/student/Courses in a browser; every
        POST below is rejected with 400 until we've done it.
        """
        if self._token:
            return self._token

        request = urllib.request.Request(
            f"{BASE_URL}{SEARCH_PAGE}",
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        )
        html = self._open(request).decode("utf-8", "replace")
        match = _TOKEN_RE.search(html)
        if not match:
            raise SelfServiceError(
                "Could not find the antiforgery token on the course search "
                "page. Self-Service may have changed; re-check the page HTML."
            )
        self._token = match.group(1)
        return self._token

    def _post_json(self, path: str, payload: dict) -> dict:
        token = self._ensure_session()
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "__RequestVerificationToken": token,
                "Referer": f"{BASE_URL}{SEARCH_PAGE}",
            },
        )
        body = self._open(request)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            snippet = body[:200].decode("utf-8", "replace")
            raise SelfServiceError(
                f"Expected JSON from {path}, got: {snippet!r}"
            ) from exc

    # -- search -----------------------------------------------------------

    @staticmethod
    def _criteria(
        *,
        subjects: list[str],
        terms: list[str],
        academic_levels: list[str],
        open_only: bool,
        page: int,
        page_size: int,
    ) -> dict:
        """Build the payload the search page posts when you tick filters."""
        return {
            "keyword": "",
            "subjects": subjects,
            "academicLevels": academic_levels,
            "courseLevels": [],
            "courseTypes": [],
            "topicCodes": [],
            "terms": terms,
            "locations": [],
            "days": [],
            "faculty": [],
            "onlineCategories": [],
            # This is the "Open Sections Only" checkbox.
            "openSections": open_only,
            "openAndWaitlistedSections": False,
            "startsAtTime": None,
            "endsByTime": None,
            "requirement": None,
            "subrequirement": None,
            "group": "",
            "courseIds": "",
            "sortOn": "None",
            "sortDirection": "Ascending",
            "currentPageIndex": page,
            "pageSize": page_size,
            "quantityPerPage": page_size,
        }

    def search(
        self,
        *,
        subjects: list[str] | None = None,
        terms: list[str] | None = None,
        academic_levels: list[str] | None = None,
        open_only: bool = True,
        page_size: int = 50,
        max_pages: int = 40,
    ) -> tuple[list[Course], dict]:
        """Run a catalog search, following pagination.

        Returns (courses, first_page_payload). The raw first page is handed
        back too because it carries the facet lists (terms, levels, subjects)
        that the caller uses to discover what's actually offered.
        """
        subjects = subjects or []
        terms = terms or []
        academic_levels = academic_levels or []

        first = self._post_json(
            POST_SEARCH_CRITERIA,
            self._criteria(
                subjects=subjects,
                terms=terms,
                academic_levels=academic_levels,
                open_only=open_only,
                page=1,
                page_size=page_size,
            ),
        )
        message = (first.get("ErrorMessage") or "").strip()
        if message:
            # An empty result set comes back as an "error" message; that is a
            # normal outcome for a watch, not a failure.
            if "no courses found" in message.lower():
                return [], first
            raise SelfServiceError(f"Self-Service said: {message}")

        courses = [_parse_course(c) for c in first.get("Courses") or []]
        total_pages = min(int(first.get("TotalPages") or 1), max_pages)

        for page in range(2, total_pages + 1):
            payload = self._post_json(
                POST_SEARCH_CRITERIA,
                self._criteria(
                    subjects=subjects,
                    terms=terms,
                    academic_levels=academic_levels,
                    open_only=open_only,
                    page=page,
                    page_size=page_size,
                ),
            )
            courses.extend(_parse_course(c) for c in payload.get("Courses") or [])

        return courses, first

    def fetch_sections(self, course: Course) -> list[Section]:
        """The "View Available Sections for XXX-YYY" click, for one course."""
        if not course.matching_section_ids:
            return []
        payload = self._post_json(
            POST_SECTIONS,
            {
                "courseId": course.course_id,
                "sectionIds": course.matching_section_ids,
            },
        )
        retrieved = payload.get("SectionsRetrieved") or {}
        sections: list[Section] = []
        for bucket in retrieved.get("TermsAndSections") or []:
            term = bucket.get("Term") or {}
            for wrapper in bucket.get("Sections") or []:
                section = _parse_section(wrapper, term)
                if section:
                    sections.append(section)
        sections.sort(key=lambda s: (s.term_code, s.number))
        return sections

    def discover_terms(self) -> list[Term]:
        """Ask Self-Service which terms currently have open sections.

        Keeping the "Open Sections Only" filter on is what scopes this to
        terms worth reporting on: terms that have closed out drop away by
        themselves, rather than needing a hardcoded cutoff date.
        """
        _, payload = self.search(open_only=True, page_size=1, max_pages=1)

        dated = {
            t.get("Code"): t
            for t in payload.get("ActivePlanTerms") or []
            if t.get("Code")
        }
        terms: list[Term] = []
        for entry in payload.get("TermFilters") or []:
            code = entry.get("Value")
            if not code:
                continue
            extra = dated.get(code, {})
            terms.append(
                Term(
                    code=code,
                    description=entry.get("Description") or code,
                    start_date=extra.get("StartDate"),
                    end_date=extra.get("EndDate"),
                    sequence=extra.get("Sequence"),
                    course_count=int(entry.get("Count") or 0),
                )
            )
        # Dated terms in chronological order; anything undated trails behind.
        terms.sort(key=lambda t: (t.start_date or "9999", t.code))
        return terms


# -- parsing --------------------------------------------------------------


def _parse_course(raw: dict) -> Course:
    return Course(
        course_id=str(raw.get("Id") or ""),
        subject=raw.get("SubjectCode") or "",
        number=raw.get("Number") or "",
        title=raw.get("Title") or "",
        matching_section_ids=[str(s) for s in raw.get("MatchingSectionIds") or []],
    )


def _format_meetings(section: dict) -> str:
    parts = []
    for meeting in section.get("FormattedMeetingTimes") or []:
        days = (meeting.get("DaysOfWeekDisplay") or "").strip()
        start = (meeting.get("StartTimeDisplay") or "").strip()
        end = (meeting.get("EndTimeDisplay") or "").strip()
        method = (meeting.get("InstructionalMethodDisplay") or "").strip()
        when = " ".join(x for x in (days, f"{start}-{end}" if start else "") if x)
        chunk = when or method or ""
        if when and method:
            chunk = f"{when} ({method})"
        if chunk:
            parts.append(chunk)
    return "; ".join(parts) or "TBA"


def _format_credits(section: dict) -> str:
    low = section.get("MinimumCredits")
    high = section.get("MaximumCredits")
    if low is None and high is None:
        return ""
    if high and high != low:
        return f"{low:g}-{high:g}"
    return f"{low:g}" if low is not None else ""


def _parse_section(wrapper: dict, term: dict) -> Section | None:
    section = wrapper.get("Section") or {}
    if not section.get("Id"):
        return None
    return Section(
        section_id=str(section["Id"]),
        name=section.get("SectionNameDisplay") or "",
        number=section.get("Number") or "",
        term_code=section.get("TermId") or term.get("Code") or "",
        term_description=term.get("Description") or "",
        available=section.get("Available"),
        capacity=section.get("Capacity"),
        waitlisted=section.get("Waitlisted"),
        status=section.get("AvailabilityStatus") or "",
        unlimited_seats=bool(section.get("HasUnlimitedSeats")),
        faculty=wrapper.get("FacultyDisplay") or "",
        location=section.get("LocationDisplay") or "",
        meeting_times=_format_meetings(section),
        credits=_format_credits(section),
        seats_display=section.get("AvailabilityDisplay")
        or wrapper.get("AvailabilityDisplay")
        or "",
    )
