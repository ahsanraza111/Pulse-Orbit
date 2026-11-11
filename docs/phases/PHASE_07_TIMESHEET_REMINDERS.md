# Phase 7 — Daily Timesheet Reminders

**Status:** Implemented (R1); live Teams verification pending  
**Created:** 2026-10-16

## Objective

Send every Teams user a daily reminder, in their own PULSE chat, to fill in their Orbit
timesheet. Reminders do not depend on an Orbit login.

## Behaviour

- **Enrollment is automatic.** The first message a user sends to PULSE in their personal
  chat records the conversation and turns reminders **on**. Group chats and channels are
  never enrolled.
- **Default schedule:** Monday to Friday at **6:00 PM** in `PULSE_ORBIT_BUSINESS_TIMEZONE`
  (default `Asia/Karachi`). Weekends are skipped.
- **Each user chooses their own time:** `orbit reminders` shows a settings card with a time
  picker; `orbit reminders time 7:30 PM` also works.
- **Reminder card buttons:** *Add entry* (posts the time-entry templates), *Snooze*
  (again after `PULSE_REMINDER_SNOOZE_MINUTES`, never past the end of the day), *Skip
  today*, and *Turn off reminders*.
- **Turning reminders off requires an Orbit sign-in.** The button and
  `orbit reminders off` are visible to everyone. If the user is not signed in, PULSE shows
  the masked Orbit login card and remembers the request; reminders are turned off as soon
  as the sign-in succeeds. Turning them back on (`orbit reminders on`) needs no sign-in.
- A user enrolling after today's default time does not get an instant reminder.

## How delivery works

1. A scheduler tick finds enabled subscriptions that are due today, not yet sent today, not
   skipped, and whose time is inside the grace window (`PULSE_REMINDER_GRACE_MINUTES`,
   default 120). A snooze that expired the same day is due regardless of the window.
2. Each due reminder is **claimed** with an atomic database update (`last_sent_on`) before
   sending, so restarts and several instances cannot send it twice.
3. The card is sent with the Teams SDK's proactive `send` to the stored conversation.
4. A failed delivery releases the claim and counts a failure; after
   `PULSE_REMINDER_MAX_SEND_FAILURES` the subscription is switched off. A user who messages
   PULSE again after that is re-enrolled.

## Running the scheduler

- **Long-running process (local, VM, container):** an in-process loop checks every
  `PULSE_REMINDER_POLL_SECONDS` (default 60). Disable it with
  `PULSE_REMINDER_SCHEDULER_ENABLED=false`.
- **Serverless (for example Vercel):** a background loop cannot run. Set
  `PULSE_REMINDER_CRON_SECRET` and call `GET`/`POST /internal/reminders/tick` every minute
  from the platform's cron with `Authorization: Bearer <secret>`. The endpoint does not
  exist without a secret and rejects any other value. The check is idempotent, so calling
  it more often than needed is safe.

## Data

Table `reminder_subscriptions` (Alembic revision `20261016_01`): Teams user, conversation
ID and service URL, enabled flag, reminder time, `last_sent_on`, `snoozed_until`,
`skip_on`, `opt_out_pending`, and `failure_count`. Run `alembic upgrade head`.

## Known limitations

- Reminders go to every enrolled user, even if the timesheet is already filled. Checking
  Orbit first is deferred (the Orbit session lasts 60 minutes, so it is usually gone by
  the evening).
- A user who has never messaged PULSE cannot be reached. Enrolling on app installation
  (`installationUpdate`) is not implemented yet.
- A user without an Orbit account cannot sign in, so cannot turn reminders off.
- One timezone for everyone; no holiday calendar.
- Serverless hosting needs a cron that can run every minute (Vercel Hobby cron is limited
  to once per day).
