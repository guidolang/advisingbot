# AdvisingBot

AdvisingBot helps faculty turn a student's Stellic degree audit into a
practical list of courses with open seats in Self-Service.

It keeps the same nested requirement order as Stellic, so you can see which
requirement each course may fulfill. A course can appear under more than one
requirement when it can count in more than one place.

## What it does

- Reads the student's current degree audit from Stellic.
- Finds courses named in a requirement's rules or comments.
- Finds courses that satisfy University Curriculum attributes, such as Fine
  Arts, Humanities, Natural Sciences, and Social Sciences.
- Checks current section availability in Self-Service.
- Excludes courses the audit records as completed, in progress, or registered.
- Flags courses that appear to be blocked by a prerequisite or a clear
  programme restriction.
- Lets you choose a semester and filter results to Online, On-ground, or both.

For each requirement, AdvisingBot initially shows up to five courses with open
seats. Select the "more courses" control when you want to see the rest.

## Before you begin

You need:

- Chrome signed in to Quinnipiac Stellic.
- OpenTabs running and connected to Chrome.

## Set up with an AI assistant

If you have been given this folder and would like help setting it up locally,
copy the prompt below into your AI coding assistant. Do not include a student
audit link or student name in the prompt.

```text
I am a non-technical faculty adviser and want to run the AdvisingBot application
on my own Mac. Please inspect the README in this project, help me install any
needed local prerequisites, and start the application. Explain each step in
plain language and stop if you need me to sign in or take an action in Chrome.

This application handles FERPA-protected student records. Keep it local to my
computer, do not deploy it, do not expose its local server, do not upload or
commit any student data, and do not put student audit links, names, or records
into files, logs, or messages. Confirm that the local course-seat cache is not
tracked by Git before making any commit.
```

## Use it

1. Double-click `bin/AdvisingBot.command`.
2. Paste the student's Stellic audit link. A link should include
   `student_username=`. You may also enter the username by itself.
3. Select **Check**.
4. Wait for the status message to finish. The first check of the day can take
   a minute or two; later checks are usually faster.
5. Use the requirement tree, semester selector, and Modality selector to
   review available choices.

The default semester is the furthest future term currently available. Use
**Copy Notes** to copy a concise list such as:

```
- CIS 255: Data Visualization (Online: 35 available seats, In-person: 35 available seats)
```

## Reading the results

- Requirement categories follow the order and nesting of the audit.
- Course rows identify the course, title, credits, and available sections.
- A course is listed only when at least one relevant section has an open seat.
- If an available course can meet more than one open requirement, it appears
  in each relevant place.
- A requirement may show no course when there is no currently open eligible
  section, or when Stellic permits an approved alternative that the audit does
  not identify well enough for AdvisingBot to recommend a specific course.
- The university-wide total-credit completion item is not expanded into a
  course list.

Treat the results as advising support, not registration clearance. Confirm
that a course satisfies the requirement in Stellic and that the student may
register before making a recommendation.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| A request to sign in | Sign in to Stellic in Chrome, then try again. |
| An OpenTabs connection error | Start OpenTabs and confirm that its Chrome extension is connected. |
| No courses for a requirement | Try another semester or modality. There may be no open eligible sections, or the requirement may need adviser review. |
| Results look out of date | Seat information refreshes daily. Check Self-Service directly when an immediate change matters. |

## Student privacy

A degree audit is a FERPA-protected education record.

- AdvisingBot runs only on your computer; it is not a hosted service.
- The student audit is used for the current check and is not saved to disk.
- The local cache contains public course and seat information only, not student
  records.
- Do not expose the local server or deploy this application to the web.
