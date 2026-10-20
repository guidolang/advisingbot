# AdvisingBot

Finding courses for a student requires checking their degree audit in Stellic, identifying unmet requirements, and searching for appropriate courses with open sections in Self-Service. AdvisingBot brings those steps together. It reads the student’s degree audit and creates a clear list of open courses that may fit their remaining requirements.

## Before you begin

You need the **[ChatGPT desktop app](https://chatgpt.com/download)** and a subscription that supports **Codex**, such as **ChatGPT Edu** or **ChatGPT Plus**.

## Install AdvisingBot

1. In the **ChatGPT desktop app**, in the upper-left corner, select the **ChatGPT** dropdown, then choose **Codex**.
2. Copy and paste this prompt into Codex:

```text
Download the public AdvisingBot Skill ZIP from https://github.com/guidolang/advisingbot/archive/refs/heads/main.zip. Do not ask me to create or sign in to a GitHub account. Extract the ZIP and install the AdvisingBot Skill from the advisingbot-main/skills/advisingbot folder. If OpenTabs needs setup, use https://opentabs.dev/docs/install. Then open AdvisingBot in Chrome.
```

Codex downloads and installs the Skill for you. AdvisingBot uses [OpenTabs](https://opentabs.dev/docs/install) to connect securely to Stellic in Chrome. If Chrome needs a one-time extension confirmation, just ask Codex to guide you through the steps.

## Use AdvisingBot

1. Start a chat and type `$AdvisingBot`. AdvisingBot opens in Chrome.
2. In Chrome, open a student’s degree audit in Stellic and copy the page address (i.e., `https://qu.stellic.com/app/audit?student_username=test`).
3. Paste the address into AdvisingBot and select **Check**.
4. Choose a semester and modality, then review the available courses.
5. To quit AdvisingBot, just close the tab in Chrome and prompt Codex to close AdvisingBot.

## Advising Report

This example shows a fictitious student advising report.

![Example AdvisingBot report](skills/advisingbot/assets/example-report.png)

## Reading the report

- Requirements follow the order and nesting shown in Stellic.
- Courses appear only when a relevant section has an open seat.
- A course may appear under more than one requirement when it can count toward each of them.
- Courses that have seats but need a prerequisite or adviser review are set aside with a reason.
- A requirement can have no recommendation when no eligible section is open or when Stellic does not identify a specific course well enough to recommend one.

Use the report to support advising. Confirm the student's final eligibility and course fit in Stellic before recommending registration.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| A message asking you to sign in | Sign in to Stellic in Chrome, then select **Check** again. |
| A message about the secure Stellic connection | In Codex, say: **Set up OpenTabs for AdvisingBot.** You can also use the [OpenTabs setup guide](https://opentabs.dev/docs/install). |
| No courses for a requirement | Try a different semester or modality. There may be no open eligible sections, or the requirement may need adviser review. |

The app runs locally. It holds the student's audit in memory for the current check and does not save it to disk. Its local cache contains public course and seat information only.
