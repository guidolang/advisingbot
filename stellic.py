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
_EXPAND_PAGES = 12      # 12 pages x 20 = 240 courses per requirement
_EXPAND_MAX_MATCHES = 200   # above this a requirement is a free-credit bucket
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

    @property
    def is_open_ended(self) -> bool:
        return bool(self.open_units) and not self.needed


# Quinnipiac names its distance programs "... Online - MS". A student in one
# should not be shown classroom sections they cannot attend.
_ONLINE_RE = re.compile(r"\bonline\b", re.I)


def _normalise_code(code: str) -> str:
    """Stellic writes "CIS 101"; Self-Service wants "CIS-101"."""
    parts = (code or "").split()
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

    @property
    def all_codes(self) -> list[str]:
        seen: dict[str, None] = {}
        for req in self.requirements:
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

                # Requirements written as a rule rather than a course list are
                # resolved with the same call Stellic's own UI makes.
                if expand:
                    open_ended = [
                        r.id for r in audit.requirements if r.is_open_ended
                    ]
                    if open_ended:
                        _apply_expansion(
                            audit,
                            _expand(
                                session, tab,
                                str(payload.get("plan_id") or ""),
                                open_ended,
                            ),
                        )
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


def _rule_texts(node: dict) -> list[str]:
    """Human-readable constraint lines, e.g. "Fulfill any 3 of the following"."""
    out = []
    for constraint in node.get("constraints") or []:
        text = re.sub(r"\s+", " ", constraint.get("type_string") or "").strip()
        if text and not constraint.get("is_hidden") and text not in out:
            out.append(text)
    return out


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
            if status in ("taken", "in_progress") or attempt.get("is_registered"):
                done.add(_normalise_code(code))
                break
    return done


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

    # Walk the tree in its own order so the report mirrors the Stellic page.
    requirements: list[Requirement] = []
    seen_ids: set[str] = set()

    def emit(node: dict, depth: int) -> None:
        node_id = str(node.get("id"))
        if node_id in seen_ids:
            return
        seen_ids.add(node_id)

        entry = data.get(node_id) or {}
        summary = entry.get("units_summary") or {}
        courses = list((needed.get(node_id) or {}).values())
        spare = open_units.get(node_id, 0.0)

        if courses or spare or (entry and not entry.get("satisfied", True)):
            total = summary.get("total")
            done = sum(
                summary.get(k) or 0
                for k in ("taken", "registered", "planned", "in_progress")
            )
            courses.sort(key=lambda c: (c.candidate, c.code))
            requirements.append(
                Requirement(
                    id=node_id,
                    name=str(node.get("screen_name") or node.get("name") or "")
                    .strip() or "Requirement",
                    satisfied=bool(entry.get("satisfied")),
                    units_total=total,
                    units_remaining=(
                        max(0.0, total - done) if isinstance(total, (int, float))
                        else None
                    ),
                    needed=courses,
                    rules=_rule_texts(node),
                    open_units=spare,
                    slots=int(entry.get("num_remaining") or 0),
                    depth=depth,
                )
            )

        for child in node.get("choices") or []:
            if isinstance(child, dict) and child.get("choices") is not None:
                emit(child, depth + 1)

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

    student = payload.get("student") or {}
    return Audit(
        student_name=student.get("name") or student.get("username") or "Student",
        username=student.get("username") or "",
        level=student.get("level") or "",
        programs=declared,
        requirements=requirements,
        online_only=any(_ONLINE_RE.search(name) for name in declared),
        completed=_completed(audit),
    )


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
