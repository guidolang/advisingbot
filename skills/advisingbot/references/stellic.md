# Stellic retrieval

Stellic is protected by Microsoft SSO. The bundled web app opens the supplied audit URL in an OpenTabs-managed Chrome tab and calls Stellic's internal API from that page context. Do not scrape the React interface or substitute Codex Browser Control for this established path.

Use OpenTabs' browser script execution in that page context. Obtain `csrftoken` from `document.cookie` and use same-origin `fetch` with `credentials: 'same-origin'`, `X-CSRFToken`, and `X-Requested-With: XMLHttpRequest`.

1. POST `/planner/getstudentprofile/` as form data with `student_username=<username>`.
2. Select `default_plan_id`, falling back to the first `plans[].id`.
3. POST `/planner/getauditinfo/` as JSON with `mainaudit`, `official`, `req_tree`, `course_plan_info`, `student_info`, and `placeholders_info` enabled and `plan_id` set to the selected plan.
4. Strip the leading XSSI prefix, usually `)]}',`, before JSON parsing.

Keep the browser payload in the application's memory for the current request. Do not write it to a temporary file, print it, or include it in an error. Clear any page-scoped variable in a `finally` block. The needed source fields are the student identity/programs, `req_tree`, and audit progress including `satisfied`, `units_summary`, and `cards_remaining`.

Use composite card identifiers to distinguish a missing specific course from an open choice requirement. Explicit course choices can be read from the tree. For broad attribute requirements, do not use Stellic's qualifying-course search as the primary candidate source; use the Self-Service course type query.

An unexpected response, missing plan, `success: false`, missing CSRF token, login redirect, or unavailable OpenTabs connection is an error. Say which one occurred and do not substitute rendered-page guesses.
