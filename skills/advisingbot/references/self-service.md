# Self-Service retrieval

The catalog uses `https://selfservice.quinnipiac.edu` and can be queried without the Stellic browser session.

Load `/student/Courses/Search` first and extract the page's `__RequestVerificationToken`. Use it as a request header together with the session cookie established by that page.

- POST `/student/Courses/PostSearchCriteria` with JSON search criteria. Include `courseTypes` for University Curriculum queries, `subjects` only for a known explicit course code, `terms` when desired, and `openSections` as appropriate. University Curriculum searches must set `openSections: true`, so only courses with an open matching section enter the candidate map. Request a page large enough for the full result: this endpoint may repeat page one when asked for later pages.
- Pair `Courses[].Requisites` with `CourseFullModels[].CourseRequisites` using their requirement identifiers. The former supplies timing and requiredness; the latter supplies the readable requisite sentence.
- Keep `Courses[].Id` and its matching section IDs. Do not repeat a broad subject lookup to rediscover them.
- POST `/student/Courses/Sections` with `courseId` and `sectionIds` to obtain current terms and sections. A positive available-seat count is open.

Use only the published section fields needed for the report. De-duplicate by course code before posting to `/student/Courses/Sections`, then attach that one result to every affected requirement. The likely useful fields are section name/number, term, available/capacity/waitlist counts, faculty, location, meeting times, credits, and online category. If the service returns an anti-forgery error, restart the Self-Service session once. If it still fails, stop with the plain-language Self-Service error.
