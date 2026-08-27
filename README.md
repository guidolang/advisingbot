# AdvisingBot

Finds Quinnipiac courses that still have **open seats**, so you don't have to
click through Self-Service one subject at a time.

Out of the box it checks, for the current semester:

- **BAN** — Business Analytics, undergraduate *and* graduate
- **CIS** — Computer Information Systems, undergraduate
- **Every elective** named by the AI & Business Analytics and CIS programs —
  currently 35 courses across AC, BAN, CIS, FIN, HM, IB, MBA, MG, MK and SB,
  read straight out of the catalog

...and prints every section with a seat left, including how many, when it
meets, where, and who teaches it.

There are two scripts, because they run on very different clocks:

| | | |
| --- | --- | --- |
| `advisingbot.py` | checks seats | run it whenever you like |
| `build_electives.py` | reads the catalog | run it once a year, when a new catalog goes live |

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

Two files feed the check, and they have different jobs.

### `watchlist.toml` — hand-maintained

Whole subjects you want swept, plus any electives the catalog misses.

```toml
[[watch]]
label = "Business Analytics (all levels)"
subject = "BAN"
levels = ["UG", "GR"]     # UG, GR, DL, LW — omit for all levels
courses = ["220", "330"]  # omit to take the whole subject
exclude = ["484"]         # optional

[manual]
courses = ["MK 630", "HM 610"]
```

`python3 advisingbot.py --list-subjects` prints every valid subject code.

### `electives.toml` — generated, don't edit

Built by `build_electives.py` from these five curriculum pages:

- [BS Applied AI & Business Analytics](https://catalog.qu.edu/business/computer-information-systems/bs-applied-ai-business-analytics/#curriculumtext)
- [Applied AI & Business Analytics minor](https://catalog.qu.edu/business/computer-information-systems/applied-ai-business-analytics-minor/#curriculumtext)
- [BS Computer Information Systems](https://catalog.qu.edu/business/computer-information-systems/computer-information-systems-bs/#curriculumtext)
- [Computer Information Systems minor](https://catalog.qu.edu/business/computer-information-systems/computer-information-systems-minor/#curriculumtext)
- [MS Applied AI & Business Analytics](https://catalog.qu.edu/graduate-studies/business/business-applied-ai-business-analytics-ms/#curriculumtext)

```bash
python3 build_electives.py            # rewrite electives.toml
python3 build_electives.py --diff     # what changed since the current file
python3 build_electives.py --dry-run  # print it instead of writing
```

Commit the result. `advisingbot.py` picks it up automatically, and warns if
it's more than a year old.

### When the catalog states a rule instead of a list

Some elective requirements can't be reduced to courses:

```
[Computer Information Systems minor]     Choose four CIS electives
[BS Computer Information Systems]        Select any 200 level or above CIS class
[Applied AI & Business Analytics minor]  Any BAN course
[MS Applied AI & Business Analytics]     Take one 600-level MK course
```

`build_electives.py` prints these at the end of every run and writes them into
`electives.toml` as comments, so they can't quietly go missing. Cover them one
of two ways:

- **A subject watch**, when the rule is broad. The `BAN` and `CIS` blocks in
  `watchlist.toml` already answer the first three above.
- **`[manual]`**, when it's a handful of courses — `"Take one 600-level MK
  course"` is best handled by naming the ones you'd actually approve.

Anything in `[manual]` that's already in `electives.toml` is ignored, so
duplicates are harmless.

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

**A heading that isn't there doesn't mean "not checked".** Watches that
found nothing are listed under *"Checked, nothing open"* at the end, so a
full-or-not-offered elective can't be mistaken for one that was skipped.

**Some sections publish no seat count at all** (Self-Service shows
`0 / 0 / 0`, mostly distance-ed `-DE` sections). Their numbers mean nothing,
so they aren't counted as open — but they're listed at the bottom of every
report so you can check them by hand rather than have them vanish silently.

## Files

| File | What it is |
| --- | --- |
| `advisingbot.py` | The seat checker — run this |
| `selfservice.py` | Client for the Self-Service JSON API |
| `watchlist.toml` | Subjects to sweep + manual electives — **edit this** |
| `build_electives.py` | Rebuilds the elective list from the catalog — run yearly |
| `catalog.py` | Parser for the catalog's curriculum tables |
| `electives.toml` | Electives harvested from the catalog — generated |
| `.last_run.json` | Previous run, written automatically, used by `--diff` |

## If it breaks

Self-Service is a vendor product (Ellucian Colleague) and can change without
notice. Two failure modes to expect:

- *"Could not find the antiforgery token"* — the search page's HTML changed.
- *"Expected JSON from ..."* — an endpoint changed shape.

In both cases the fix is to open the page in a browser, watch the network tab
while clicking the filters, and update `selfservice.py` to match.

The catalog is a CourseLeaf site and is far more stable, but if
`build_electives.py` reports *"No elective courses found at all"*, its table
markup has changed — check `catalog.py` against a curriculum page.
