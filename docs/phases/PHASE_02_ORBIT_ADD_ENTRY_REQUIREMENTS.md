# Phase 2 — Orbit Add Entry Requirements

**Status:** Implemented; live Orbit contract verification pending  
**Created:** 2026-09-22  
**Scope:** Authenticate an Orbit user and create one confirmed timesheet entry

## 1. Objective

Allow an authenticated employee to ask PULSE in Microsoft Teams to add an Orbit
timesheet entry using natural language. PULSE must resolve the employee's
assigned project and task, show a normalized draft, obtain explicit confirmation,
and then create the entry through Orbit's Supabase REST API.

This phase delivers only the minimum workflow needed to add an entry safely.

## 2. Confirmed integration model

Orbit uses Supabase Auth and the Supabase REST/PostgREST Data API. This is not a
raw PostgreSQL connection.

```text
Orbit email/password
        |
        v
Supabase password authentication
        |
        v
User access token + refresh token
        |
        v
Supabase REST API under the user's RLS permissions
        |
        v
Orbit timesheet entry
```

Every Orbit data request must use the public Orbit anon key plus the authenticated
user's access token. A service-role key must never be used for employee actions.

## 3. In scope

- Secure Orbit sign-in with the employee's email and password.
- User-scoped access and refresh token handling.
- Resolve the authenticated Supabase user to an Orbit employee.
- Retrieve only projects available to the authenticated employee.
- Retrieve active tasks related to the selected project.
- Extract entry date, project, task, duration, and notes from a Teams request.
- Resolve ambiguous or missing values through follow-up questions.
- Display a complete entry draft in Teams.
- Require explicit confirmation tied to that exact draft.
- Create a `draft` record in `timesheet_entries`.
- Report the actual Orbit result to the employee.
- Safe errors, audit metadata, timeouts, and automated tests.

## 4. Out of scope

- Listing historical timesheet entries as a standalone feature.
- Editing an existing entry.
- Deleting an existing entry.
- Submitting a weekly/monthly timesheet for approval.
- Approving another employee's timesheet.
- Manager or administrator workflows.
- Raw database credentials or direct PostgreSQL access.
- Long-term conversation memory unrelated to the pending entry draft.

## 5. User journey

Example conversation:

```text
Employee:
Add 2 hours today on Alpha project, API Development task. Fixed retry handling.

PULSE:
Please confirm this Orbit entry:
- Date: 22 Sep 2026
- Project: Alpha
- Task: API Development
- Duration: 2h 00m
- Notes: Fixed retry handling.
- Status: Draft

Reply "confirm" to create it or "cancel" to discard it.

Employee:
confirm

PULSE:
Orbit entry created successfully. Reference: <entry identifier>
```

If project or task matching is ambiguous, PULSE must present the valid matching
options instead of guessing.

## 6. Functional requirements

### ORB-ADD-001: Secure sign-in

- Authenticate against the configured Orbit Supabase Auth endpoint using the
  password grant.
- The request contains email and password only at the authentication boundary.
- Passwords must not be sent to Groq, written to logs, stored in the database, or
  passed as command-line arguments.
- `orbit login` must render an Adaptive Card inside Teams with a password-masked
  input. No browser link or redirect is allowed.
- Card submission must be handled as a dedicated bot action and must never be
  sent to Groq or rendered as a normal chat message.
- Successful authentication must send a new Teams message rather than placing
  the success text in the login card's action-response footer.
- The success message must offer no more than three context-specific time-entry
  templates using `Action.Compose`. Selecting one inserts an editable `orbit add`
  command into the compose box and must not submit it automatically.
- A password sent as ordinary Teams text must be rejected before LLM routing;
  the user must be told to delete the message, rotate the exposed password, and
  use the masked card.
- Invalid credentials must return a generic authentication failure without
  revealing provider details.

### ORB-ADD-002: Session management

- Store access and refresh tokens encrypted and associate them with the verified
  Teams/Entra user identity.
- Validate an access token through the Supabase user endpoint before protected
  operations when required.
- Refresh an expired access token using the refresh token.
- If refresh fails, remove/disable the invalid session and request sign-in again.
- Tokens must never appear in logs, prompts, Teams replies, URLs, or exceptions.

### ORB-ADD-003: Resolve Orbit employee

- Obtain the authenticated Supabase user's immutable user ID.
- Query `employees` by that user ID.
- Resolve exactly one `employee_id` and its `organization_id`.
- Stop safely when no employee exists or the result is ambiguous.

### ORB-ADD-004: Resolve assigned project

- Query projects through the employee/project assignment relationship used by
  Orbit, expected to involve `project_team_members`.
- Do not authorize an entry from a free-form project name alone.
- Match against only the projects returned for the authenticated employee.
- Matching may be case-insensitive and may resolve a unique high-confidence
  abbreviation, plural variant, or close spelling against the live project list.
- Multiple or low-confidence matches require user selection; the application
  must not ask the LLM to invent an identifier.
- The selected project ID and canonical project name become part of the draft.

### ORB-ADD-005: Resolve active task

- Query `project_tasks` for the selected project.
- Include the related `tasks` record and restrict results to active mappings.
- Match only against tasks returned for the selected project.
- A unique high-confidence abbreviation, plural variant, or close spelling may
  resolve to the canonical task name.
- Multiple matches require user selection.
- The selected task ID and canonical task name become part of the draft.

### ORB-ADD-006: Collect and normalize entry fields

The application must obtain:

- `entry_date`
- `project_id`
- `task_id`
- `duration_minutes`
- `description`
- `employee_id`
- `organization_id`

Natural-language interpretation may be performed by the LLM, but all output must
be parsed into a typed draft and validated deterministically.

### ORB-ADD-007: Validate the draft

- Date must be a valid Orbit work date interpreted in the configured business
  timezone.
- Duration must be converted to a positive integer number of minutes.
- Project and task IDs must come from the authenticated employee's live Orbit data.
- Notes must respect Orbit's confirmed required/maximum-length rules.
- Locked periods and other Orbit restrictions must be checked before confirmation
  when the relevant API contract is confirmed.
- Invalid or missing values must produce a specific corrective question.

### ORB-ADD-008: Require confirmation

- Show canonical date, project, task, duration, notes, and status before creation.
- Present the draft as an Adaptive Card with **Confirm entry** and **Cancel**
  `Action.Execute` buttons. A click must perform the selected action directly;
  it must not insert text into the compose box or require the user to send a
  follow-up message.
- Do not create an entry until the user explicitly confirms.
- Confirmation must reference a server-side pending draft, not values supplied by
  the confirmation message.
- Each card action must carry only the opaque pending-draft ID. A stale card must
  never confirm or cancel a newer draft.
- The confirm action must acknowledge the Teams invoke promptly and report the
  final Orbit result in a separate message so a slow provider call does not make
  Teams display an action timeout while the mutation continues.
- A changed request invalidates the previous confirmation.
- Pending drafts expire after a configurable short period.
- `cancel` discards the pending draft without calling Orbit.

### ORB-ADD-009: Create the Orbit entry

- Create the entry through `POST /rest/v1/timesheet_entries`.
- Use the Orbit anon key and the authenticated user's bearer token.
- Submit the following normalized payload:

```json
{
  "description": "<validated notes>",
  "duration_minutes": 120,
  "entry_date": "2026-09-22",
  "project_id": "<resolved project id>",
  "status": "draft",
  "task_id": "<resolved task id>",
  "employee_id": "<resolved employee id>",
  "organization_id": "<resolved organization id>"
}
```

- Request a representation of the created row or otherwise verify the affected
  row and entry identifier.
- A successful HTTP status alone must not be treated as proof when no row result
  can be verified.
- Use an idempotency strategy to prevent duplicate creation after retries or
  repeated confirmation.

### ORB-ADD-010: Report the result

- Success is reported only after Orbit confirms creation.
- The response should include the canonical project, task, date, duration, and a
  safe entry reference.
- Authentication, validation, authorization/RLS, locked-period, network, timeout,
  and provider errors must have distinct safe user messages.
- Raw provider responses, SQL details, tokens, or personal data must not be shown.

### ORB-ADD-011: Audit the mutation

- Record the verified Teams actor, Orbit employee ID, operation, draft values,
  confirmation timestamp, correlation ID, Orbit entry reference, and outcome.
- Never include passwords, access tokens, refresh tokens, or authorization headers.

## 7. Orbit API contract observed from the example

All paths below are relative to the configured Orbit Supabase URL. Values must be
URL encoded or passed through a typed HTTP client rather than manually concatenated.

### Authenticate

```http
POST /auth/v1/token?grant_type=password
apikey: <Orbit anon key>
Content-Type: application/json
```

```json
{
  "email": "<employee email>",
  "password": "<employee password>"
}
```

### Resolve authenticated user

```http
GET /auth/v1/user
apikey: <Orbit anon key>
Authorization: Bearer <user access token>
```

### Resolve employee

```http
GET /rest/v1/employees?user_id=eq.<auth-user-id>&select=id,organization_id
```

### Resolve project tasks

```http
GET /rest/v1/project_tasks?project_id=eq.<project-id>&is_active=eq.true&select=task:tasks(id,name)
```

### Create entry

```http
POST /rest/v1/timesheet_entries
apikey: <Orbit anon key>
Authorization: Bearer <user access token>
Content-Type: application/json
Prefer: return=representation
```

The exact assigned-project query must be confirmed from the current Orbit client.

## 8. Application architecture requirements

The Phase 2 implementation must preserve the existing dependency direction.

Suggested application ports:

```text
OrbitAuthPort
  sign_in(email, password)
  refresh(refresh_token)
  get_authenticated_user(access_token)

OrbitTimesheetPort
  get_employee(auth_user_id)
  list_assigned_projects(employee_id)
  list_active_tasks(project_id)
  create_entry(entry)

OrbitSessionStore
  get(teams_user_id)
  save(teams_user_id, tokens)
  delete(teams_user_id)

PendingEntryStore
  save(draft)
  get(draft_id, teams_user_id)
  consume(draft_id)
```

Use cases must depend on these ports, not on `requests`, Supabase, Teams, or Groq
implementations directly. Concrete adapters are created in the composition root.

## 9. Configuration requirements

The following values must come from environment configuration and must not be
hardcoded:

```text
PULSE_ORBIT_SUPABASE_URL
PULSE_ORBIT_SUPABASE_ANON_KEY
PULSE_ORBIT_HTTP_TIMEOUT_SECONDS
PULSE_ORBIT_CONFIRMATION_TTL_SECONDS
PULSE_ORBIT_BUSINESS_TIMEZONE
```

The real `.env` remains ignored. `.env.example` contains placeholders only.

## 10. Reliability requirements

- Use async HTTP calls so Orbit requests do not block the FastAPI event loop.
- Configure connection and response timeouts.
- Retry only safe reads and token refresh under a bounded policy.
- Do not blindly retry entry creation.
- Use correlation IDs across Teams, application logs, and Orbit calls.
- Validate response status and JSON shape before consuming it.
- Redact sensitive headers and fields from logs and telemetry.

## 11. Test requirements

- Authentication success, invalid credentials, timeout, and malformed response.
- Access-token refresh success and failure.
- Employee missing or ambiguous.
- No assigned projects and no active tasks.
- Exact, partial, ambiguous, and missing project/task matches.
- Duration/date/notes validation.
- Draft confirmation, cancellation, modification, expiry, and replay attempt.
- Successful creation with returned entry ID.
- RLS denial, locked-period rejection, network error, and provider error.
- Duplicate confirmation does not create a second entry.
- Tests must use fakes/mocks and never call the production Orbit project.

## 12. Acceptance criteria

Phase 2 add-entry work is complete when:

1. A Teams user can securely link their own Orbit account.
2. PULSE resolves that user's Orbit employee profile.
3. Only assigned projects and their active tasks can be selected.
4. Natural-language input becomes a validated typed draft.
5. PULSE shows the complete draft and requires explicit confirmation.
6. One confirmation produces at most one Orbit entry.
7. PULSE verifies and reports the created Orbit entry.
8. Passwords and tokens never enter prompts, logs, Git, or Teams responses.
9. Automated tests cover success, failure, authorization, ambiguity, expiry, and
   duplicate-prevention paths.
10. The implementation remains replaceable through injected ports/adapters.

## 13. Decisions required before implementation

- Confirm which Orbit Supabase project URL is current; the browser screenshot and
  provided example reference different projects.
- Confirm the current public anon key through an approved configuration source.
- Confirm the exact employee-to-project assignment query and relevant RLS policy.
- Confirm Orbit duration increments, per-entry/day maximums, and notes constraints.
- Confirm business timezone and interpretation of `today`.
- Confirm locked-period and duplicate-entry rules.
- Confirm whether new entries must always start as `draft`.
- Select the secure Teams-to-Orbit sign-in UX.
- Select the encrypted session store and retention/logout policy.

## 14. Implemented MVP

The repository now contains:

- Supabase password authentication, refresh, and authenticated-user adapters.
- Encrypted process-local session storage with replaceable storage ports.
- An in-chat Teams Adaptive Card with masked email/password inputs.
- A dedicated card-submit handler that authenticates directly against Orbit,
  does not use Groq, does not persist the password, and replaces the card with a
  safe success or retry response.
- Employee, RLS-scoped project, and active project-task lookup.
- Groq-backed structured entry parsing with deterministic validation.
- Canonical project/task matching and ambiguity handling.
- Expiring server-side pending drafts and per-user confirmation locking.
- One-shot create behavior with `Prefer: return=representation` verification.
- Explicit uncertain-outcome handling for create timeouts; no blind retry.
- Teams commands for login, add, confirm, cancel, and logout.
- Unit/integration coverage using fakes and HTTP mock transports only.

Current MVP limitations:

- Sessions and pending drafts are encrypted/in-memory and are lost on restart.
- Assigned projects currently rely on Orbit's user-JWT RLS behavior, matching the
  provided example. The exact `project_team_members` query remains to be verified.
- No production Orbit call has been made by automated tests.
- The current Orbit Supabase project URL/key must be confirmed because the browser
  screenshot and provided example reference different projects.
- List, edit, delete, and submit-for-approval remain out of scope.

Live mutation testing must not begin until the current Orbit project configuration
is confirmed and a disposable test entry/date is selected.
