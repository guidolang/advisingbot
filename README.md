# AdvisingBot

Paste a Stellic audit link. Get back one list of the courses a student still
needs, can actually take, and that have **open seats** in Self-Service now —
plus a plain-text version to paste into notes or an email.

```
CIS 440: IT Project Management (In-person: 17 available seats)
EC 272: Advanced Applied Statistics (In-person: 6 available seats)
MU 130: Understanding Music (Online: 1 available seat, In-person: 42 available)
```

Requirements appear in the same nested order as the Stellic audit. Courses sit
under every unmet requirement they can count toward, so one course may appear
in more than one branch. Each branch retains the audit's rule or remaining
slot count. Courses ruled out are not discarded — they collapse into a "set
aside" block with the reason.

## Requirements

- Python 3.11+, standard library only — nothing to install.
- **OpenTabs** running, Chrome extension connected.
- You, signed in to Stellic in that Chrome.

## Use it

Double-click `bin/AdvisingBot.command`. It starts the server and opens the app.
Paste an audit URL (a bare username also works) and press Check.

```bash
bin/AdvisingBot.command --restart   # stop whatever holds the port, start fresh
bin/AdvisingBot.command --stop
python3 app.py                      # start by hand
```

Restart after changing code — a running server keeps serving what it started
with. A first check takes a minute or two on a cold cache; later checks that
day hit the cache and take seconds.

Smoke tests:

```bash
python3 opentabs.py                  # "connected to opentabs v0.0.109"
python3 stellic.py <username>        # requirements + courses still needed
python3 seatcache.py BAN-610 CIS-440 # run twice: live, then cached
```

## How it works

```
you ─► app.py ─► stellic.py ─► opentabs.py ─► OpenTabs ─► your Chrome ─► Stellic
          └────► seatcache.py ─► selfservice.py ────────────────────► Self-Service
```

| File | Purpose |
| --- | --- |
| `app.py` | Local server + JSON API. Loopback only. |
| `stellic.py` | Fetches and parses a degree audit |
| `eligibility.py` | Prerequisites and programme restrictions |
| `opentabs.py` | Minimal MCP-over-HTTP client for OpenTabs |
| `seatcache.py` | Day-old-is-fine cache in front of Self-Service |
| `selfservice.py` | Client for the Self-Service course API |
| `static/index.html` | The interface |
| `cache/seats.json` | Cached seat data — gitignored |

Stellic and Self-Service are vendor products with undocumented internals that
can change without notice. Knowledge of each is confined to its own module, and
both fail loudly rather than returning a quietly empty list.

### Reading the audit

Stellic's UI is a React tree that changes shape often, so we don't scrape it.
We open the audit and call Stellic's own API from inside the authenticated
page:

```
POST /planner/getstudentprofile/   form-encoded  -> default_plan_id
POST /planner/getauditinfo/        JSON          -> req_tree + audit_data
```

Both responses carry a `)]}',` XSSI prefix that must come off first, and the
result is pulled back in slices because script results truncate around 50 KB.

**How the audit says "still to take".** `req_tree` is the requirement
structure; `audit_data` is keyed by requirement id and carries
`cards_remaining` — one entry per unfilled slot, identified by a *composite* of
the sub-requirement id and a zero-padded course-parent id:

- composite matches a leaf's `course_parent_id` → a **specific course** is
  missing (CIS 440)
- it doesn't → the card belongs to an **open requirement** ("any 3 of the
  following", "a course carrying UC_NS")

Parent requirements repeat every card their children carry — the same slot can
appear a dozen times — so cards are collapsed by composite id, and the
"N more needed" count comes from Stellic's `num_remaining`, not from counting
cards.

### Expanding open requirements

Resolved with the call Stellic's own *"Search qualifying courses"* button makes:

```
GET /planner/searchcourses/?counts_for=<requirement_id>&plan_id=<uuid>
    &brief_mode=true&size=20&from=<n>&sort_by=code
```

This is the only way to resolve requirements written as *attributes* rather
than course lists. The endpoint caps a page at 20 rows whatever `size` asks
for, so walk it until `last_page`. When Stellic instead writes an explicit
course list in the requirement text, AdvisingBot reads those course codes and
checks them through the same eligibility and seat pipeline.

Expansion matters — one junior went from 3 courses with open seats to 53. Two
limits keep it sane: a requirement is not expanded past 240 courses, and one
matching more than 200 is skipped, since "any of 359 courses counts" is a fact
about the catalogue, not advice.

### Deciding who may take what

Open seats are not eligibility. Three checks live in `eligibility.py`:

**Already taken.** `course_plan_info` (from `getauditinfo`) is keyed by course
code, one entry per attempt. Done if a non-failing grade; under way if
registered now — a course finishing this term finishes before anything we
recommend starts. A failed attempt leaves the course still to take.

**Prerequisites.** Self-Service publishes requisites twice, and neither half is
usable alone, so they're joined on an internal requirement code: `Requisites`
says whether a rule is required and must be finished first;
`CourseFullModels[].CourseRequisites` carries the printed sentence. Only a rule
that is *required* **and** *must be completed first* can block. Corequisites and
"Recommended" rules never block.

Sentences are typed by hand, so the parser reads the shapes it recognises and
refuses the rest — ~1,340 of 1,765 requisites parse, and an unparsed requisite
never blocks anything. Within one requisite, `or` anywhere makes the whole list
alternatives; its absence makes them all required.

```
Take CIS*101.                          -> CIS 101 required
Take MA*170, MA*176 or MA*275.         -> any one of the three
Sophomore status or permission of the instructor.   -> not parsed
```

**Written for another programme.** A description sentence rules a course out
only if it names an *audience* (`designed for`, `restricted to`, `open only
to`) **and** that audience names a programme (`majors`, `track`, `School of`).
Both halves are needed — "limited to 12 students" and "students in their
sophomore year" are not programmes. Negations invert cleanly. School of
Business subjects skip this check entirely.

Everything errs the same way: a course is dropped only on clear evidence.
Hiding a course an adviser should have seen is worse than listing one they then
rule out.

### Caching

Each course records the date it was last checked and is served from
`cache/seats.json` if that date is today. Keyed by **course, not student** —
which is what makes it pay, since your second and third advisee mostly share
courses with the first.

The subject index and term list refresh once a day. The index carries each
course's description and requisite rules alongside its section ids, so
eligibility can be judged even for a course with no sections to look up.

### Two Self-Service quirks

- **Its "Open Sections Only" filter is wrong** — it drops sections that
  genuinely have seats. Pull every section and check counts yourself.
- **Some sections publish no seat count** (`0 / 0 / 0`, mostly distance-ed).
  Never counted as open, never silently dropped.

## If it breaks

| Message | Cause |
| --- | --- |
| `No OpenTabs token found` | OpenTabs isn't configured. Set `OPENTABS_TOKEN`, or check `~/.claude.json`. |
| `Could not reach OpenTabs` | The OpenTabs app isn't running. |
| `Stellic redirected to the sign-in page` | Sign in to Stellic in Chrome, then retry. |
| `Stellic did not return an audit` | Its internal API changed — see `stellic.py`. |
| `Could not find the antiforgery token` | Self-Service's HTML changed — see `selfservice.py`. |

## Data and confidentiality

A degree audit is a FERPA-protected education record. Use this only for
students you are authorized to advise. **Not a hosted app, and it must not
become one.**

- The server binds `127.0.0.1` only. Don't expose the port; don't deploy this.
- The audit is held in memory for one request and **never written to disk**.
- `cache/seats.json` holds public catalogue data keyed by course — nothing
  student-specific reaches it.
- **No LLM, analytics, or third-party service.** Outbound connections total
  three: `qu.stellic.com` (from your own Chrome tab), `selfservice.quinnipiac.edu`
  (course lookups — no student identifier is ever in a request), and
  `127.0.0.1:9515` (OpenTabs). All parsing and eligibility logic runs locally.

Chrome and OpenTabs sit inside the trust boundary, since the audit passes
through both. A source review of OpenTabs 0.0.109 found that browser tool calls
carry no telemetry, its local audit log records only tool name and duration,
and its extension connects only to `localhost`. OpenTabs' optional product
telemetry contains no student data; disable it with `opentabs telemetry
disable`. Leave `OPENTABS_URL` and `OPENTABS_TOKEN` unset — both can move the
loopback assumption.

Listed courses are candidates, not clearances. Eligibility, prerequisites, and
what actually satisfies a requirement are determined by Quinnipiac. Verify in
Stellic and Self-Service before advising a student to register.
