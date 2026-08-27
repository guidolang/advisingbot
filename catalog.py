"""
Reads program curricula out of the Quinnipiac catalog.

The catalog (catalog.qu.edu) is a CourseLeaf site. Every curriculum page has
one or more `sc_courselist` tables whose rows come in three flavours:

    areaheader   a section label      "BAN Elective Courses"
    course       an actual course     "BAN 310  Web Analytics"
    comment      free text / a rule   "Select any 200 level or above CIS class"

We keep the catalog's own grouping - core, electives, concentrations - because
that is how an adviser reads a program, and reproduce it verbatim rather than
imposing our own categories.

Not every requirement reduces to a list of courses. "Any BAN course" and "Take
one 600-level MK course" are requirements a human has to interpret, so they are
recorded as rules on the group rather than silently dropped.

Course codes are read from the `showCourse(this, 'BAN 310')` handler on each
link, the one place they appear with ordinary spaces rather than non-breaking
ones.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE = "https://catalog.qu.edu"

USER_AGENT = (
    "AdvisingBot/1.0 (course-availability checker; "
    "contact: Quinnipiac advising)"
)

# Index pages listing every School of Business program.
SCHOOL_INDEXES: list[tuple[str, str]] = [
    ("undergraduate", f"{BASE}/business/"),
    ("graduate", f"{BASE}/graduate-studies/business/"),
]

_TABLE_RE = re.compile(r'<table class="sc_courselist.*?</table>', re.S)
_ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.S)
_CLASS_RE = re.compile(r"<tr[^>]*class=\"([^\"]*)\"")
_CELL_RE = re.compile(r"<t[dh]([^>]*)>(.*?)</t[dh]>", re.S)
_CODE_RE = re.compile(r"showCourse\(this,\s*'([A-Z]{2,4})\s+([0-9]{3}[A-Z]?)'\)")
_CROSSLIST_RE = re.compile(r"\(([A-Z]{2,4})\s+([0-9]{3}[A-Z]?)\)")
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_HEAD_RE = re.compile(r"<(h[2-4])[^>]*>(.*?)</\1>", re.S)
_TAGS_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r'href="([^"#?]+/)"[^>]*>(.*?)</a>', re.S)

# Comment rows that are bookkeeping, not requirements.
_NOISE_RE = re.compile(r"^\s*(total credits|code\s+title)", re.I)

# A rule is only worth recording if it points at courses the page never lists.
# "Select 2 of the following electives" is answered by the rows beneath it;
# "Any BAN course" and "any 200 level or above CIS class" are not.
_OPEN_ENDED_RE = re.compile(
    r"\b(any|all|choose|take\s+one|or\s+above|\d00[- ]level|level\s+\d00)\b",
    re.I,
)

_ELECTIVE_RE = re.compile(r"elective", re.I)

# Links above the first real heading pick up the catalog's edition breadcrumb.
_EDITION_RE = re.compile(r"^\s*\d{4}\s*[-\u2013]\s*\d{4}\s+Edition\s*$", re.I)


class CatalogError(RuntimeError):
    """Raised when a catalog page can't be fetched or parsed."""


@dataclass(frozen=True)
class CourseRef:
    subject: str
    number: str
    title: str = ""
    alternative: bool = False   # an "or FIN 610" row
    cross_listed: bool = False  # "(AC 350)" named in another course's title

    @property
    def code(self) -> str:
        return f"{self.subject}-{self.number}"

    def __str__(self) -> str:
        return f"{self.subject} {self.number}"


@dataclass
class Group:
    """One labelled block of a curriculum: a core, an elective pool, a track."""

    name: str
    credits: str = ""
    notes: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    courses: list[CourseRef] = field(default_factory=list)
    is_elective: bool = False


@dataclass
class Program:
    name: str
    url: str
    level: str = "undergraduate"
    category: str = ""
    groups: list[Group] = field(default_factory=list)

    # -- views the rest of the toolchain uses ----------------------------

    @property
    def courses(self) -> list[CourseRef]:
        seen: set[tuple[str, str]] = set()
        out: list[CourseRef] = []
        for group in self.groups:
            for course in group.courses:
                key = (course.subject, course.number)
                if key not in seen:
                    seen.add(key)
                    out.append(course)
        return out

    @property
    def electives(self) -> list[CourseRef]:
        seen: set[tuple[str, str]] = set()
        out: list[CourseRef] = []
        for group in self.groups:
            if not group.is_elective:
                continue
            for course in group.courses:
                key = (course.subject, course.number)
                if key not in seen:
                    seen.add(key)
                    out.append(course)
        return out

    @property
    def rules(self) -> list[tuple[str, str]]:
        """(group name, rule text) for every unresolved requirement."""
        return [(g.name, r) for g in self.groups for r in g.rules]

    @property
    def subjects(self) -> set[str]:
        return {c.subject for c in self.courses}


# -- fetching --------------------------------------------------------------


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


# -- text helpers ----------------------------------------------------------


def _text(fragment: str) -> str:
    plain = html.unescape(_TAGS_RE.sub(" ", fragment))
    return re.sub(r"\s+", " ", plain.replace("\xa0", " ")).strip()


def _cells(row: str) -> list[tuple[str, str]]:
    """[(css class, inner html)] for each cell in a row."""
    out = []
    for attrs, body in _CELL_RE.findall(row):
        match = re.search(r'class="([^"]*)"', attrs)
        out.append((match.group(1) if match else "", body))
    return out


def _split_row(row: str) -> tuple[str, str, str]:
    """A course row's (code cell, title, credits)."""
    code_html = title = credits = ""
    for css, body in _cells(row):
        if "codecol" in css:
            code_html = body
        elif "hourscol" in css:
            credits = _text(body)
        elif not title:
            title = _text(body)
    return code_html, title, credits


def _label_and_credits(row: str) -> tuple[str, str]:
    """A header row's (label, credits), keeping the credit count out of the
    label so "University Curriculum 46" doesn't become the group's name."""
    label = credits = ""
    for css, body in _cells(row):
        if "hourscol" in css:
            credits = _text(body)
        elif not label:
            label = _text(body)
    return label, credits


def _table_captions(page: str) -> list[str]:
    """The nearest preceding <h2>-<h4> for each courselist table.

    The MS concentration tables carry no internal header, so their heading -
    "Concentration in Marketing" - is the only thing that names them.
    """
    captions = []
    for match in _TABLE_RE.finditer(page):
        heads = _HEAD_RE.findall(page[: match.start()])
        captions.append(_text(heads[-1][1]) if heads else "")
    return captions


# -- parsing ---------------------------------------------------------------


def parse_program(
    name: str, url: str, page: str, *, level: str = "undergraduate",
    category: str = "",
) -> Program:
    heading = _H1_RE.search(page)
    program = Program(
        name=_text(heading.group(1)) if heading else name,
        url=url,
        level=level,
        category=category,
    )

    captions = _table_captions(page)

    for index, table in enumerate(_TABLE_RE.findall(page)):
        caption = captions[index] if index < len(captions) else ""
        current: Group | None = None

        def ensure(group: Group | None) -> Group:
            """Rows can appear before any header - open a group for them."""
            if group is not None:
                return group
            new = Group(name=caption or "Requirements")
            program.groups.append(new)
            return new

        for row in _ROW_RE.findall(table):
            classes = (_CLASS_RE.search(row) or [None, ""])[1]
            if "hidden" in classes:
                continue

            flat = _text(row)
            if not flat or _NOISE_RE.match(flat):
                continue

            is_header = "areaheader" in classes
            is_course = "codecol" in row and not is_header

            if is_header:
                label, credits = _label_and_credits(row)
                # "CIS electives (Select any 200 level or above CIS class...)"
                # is a heading and a rule at once. Show the short heading and
                # let the rule text carry the qualification.
                name = label
                if _OPEN_ENDED_RE.search(label) and "(" in label:
                    name = label.split("(", 1)[0].strip(" :-") or label
                current = Group(
                    name=name or caption or "Requirements",
                    credits=credits,
                    is_elective=bool(_ELECTIVE_RE.search(label)),
                )
                program.groups.append(current)
                # A header can name courses inline: the BS in CIS points at
                # SB 488 from inside its elective header.
                for subject, number in _CODE_RE.findall(row):
                    current.courses.append(CourseRef(subject, number))
                if _OPEN_ENDED_RE.search(label):
                    current.rules.append(label)
                    current.notes = [n for n in current.notes if n != label]
                continue

            if is_course:
                current = ensure(current)
                code_html, title, credits = _split_row(row)
                alternative = "orclass" in classes
                for subject, number in _CODE_RE.findall(code_html):
                    current.courses.append(
                        CourseRef(subject, number, title, alternative)
                    )
                # "Data Analysis with Excel (AC 350)" - the same class under a
                # second subject code, which is how Self-Service may list it.
                for subject, number in _CROSSLIST_RE.findall(title):
                    current.courses.append(
                        CourseRef(subject, number, title, cross_listed=True)
                    )
                continue

            # A comment row: prose, and sometimes course links inside it.
            current = ensure(current)
            note, _ = _label_and_credits(row)
            note = note or flat
            if note not in current.notes:
                current.notes.append(note)
            if _ELECTIVE_RE.search(flat):
                # "Take the following three electives" is the only marker the
                # MS concentration tables give.
                current.is_elective = True
            if _OPEN_ENDED_RE.search(note) and note not in current.rules:
                current.rules.append(note)
            for subject, number in _CODE_RE.findall(row):
                current.courses.append(CourseRef(subject, number))

    _dedupe(program)
    return program


def _dedupe(program: Program) -> None:
    """Drop repeats within each group, and groups left with nothing in them."""
    kept: list[Group] = []
    for group in program.groups:
        seen: set[tuple[str, str]] = set()
        unique: list[CourseRef] = []
        for course in group.courses:
            key = (course.subject, course.number)
            if key not in seen:
                seen.add(key)
                unique.append(course)
        group.courses = unique
        # A header with no courses is a pointer elsewhere ("University
        # Curriculum, 46 credits"). Keep it only if it carries a rule.
        if group.courses or group.rules:
            kept.append(group)
    program.groups = kept


# -- program discovery -----------------------------------------------------


def discover(
    indexes: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Find every program page linked from the school index pages.

    Returns (level, category, title, url). Category is the index page's own
    heading for that block - "Baccalaureate Degrees", "Minors" - which is a
    sensible way to group a long dropdown.
    """
    found: dict[str, tuple[str, str, str, str]] = {}

    for level, index_url in indexes or SCHOOL_INDEXES:
        page = fetch(index_url)
        prefix = index_url[len(BASE):]

        for match in _LINK_RE.finditer(page):
            href, label = match.group(1), _text(match.group(2))
            if not href.startswith(prefix) or href == prefix or not label:
                continue
            # The nearest heading above the link is its section on the page.
            heads = _HEAD_RE.findall(page[: match.start()])
            category = _text(heads[-1][1]) if heads else ""
            if not category or _EDITION_RE.match(category):
                category = f"{level.capitalize()} programs"
            url = f"{BASE}{href}"
            found.setdefault(url, (level, category, label, url))

    return list(found.values())


def scrape(
    *,
    delay: float = 0.5,
    progress=None,
    indexes: list[tuple[str, str]] | None = None,
) -> list[Program]:
    """Discover and parse every program, politely, skipping non-curricula."""
    programs: list[Program] = []
    targets = discover(indexes)

    for index, (level, category, label, url) in enumerate(targets):
        if progress:
            progress(label, url, index + 1, len(targets))
        if index:
            time.sleep(delay)
        page = fetch(url)
        if "sc_courselist" not in page:
            # A department landing page, not a program of study.
            continue
        program = parse_program(
            label, url, page, level=level, category=category
        )
        if program.groups:
            programs.append(program)

    programs.sort(key=lambda p: (p.level, p.category, p.name))
    return programs
