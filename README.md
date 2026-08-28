# AdvisingBot

Paste a Stellic audit link. Get back the courses that student still needs to
graduate, grouped exactly the way Stellic groups them, with the ones that have
**open seats** in Self-Service marked online, on-ground, or both — plus a plain
text version you can copy into notes or an email.

```
Elijah Alston — Junior
Courses still needed with open seats — Fall 2026

Computer Information Systems Core (3 more courses needed)
  CIS 440   IT Project Management                    17 seats  [on-ground]

Business Essentials (1 more course needed)
  EC 272    Advanced Applied Statistics               6 seats  [on-ground]
```

## This runs on your machine only

Not a hosted app, and it must not become one:

- Stellic is behind Microsoft SSO. The only way in is your own signed-in
  browser, borrowed through OpenTabs, which listens on `127.0.0.1`.
- A degree audit is a protected education record under FERPA.

So the server binds the loopback interface, audits are held in memory for a
single request and never written to disk, and only public course/seat data is
cached. Don't expose the port, and don't deploy this.

## Requirements

- Python 3.11+, **no packages to install** — standard library only.
- **OpenTabs** running, with its Chrome extension connected.
- You, signed in to Stellic in that Chrome.

## Use it

```bash
python3 app.py
```

Open <http://127.0.0.1:8765>, paste an audit URL, press Check. A bare username
works too.

The first check for a student takes ~15 seconds: it opens the audit in a
background tab, reads it, then looks up each course in Self-Service. Later
checks that day are much faster — see caching below.

Three controls: **Semester** (defaults to the term in session), **Delivery**
(online only / on-ground only / either), and **Show** (only courses with open
seats, or everything still needed). The copy box tracks whatever you've
filtered to.

## Checking it works

```bash
python3 opentabs.py                  # "connected to opentabs v0.0.109"
python3 stellic.py <username>        # requirements + courses still needed
python3 seatcache.py BAN-610 CIS-440 # run twice: live, then all cached
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
| `opentabs.py` | Minimal MCP-over-HTTP client for OpenTabs |
| `seatcache.py` | Day-old-is-fine cache in front of Self-Service |
| `selfservice.py` | Client for the Self-Service course API |
| `static/index.html` | The interface |
| `cache/seats.json` | Cached seat data — gitignored |

### Reading the audit

Stellic's UI is a React tree that changes shape often, so we don't scrape it.
Instead we open the audit and call Stellic's own API from inside the
authenticated page:

```
POST /planner/getstudentprofile/   form-encoded  -> default_plan_id
POST /planner/getauditinfo/        JSON          -> req_tree + audit_data
```

Both responses carry a `)]}',` XSSI prefix that has to come off first, and the
result is pulled back in slices because script results truncate around 50 KB.

**How the audit says "still to take".** `req_tree` is the requirement
structure; `audit_data` is keyed by requirement id and carries
`cards_remaining` — one entry per unfilled slot, identified by a *composite* of
the sub-requirement id and a zero-padded course-parent id. Each card resolves
one of two ways:

- the composite matches a leaf's `course_parent_id` → **a specific course is
  missing** (CIS 440)
- it doesn't → the card belongs to an open requirement ("Fulfill any 3 of the
  following") → we expand that requirement's own options into **candidates**,
  tagged *choose from* in the UI

That second path is what turns "any 3 CIS electives" into a list you can act
on. Candidates are labelled as such: Stellic decides what actually counts.

Requirements stated as attributes rather than courses ("a course with attribute
UC_NS") can't be expanded from the audit alone. Those keep their wording and
show as a rule, so a requirement never looks emptier than it is.

Parent requirements repeat every card their children carry — the same slot can
appear a dozen times across `audit_data` — so cards are collapsed by composite
id, and the "N more courses needed" count comes from Stellic's own
`num_remaining` rather than from counting cards.

### Caching

Seat counts move slowly enough that checking a course twice in a day tells you
nothing new, and every lookup is load on a system we don't own. So each course
records the date it was last checked and is served from `cache/seats.json` if
that date is today.

The cache is keyed by course, not by student, which is what makes it pay: your
second and third advisee of the morning mostly share courses with the first.
Each result shows how many courses were looked up live versus already checked.

A stale course costs one request. The subject index and term list refresh once
a day, in one request each.

### Two things about Self-Service data

**We ignore its "Open Sections Only" filter.** It drops sections that genuinely
have seats, so we pull every section and check the counts ourselves.

**Some sections publish no seat count** (`0 / 0 / 0`, mostly distance-ed
sections). They're never counted as open, but they aren't silently dropped
either.

## If it breaks

| Message | Cause |
| --- | --- |
| `No OpenTabs token found` | OpenTabs isn't configured. Set `OPENTABS_TOKEN`, or check `~/.claude.json`. |
| `Could not reach OpenTabs` | The OpenTabs app isn't running. |
| `Stellic redirected to the sign-in page` | Sign in to Stellic in Chrome, then retry. |
| `Stellic did not return an audit` | Its internal API changed — see `stellic.py`. |
| `Could not find the antiforgery token` | Self-Service's HTML changed — see `selfservice.py`. |

Both Stellic and Self-Service are vendor products with undocumented internals
and can change without notice. All knowledge of each is confined to its own
module, and both fail loudly rather than returning a quietly empty list.

## Confidentiality

This tool reads degree audits through your authenticated Stellic session. Use
it only for students you are authorized to advise. It performs no action on the
student's record and writes no audit data to disk. Seat information is a
snapshot and may be up to a day old; eligibility, prerequisites, and whether a
course actually satisfies a requirement are determined by Quinnipiac
University. Verify in Self-Service and Stellic before advising a student to
register.
