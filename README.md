# AdvisingBot

Finds Quinnipiac courses that still have **open seats**, so you don't have to
click through Self-Service one subject at a time.

Out of the box it checks, for the current semester:

- **BAN** — Business Analytics, undergraduate *and* graduate
- **CIS** — Computer Information Systems, undergraduate
- **MK / MG / IB** — Marketing, Management, International Business

...and prints every section with a seat left, including how many, when it
meets, where, and who teaches it.

## Requirements

Python 3.11 or newer. **No packages to install** — standard library only.

## Use it

```bash
python3 advisingbot.py
```

That's the whole thing. It takes about 25 seconds.

```
Open seats - Fall 2026 (26/FA)
==============================

Business Analytics (all levels)
-------------------------------

  BAN-615  Predictive Business Analytics
    BAN*615-01       25 of 35 open
                     M/W 11:00 AM-12:15 PM (Lecture) | Mount Carmel | Friday, C
...
26 section(s) with open seats, 255 seat(s) total, across 23 course(s).
```

### Other things it does

```bash
python3 advisingbot.py --term "Spring 2027"   # a specific term
python3 advisingbot.py --term next            # the upcoming term
python3 advisingbot.py --term all             # every term on offer
python3 advisingbot.py --list-terms           # what terms exist right now
python3 advisingbot.py --list-subjects        # every subject code + count

python3 advisingbot.py --subject CIS --levels UG   # one-off, ignores watchlist
python3 advisingbot.py --include-closed            # show full sections too

python3 advisingbot.py --csv seats.csv --json seats.json
python3 advisingbot.py --diff                 # what changed since last run
```

`--diff` is the one to run repeatedly during registration season — it reports
seats that just opened up, filled, or moved:

```
Changes since 2026-08-26T09:15:02
----------------------------------------
  NEWLY OPEN   MG-312 MG*312-01  (2 seat(s))
  DOWN         BAN-615 BAN*615-01  25 -> 21
  NOW FULL     IB-335 IB*335-01
```

## Choosing what to watch

Edit [`watchlist.toml`](watchlist.toml). Each block is one search:

```toml
[[watch]]
label = "Marketing (selected)"
subject = "MK"
levels = ["UG", "GR"]     # UG, GR, DL, LW — omit for all levels
courses = ["101", "310"]  # omit to take the whole subject
exclude = ["484"]         # optional
```

The MK / MG / IB blocks ship as whole-subject watches so the first run shows
you everything. Once you know which courses you actually advise into,
uncomment `courses` and list them.

`python3 advisingbot.py --list-subjects` prints every valid subject code.

## How it works

Self-Service is a single-page app, and its UI actions map onto three JSON
endpoints. AdvisingBot calls those directly instead of driving a browser —
same data, no clicking, and it can run unattended:

| What you'd click | What the script calls |
| --- | --- |
| Opening the course search page | `GET /student/Courses/Search` (session cookie + CSRF token) |
| Picking a subject, term, level | `POST /student/Courses/PostSearchCriteria` |
| "View Available Sections for BAN-610" | `POST /student/Courses/Sections` |

The course catalog is public, so **no login, password, or student data is
involved anywhere** — the script sees exactly what a signed-out visitor sees.
It also paces itself (0.4s between requests, tunable with `--delay`).

### Two things worth knowing about the data

**We do not use Self-Service's "Open Sections Only" checkbox.** It drops
sections that genuinely have seats — as of this writing `CIS*101-01` has a
free seat but that filter excludes it. AdvisingBot pulls every section in the
term and checks the seat counts itself, which is why it finds a few sections
the website's own filter hides.

**Some sections publish no seat count at all** (Self-Service shows
`0 / 0 / 0`, mostly distance-ed `-DE` sections). Their numbers mean nothing,
so they aren't counted as open — but they're listed at the bottom of every
report so you can check them by hand rather than have them vanish silently.

## Files

| File | What it is |
| --- | --- |
| `advisingbot.py` | The command-line tool — run this |
| `selfservice.py` | Client for the Self-Service JSON API |
| `watchlist.toml` | Which subjects/courses to check — edit this |
| `.last_run.json` | Previous run, written automatically, used by `--diff` |

## If it breaks

Self-Service is a vendor product (Ellucian Colleague) and can change without
notice. Two failure modes to expect:

- *"Could not find the antiforgery token"* — the search page's HTML changed.
- *"Expected JSON from ..."* — an endpoint changed shape.

In both cases the fix is to open the page in a browser, watch the network tab
while clicking the filters, and update `selfservice.py` to match.
