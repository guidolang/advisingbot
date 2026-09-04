"""
Whether a course is worth putting in front of *this* student.

Open seats are not the same thing as eligibility. Three checks stand between
"the audit says a course could count" and "tell the adviser about it":

  * the student has not already taken it,
  * its prerequisites are met,
  * its description does not hand the course to some other programme.

Everything here errs the same way. A course is dropped only when the evidence
is clear; anything ambiguous is kept and shown. Hiding a course an adviser
should have seen is a worse failure than listing one they then rule out, and
Quinnipiac - not this tool - decides who may register for what.

Prerequisite text
-----------------
Self-Service publishes requisites twice: an opaque `Requisites` list keyed by
an internal requirement code, and, in `CourseFullModels`, the same rules as the
sentence the catalogue prints:

    Take CIS*101.                    - Must be completed prior to taking this course.
    Take FIN*310, FIN*320.           - Must be completed prior to taking this course.
    Take MA*170, MA*176 or MA*275.   - Must be completed prior to taking this course.
    Take BIO*101L.                   - Must be taken at the same time as this course.

Only the first kind can make a course unavailable *now*. A corequisite is
satisfied by registering for both at once, and a requisite marked "Recommended"
is advice, so neither blocks.

The sentences are written by hand and the long tail shows it ("Sophomore status
or permission of the instructor", "Take one 200-level English course"). So the
parser reads the shapes it recognises and refuses the rest: a requisite it
cannot parse into course codes never blocks anything.

Within one requisite, `or` anywhere makes the whole list alternatives and the
absence of `or` makes it a conjunction. That is exactly right for the ~1,000
requisites written as a plain list or a plain choice, and deliberately lenient
for the handful that mix the two ("Take MK*201 and one course from: ...").
"""

from __future__ import annotations

import re

# Subject codes taught in the School of Business. Used to decide which course
# descriptions are worth reading for a programme restriction: a business
# student is the audience a business course is written for.
BUSINESS_SUBJECTS = frozenset({
    "AC",    # Accounting
    "BAN",   # Business Analytics
    "BBA",   # Business Administration
    "BLW",   # Business Law
    "CIS",   # Computer Information Systems
    "EC",    # Economics
    "ENT",   # Entrepreneurship
    "FIN",   # Finance
    "IB",    # International Business
    "MBA",   # Master of Business Administration
    "MG",    # Management
    "MK",    # Marketing
    "OL",    # Organizational Leadership
    "PMBA",  # Professional MBA
    "SB",    # Business (School of Business core)
    "STR",   # Strategy
})


# -- course codes ----------------------------------------------------------

# Self-Service writes "CIS*101"; Stellic and the catalogue prose write
# "CIS 101". Both turn up inside requisite sentences. The number may carry a
# section suffix (101L, 101H, 101HL, 810PBL) but never a "-level" band, which
# is prose about a whole tier of courses rather than one course.
_CODE_RE = re.compile(
    r"\b([A-Z]{2,5})\s*(?:\*|\s)\s*(\d{3}[A-Z]{0,3})\b(?!-?LEVEL)"
)

# The catalogue is typed by hand, and "Take BIO*101and BIO*101L" happens. Left
# alone the connective is read as part of the course number.
_GLUED_RE = re.compile(r"(AND|OR)$")


def _number(raw: str) -> str:
    return _GLUED_RE.sub("", raw)


def normalise(code: str) -> str:
    """Any of "CIS 101", "CIS*101", "CIS-101" -> "CIS-101"."""
    match = _CODE_RE.search((code or "").upper().replace("-", " "))
    if not match:
        return (code or "").strip().upper()
    return f"{match.group(1)}-{_number(match.group(2))}"


def _codes_in(text: str) -> list[str]:
    return [f"{s}-{_number(n)}" for s, n in _CODE_RE.findall(text.upper())]


# -- prerequisites ---------------------------------------------------------

# Trailers the catalogue adds after the course list. They qualify a requisite
# we cannot check (we have no grades), not the list of courses itself, so they
# are removed rather than treated as prose we failed to understand.
_TRAILER_RE = re.compile(
    r"\bminimum\s+grade\b.*$|\bor\s+transfer\s+credit\b|\bor\s+equivalent\b",
    re.I,
)

# What is allowed to remain once the course codes are taken out. Anything else
# means the sentence says something we are not reading correctly.
_FILLER_RE = re.compile(r"\b(?:take|and|or|required|course|courses)\b", re.I)


def _parse_requisite(text: str) -> tuple[list[str], bool] | None:
    """One requisite sentence -> (course codes, all_required).

    Returns None when the sentence is not a plain list of courses, which is
    the signal to ignore it rather than guess.
    """
    body = " ".join((text or "").split())
    if not body:
        return None
    body = _TRAILER_RE.sub(" ", body)

    codes = _codes_in(body)
    if not codes:
        return None

    # Whatever is left after the codes and the connectives must be empty, or
    # the sentence carries a condition this parser does not model - a
    # programme name, a placement level, "permission of the instructor".
    residue = _CODE_RE.sub(" ", body.upper())
    residue = _FILLER_RE.sub(" ", residue)
    residue = re.sub(r"[^A-Z]+", " ", residue)
    if residue.strip():
        return None

    # "or" anywhere makes the list alternatives; otherwise every course is
    # required. See the module docstring for why the mixed case is lenient.
    return list(dict.fromkeys(codes)), not re.search(r"\bor\b", body, re.I)


def _blocks_now(requisite: dict) -> bool:
    """Only a required, must-come-first requisite can rule a course out today."""
    if not requisite.get("required", True):
        return False               # "Recommended ... but is not required."
    return requisite.get("order") == "Previous"


def missing_prerequisites(
    requisites: list[dict], completed: set[str]
) -> list[str]:
    """Which prerequisite courses this student still lacks.

    `completed` holds everything already taken or currently under way, in
    "CIS-101" form. An empty result means nothing stands in the way that we
    can see.
    """
    missing: list[str] = []
    for requisite in requisites or []:
        if not _blocks_now(requisite):
            continue
        parsed = _parse_requisite(requisite.get("text") or "")
        if parsed is None:
            continue               # unreadable: never block on it
        codes, all_required = parsed
        if all_required:
            missing.extend(c for c in codes if c not in completed)
        elif not any(c in completed for c in codes):
            # A choice, none of them held: report the whole choice.
            missing.append(" or ".join(codes))
    return list(dict.fromkeys(missing))


# -- programme restrictions ------------------------------------------------

# A sentence that hands the course to a particular audience. The audience is
# whatever follows, up to the end of the clause.
_AUDIENCE_RE = re.compile(
    r"""(?:designed|intended|reserved|written)\s
            (?:primarily\s|specifically\s|exclusively\s|only\s)*for\s
      | (?:^|\.\s|\bis\s|\bare\s)(?:primarily\s)?(?:open\s+)?only\s+(?:for|to)\s
      | (?:enrollment\s+(?:in\s+this\s+course\s+)?is\s+)?
            restricted\s+to\s
      | \bis\s+primarily\s+for\s
      | ^for\s
    """,
    re.I | re.X,
)

# The audience has to name a programme, and naming one takes one of these
# nouns. Anything vaguer is a sentence about something else - "limited to 12
# students", "skills required for positions in the corporate sector" - or an
# audience defined by year rather than programme ("students in their sophomore
# year"), which says nothing about whether this student belongs in the room.
_PROGRAMME_RE = re.compile(
    r"\bmajors?\b|\bminors?\b|\bprograms?\b|\btracks?\b|\bconcentrations?\b"
    r"|\bschool\s+of\b|\bcollege\s+of\b|\bhonors\b|\bcandidates\b"
    r"|\bpathways?\b|\bmajoring\b",
    re.I,
)

_NEGATED_RE = re.compile(r"\bnon-?\s?([a-z]+)", re.I)

# Words too common across programme names to tell two programmes apart.
_STOPWORDS = frozenset({
    "and", "the", "of", "in", "for", "studies", "study", "science", "sciences",
    "arts", "bachelor", "master", "bs", "ba", "ms", "mba", "degree", "program",
    "programs", "major", "majors", "minor", "students", "student", "school",
    "college", "applied", "general", "professional", "online", "completion",
})


def _words(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z]+", (text or "").lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def restriction(description: str, programmes: list[str]) -> str | None:
    """The sentence that reserves this course for someone else, if there is one.

    Returns None when the description names no audience, or names one this
    student belongs to. "Business" counts as a match for every School of
    Business programme, which is how a business student keeps EC 111 while
    losing "designed for nursing majors".
    """
    if not description:
        return None

    mine = set()
    for programme in programmes or []:
        mine |= _words(programme)
    mine.add("business")

    for sentence in re.split(r"(?<=[.;])\s+", description):
        sentence = sentence.strip()
        match = _AUDIENCE_RE.search(sentence)
        if not match:
            continue
        audience = sentence[match.end():]
        if not _PROGRAMME_RE.search(audience):
            continue

        # "designed for non-business majors" excludes exactly the students
        # "designed for business majors" would have kept - and, read the other
        # way, "designed for non-science majors" is an invitation to one.
        negated = {m.group(1).lower() for m in _NEGATED_RE.finditer(audience)}
        if negated:
            return sentence if negated & mine else None

        if _words(audience) & mine:
            return None
        return sentence

    return None
