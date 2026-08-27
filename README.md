# AdvisingBot

Finds Quinnipiac courses that still have **open seats**, so nobody has to click
through Self-Service one subject at a time.

It is two things sharing one engine:

| | | |
| --- | --- | --- |
| **A web page** | Pick a semester, a program, and a delivery mode; see every course with a seat left, grouped the way the Catalog groups it | for faculty and students |
| **A command-line tool** | Sweep a watchlist of subjects and print what's open | for you |

Both read the same public sources, and neither ever logs in.

## The three scripts

They run on very different clocks, which is why they're separate:

| Script | What it does | How often |
| --- | --- | --- |
| `build_catalog.py` | Reads all 40 School of Business curricula from the Catalog → `programs.json` | once a year, when a new Catalog edition goes live |
| `build_site.py` | Combines `programs.json` with live Self-Service seat data → `docs/data.json` | daily, from GitHub Actions |
| `advisingbot.py` | Prints open seats for your own watchlist | whenever you like |

## Requirements

Python 3.11 or newer. **No packages to install** — standard library only.

## The web page

`docs/index.html` is a single static file that reads `docs/data.json`. No
server, no framework, no build step.

### Publishing it

1. **Settings → Pages → Source: GitHub Actions.**
   On a private repo this needs a paid plan; otherwise make the repo public —
   it contains no secrets and no student data.
2. **Actions → "Refresh open seats" → Run workflow.**

That collects fresh data, commits `docs/data.json`, and publishes the page.

### Making it automatic

The workflow is manual on purpose right now. To run it every morning,
uncomment the `schedule:` block in
[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml):

```yaml
schedule:
  - cron: "0 10 * * *"   # 6:00 a.m. Eastern during EDT
```

GitHub cron is always UTC and ignores daylight saving, so one entry can't be
6:00 a.m. year-round. The file explains the two-entry workaround.

### What it shows

- **Semester** — every term Self-Service currently offers, defaulting to the
  one in session.
- **Program** — all 40 SoB programs: bachelor's, minors, certificates,
  master's, dual degrees.
- **Delivery** — online only, on-ground only, or either. Hybrid sections
  (part web, part classroom) show up under both.

Courses appear under the Catalog's own group names — *Accounting Core*,
*BAN Elective Courses*, *Concentration in Finance* — rather than categories
we invent. A course with several open sections collapses to one line and
expands on click, so a long program stays scannable. Selections are
remembered between visits.

## The command-line tool

```bash
python3 advisingbot.py
```

```
Open seats - Fall 2026 (26/FA)
==============================

Business Analytics (all levels)
-------------------------------

  BAN-615  Predictive Business Analytics
    BAN*615-01       25 of 35 open
                     M/W 11:00 AM-12:15 PM (Lecture) | Mount Carmel | Friday, C
...
15 section(s) with open seats, 190 seat(s) total, across 14 course(s).
```

```bash
python3 advisingbot.py --term "Spring 2027"    # a specific term
python3 advisingbot.py --term next             # the upcoming term
python3 advisingbot.py --list-terms            # what terms exist right now
python3 advisingbot.py --list-subjects         # every subject code + count
python3 advisingbot.py --subject CIS --levels UG   # one-off, ignores watchlist
python3 advisingbot.py --include-closed        # show full sections too
python3 advisingbot.py --csv seats.csv --json seats.json
python3 advisingbot.py --diff                  # what changed since last run
```

`--diff` is the one to run repeatedly during registration season:

```
Changes since 2026-08-26T09:15:02
----------------------------------------
  NEWLY OPEN   MG-312 MG*312-01  (2 seat(s))
  DOWN         BAN-615 BAN*615-01  25 -> 21
  NOW FULL     IB-335 IB*335-01
```

## Configuration

### `watchlist.toml` — hand-maintained, drives the CLI only

```toml
[[watch]]                       # whole subjects to sweep
label = "Business Analytics (all levels)"
subject = "BAN"
levels = ["UG", "GR"]           # UG, GR, DL, LW — omit for all
courses = ["220", "330"]        # omit to take the whole subject
exclude = ["484"]

[electives]                     # draw electives from these programs
programs = ["Applied AI and Business Analytics",
            "Computer Information Systems"]

[manual]                        # electives the Catalog states as prose
courses = ["MK 630", "HM 610"]
```

Without `[electives].programs`, "electives" would mean every elective in the
School of Business. The web page is not filtered this way — it shows each
program's own electives.

### `programs.json` — generated, don't edit

```bash
python3 build_catalog.py            # rewrite it
python3 build_catalog.py --diff     # what changed since the current file
python3 build_catalog.py --dry-run  # print a summary, write nothing
```

Commit the result. Everything downstream reads this file instead of hitting
the Catalog, and `advisingbot.py` warns if it's over a year old.

### When the Catalog states a rule instead of a list

40 requirements across the School of Business can't be reduced to courses:

```
[Minor in Computer Information Systems]  Choose four CIS electives
[BS Computer Information Systems]        Select any 200 level or above CIS class
[MS Applied AI & Business Analytics]     Take one 600-level MK course
[Full-Time MBA]                          Take two other 600-level FIN electives
```

These are printed after every `build_catalog.py` run, kept on their group in
`programs.json`, and shown on the web page in a callout marked *"stated as a
rule, not a course list"* — so a student can see that a requirement exists
even where no course list does. For the CLI, cover them with a subject watch
or by naming courses under `[manual]`.

## How it works

Self-Service is a single-page app, and its UI actions map onto three JSON
endpoints. AdvisingBot calls those directly instead of driving a browser:

| What you'd click | What the script calls |
| --- | --- |
| Opening the course search page | `GET /student/Courses/Search` (session cookie + CSRF token) |
| Picking a subject, term, level | `POST /student/Courses/PostSearchCriteria` |
| "View Available Sections for BAN-610" | `POST /student/Courses/Sections` |

The Catalog is a CourseLeaf site whose curriculum tables are read directly.
Both sources are public, so **no login, password, or student data is involved
anywhere**.

### Keeping the load off Self-Service

A full refresh of all 40 programs is **183 requests in about 75 seconds**,
once a day. Four things get it there:

- **One search covers every subject.** The endpoint accepts a list, so 43
  subjects cost one request rather than 43.
- **One search covers every term.** Omitting the term filter makes sections
  come back bucketed by term, so terms are separated afterwards instead of
  re-running the whole scrape per semester.
- **Courses are deduplicated first.** SB 488 sits in a dozen curricula and is
  fetched once.
- **Unoffered courses are skipped.** The search says which courses have no
  sections at all — 137 of 318 — and those cost nothing.

That leaves one request per offered course, paced 0.4s apart. Requests are
serial on purpose: for a nightly job there's nothing to gain from
parallelism, and staying gentle is what makes this defensible.

Everyone viewing the page reads a static file, so traffic to the site costs
Self-Service nothing at all.

### Three things worth knowing about the data

**We do not use Self-Service's "Open Sections Only" checkbox.** It drops
sections that genuinely have seats — as of this writing `CIS*101-01` has a
free seat but that filter excludes it. AdvisingBot pulls every section and
checks the seat counts itself.

**`currentPageIndex` does not work.** Asking `PostSearchCriteria` for page 2
returns page 1 again, so paging silently yields duplicates. We request a
single page large enough for the whole result set and raise if the server
reports more rows than it returned.

**Some sections publish no seat count** (Self-Service shows `0 / 0 / 0`,
mostly distance-ed `-DE` sections). Their numbers mean nothing, so they are
never counted as open — but they're shown as *"no seat count published"*
rather than hidden, because a section that might be registerable shouldn't
vanish.

## Files

| File | What it is |
| --- | --- |
| `advisingbot.py` | The command-line seat checker |
| `build_site.py` | Builds `docs/data.json` for the web page |
| `build_catalog.py` | Rebuilds `programs.json` from the Catalog — run yearly |
| `selfservice.py` | Client for the Self-Service JSON API |
| `catalog.py` | Parser for the Catalog's curriculum tables |
| `watchlist.toml` | CLI configuration — **edit this** |
| `programs.json` | All 40 SoB curricula — generated |
| `docs/index.html` | The web page |
| `docs/data.json` | Seat data the page reads — generated |
| `.github/workflows/refresh.yml` | The daily refresh |
| `.last_run.json` | Previous CLI run, used by `--diff` |

## If it breaks

Self-Service is a vendor product (Ellucian Colleague) and can change without
notice:

- *"Could not find the antiforgery token"* — the search page's HTML changed.
- *"Expected JSON from ..."* — an endpoint changed shape.
- *"reported N courses but returned M"* — raise `page_size`.

The fix is to open the page in a browser, watch the network tab while
clicking the filters, and update `selfservice.py` to match.

The Catalog is more stable, but if `build_catalog.py` reports *"No programs
parsed at all"*, its table markup has changed — check `catalog.py` against a
curriculum page.

## Notice

This project aggregates information from two publicly accessible sources, the
Quinnipiac University Academic Catalog and the Quinnipiac University
Self-Service course catalog. It performs no authentication and accesses no
student, enrollment, or other non-public record. It is not affiliated with,
endorsed by, or an official publication of Quinnipiac University, and seat
data is a periodic snapshot rather than a live feed. The full notice appears
on the web page itself.
