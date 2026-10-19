"""
Reads a student's degree audit out of Stellic.

Stellic sits behind Microsoft SSO, so there is no anonymous path to this data.
We borrow the adviser's own logged-in Chrome through OpenTabs, open the audit,
and call Stellic's internal API from inside the page. That is steadier than
scraping the rendered audit, which is a React tree that changes shape often.

Three calls do it:

    POST /planner/getstudentprofile/   form-encoded, student_username=...
                                       -> default_plan_id
    POST /planner/getauditinfo/        JSON, needs that plan_id
                                       -> req_tree + audit_data

Responses carry a `)]}',` XSSI prefix that has to come off before parsing.

How the audit says "still to take"
----------------------------------
`req_tree` is the requirement structure: nested nodes with `screen_name`,
`min_units` and `choices`. Every leaf choice that stands for one course slot
carries a `course_parent_id` and a `course` constraint naming the course.

`audit_data` is keyed by requirement id and holds the progress: `satisfied`,
`units_summary`, and - the useful part - `cards_remaining`, a list of
`{requirement_id, units}` where `requirement_id` is a *composite* of the
sub-requirement id and a zero-padded course-parent id.

So each remaining card resolves one of two ways:

  * the composite matches a `course_parent_id` -> a specific course is missing
  * it doesn't -> the card belongs to an open requirement ("Fulfill any 3 of
    the following", "Natural Sciences"). We attribute it by longest-id-prefix
    and expand that requirement's own choices into candidate courses.

That second path is what turns "any 3 CIS electives" into a list an adviser can
actually act on. Candidates are labelled as such: Stellic decides what really
counts, not us.

Nothing here is written to disk. Audits are student records.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import opentabs as ot
from opentabs import OpenTabsError

# Pulls profile + audit, stashes the JSON on the page, returns its length.
# The result is retrieved in slices because browser_execute_script truncates
# around 50 KB and a real audit runs to several hundred.
_FETCH_JS = r"""
(async () => {
  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1];
  if (!csrf) throw new Error('no csrftoken cookie - not signed in');
  const strip = t => JSON.parse(t.replace(/^\)\]\}'[,]?\s*/, ''));
  const send = async (path, body, asJson) => {
    const r = await fetch(path, {
      method: 'POST', credentials: 'same-origin',
      headers: {
        'Content-Type': asJson ? 'application/json'
                               : 'application/x-www-form-urlencoded',
        'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest'},
      body: asJson ? JSON.stringify(body) : body});
    if (!r.ok) throw new Error(path + ' returned ' + r.status);
    return strip(await r.text());
  };
  const username = new URL(location.href).searchParams.get('student_username');
  if (!username) throw new Error('no student_username in the URL');
  const profile = await send('/planner/getstudentprofile/',
      'student_username=' + encodeURIComponent(username), false);
  const planId = profile.default_plan_id ||
                 (profile.plans && profile.plans[0] && profile.plans[0].id);
  if (!planId) throw new Error('student has no degree plan');
  const audit = await send('/planner/getauditinfo/', {
      mainaudit: true, plan_id: planId, official: true, req_tree: true,
      course_plan_info: true, student_info: true, placeholders_info: true,
      uids: [], isTemplate: false}, true);
  if (audit && audit.success === false) {
    throw new Error('getauditinfo: ' + JSON.stringify(audit.message));
  }
  window.__advisingbot = JSON.stringify({
    plan_id: planId,
    student: {
      name: [profile.first_name, profile.last_name].filter(Boolean).join(' '),
      username: username,
      level: profile.level_name || '',
      programs: (profile.degrees || []).map(d => d.name || d).filter(Boolean),
    },
    audit: audit,
  });
  return window.__advisingbot.length;
})()
"""

# Stellic's own "Search qualifying courses" control behind each requirement.
# `counts_for` is the requirement id; the reply lists every course that
# satisfies it, which is the only way to resolve requirements written as
# attributes ("a course carrying UC_NS") rather than as a course list.
_EXPAND_JS = r"""
(async () => {
  const ids = __IDS__, planId = __PLAN__, maxPages = __PAGES__;
  const maxMatches = __MAXMATCH__;
  const strip = t => JSON.parse(t.replace(/^\)\]\}'[,]?\s*/, ''));
  const out = {};
  for (const id of ids) {
    const courses = [];
    let total = null, from = 0, page = 0;
    try {
      // The endpoint caps a page at 20 rows whatever `size` asks for, so walk
      // it until it says last_page.
      while (page < maxPages) {
        const url = '/planner/searchcourses/?counts_for=' + encodeURIComponent(id)
          + '&brief_mode=true&mode=search&only_prereq_met=false'
          + '&plan_id=' + encodeURIComponent(planId)
          + '&size=20&from=' + from + '&sort_by=code&view_type=list';
        const r = await fetch(url, {credentials: 'same-origin',
          headers: {'X-Requested-With': 'XMLHttpRequest'}});
        if (!r.ok) { out[id] = {error: r.status}; break; }
        const d = strip(await r.text());
        total = d.num_matches;
        // A requirement that thousands of courses satisfy is a free-credit
        // bucket, not a choice. Listing the catalogue is not advice.
        if (total > maxMatches) { out[id] = {total: total, courses: [], broad: true}; break; }
        // Keep only code and name: the raw reply carries every offering and
        // instructor going back years, far past what can be returned here.
        for (const c of d.data_list || []) courses.push([c.code, c.short_name || c.name]);
        page++;
        from = d.till != null ? d.till : from + 20;
        if (d.last_page || !(d.data_list || []).length) break;
      }
      if (!out[id]) out[id] = {total: total, courses: courses};
    } catch (e) { out[id] = {error: String(e)}; }
  }
  return JSON.stringify(out);
})()
"""

_CHUNK = 40_000
_EXPAND_PAGES = 50      # 50 pages x 20 = 1,000 courses per requirement
_EXPAND_MAX_MATCHES = 1_000
_AUDIT_PATH = "/app/audit"


class AuditError(RuntimeError):
    """Raised when an audit can't be fetched or understood."""


@dataclass
class NeededCourse:
    code: str                  # "CIS 351"
    title: str = ""
    units: float | None = None
    slots: int = 1             # how many remaining slots ask for this course
    candidate: bool = False    # True => one option for an open requirement

    @property
    def lookup_code(self) -> str:
        """Self-Service form: "CIS-351"."""
        parts = self.code.split()
        return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else self.code


@dataclass
class Requirement:
    id: str
    name: str
    satisfied: bool = False
    units_total: float | None = None
    units_remaining: float | None = None
    needed: list[NeededCourse] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    open_units: float = 0.0    # remaining units with no course list at all
    slots: int = 0             # how many more courses this requirement wants
    depth: int = 0
    # A range, attribute, or other course-set rule whose concrete courses
    # only Stellic's qualifying-course search can enumerate.
    expand_qualifying_courses: bool = False
    review: str | None = None

    @property
    def is_open_ended(self) -> bool:
        return bool(self.open_units) and not self.needed


# Quinnipiac names its distance programs "... Online - MS". A student in one
# should not be shown classroom sections they cannot attend.
_ONLINE_RE = re.compile(r"\bonline\b", re.I)
_RULE_COURSE_RE = re.compile(r"\b([A-Z]{2,5})\s+(\d{3}[A-Z]{0,3})\b")
_COURSE_TYPE_RE = re.compile(r"\b(UC_[A-Z0-9]+)\b")
_UNIVERSITY_TOTAL_RE = re.compile(
    r"\btotal\s+\d+\s+credits?\b.*\buniversity degree completion\b", re.I
)
_CORE_RE = re.compile(r"\bcore\b", re.I)
_HIDDEN_COURSE_RE = re.compile(
    r"\b[A-Z]{2,5}\s+\d{3}[A-Z]{0,3}\s*"
    r"\(\s*hidden from (?:the )?student[^)]*\)", re.I
)
_CROSS_LISTED_ALIAS_RE = re.compile(
    r"\([^()]*\b([A-Z]{2,5})\s*(\d{3}[A-Z]{0,3})\b[^()]*\)", re.I
)


def _is_university_total(name: str) -> bool:
    """The university-wide credit total is not a useful course-choice pool."""
    return bool(_UNIVERSITY_TOTAL_RE.search(name or ""))


def _normalise_code(code: str) -> str:
    """Stellic writes "CIS 101"; Self-Service wants "CIS-101"."""
    parts = (code or "").replace("*", " ").replace("-", " ").split()
    return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else (code or "").strip()


@dataclass
class Audit:
    student_name: str
    username: str
    level: str
    programs: list[str]
    requirements: list[Requirement]
    online_only: bool = False
    # Everything this student has already passed or is sitting in right now,
    # in Self-Service form ("CIS-101"). Two jobs: never suggest one of these
    # again, and treat them as satisfying other courses' prerequisites.
    completed: set[str] = field(default_factory=set)
    # Approved one-for-one substitutions that satisfy a listed prerequisite,
    # without treating the original course as completed for other purposes.
    prerequisite_completed: set[str] = field(default_factory=set)

    @property
    def all_codes(self) -> list[str]:
        seen: dict[str, None] = {}
        for req in self.requirements:
            if _is_university_total(req.name):
                continue
            for course in req.needed:
                seen.setdefault(course.lookup_code, None)
        return list(seen)


# -- fetching --------------------------------------------------------------


def normalise_url(url: str) -> str:
    """Accept an audit link, a bare username, or any Stellic student URL."""
    url = url.strip()
    if not url:
        raise AuditError("No Stellic URL given.")
    if "://" not in url:
        if "/" in url or "." in url:
            url = "https://" + url
        else:  # a bare username
            return (
                f"https://qu.stellic.com{_AUDIT_PATH}"
                f"?student_username={url}"
            )

    parsed = urlparse(url)
    username = (parse_qs(parsed.query).get("student_username") or [""])[0]
    if not username:
        raise AuditError(
            "That link has no ?student_username= in it. Copy the URL from a "
            "student's audit page in Stellic."
        )
    return f"{parsed.scheme}://{parsed.netloc}{_AUDIT_PATH}?student_username={username}"


def _slurp(session: ot.OpenTabs, tab: int) -> dict:
    """Run the audit fetch in the page and bring the JSON back in slices."""
    try:
        total = int(ot.run_script(session, tab, _FETCH_JS))
    except OpenTabsError as exc:
        raise AuditError(f"Stellic did not return an audit: {exc}") from exc

    parts = []
    for start in range(0, total, _CHUNK):
        parts.append(
            ot.run_script(
                session, tab,
                f"window.__advisingbot.slice({start},{start + _CHUNK})",
            )
        )
    ot.run_script(session, tab, "delete window.__advisingbot; 1")

    blob = "".join(part for part in parts if isinstance(part, str))
    if len(blob) != total:
        raise AuditError(
            f"Audit came back incomplete ({len(blob)} of {total} characters)."
        )
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        raise AuditError(f"Audit JSON would not parse: {exc}") from exc


def _expand(
    session: ot.OpenTabs, tab: int, plan_id: str, ids: list[str]
) -> dict[str, dict]:
    """Ask Stellic which courses satisfy each of these requirements."""
    if not ids or not plan_id:
        return {}
    script = (
        _EXPAND_JS.replace("__IDS__", json.dumps(ids))
        .replace("__PLAN__", json.dumps(plan_id))
        .replace("__PAGES__", str(_EXPAND_PAGES))
        .replace("__MAXMATCH__", str(_EXPAND_MAX_MATCHES))
    )
    try:
        raw = ot.run_script(session, tab, script)
    except OpenTabsError:
        # Expansion is a bonus; a failure here must not lose the audit.
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _apply_expansion(audit: "Audit", found: dict[str, dict]) -> None:
    """Turn "any course carrying UC_NS" into the courses that carry it."""
    for requirement in audit.requirements:
        entry = found.get(requirement.id)
        if not entry or entry.get("error"):
            continue
        courses = entry.get("courses") or []
        if not courses:
            if entry.get("broad"):
                requirement.rules.append(
                    f"Any of {entry.get('total')} courses counts here - too "
                    "broad to list."
                )
            continue
        known = {c.code for c in requirement.needed}
        for code, title in courses:
            if code and code not in known:
                known.add(code)
                requirement.needed.append(
                    NeededCourse(code=code, title=title or "", candidate=True)
                )
        requirement.open_units = 0.0
        total = entry.get("total")
        if isinstance(total, int) and total > len(courses):
            requirement.rules.append(
                f"Showing {len(courses)} of {total} qualifying courses."
            )


def fetch(
    url: str,
    *,
    session: ot.OpenTabs | None = None,
    attempts: int = 2,
    expand: bool = True,
) -> Audit:
    """Fetch and parse one student's audit."""
    target = normalise_url(url)
    session = session or ot.OpenTabs()
    session.connect()

    for attempt in range(attempts):
        try:
            tab = ot.open_tab(session, target)
            try:
                info = ot.wait_for_load(session, tab)
                landed = info.get("url") or ""
                if "login.microsoftonline.com" in landed or "/login" in landed:
                    raise AuditError(
                        "Stellic redirected to the sign-in page. Sign in to "
                        "Stellic in Chrome, then try again."
                    )
                payload = _slurp(session, tab)
                audit = parse(payload)

                # Some requirements hide their candidates behind a range or
                # attribute rule. Healthcare Analytics, for example, exposes
                # its 600-level HM elective this way. Ask only those leaf
                # requirements for their qualifying courses: querying their
                # parent concentration would merge every course from every
                # child requirement into one misleading list.
                if expand:
                    expandable = [
                        r.id for r in audit.requirements
                        if not r.satisfied
                        and r.slots
                        and r.expand_qualifying_courses
                        and not _is_university_total(r.name)
                    ]
                    if expandable:
                        _apply_expansion(
                            audit,
                            _expand(
                                session, tab,
                                str(payload.get("plan_id") or ""),
                                expandable,
                            ),
                        )
                    _exclude_core_courses_from_candidates(audit)
                return audit
            finally:
                ot.close_tab(session, tab)
        except (OpenTabsError, AuditError) as exc:
            # Tabs occasionally vanish mid-run; one retry clears it.
            if attempt + 1 < attempts and ot.is_transient(exc):
                time.sleep(1.0)
                continue
            raise
    raise AuditError("Could not read the audit.")


# -- parsing ---------------------------------------------------------------


def _index(tree: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """Index req_tree nodes by id and by course_parent_id."""
    by_id: dict[str, dict] = {}
    by_parent: dict[str, dict] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            node_id = node.get("id")
            if node_id is not None:
                by_id.setdefault(str(node_id), node)
            parent = node.get("course_parent_id")
            if parent:
                by_parent[str(parent)] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree)
    return by_id, by_parent


def _course_of(node: dict) -> tuple[str, str, float | None] | None:
    """The single course a leaf choice stands for, if it is one."""
    def dig(constraint: dict):
        data = constraint.get("data") or {}
        if constraint.get("type") == "course" and data.get("course"):
            course = data["course"]
            return course.get("code"), course.get("name", ""), course.get("units")
        for nested in constraint.get("constraints") or []:
            found = dig(nested)
            if found:
                return found
        return None

    for constraint in node.get("constraints") or []:
        found = dig(constraint)
        if found and found[0]:
            return found
    return None


def _descendant_courses(node: dict, limit: int = 60) -> list[tuple[str, str, float | None]]:
    """Every course named anywhere beneath a requirement.

    This is how an open requirement - "Fulfill any 3 of the following" - turns
    into a list of candidates.
    """
    out: list[tuple[str, str, float | None]] = []
    seen: set[str] = set()

    def walk(current) -> None:
        if len(out) >= limit:
            return
        if isinstance(current, dict):
            found = _course_of(current)
            if found and found[0] and found[0] not in seen:
                seen.add(found[0])
                out.append(found)
            for choice in current.get("choices") or []:
                walk(choice)
        elif isinstance(current, list):
            for item in current:
                walk(item)

    for choice in node.get("choices") or []:
        walk(choice)
    return out


def _visible_texts(values: list[object]) -> list[str]:
    """Human-readable constraint lines, omitting hidden course references."""
    out: list[str] = []

    def add(value) -> None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = re.sub(r"\s+", " ", item or "").strip() if isinstance(item, str) else ""
            text = _HIDDEN_COURSE_RE.sub("", text).strip()
            if text and text not in out:
                out.append(text)

    for value in values:
        add(value)
    return out


def _rule_texts(node: dict) -> list[str]:
    """Human-readable constraint lines, e.g. "Fulfill any 3 of the following"."""
    values: list[object] = []
    for constraint in node.get("constraints") or []:
        if not constraint.get("is_hidden"):
            values.extend([
                constraint.get("type_string"),
                constraint.get("comment"),
                constraint.get("comments"),
            ])
    values.extend([node.get("comment"), node.get("comments")])
    return _visible_texts(values)


def _exception_overrides(audit: dict) -> dict[str, tuple[list[str], list[str]]]:
    """Approved per-plan course-set changes keyed by requirement id.

    Stellic keeps these changes outside ``req_tree``. Reading the unchanged
    tree would otherwise restore a course that an approved exception replaced.
    """
    exceptions = ((audit.get("student_audit") or {}).get("exceptions") or {})
    overrides: dict[str, tuple[list[str], list[str]]] = {}
    for requirement_id, entries in exceptions.items():
        for exception in reversed(entries or []):
            definition = exception.get("data") or {}
            constraint_data = definition.get("data") or {}
            courses: list[str] = []
            for course_set in constraint_data.get("conditional_course_sets") or []:
                if course_set.get("is_hidden") or course_set.get("is_excluded"):
                    continue
                for code in course_set.get("courses") or []:
                    normalised = _normalise_code(str(code))
                    if normalised and normalised not in courses:
                        courses.append(normalised)
            if courses:
                overrides[str(requirement_id)] = (
                    courses, _visible_texts([definition.get("type_string")])
                )
                break
    return overrides


def _constraint_courses(node: dict) -> set[str]:
    """Concrete, visible courses in a tree constraint's conditional sets."""
    courses: set[str] = set()
    for constraint in node.get("constraints") or []:
        for course_set in ((constraint.get("data") or {}).get(
            "conditional_course_sets"
        ) or []):
            if course_set.get("is_hidden") or course_set.get("is_excluded"):
                continue
            courses.update(
                _normalise_code(str(code))
                for code in course_set.get("courses") or []
            )
    return {code for code in courses if code}


def _has_unmodelled_alternative(node: dict) -> bool:
    """True when Stellic allows an approved substitute we cannot identify.

    ``OTHERS_MAY_COUNT`` is an intentionally broad Stellic marker.  It means
    that a named course (for example FYS 101) is only one possible route, so
    presenting it as *the* remaining course would be misleading.  Leave such
    a node out until Stellic has resolved the approved substitution.
    """
    for constraint in node.get("constraints") or []:
        data = constraint.get("data") or {}
        if data.get("name") == "OTHERS_MAY_COUNT":
            return True
    return False


def _needs_qualifying_course_search(node: dict) -> bool:
    """Whether a concrete elective list is hidden behind a course-set rule."""
    return any(
        constraint.get("type") == "xfromcourseset"
        for constraint in node.get("constraints") or []
    )


def _courses_named_in_rules(rules: list[str]) -> list[str]:
    """Course codes Stellic names only in a requirement's explanatory text."""
    seen: dict[str, None] = {}
    for rule in rules:
        for subject, number in _RULE_COURSE_RE.findall(rule or ""):
            seen.setdefault(f"{subject} {number}", None)
    return list(seen)


def course_types_named_in_rules(rules: list[str]) -> list[str]:
    """Self-Service Course Types named in a Stellic requirement rule."""
    seen: dict[str, None] = {}
    for rule in rules:
        for code in _COURSE_TYPE_RE.findall(rule or ""):
            seen.setdefault(code, None)
    return list(seen)


def _completed(audit: dict) -> set[str]:
    """Courses the student has passed or is currently taking.

    `course_plan_info` is keyed by course code and holds one entry per
    attempt, each carrying a status and, once graded, the grade itself. A
    failed attempt leaves the course still to take, so it does not count; a
    course under way this term does, because it will be finished before
    anything we are recommending starts.
    """
    done: set[str] = set()
    for code, attempts in (audit.get("course_plan_info") or {}).items():
        for attempt in attempts or []:
            grade = attempt.get("grade") or {}
            if grade.get("is_fail"):
                continue
            status = attempt.get("progress_status") or attempt.get("status")
            if status in ("taken", "completed", "in_progress", "registered") \
                    or attempt.get("is_registered"):
                done.add(_normalise_code(code))
                for alias_subject, alias_number in _CROSS_LISTED_ALIAS_RE.findall(
                    str(attempt.get("name") or "")
                ):
                    done.add(_normalise_code(f"{alias_subject} {alias_number}"))
                break
    return done


def _exclude_core_courses_from_candidates(audit: Audit) -> None:
    """Keep a programme's required-core courses out of elective choices."""
    core_courses = {
        course.lookup_code
        for requirement in audit.requirements
        if _CORE_RE.search(requirement.name)
        for course in requirement.needed
        if not course.candidate
    }
    for requirement in audit.requirements:
        requirement.needed = [
            course for course in requirement.needed
            if not (course.candidate and course.lookup_code in core_courses)
        ]


def parse(payload: dict) -> Audit:
    audit = payload.get("audit") or {}
    if audit.get("success") is False:
        raise AuditError(f"Stellic refused the audit: {audit.get('message')}")

    tree = audit.get("req_tree")
    data = audit.get("audit_data")
    if not tree or data is None:
        raise AuditError(
            "Audit response had no requirement tree - Stellic's API may have "
            "changed."
        )

    by_id, by_parent = _index(tree)
    exception_overrides = _exception_overrides(audit)
    # Longest first so "1284239" wins over "128" when splitting composite ids.
    ordered_ids = sorted(by_id, key=len, reverse=True)

    # requirement id -> {course code: NeededCourse}
    needed: dict[str, dict[str, NeededCourse]] = {}
    open_units: dict[str, float] = {}

    # A parent requirement repeats every card its children carry, so the same
    # slot shows up many times across audit_data. Each distinct composite id is
    # one slot; collapse them before counting anything.
    cards: dict[str, float | None] = {}
    for entry in data.values():
        for card in entry.get("cards_remaining") or []:
            composite = str(card.get("requirement_id") or "")
            if composite:
                cards.setdefault(composite, card.get("units"))

    for composite, units in cards.items():
        owner = next(
            (i for i in ordered_ids if composite.startswith(i)), None
        )

        leaf = by_parent.get(composite)
        if leaf is not None:
            found = _course_of(leaf)
            if found:
                code, title, course_units = found
                key = owner or composite
                bucket = needed.setdefault(key, {})
                if code in bucket:
                    bucket[code].slots += 1
                else:
                    bucket[code] = NeededCourse(
                        code=code, title=title, units=course_units or units,
                    )
                continue

        # No specific course: an open requirement. Expand its options once,
        # and count how many of them the student still has to pick.
        node = by_id.get(owner) if owner else None
        if node is None:
            continue
        options = _descendant_courses(node)
        if options:
            bucket = needed.setdefault(owner, {})
            for code, title, course_units in options:
                if code not in bucket:
                    bucket[code] = NeededCourse(
                        code=code, title=title, units=course_units,
                        candidate=True,
                    )
        else:
            open_units[owner] = open_units.get(owner, 0.0) + (units or 0.0)

    # An approved exception replaces the original tree constraint. It is not
    # merely another candidate list, so overwrite any inherited tree choices.
    for requirement_id, (codes, _rules) in exception_overrides.items():
        if requirement_id in by_id:
            needed[requirement_id] = {
                code: NeededCourse(code=code.replace("-", " ", 1), candidate=True)
                for code in codes
            }

    # Walk the tree in its own order so the report mirrors the Stellic page.
    requirements: list[Requirement] = []
    seen_ids: set[str] = set()

    def emit(node: dict, depth: int, hidden_by_satisfied_ancestor: bool = False) -> None:
        node_id = str(node.get("id"))
        if node_id in seen_ids:
            return
        seen_ids.add(node_id)

        entry = data.get(node_id) or {}
        summary = entry.get("units_summary") or {}
        satisfied = bool(entry.get("satisfied"))
        name = str(node.get("screen_name") or node.get("name") or "").strip()
        name = name or "Requirement"
        courses = list((needed.get(node_id) or {}).values())
        rules = exception_overrides.get(node_id, ([], _rule_texts(node)))[1]
        # A choice branch without audit data is explanatory only.  In
        # particular, the Capstone tree names every school alternative, while
        # only the student's own branch receives an audit-data entry.  Do not
        # turn the names in inactive branches into course recommendations.
        if entry:
            known_courses = {course.code for course in courses}
            for code in _courses_named_in_rules(rules):
                if code not in known_courses:
                    known_courses.add(code)
                    courses.append(NeededCourse(code=code, candidate=True))
        spare = open_units.get(node_id, 0.0)
        has_unmodelled_alternative = _has_unmodelled_alternative(node)
        # The University Curriculum's capstone umbrella is a menu of
        # college-specific alternatives.  If Stellic does not attach an
        # actionable course to the student's branch, we cannot reliably pick
        # one from that menu.  Do not show every college's capstone as though
        # it applied to this student.
        unresolved_capstone = (
            "integrative capstone experience" in name.lower()
            and not courses
            and not spare
        )
        # A satisfied requirement can still inherit a descendant's remaining
        # cards in the audit tree. Its choice list is explanatory only, not a
        # recommendation, so never surface those alternate courses.
        review = None
        if unresolved_capstone:
            review = (
                "Stellic does not identify a specific course for this "
                "Integrative Capstone requirement. Review with adviser."
            )
        if (
            satisfied
            or _is_university_total(name)
            or has_unmodelled_alternative
        ):
            courses = []
            spare = 0.0

        if not (
            hidden_by_satisfied_ancestor
            or has_unmodelled_alternative
        ) and (
            courses or spare or (entry and not entry.get("satisfied", True))
        ):
            total = summary.get("total")
            done = sum(
                summary.get(k) or 0
                for k in ("taken", "registered", "planned", "in_progress")
            )
            courses.sort(key=lambda c: (c.candidate, c.code))
            requirements.append(
                Requirement(
                    id=node_id,
                    name=name,
                    satisfied=satisfied,
                    units_total=total,
                    units_remaining=(
                        max(0.0, total - done) if isinstance(total, (int, float))
                        else None
                    ),
                    needed=courses,
                    rules=rules,
                    open_units=spare,
                    slots=int(entry.get("num_remaining") or 0),
                    depth=depth,
                    expand_qualifying_courses=_needs_qualifying_course_search(node),
                    review=review,
                )
            )

        for child in node.get("choices") or []:
            if isinstance(child, dict) and child.get("choices") is not None:
                emit(
                    child,
                    depth + 1,
                    hidden_by_satisfied_ancestor or satisfied
                    or has_unmodelled_alternative,
                )

    for program in tree.get("programs") or []:
        emit(program, 0)

    # The profile's `degrees` comes back empty, so take the declared programs
    # from the audit itself. University-wide blocks ("Total 122 Credits for
    # University Degree Completion") are scaffolding, not programs.
    declared = [
        str(program.get("screen_name") or "").strip()
        for program in tree.get("programs") or []
        if not program.get("is_uni_req")
    ]
    declared = [name for name in declared if name]
    if not declared:
        declared = [p for p in ((payload.get("student") or {}).get("programs") or []) if p]

    completed = _completed(audit)
    prerequisite_completed = set(completed)
    for requirement_id, (replacement_codes, _rules) in exception_overrides.items():
        original_codes = _constraint_courses(by_id.get(requirement_id) or {})
        replacement_codes = set(replacement_codes)
        removed = original_codes - replacement_codes
        added = replacement_codes - original_codes
        # Only a one-for-one approved change establishes a prerequisite
        # substitution. Broader exceptions need an adviser's judgment.
        if len(removed) == len(added) == 1 and added <= completed:
            prerequisite_completed.update(removed)
    # A parent-level rule can name a course that is already complete while a
    # different child keeps the larger University Curriculum block unmet.
    # Stellic's completed record controls: do not surface that course again.
    for requirement in requirements:
        requirement.needed = [
            course for course in requirement.needed
            if course.lookup_code not in completed
        ]

    student = payload.get("student") or {}
    result = Audit(
        student_name=student.get("name") or student.get("username") or "Student",
        username=student.get("username") or "",
        level=student.get("level") or "",
        programs=declared,
        requirements=requirements,
        online_only=any(_ONLINE_RE.search(name) for name in declared),
        completed=completed,
        prerequisite_completed=prerequisite_completed,
    )
    _exclude_core_courses_from_candidates(result)
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 stellic.py <stellic-audit-url|username>")
    result = fetch(sys.argv[1])
    print(f"{result.student_name} ({result.level})")
    for program in result.programs:
        print(f"  program: {program}")
    if result.online_only:
        print("  online program - only online sections are relevant")
    print(f"\n{len(result.requirements)} unmet requirement(s):\n")
    for req in result.requirements:
        pad = "  " * req.depth
        remaining = (
            f" - {req.units_remaining:g} of {req.units_total:g} units left"
            if req.units_remaining else ""
        )
        print(f"{pad}{req.name}{remaining}")
        for rule in req.rules:
            print(f"{pad}   rule: {rule}")
        if req.open_units:
            print(f"{pad}   {req.open_units:g} open units (any course)")
        pool = [c for c in req.needed if c.candidate]
        if pool and req.slots:
            print(f"{pad}   choose {req.slots} of the following:")
        for course in req.needed:
            mark = "option" if course.candidate else "required"
            print(f"{pad}   {course.code:<10} {course.title[:40]:<42} {mark}")
    print(f"\ndistinct courses to check: {len(result.all_codes)}")
