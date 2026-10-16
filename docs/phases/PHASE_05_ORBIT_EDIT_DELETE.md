# Phase 5 — Orbit Edit and Delete Entries

**Status:** Implemented (form-based edit and delete); live Orbit verification pending  
**Created:** 2026-10-13

## Objective

Complete the Orbit CRUD set: an authenticated Teams user can edit or delete their
own existing timesheet entries from Teams, with an explicit confirmation before
anything is sent to Orbit.

## Scope

- **Form-based edit** of date, duration, project, task, and notes.
- **Delete** (hard delete in Orbit) after an explicit confirmation.
- Natural-language edit and bulk edit/delete are deferred to later phases.

## Rules

- Only the employee's own **draft** entries can be edited or deleted. Submitted,
  approved, and rejected entries are never modified; the detail card does not show
  Edit/Delete for them and the service and Supabase request re-check the status.
- Edit and delete start only from the **View details** card, so the target entry is
  always an explicit selection, never a guess from chat text.
- Every change is prepared server-side, shown in a confirmation card, and applied
  only when **Update entry** / **Delete entry** is selected.
- A confirmation is one-shot and bound to an opaque change ID. Repeated card
  delivery, stale cards, and expired confirmations cannot apply a change.
- Before applying, PULSE re-reads the entry. If it changed in Orbit after review, or
  is no longer a draft, nothing is sent.
- Update and delete requests are scoped to `id`, `employee_id`, and
  `status = draft`; zero affected rows is reported as not applied.
- A timeout is reported as **needs verification** and is never retried
  automatically.
- A new edit/delete request replaces the user's previous unconfirmed one.

## Flow

1. `show my timesheet` → **View details** → **Edit** or **Delete** (drafts only).
2. **Edit** posts a form prefilled with the current values. Duration accepts
   `1h 30m`, `90m`, `1.5h`, or `1:30`. If the project changes, the task is
   resolved against that project's active tasks (ambiguous or unknown tasks are
   rejected). **Review changes** posts a before → after card.
3. **Delete** posts the full entry with a permanent-deletion warning.
4. Confirm or cancel on the card. The result is posted as updated, deleted,
   failed, or needs verification.

## Natural-language edit and delete (Phase 6A)

A message such as `edit yesterday's ADGM entry to 3 hours`, `orbit update the
code review entry notes to API fix`, or `delete today's standup entry` is parsed
by Groq into a **selector** (date or range, project, task, a word from the notes)
and the **changes** (duration, notes, date, project, task).

- PULSE searches only the user's own **draft** entries. With no date in the message
  the current Monday–Sunday week is searched.
- **One match** goes straight to the same before → after (or delete) confirmation
  card. **Several matches** (up to 8) show a selection card; the user picks one.
  More than 8 matches, or no matches, ask the user to narrow the request.
- A project change must also name the task. The user must say both what identifies
  the entry and what to change.
- Nothing is applied until the confirmation button is selected; all safety rules
  above apply unchanged. A new request invalidates an open selection.
- There is no bulk delete.

## Reliability limitation

Pending changes are process-local; a restart invalidates unconfirmed ones and the
user opens the entry again. Edit/delete is not audit-logged beyond application
logs in this phase.
