# Phase 3 - Orbit Timesheet View and List

**Status:** Implementation approved  
**Scope:** Read-only, employee-scoped timesheet listing and entry details

## 1. Goal

Allow an authenticated employee to ask for Orbit timesheet entries in natural
language and review the results entirely inside Microsoft Teams. This phase must
not edit, delete, submit, or approve any entry.

## 2. Supported requests

Examples include:

```text
Show my timesheet for today
List yesterday's entries
Show my entries for this week
Show entries from 2026-09-20 to 2026-09-25
Show my draft entries for this week
Show ADGM Forms entries for today
```

`show my timesheet` defaults to the current Monday-to-Sunday business week.
Relative dates are interpreted in `PULSE_ORBIT_BUSINESS_TIMEZONE`.

## 3. Query interpretation

The LLM may extract only these filters:

- inclusive `start_date`
- inclusive `end_date`
- optional project name
- optional entry status

The application must parse the LLM response into a typed query and validate it
deterministically. The LLM must never provide employee IDs, organization IDs,
project IDs, entry IDs, pagination offsets, SQL, or PostgREST expressions.

Project text is resolved against the authenticated employee's live assigned
projects. Ambiguous or low-confidence matches are rejected rather than guessed.
Supported statuses are `draft`, `submitted`, `approved`, and `rejected`.

## 4. Authorization

1. Load the Orbit session by verified Teams/Entra user ID.
2. Resolve the Supabase auth user and exactly one Orbit employee.
3. Every list request filters by that resolved `employee_id`.
4. Every detail request filters by both opaque entry ID and resolved
   `employee_id`.
5. Orbit RLS remains an additional authorization boundary.
6. Entry IDs are used only as hidden Adaptive Card action data and are not
   rendered as user-facing references.

## 5. List behavior

- Default page size is 5 and is configurable up to 10.
- Results are ordered by entry date descending with a stable ID tie-breaker.
- The card shows the requested date range, page position, result count, and
  total duration for the displayed page.
- Each summary shows date, canonical project, canonical task, duration, status,
  and truncated notes.
- Each result has a **View details** `Action.Execute` button.
- **Previous** and **Next** actions use a validated resolved query embedded in
  the card action data; they never accept an employee ID from the client.
- Empty results return a valid informational Adaptive Card.
- A configured maximum date range prevents unbounded reads.

## 6. Detail behavior

The detail card displays:

- date
- canonical project
- canonical task
- duration
- complete notes
- status

The card may provide **Back to list** using the validated list context. Edit and
delete actions are deferred to later phases.

## 7. Teams action reliability

Read actions acknowledge `Action.Execute` immediately with a loading card, then
post the completed result as a new Teams message. Duplicate invoke deliveries
must not produce duplicate result messages.

## 8. Orbit API contract

List requests use the signed-in user's bearer token:

```http
GET /rest/v1/timesheet_entries
```

Required filters and projection:

```text
employee_id=eq.<resolved employee id>
and=(entry_date.gte.<start>,entry_date.lte.<end>)
select=id,entry_date,duration_minutes,description,status,
       project:projects(name),task:tasks(name)
order=entry_date.desc,id.desc
```

Project and status filters, limit, and offset are added only after deterministic
validation. `Prefer: count=exact` and `Content-Range` provide pagination count.

## 9. Error behavior

- Missing/expired session: ask for `orbit login`.
- Invalid or oversized date range: corrective message.
- Ambiguous project: show safe matching names and ask for specificity.
- Entry not found for the employee: generic not-found card.
- Orbit/network/provider error: safe retry message without raw payloads, tokens,
  SQL, or internal identifiers.

## 10. Acceptance criteria

- Natural-language today, yesterday, week, date-range, project, and status
  filters produce typed validated queries.
- Only the authenticated employee's entries are requested.
- Pagination and detail actions execute directly without requiring typed
  follow-up messages.
- A stale/tampered detail ID cannot bypass employee filtering or RLS.
- No mutation request is made in this phase.
- Unit tests cover parsing, service validation, PostgREST requests, response
  parsing, cards, pagination actions, detail actions, empty results, and errors.
