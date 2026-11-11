from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pulse.application.reminder_models import ReminderSubscription
from pulse.infrastructure.database import ReminderSubscriptionRecord


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_model(record: ReminderSubscriptionRecord) -> ReminderSubscription:
    return ReminderSubscription(
        teams_user_id=record.teams_user_id,
        conversation_id=record.conversation_id,
        service_url=record.service_url,
        enabled=record.enabled,
        reminder_time=record.reminder_time,
        last_sent_on=record.last_sent_on,
        snoozed_until=_utc(record.snoozed_until) if record.snoozed_until else None,
        skip_on=record.skip_on,
        opt_out_pending=record.opt_out_pending,
        failure_count=record.failure_count,
    )


class PostgresReminderStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def upsert_conversation(
        self,
        teams_user_id: str,
        conversation_id: str,
        service_url: str,
        *,
        default_time: time,
        initial_last_sent_on: date | None,
        now: datetime,
    ) -> ReminderSubscription:
        now = _utc(now)
        for attempt in (1, 2):
            try:
                async with self._sessions() as database_session:
                    async with database_session.begin():
                        record = await database_session.get(
                            ReminderSubscriptionRecord, teams_user_id, with_for_update=True
                        )
                        if record is None:
                            record = ReminderSubscriptionRecord(
                                teams_user_id=teams_user_id,
                                conversation_id=conversation_id,
                                service_url=service_url,
                                enabled=True,
                                reminder_time=default_time,
                                last_sent_on=initial_last_sent_on,
                                opt_out_pending=False,
                                failure_count=0,
                                created_at=now,
                                updated_at=now,
                            )
                            database_session.add(record)
                        else:
                            record.conversation_id = conversation_id
                            record.service_url = service_url
                            if not record.enabled and record.failure_count > 0:
                                # Switched off only because delivery failed; the user is
                                # reachable again, so resume.
                                record.enabled = True
                                record.failure_count = 0
                            record.updated_at = now
                        return _to_model(record)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    async def get(self, teams_user_id: str) -> ReminderSubscription | None:
        async with self._sessions() as database_session:
            record = await database_session.get(ReminderSubscriptionRecord, teams_user_id)
            return _to_model(record) if record else None

    async def list_due(
        self,
        *,
        local_date: date,
        window_start: time,
        window_end: time,
        day_start: datetime,
        now: datetime,
    ) -> list[ReminderSubscription]:
        day_start, now = _utc(day_start), _utc(now)
        record = ReminderSubscriptionRecord
        snooze_expired_today = and_(
            record.snoozed_until.is_not(None),
            record.snoozed_until >= day_start,
            record.snoozed_until <= now,
        )
        scheduled_in_window = and_(
            or_(record.snoozed_until.is_(None), record.snoozed_until < day_start),
            record.reminder_time >= window_start,
            record.reminder_time <= window_end,
        )
        statement = (
            select(record)
            .where(
                record.enabled.is_(True),
                or_(record.last_sent_on.is_(None), record.last_sent_on < local_date),
                or_(record.skip_on.is_(None), record.skip_on != local_date),
                or_(snooze_expired_today, scheduled_in_window),
            )
            .order_by(record.reminder_time, record.teams_user_id)
        )
        async with self._sessions() as database_session:
            rows = await database_session.scalars(statement)
            return [_to_model(row) for row in rows]

    async def claim(self, teams_user_id: str, local_date: date) -> bool:
        record = ReminderSubscriptionRecord
        statement = (
            update(record)
            .where(
                record.teams_user_id == teams_user_id,
                record.enabled.is_(True),
                or_(record.last_sent_on.is_(None), record.last_sent_on < local_date),
            )
            .values(last_sent_on=local_date, snoozed_until=None)
        )
        async with self._sessions() as database_session:
            async with database_session.begin():
                result = await database_session.execute(statement)
                return bool(result.rowcount == 1)

    async def record_delivered(self, teams_user_id: str) -> None:
        await self._update(teams_user_id, failure_count=0)

    async def record_failure(self, teams_user_id: str, *, max_failures: int) -> bool:
        async with self._sessions() as database_session:
            async with database_session.begin():
                record = await database_session.get(
                    ReminderSubscriptionRecord, teams_user_id, with_for_update=True
                )
                if record is None:
                    return False
                record.failure_count += 1
                record.last_sent_on = None  # release today's claim so a later tick retries
                disabled = record.failure_count >= max_failures
                if disabled:
                    record.enabled = False
                return disabled

    async def set_time(self, teams_user_id: str, reminder_time: time) -> bool:
        return await self._update(teams_user_id, reminder_time=reminder_time)

    async def set_enabled(self, teams_user_id: str, enabled: bool) -> bool:
        return await self._update(
            teams_user_id, enabled=enabled, opt_out_pending=False, failure_count=0
        )

    async def set_opt_out_pending(self, teams_user_id: str, pending: bool) -> bool:
        return await self._update(teams_user_id, opt_out_pending=pending)

    async def snooze(self, teams_user_id: str, until: datetime) -> bool:
        return await self._update(teams_user_id, snoozed_until=_utc(until), last_sent_on=None)

    async def skip_on(self, teams_user_id: str, local_date: date) -> bool:
        return await self._update(teams_user_id, skip_on=local_date)

    async def _update(self, teams_user_id: str, **values: object) -> bool:
        statement = (
            update(ReminderSubscriptionRecord)
            .where(ReminderSubscriptionRecord.teams_user_id == teams_user_id)
            .values(**values, updated_at=datetime.now(UTC))
        )
        async with self._sessions() as database_session:
            async with database_session.begin():
                result = await database_session.execute(statement)
                return bool(result.rowcount == 1)
