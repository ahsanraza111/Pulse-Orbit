# PULSE

PULSE is an internal Microsoft Teams assistant. Phase 1 proves the complete
Teams message -> FastAPI -> Groq -> Teams reply path. Phase 2 adds a confirmed,
user-scoped Orbit timesheet entry workflow without coupling Orbit logic to Teams
or Groq.

## Current scope

- FastAPI service with liveness and readiness endpoints.
- Authenticated Microsoft Teams bot endpoint at `/api/messages`.
- Async Groq chat completion integration.
- In-chat Orbit sign-in card, token refresh, project/task resolution, entry
  confirmation, and draft creation.
- Explicit dependency injection through a composition root.
- Unit and HTTP tests that do not require real credentials.
- Product and phase documentation under `docs/`.

Orbit list/edit/delete and approval workflows are intentionally deferred. Product
requirements and safety rules are captured in [the PRD](docs/PRD.md) and the
[Phase 2 add-entry specification](docs/phases/PHASE_02_ORBIT_ADD_ENTRY_REQUIREMENTS.md).

## Requirements

- Python 3.12+
- A Microsoft/Entra bot registration with client ID, client secret, and tenant ID
- A Groq API key
- ngrok for exposing local port `3978` to Teams

## Local setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` and supply your credentials. Do not commit `.env`.

For Orbit add-entry support, also set the current Orbit Supabase URL,
publishable/legacy anon key, PostgreSQL connection values, and a Fernet
encryption key. Generate the encryption key locally:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that output into `PULSE_ORBIT_SESSION_ENCRYPTION_KEY`.

Create the database schema through Alembic (do not create application tables
manually in pgAdmin):

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

PULSE persists encrypted Orbit access and refresh tokens in PostgreSQL for a
fixed 60-minute application session. Provider token refresh does not extend this
window. The database password and encryption key must remain only in `.env`.

Start PULSE:

```powershell
pulse
```

In another terminal, expose it:

```powershell
ngrok http 3978
```

Set the bot messaging endpoint in Azure/Teams configuration to:

```text
https://<your-ngrok-host>/api/messages
```

Verify the service without invoking Groq:

```powershell
Invoke-RestMethod http://localhost:3978/health/live
Invoke-RestMethod http://localhost:3978/health/ready
```

`PULSE_TEAMS_SKIP_AUTH=true` is only for a local Microsoft 365 Agents
Playground connection. Keep it `false` whenever the endpoint is exposed by
ngrok.

## Orbit add-entry flow

In Teams, link the current user's Orbit account:

```text
orbit login
```

PULSE displays a password-masked Adaptive Card directly in Teams. Enter the Orbit
email and password in that card and select **Connect Orbit**. No browser link or
redirect is used. The password is used only for that authentication attempt and
is not stored. Never send a password as an ordinary Teams chat message. After a
successful login, PULSE sends a separate success message with up to three
time-entry templates. Selecting a template inserts an editable `orbit add`
command into the Teams compose box; it is not submitted automatically.

Prepare an entry using natural language after the command prefix:

```text
orbit add 2 hours today on Alpha project, API Development task. Fixed retries.
```

The prefix is optional for clear time-entry requests. For example:

```text
add an entry for today on ADGM forms, backend dev task, 4 hours, notes API fix
```

PULSE compares the requested names with the user's live assigned Orbit projects
and active tasks. It accepts clear abbreviations, plural variants, and close
spellings, then shows the canonical names in an interactive confirmation card.
Ambiguous matches are never guessed. Select **Confirm entry** or **Cancel** on
the card; no follow-up message is required. The text commands remain available
as a fallback:

```text
orbit confirm
orbit cancel
```

Remove the process-local Orbit session with:

```text
orbit logout
```

Orbit sessions survive PULSE restarts until their fixed expiry. Pending entry
drafts remain process-local in this phase and must be recreated after a restart.

## Tests and lint

```powershell
pytest
ruff check .
```

## Project layout

```text
src/pulse/
  application/       use cases and provider-neutral ports
  core/              configuration and logging
  infrastructure/    Groq and future Orbit adapters
  presentation/      FastAPI and Teams delivery adapters
  bootstrap.py       dependency composition root
  main.py            process entry point
```

## Configuration

All configuration uses `PULSE_`-prefixed environment variables. The checked-in
`.env.example` lists every setting. Startup fails early when authenticated Teams
or Groq credentials are missing; secret values are represented as `SecretStr`
and are never returned by health endpoints.
