# PULSE Product Requirements Document

**Status:** Approved for phased implementation  
**Version:** 0.1  
**Last updated:** 2026-09-18  
**Product owner:** To be assigned

## 1. Product summary

PULSE is an internal Microsoft Teams assistant that lets an authenticated
employee converse naturally and manage their Orbit timesheet without leaving
Teams. The first release proves the Teams-to-AI communication path. Subsequent
releases add Orbit authentication, assigned-project discovery, and controlled
timesheet create, read, update, and delete operations.

## 2. Problem

Employees must currently open Orbit, sign in with email and password, find an
assigned project and task, enter task notes and hours, and submit the entry.
Corrections require returning to Orbit to edit or delete an entry. This context
switching is repetitive and increases the chance of missing or inaccurate
timesheets.

## 3. Goals

1. Confirm that an employee can message PULSE in Teams and receive a Groq-backed
   response in the same conversation.
2. Let an authorized employee view their assigned Orbit projects and tasks.
3. Let an employee draft a timesheet entry using natural language.
4. Require an explicit confirmation before any create, update, or delete.
5. Let an employee view, edit, and delete their own existing entries.
6. Keep credentials, permissions, audit data, and business rules outside the LLM.
7. Maintain a modular design so Teams, Groq, and Orbit adapters can be replaced.

## 4. Non-goals for the initial release

- Autonomous submission without user confirmation.
- Manager approval workflows or entries for another employee.
- Payroll, invoicing, or project administration.
- Browser automation until Orbit's available API and authentication mechanism
  have been assessed.
- Long-term conversational memory or training Groq on company data.

## 5. Users and primary journey

The initial user is an internal employee who already has an Orbit account and
has installed or can access PULSE in Microsoft Teams.

Example target journey:

1. Employee: "Aaj Orbit mein Alpha project ke API task par 3 hours add karo;
   note: fixed retry handling."
2. PULSE resolves only projects and tasks assigned to that employee.
3. PULSE shows a structured draft: date, project, task, notes, and hours.
4. Employee explicitly confirms or cancels.
5. PULSE submits through the Orbit adapter and reports the confirmed Orbit result.

## 6. Functional requirements

### FR-1: Teams conversation

- Receive text messages from Microsoft Teams through `/api/messages`.
- Validate Bot Framework/Teams authentication for any exposed environment.
- Return replies to the originating conversation.
- Ignore empty messages and handle upstream errors with a safe user-facing reply.

### FR-2: AI response

- Send the user message and controlled system instruction to Groq.
- Use an asynchronously invoked, configurable Groq model.
- Apply input length, output token, timeout, and temperature limits.
- Never send application credentials or raw authentication tokens to Groq.

### FR-3: Orbit authentication

- Support Orbit's verified authentication mechanism after discovery.
- If email/password must be collected, never ask for or echo passwords in normal
  Teams chat. Use a secure sign-in surface or an organization-approved secret flow.
- Associate the Orbit session with the authenticated Teams/Entra identity.
- Expire sessions and support sign-out/revocation.

### FR-4: Assigned work discovery

- List only projects assigned to the signed-in Orbit user.
- List tasks related to a selected assigned project.
- Handle ambiguous project or task names by asking the user to choose.

### FR-5: Timesheet creation

- Collect and validate work date, assigned project, related task, task notes, and
  hours.
- Show a complete, normalized draft before submission.
- Submit only after an explicit user confirmation tied to that draft.
- Prevent accidental duplicate submission using an idempotency mechanism where
  Orbit supports it, or an application-side equivalent.

### FR-6: Timesheet retrieval

- List the current user's entries for a requested date or bounded date range.
- Present stable entry identifiers needed for edit/delete selection.

### FR-7: Timesheet update

- Allow edits only to an entry the current user is permitted to change.
- Show old and new values and require explicit confirmation before saving.
- Report Orbit's actual response; never infer success from an LLM response.

### FR-8: Timesheet deletion

- Allow deletion only for an authorized, unambiguous entry.
- Show entry details and require explicit confirmation immediately before deletion.
- Treat delete as high-risk, record an audit event, and report the actual result.

### FR-9: Cancellation and expiry

- The user can cancel a pending mutation.
- Confirmation drafts expire after a configurable period and cannot be replayed.
- A materially changed request creates a new draft and invalidates the prior one.

### FR-10: Auditability

- Record actor identity, operation, target entry, timestamp, correlation ID,
  confirmation, and outcome for mutations.
- Do not store passwords, access tokens, or unnecessary message content in logs.

## 7. Business rules

- Hours must be positive and conform to Orbit's maximum and increment rules once
  those rules are confirmed.
- Work date and timezone must be displayed before confirmation.
- A project/task must come from the authenticated user's assigned Orbit data;
  free-form names alone cannot authorize a mutation.
- The deterministic application layer validates and executes operations. The LLM
  may interpret intent and extract a draft, but cannot directly perform a mutation.
- Create, update, and delete always require explicit confirmation in v1.
- PULSE must say when data is unknown instead of inventing projects, tasks, entry
  identifiers, or successful results.

## 8. Quality attributes

- **Security:** authenticated webhook, least privilege, secret isolation, safe
  logging, input validation, and dependency scanning.
- **Reliability:** timeouts, bounded retries for safe reads, idempotency for writes,
  and clear partial-failure behavior.
- **Maintainability:** typed interfaces, dependency injection, small use cases,
  adapters at system boundaries, and automated tests.
- **Observability:** structured logs, correlation IDs, latency, error rate, and
  provider health without sensitive payloads.
- **Performance:** acknowledge/respond within Teams channel limits; send a typing
  indication or progress message for long operations in a later phase.
- **Privacy:** minimum required data is sent to Groq and retained according to the
  organization's policy.

## 9. Technical architecture and stack

- Python 3.12+
- FastAPI and Uvicorn
- Microsoft Teams Python SDK with its FastAPI adapter
- Groq async Python SDK
- Pydantic Settings for environment configuration
- Ports-and-adapters structure with an explicit composition root for dependency
  injection
- Pytest and Ruff for verification
- Persistence and secret store to be selected before Orbit authentication work

Core boundaries:

```text
Microsoft Teams -> Teams adapter -> Chat / Timesheet use cases
                                      |             |
                                  LLM port      Orbit port
                                      |             |
                                  Groq adapter  Orbit adapter
```

The Orbit adapter owns HTTP/session details. Use cases depend only on a typed
Orbit port. The LLM never receives Orbit credentials and cannot call the Orbit
adapter directly.

## 10. Delivery phases

### Phase 0: Product and engineering foundation

- PRD, architecture rules, configuration convention, repository skeleton.

### Phase 1: Teams + Groq connectivity

- Authenticated Teams message endpoint, FastAPI health endpoints, Groq replies,
  dependency injection, error handling, automated tests, and ngrok instructions.

### Phase 2: Orbit discovery and secure authentication design

- Document Orbit endpoints or browser constraints, login/session behavior, CSRF,
  rate limits, project/task/entry shapes, and credential-storage decision.
- Build a fake Orbit adapter and contract tests before using real credentials.

### Phase 3: Read-only Orbit workflows

- Secure sign-in/out, assigned projects/tasks, and timesheet retrieval.

### Phase 4: Create entry

- Intent extraction, deterministic validation, confirmation state, idempotent
  submission, result reporting, and audit trail.

### Phase 5: Edit and delete

- Entry selection, before/after diff, confirmation, update/delete execution, and
  audit trail.

### Phase 6: Production hardening

- Durable storage, secret vault, access policy, monitoring, rate limiting,
  deployment pipeline, backup/retention policies, and security review.

## 11. Phase 1 acceptance criteria

1. The service starts only with valid runtime configuration.
2. `GET /health/live` returns a successful process health result.
3. `GET /health/ready` reports whether Teams and Groq are configured without
   exposing secrets.
4. A valid Teams text message reaches the chat service and its Groq response is
   returned in the same conversation.
5. Empty, oversized, provider-failed, and unexpected-error paths have tests and
   safe responses.
6. The chat use case can be tested with a fake LLM without Teams or Groq.
7. `.env` is ignored and a safe `.env.example` is documented.

## 12. Open questions before Phase 2

- Does Orbit provide a documented internal API, or is it currently web-only?
- Is Orbit authentication session-cookie, token, SSO, or another mechanism?
- Is each employee expected to link their own Orbit account, or will a service
  account act on their behalf?
- What are Orbit's hour limits, increments, timezone, locked-period, and duplicate
  entry rules?
- Which projects/tasks are mutable, and can submitted entries become locked?
- What audit retention period and credential store are organization-approved?

These questions do not block Phase 1, but they block a safe production Orbit
mutation implementation.

