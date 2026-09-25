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
    PendingTimesheetEntry,
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
                raise OrbitMutationUncertainError(
                    "Orbit did not confirm whether the entry was created. "
                    "Check Orbit before retrying."
                ) from exc
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
