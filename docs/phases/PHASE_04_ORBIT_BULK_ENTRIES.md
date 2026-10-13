# Phase 4 — Orbit Bulk Entries

**Status:** Implemented; live Orbit batch verification pending  
**Created:** 2026-09-30

## Objective

Allow an authenticated Teams user to prepare and confirm multiple Orbit entries
for one day or repeat a set of entry templates across an inclusive date range.

## Supported modes

1. **Single-day bulk:** multiple entries are created once on one date.
2. **Weekday range:** every template is repeated Monday through Friday within
   the inclusive range.
3. **All-day range:** every template is repeated on every calendar day within
   the inclusive range.

PULSE must not guess the recurrence for a multi-day range. If it is omitted,
PULSE presents **Weekdays only** and **Every day** buttons before resolution and
confirmation.

## Requirements

- Parse the request into a typed date range, optional recurrence, and entry
  templates containing project, task, duration, and notes.
- Resolve every project against the authenticated employee's assigned projects.
- Resolve every task against the selected project's active tasks.
- Preserve the whole batch while an ambiguous project or task is resolved with
  interactive Teams buttons.
- Generate and store the exact entry set before confirmation.
- Show start/end dates, recurrence, day count, template count, generated entry
  count, daily duration, overall duration, and every resolved template.
- Create nothing until **Create all entries** is selected.
- A batch confirmation is one-shot and tied to an opaque server-side batch ID.
- Repeated card delivery must not create duplicate entries.
- Report created, failed, and uncertain outcomes per generated entry.
- Never retry an uncertain Orbit mutation automatically.
- A new single-entry request invalidates an old bulk draft and a new bulk request
  invalidates an old single-entry draft.

## Limits

- Maximum templates per request: configurable, default 10.
- Maximum inclusive date range: configurable, default 31 days.
- Maximum generated entries: configurable, default 100.
- Maximum total duration per day: configurable through the existing Orbit
  duration limit, default 1,440 minutes.

## Example

```text
Add these entries from 21 Sep 2026 to 30 Sep 2026, weekdays only:
- ADGM Website, Meetings and Calls, 30 minutes, notes standup
- ADGM Website, Backend Development, 2 hours, notes API fix
- Form Submission, Code Review, 1 hour, notes PR review
```

For this inclusive range PULSE generates eight weekdays, three entries per day,
24 total entries, 3h 30m per day, and 28 hours overall.

## Reliability limitation

Pending batch preparation and confirmation state is process-local. A process
restart invalidates unconfirmed batches. Confirmed entry creation is sequential;
therefore provider failures can produce a partial batch. PULSE reports the exact
outcome and does not falsely claim all entries succeeded.
