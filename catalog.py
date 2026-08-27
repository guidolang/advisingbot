"""
Reads elective course lists out of the Quinnipiac catalog.

The catalog (catalog.qu.edu) is a CourseLeaf site. Every curriculum page has
one or more `sc_courselist` tables whose rows come in three flavours:

    areaheader   a section label      "BAN Elective Courses"
    course       an actual course     "BAN 310  Web Analytics"
    comment      free text / a rule   "Select any 200 level or above CIS class"

A course counts as an elective when the nearest preceding header or comment
says so. Not every elective can be reduced to a list of courses - plenty of
them are stated as rules ("Any BAN course", "Take one 600-level MK course") -
so those are collected separately and reported rather than quietly dropped.

Course codes are read from the `showCourse(this, 'BAN 310')` handler on each
link, which is the one place they appear with ordinary spaces rather than
non-breaking ones.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = (
    "AdvisingBot/1.0 (course-availability checker; "
    "contact: Quinnipiac advising)"
)

# The programs whose electives we care about.
PROGRAMS: list[tuple[str, str]] = [
    (
        "BS Applied AI & Business Analytics",
        "https://catalog.qu.edu/business/computer-information-systems"
        "/bs-applied-ai-business-analytics/",
    ),
    (
        "Applied AI & Business Analytics minor",
        "https://catalog.qu.edu/business/computer-information-systems"
        "/applied-ai-business-analytics-minor/",
    ),
    (
        "BS Computer Information Systems",
        "https://catalog.qu.edu/business/computer-information-systems"
        "/computer-information-systems-bs/",
    ),
    (
        "Computer Information Systems minor",
        "https://catalog.qu.edu/business/computer-information-systems"
        "/computer-information-systems-minor/",
    ),
    (
        "MS Applied AI & Business Analytics",
        "https://catalog.qu.edu/graduate-studies/business"
        "/business-applied-ai-business-analytics-ms/",
    ),
]

_TABLE_RE = re.compile(r'<table class="sc_courselist.*?</table>', re.S)
_ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.S)
_CLASS_RE = re.compile(r'<tr[^>]*class="([^"]*)"')
_CODE_RE = re.compile(r"showCourse\(this,\s*'([A-Z]{2,4})\s+([0-9]{3}[A-Z]?)'\)")
_TITLE_CELL_RE = re.compile(r"<td(?![^>]*codecol)[^>]*>(.*?)</td>", re.S)
_CROSSLIST_RE = re.compile(r"\(([A-Z]{2,4})\s+([0-9]{3}[A-Z]?)\)")
_TAGS_RE = re.compile(r"<[^>]+>")

# Comment rows that are bookkeeping, not requirements.
_NOISE_RE = re.compile(r"^\s*(total credits|code\s+title)", re.I)

# A rule is only worth reporting if it points at courses the page never lists.
# "Select 2 of the following electives" is answered by the rows beneath it;
# "Any BAN course" and "any 200 level or above CIS class" are not.
_OPEN_ENDED_RE = re.compile(
    r"\b(any|all|choose|take\s+one|or\s+above|\d00[- ]level|level\s+\d00)\b",
    re.I,
)


class CatalogError(RuntimeError):
    """Raised when a catalog page can't be fetched or parsed."""


@dataclass(frozen=True)
class Elective:
    subject: str
    number: str
    title: str

    @property
    def code(self) -> str:
        return f"{self.subject}-{self.number}"

    def __str__(self) -> str:
        return f"{self.subject} {self.number}"


@dataclass
class Rule:
    """An elective requirement stated as prose rather than a course list."""

    program: str
    text: str


@dataclass
class ProgramElectives:
    name: str
    url: str
    electives: list[Elective] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    cross_listings: list[Elective] = field(default_factory=list)


def fetch(url: str, *, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CatalogError(f"{exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise CatalogError(f"Could not reach {url}: {exc.reason}") from exc


def _text(fragment: str) -> str:
    """Flatten an HTML fragment to readable text."""
    plain = html.unescape(_TAGS_RE.sub(" ", fragment))
    return re.sub(r"\s+", " ", plain.replace("\xa0", " ")).strip()


def _row_title(row: str) -> str:
    """The course title, i.e. the first cell that isn't the code column."""
    for cell in _TITLE_CELL_RE.findall(row):
        text = _text(cell)
        if text and not re.fullmatch(r"[\d.\s]*", text):
            return text
    return ""


def parse_program(name: str, url: str, page: str) -> ProgramElectives:
    """Pull the elective courses and unresolved rules out of one page."""
    result = ProgramElectives(name=name, url=url)
    seen: set[tuple[str, str]] = set()
    seen_rules: set[str] = set()

    for table in _TABLE_RE.findall(page):
        # Each table restarts the section context.
        in_electives = False

        for row in _ROW_RE.findall(table):
            classes = (_CLASS_RE.search(row) or [None, ""])[1]
            if "hidden" in classes:
                continue

            text = _text(row)
            if not text or _NOISE_RE.match(text):
                continue

            is_header = "areaheader" in classes
            is_course = "codecol" in row and not is_header
            mentions_electives = "elective" in text.lower()

            if is_header:
                # A header always sets the context - "Required Core Courses"
                # ends an elective block just as surely as it starts one.
                in_electives = mentions_electives
            elif not is_course and mentions_electives:
                # "Take the following three electives" - a comment that opens
                # an elective block, as the MS concentration tables do.
                in_electives = True

            if not in_electives:
                continue

            if is_course:
                title = _row_title(row)
                for subject, number in _CODE_RE.findall(row):
                    if (subject, number) not in seen:
                        seen.add((subject, number))
                        result.electives.append(Elective(subject, number, title))
                # "Data Analysis with Excel (AC 350)" - the same class listed
                # under a second subject code, which is how it may appear in
                # Self-Service.
                for subject, number in _CROSSLIST_RE.findall(title):
                    if (subject, number) not in seen:
                        seen.add((subject, number))
                        result.cross_listings.append(
                            Elective(subject, number, title)
                        )
            else:
                # A header or comment inside an elective block. Any course
                # codes linked from it count (BS CIS names SB 488 this way);
                # the prose itself is kept as a rule for a human to read.
                for subject, number in _CODE_RE.findall(row):
                    if (subject, number) not in seen:
                        seen.add((subject, number))
                        result.electives.append(
                            Elective(subject, number, _row_title(row))
                        )
                if _OPEN_ENDED_RE.search(text) and text not in seen_rules:
                    seen_rules.add(text)
                    result.rules.append(Rule(program=name, text=text))

    result.electives.sort(key=lambda e: (e.subject, e.number))
    result.cross_listings.sort(key=lambda e: (e.subject, e.number))
    return result


def scrape(
    programs: list[tuple[str, str]] | None = None,
    *,
    delay: float = 0.5,
    progress=None,
) -> list[ProgramElectives]:
    """Fetch and parse every program, politely."""
    out: list[ProgramElectives] = []
    for index, (name, url) in enumerate(programs or PROGRAMS):
        if progress:
            progress(name, url)
        if index:
            time.sleep(delay)
        out.append(parse_program(name, url, fetch(url)))
    return out
