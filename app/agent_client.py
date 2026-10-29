"""Typed httpx wrapper for every agent endpoint the dashboard uses (spec section 4)."""

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from .agent_models import (
    Draft,
    Followup,
    Health,
    Preview,
    RunResult,
    TaskCreate,
    TaskDetail,
    TaskPage,
    ThreadSummary,
    User,
)

M = TypeVar("M", bound=BaseModel)


class AgentError(Exception):
    """The agent answered with an error response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class AgentAuthError(AgentError):
    """401/403: the dashboard's key is invalid or not authorized for this resource."""


class AgentNotFound(AgentError):
    pass


class AgentUnavailable(Exception):
    """The agent could not be reached, timed out, or returned 5xx."""


class AgentClient:
    def __init__(
        self, base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "followup-demo-dashboard/1.1"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None = None,
        json: Any = None,
        params: dict | None = None,
    ) -> httpx.Response:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            resp = await self._http.request(method, path, json=json, params=params, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise AgentUnavailable(str(exc) or exc.__class__.__name__) from exc
        if resp.status_code >= 500:
            raise AgentUnavailable(f"Agent returned {resp.status_code}")
        if resp.status_code >= 400:
            code, message = "error", resp.reason_phrase
            try:
                err = resp.json().get("error", {})
                code, message = err.get("code", code), err.get("message", message)
            except ValueError:
                pass
            cls = {401: AgentAuthError, 403: AgentAuthError, 404: AgentNotFound}.get(resp.status_code, AgentError)
            raise cls(resp.status_code, code, message)
        return resp

    async def _get(self, path: str, model: type[M], **kw: Any) -> M:
        return model.model_validate((await self._request("GET", path, **kw)).json())

    async def _post(self, path: str, model: type[M], **kw: Any) -> M:
        return model.model_validate((await self._request("POST", path, **kw)).json())

    # System
    async def health(self) -> Health:
        return await self._get("/v1/health", Health)

    # Users
    async def list_users(self) -> list[User]:
        data = (await self._request("GET", "/v1/users")).json()["data"]
        return [User.model_validate(u) for u in data]

    async def update_user(self, user_id: str, changes: dict, *, idempotency_key: str) -> User:
        resp = await self._request("PATCH", f"/v1/users/{user_id}", json=changes, idempotency_key=idempotency_key)
        return User.model_validate(resp.json())

    async def connect_url(self, user_id: str) -> str:
        return (await self._request("GET", f"/v1/users/{user_id}/connect")).json()["url"]

    async def list_threads(self, user_id: str, q: str = "", limit: int = 10) -> list[ThreadSummary]:
        params = {"limit": limit, **({"q": q} if q else {})}
        data = (await self._request("GET", f"/v1/users/{user_id}/threads", params=params)).json()["data"]
        return [ThreadSummary.model_validate(t) for t in data]

    # Tasks
    async def create_task(self, task: TaskCreate, *, idempotency_key: str) -> TaskDetail:
        body = task.model_dump(mode="json", exclude_none=True)
        return await self._post("/v1/tasks", TaskDetail, json=body, idempotency_key=idempotency_key)

    async def list_tasks(self, **filters: str) -> list:
        """All tasks, following pagination."""
        tasks, cursor, seen = [], None, set()
        for _ in range(100):
            params = {k: v for k, v in filters.items() if v} | {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = await self._get("/v1/tasks", TaskPage, params=params)
            tasks.extend(page.data)
            cursor = page.next_cursor
            if not cursor:
                return tasks
            if cursor in seen:
                raise AgentError(502, "bad_cursor", "Agent returned a repeated pagination cursor")
            seen.add(cursor)
        raise AgentError(502, "too_many_pages", "Task list exceeded 100 pages")

    async def get_task(self, task_id: str) -> TaskDetail:
        return await self._get(f"/v1/tasks/{task_id}", TaskDetail)

    async def update_task(self, task_id: str, changes: dict, *, idempotency_key: str) -> TaskDetail:
        resp = await self._request("PATCH", f"/v1/tasks/{task_id}", json=changes, idempotency_key=idempotency_key)
        return TaskDetail.model_validate(resp.json())

    async def preview(self, task_id: str) -> Preview:
        return await self._post(f"/v1/tasks/{task_id}/preview", Preview)

    async def run(self, task_id: str, *, idempotency_key: str) -> RunResult:
        return await self._post(f"/v1/tasks/{task_id}/run", RunResult, idempotency_key=idempotency_key)

    async def pause(self, task_id: str, *, idempotency_key: str) -> TaskDetail:
        return await self._post(f"/v1/tasks/{task_id}/pause", TaskDetail, idempotency_key=idempotency_key)

    async def resume(self, task_id: str, *, idempotency_key: str) -> TaskDetail:
        return await self._post(f"/v1/tasks/{task_id}/resume", TaskDetail, idempotency_key=idempotency_key)

    async def stop(self, task_id: str, reason: str, *, idempotency_key: str) -> TaskDetail:
        return await self._post(
            f"/v1/tasks/{task_id}/stop", TaskDetail, json={"reason": reason}, idempotency_key=idempotency_key
        )

    async def post_event(self, task_id: str, event: dict, *, idempotency_key: str) -> TaskDetail:
        return await self._post(f"/v1/tasks/{task_id}/events", TaskDetail, json=event, idempotency_key=idempotency_key)

    # Drafts
    async def list_drafts(self) -> list[Draft]:
        data = (await self._request("GET", "/v1/drafts")).json()["data"]
        return [Draft.model_validate(d) for d in data]

    async def approve(
        self, draft_id: str, *, idempotency_key: str, subject: str | None = None, body: str | None = None
    ) -> Followup:
        edits = {k: v for k, v in {"subject": subject, "body": body}.items() if v is not None}
        return await self._post(
            f"/v1/drafts/{draft_id}/approve", Followup, json=edits or None, idempotency_key=idempotency_key
        )

    async def reject(self, draft_id: str, feedback: str, *, idempotency_key: str) -> Followup:
        return await self._post(
            f"/v1/drafts/{draft_id}/reject", Followup, json={"feedback": feedback}, idempotency_key=idempotency_key
        )
