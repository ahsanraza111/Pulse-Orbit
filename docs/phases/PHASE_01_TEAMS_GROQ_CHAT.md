# Phase 1 — Microsoft Teams + Groq chat

**Status:** Implemented; live credential verification pending  
**Implemented:** 2026-09-18

## Implemented

- FastAPI application served on configurable host/port (default `3978`).
- Microsoft Teams SDK FastAPI adapter registering `POST /api/messages`.
- Client-secret authentication configuration for the Teams bot.
- Asynchronous Groq chat-completion adapter.
- Provider-neutral `LLMClient` port and `ChatService` use case.
- Constructor/composition-root dependency injection.
- Empty and oversized input validation.
- Safe Teams responses for validation, provider, and unexpected failures.
- Bot-mention cleanup for channel messages.
- Liveness and configuration-readiness endpoints.
- Unit and HTTP tests using fakes; real credentials are not required for tests.
- ngrok and bot endpoint setup steps in the README.

## Runtime flow

```text
Teams -> POST /api/messages -> Teams SDK authentication
      -> TeamsMessageHandler -> ChatService -> GroqLLMClient
      <- same Teams conversation <- response text
```

## Configuration required for live verification

- `PULSE_TEAMS_CLIENT_ID`
- `PULSE_TEAMS_CLIENT_SECRET`
- `PULSE_TEAMS_TENANT_ID`
- `PULSE_GROQ_API_KEY`

The bot messaging endpoint must be the ngrok HTTPS URL plus `/api/messages`.
`PULSE_TEAMS_SKIP_AUTH` must remain `false` for ngrok/Teams traffic.

## Acceptance status

- Automated application behavior: 16 tests passing.
- Teams SDK initialization and `/api/messages` route registration: verified with
  authenticated test configuration.
- Dependency health, lint, and byte-code compilation: passing.
- Local Uvicorn startup and both health endpoints: smoke-tested successfully.
- Live Teams round trip: pending the owner's local `.env`, active ngrok tunnel,
  bot endpoint update, and Teams app installation.

## Deferred to later phases

- Conversation persistence and multi-turn state.
- Orbit login and assigned project/task retrieval.
- Timesheet create, edit, and delete.
- Adaptive Cards, confirmation UI, durable audit store, and production deployment.
