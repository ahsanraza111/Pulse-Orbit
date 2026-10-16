from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from pulse.application.errors import (
    OrbitAuthenticationError,
    OrbitMutationUncertainError,
    OrbitNotFoundError,
    OrbitProviderError,
)
from pulse.application.orbit_models import (
    CreatedTimesheetEntry,
    OrbitEmployee,
    OrbitProject,
    OrbitSession,
    OrbitTask,
    OrbitTimesheetEntry,
    PendingEntryChange,
    PendingTimesheetEntry,
    TimesheetEntryBatch,
)

ENTRY_SELECT = (
    "id,entry_date,duration_minutes,description,status,project_id,task_id,"
    "project:projects(name),task:tasks(name)"
)


class SupabaseOrbitClient:
    def __init__(self, client: httpx.AsyncClient, *, base_url: str, api_key: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def sign_in(self, email: str, password: str) -> OrbitSession:
        response = await self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
            authenticated=False,
            auth_operation=True,
        )
        return self._parse_session(response)

    async def refresh(self, refresh_token: str) -> OrbitSession:
        response = await self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
            authenticated=False,
            auth_operation=True,
        )
        return self._parse_session(response)

    async def get_authenticated_user_id(self, access_token: str) -> str:
        response = await self._request("GET", "/auth/v1/user", access_token=access_token)
        payload = self._json_object(response)
        user_id = payload.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise OrbitProviderError("Orbit returned an invalid user response.")
        return user_id

    async def get_employee(self, access_token: str, auth_user_id: str) -> OrbitEmployee:
        response = await self._request(
            "GET",
            "/rest/v1/employees",
            access_token=access_token,
            params={
                "user_id": f"eq.{auth_user_id}",
                "select": "id,organization_id",
                "limit": "2",
            },
        )
        rows = self._json_list(response)
        if len(rows) != 1:
            raise OrbitNotFoundError("A unique Orbit employee profile could not be resolved.")
        employee_id = rows[0].get("id")
        organization_id = rows[0].get("organization_id")
        if not isinstance(employee_id, str) or not isinstance(organization_id, str):
            raise OrbitProviderError("Orbit returned an invalid employee response.")
        return OrbitEmployee(id=employee_id, organization_id=organization_id)

    async def list_assigned_projects(
        self, access_token: str, employee_id: str
    ) -> list[OrbitProject]:
        # Orbit's current example queries projects with the user's JWT and relies on
        # RLS to return only authorized rows. employee_id remains in this port so the
        # adapter can move to project_team_members without changing application code.
        _ = employee_id
        response = await self._request(
            "GET",
            "/rest/v1/projects",
            access_token=access_token,
            params={"select": "id,name", "order": "name.asc"},
        )
        projects: list[OrbitProject] = []
        for row in self._json_list(response):
            item_id, name = row.get("id"), row.get("name")
            if isinstance(item_id, str) and isinstance(name, str):
                projects.append(OrbitProject(id=item_id, name=name))
        return projects

    async def list_active_tasks(self, access_token: str, project_id: str) -> list[OrbitTask]:
        response = await self._request(
            "GET",
            "/rest/v1/project_tasks",
            access_token=access_token,
            params={
                "project_id": f"eq.{project_id}",
                "is_active": "eq.true",
                "select": "task:tasks(id,name)",
            },
        )
        tasks: list[OrbitTask] = []
        for row in self._json_list(response):
            task = row.get("task")
            if not isinstance(task, dict):
                continue
            item_id, name = task.get("id"), task.get("name")
            if isinstance(item_id, str) and isinstance(name, str):
                tasks.append(OrbitTask(id=item_id, name=name))
        return tasks

    async def create_entry(
        self, access_token: str, entry: PendingTimesheetEntry
    ) -> CreatedTimesheetEntry:
        payload = {
            "description": entry.description,
            "duration_minutes": entry.duration_minutes,
            "entry_date": entry.entry_date.isoformat(),
            "project_id": entry.project_id,
            "status": "draft",
            "task_id": entry.task_id,
            "employee_id": entry.employee_id,
            "organization_id": entry.organization_id,
        }
        response = await self._request(
            "POST",
            "/rest/v1/timesheet_entries",
            access_token=access_token,
            json=payload,
            extra_headers={"Prefer": "return=representation"},
            mutation=True,
        )

        rows = self._json_list(response)
        if len(rows) != 1 or not isinstance(rows[0].get("id"), str):
            raise OrbitMutationUncertainError(
                "Orbit did not return the created entry. Check Orbit before retrying."
            )
        row = rows[0]
        returned_date = self._parse_date(row.get("entry_date"), entry.entry_date)
        duration = row.get("duration_minutes", entry.duration_minutes)
        if not isinstance(duration, int):
            duration = entry.duration_minutes
        return CreatedTimesheetEntry(
            id=row["id"], entry_date=returned_date, duration_minutes=duration
        )

    async def list_entries(
        self,
        access_token: str,
        *,
        employee_id: str,
        start_date: date,
        end_date: date,
        project_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> TimesheetEntryBatch:
        params = {
            "employee_id": f"eq.{employee_id}",
            "and": (
                f"(entry_date.gte.{start_date.isoformat()},"
                f"entry_date.lte.{end_date.isoformat()})"
            ),
            "select": ENTRY_SELECT,
            "order": "entry_date.desc,id.desc",
            "limit": str(limit),
            "offset": str(offset),
        }
        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if status:
            params["status"] = f"eq.{status}"
        response = await self._request(
            "GET",
            "/rest/v1/timesheet_entries",
            access_token=access_token,
            params=params,
            extra_headers={"Prefer": "count=exact"},
        )
        rows = self._json_list(response)
        entries = tuple(self._parse_timesheet_entry(row) for row in rows)
        return TimesheetEntryBatch(
            entries=entries,
            total_count=self._parse_total_count(response, offset + len(entries)),
        )

    async def get_entry(
        self,
        access_token: str,
        *,
        employee_id: str,
        entry_id: str,
    ) -> OrbitTimesheetEntry | None:
        response = await self._request(
            "GET",
            "/rest/v1/timesheet_entries",
            access_token=access_token,
            params={
                "id": f"eq.{entry_id}",
                "employee_id": f"eq.{employee_id}",
                "select": ENTRY_SELECT,
                "limit": "2",
            },
        )
        rows = self._json_list(response)
        if not rows:
            return None
        if len(rows) != 1:
            raise OrbitProviderError("Orbit returned an ambiguous timesheet entry.")
        return self._parse_timesheet_entry(rows[0])

    async def update_entry(
        self, access_token: str, change: PendingEntryChange
    ) -> OrbitTimesheetEntry:
        after = change.after
        fields = change.changed_fields
        if change.kind != "update" or after is None or not fields:
            raise OrbitProviderError("There is no Orbit entry change to apply.")
        values: dict[str, Any] = {
            "entry_date": after.entry_date.isoformat(),
            "duration_minutes": after.duration_minutes,
            "description": after.description,
            "project_id": after.project_id,
            "task_id": after.task_id,
        }
        response = await self._request(
            "PATCH",
            "/rest/v1/timesheet_entries",
            access_token=access_token,
            params={
                "id": f"eq.{change.before.id}",
                "employee_id": f"eq.{change.employee_id}",
                "status": "eq.draft",
                "select": ENTRY_SELECT,
            },
            json={name: values[name] for name in fields},
            extra_headers={"Prefer": "return=representation"},
            mutation=True,
            mutation_message=(
                "Orbit did not confirm whether the entry was updated. "
                "Check Orbit before retrying."
            ),
        )
        rows = self._json_list(response)
        if not rows:
            raise OrbitNotFoundError(
                "Orbit did not update the entry. It was not found or is no longer a draft."
            )
        if len(rows) != 1:
            raise OrbitMutationUncertainError(
                "Orbit returned an unexpected update result. Check Orbit before retrying."
            )
        return self._parse_timesheet_entry(rows[0])

    async def delete_entry(
        self,
        access_token: str,
        *,
        employee_id: str,
        entry_id: str,
    ) -> None:
        response = await self._request(
            "DELETE",
            "/rest/v1/timesheet_entries",
            access_token=access_token,
            params={
                "id": f"eq.{entry_id}",
                "employee_id": f"eq.{employee_id}",
                "status": "eq.draft",
                "select": "id",
            },
            extra_headers={"Prefer": "return=representation"},
            mutation=True,
            mutation_message=(
                "Orbit did not confirm whether the entry was deleted. "
                "Check Orbit before retrying."
            ),
        )
        rows = self._json_list(response)
        if not rows:
            raise OrbitNotFoundError(
                "Orbit did not delete the entry. It was not found or is no longer a draft."
            )
        if len(rows) != 1:
            raise OrbitMutationUncertainError(
                "Orbit returned an unexpected delete result. Check Orbit before retrying."
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        authenticated: bool = True,
        auth_operation: bool = False,
        mutation: bool = False,
        mutation_message: str = (
            "Orbit did not confirm whether the entry was created. "
            "Check Orbit before retrying."
        ),
    ) -> httpx.Response:
        headers = {"apikey": self._api_key, "Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if not access_token:
                raise OrbitAuthenticationError("Orbit authentication is required.")
            headers["Authorization"] = f"Bearer {access_token}"
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
                json=json,
            )
        except httpx.TimeoutException as exc:
            if mutation:
                raise OrbitMutationUncertainError(mutation_message) from exc
            raise OrbitProviderError("Orbit timed out. Please try again.") from exc
        except httpx.RequestError as exc:
            raise OrbitProviderError("Orbit is temporarily unreachable.") from exc

        if auth_operation and response.status_code in {400, 401, 403, 422}:
            raise OrbitAuthenticationError("Orbit rejected the supplied credentials or session.")
        if response.status_code == 401:
            raise OrbitAuthenticationError("The Orbit session is no longer valid.")
        if response.status_code == 403:
            raise OrbitProviderError("Orbit denied this operation.")
        if response.is_error:
            raise OrbitProviderError(
                f"Orbit request failed with status {response.status_code}."
            )
        return response

    @staticmethod
    def _parse_session(response: httpx.Response) -> OrbitSession:
        payload = SupabaseOrbitClient._json_object(response)
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise OrbitProviderError("Orbit returned an invalid authentication response.")
        expires_at_value = payload.get("expires_at")
        if isinstance(expires_at_value, int | float):
            expires_at = datetime.fromtimestamp(expires_at_value, tz=UTC)
        else:
            expires_in = payload.get("expires_in", 3600)
            if not isinstance(expires_in, int | float):
                expires_in = 3600
            expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
        return OrbitSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OrbitProviderError("Orbit returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise OrbitProviderError("Orbit returned an unexpected response shape.")
        return payload

    @staticmethod
    def _json_list(response: httpx.Response) -> list[dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OrbitProviderError("Orbit returned malformed JSON.") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise OrbitProviderError("Orbit returned an unexpected response shape.")
        return payload

    @staticmethod
    def _parse_date(value: Any, fallback: date) -> date:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _parse_timesheet_entry(row: dict[str, Any]) -> OrbitTimesheetEntry:
        entry_id = row.get("id")
        entry_date_value = row.get("entry_date")
        duration = row.get("duration_minutes")
        project = row.get("project")
        task = row.get("task")
        if (
            not isinstance(entry_id, str)
            or not isinstance(entry_date_value, str)
            or not isinstance(duration, int)
            or not isinstance(project, dict)
            or not isinstance(task, dict)
            or not isinstance(project.get("name"), str)
            or not isinstance(task.get("name"), str)
        ):
            raise OrbitProviderError("Orbit returned an invalid timesheet entry.")
        try:
            entry_date = date.fromisoformat(entry_date_value)
        except ValueError as exc:
            raise OrbitProviderError("Orbit returned an invalid timesheet date.") from exc
        description = row.get("description")
        status = row.get("status")
        project_id = row.get("project_id")
        task_id = row.get("task_id")
        return OrbitTimesheetEntry(
            id=entry_id,
            entry_date=entry_date,
            duration_minutes=duration,
            description=description if isinstance(description, str) else "",
            status=status if isinstance(status, str) and status else "unknown",
            project_name=project["name"],
            task_name=task["name"],
            project_id=project_id if isinstance(project_id, str) else "",
            task_id=task_id if isinstance(task_id, str) else "",
        )

    @staticmethod
    def _parse_total_count(response: httpx.Response, fallback: int) -> int:
        content_range = response.headers.get("content-range", "")
        _, separator, total = content_range.rpartition("/")
        if separator and total.isdigit():
            return int(total)
        return fallback
