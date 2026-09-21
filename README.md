# PULSE

PULSE is an internal Microsoft Teams assistant. Phase 1 proves the complete
Teams message -> FastAPI -> Groq -> Teams reply path. Later phases add safe
Orbit timesheet workflows without coupling Orbit logic to Teams or Groq.

## Current scope

- FastAPI service with liveness and readiness endpoints.
- Authenticated Microsoft Teams bot endpoint at `/api/messages`.
- Async Groq chat completion integration.
- Explicit dependency injection through a composition root.
- Unit and HTTP tests that do not require real credentials.
- Product and phase documentation under `docs/`.

Orbit login and timesheet operations are intentionally not implemented in
Phase 1. Their requirements and safety rules are captured in [the PRD](docs/PRD.md).

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
