---
name: advising-report
description: Open Quinnipiac's local AdvisingBot web app to turn a Stellic audit into current course availability.
metadata:
  short-description: Run the local Stellic advising app
---

# AdvisingBot

Launch the bundled local app for one adviser.

## App workflow

- Run `python3 <skill>/scripts/launch.py`. It opens AdvisingBot at `http://127.0.0.1:8765` in the user's default external browser. Do not open it in Codex's internal browser.
- The `app/` directory is the bundled application. Do not reimplement its API calls, data handoff, or rendering in a browser-control script.
- A faculty member pastes a Stellic audit link containing `student_username` into the local page and selects **Check**. The app shows the requirement tree, current open sections, and **Copy Notes**.
- Keep the process running while the local page is in use. Stop it when the user asks or when the task is complete. When the launcher exits, it stops OpenTabs only if it started OpenTabs for this app. If the app is already running, reuse it without starting another OpenTabs instance.

## Retrieval

Use OpenTabs with Chrome signed in to Stellic. The application makes same-origin Stellic requests inside the authenticated browser tab. Read [Stellic retrieval](references/stellic.md) before changing that behavior.

If OpenTabs is missing or not connected, use the [OpenTabs installation guide](https://opentabs.dev/docs/install) as the setup source. Complete installation and health checks where possible; ask the faculty member only for the one-time Chrome extension confirmation that Chrome requires.

Use the public Self-Service course API for catalog metadata and seat availability. The same-day cache contains public catalog and seat data only. It never saves a student audit. Read [Self-Service retrieval](references/self-service.md) before changing retrieval behavior.

Build a de-duplicated candidate map before asking for sections. Query a section only for a concrete course named in an open requirement or returned by an open-section-only University Curriculum course-type query. Reuse one section result wherever the same course appears in several requirements.

## Interpret the audit conservatively

- Preserve the order and nesting of open, actionable requirements. Repeat a course under each requirement it can satisfy.
- Omit satisfied requirements and descendants, inactive alternatives, explanatory labels, `OTHERS_MAY_COUNT` substitutions, unresolved Integrative Capstone umbrellas, and the university-wide total-credit bucket.
- Obtain University Curriculum candidates from Self-Service `courseTypes`: `UC_FA`, `UC_SS`, `UC_HU`, and `UC_NS`. Search with `openSections: true`, retain returned course and matching section IDs, and use those IDs directly for sections.
- Do not expand a broad, free-credit, or indeterminate choice into the catalog. Leave it as `Review with adviser` and explain that Stellic did not identify a bounded course set.
- Exclude courses already completed, in progress, or registered. For a clear unmet prerequisite, retain the course but label it `Needs prerequisite`. Treat unclear prerequisite prose, substitutions, and program restrictions as `Review with adviser`.
- Include only courses with at least one open section. Keep `Seat availability not published` distinct from an open seat.

## Easy-to-understand errors

Stop before writing a misleading report. State the failing service and the next useful action. Do not expose cookies, CSRF tokens, auth headers, raw error pages, raw student payloads, or endpoint response snippets.

## Verification

Before reporting success, verify that the local page responds and its static interface loads. If live testing is authorized, use one advisee and verify only the visible summary. Do not print or save an audit payload.
