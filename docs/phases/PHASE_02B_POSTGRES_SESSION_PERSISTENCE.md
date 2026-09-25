# Phase 2B - PostgreSQL Orbit Session Persistence

**Status:** Implementation approved  
**Database:** PostgreSQL (`pulse`)  
**Session lifetime:** 60 minutes, absolute (non-sliding)

## 1. Goal

Persist each verified Teams user's encrypted Orbit session so an application
restart does not force an immediate Orbit login. PULSE must require a fresh
Orbit login after the fixed 60-minute application session expires.

## 2. Required behavior

1. A successful Orbit login replaces any existing session for the same verified
   Teams/Entra user and starts a new 60-minute PULSE session.
2. Access and refresh tokens are encrypted by the application before they are
   written to PostgreSQL.
3. Orbit email addresses and passwords are never stored.
4. Refreshing an Orbit provider token updates the encrypted tokens and provider
   expiry without extending the PULSE session expiry.
5. Expired sessions are rejected and deleted.
6. `orbit logout` deletes the persisted session immediately.
7. Changing the Fernet encryption key invalidates sessions encrypted with the
   previous key; an unreadable row must be deleted safely.
8. Database failures must fail closed. PULSE must not silently fall back to an
   in-memory authenticated session.
9. Database credentials, tokens, and encryption material must not appear in
   logs, Teams responses, source control, or migrations.

## 3. Schema

`orbit_sessions` contains one row per Teams user:

| Column | Type | Rules |
| --- | --- | --- |
| `id` | UUID text | Primary key |
| `teams_user_id` | varchar(255) | Unique, required |
| `encrypted_access_token` | text | Required, Fernet ciphertext |
| `encrypted_refresh_token` | text | Required, Fernet ciphertext |
| `provider_token_expires_at` | timestamptz | Required |
| `session_expires_at` | timestamptz | Required, fixed at login + 60 minutes |
| `created_at` | timestamptz | Required |
| `updated_at` | timestamptz | Required |
| `last_used_at` | timestamptz | Nullable |

An index on `session_expires_at` supports expiry cleanup. Alembic owns schema
creation and versioning; production code must not create tables automatically.

## 4. Architecture

- The application-layer `OrbitSessionStore` protocol remains the dependency
  boundary.
- `PostgresOrbitSessionStore` implements the protocol with SQLAlchemy async
  sessions and `asyncpg`.
- The composition root injects PostgreSQL storage whenever Orbit is configured.
- The in-memory implementation remains available only for isolated tests.
- Readiness checks verify both database connectivity and the migrated
  `orbit_sessions` table.

## 5. Configuration

Required when Orbit is enabled:

```text
PULSE_DATABASE_HOST
PULSE_DATABASE_PORT
PULSE_DATABASE_NAME
PULSE_DATABASE_USER
PULSE_DATABASE_PASSWORD
PULSE_ORBIT_SESSION_TTL_MINUTES=60
```

The database password stays in `.env`; `.env` is excluded from Git.

## 6. Acceptance criteria

- A user remains logged in after restarting PULSE within the 60-minute window.
- Provider-token refresh does not move `session_expires_at`.
- A session is unavailable after 60 minutes and its row is deleted.
- Login again starts a new 60-minute window.
- Logout removes the row.
- Stored ciphertext does not contain plaintext access or refresh tokens.
- Stale/unreadable ciphertext does not authenticate the user.
- Unit tests cover persistence, expiry, refresh-without-extension, replacement,
  logout, and encryption-at-rest.
