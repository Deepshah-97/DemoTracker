"""Demo dashboard: FastAPI app, pages, form handlers, webhook endpoint and SSE stream."""

import asyncio
import json
import logging
import re
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import select
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, webhooks
from .agent_client import AgentAuthError, AgentClient, AgentError, AgentNotFound, AgentUnavailable
from .agent_models import SCENARIO_GUIDE, SCENARIOS, Client, Task, TaskCreate
from .broadcast import Message, broadcaster
from .config import Settings, get_settings
from .mock_agent import MockAgent

log = logging.getLogger("dashboard")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")

BOARD_COLUMNS = [
    ("waiting", "Waiting"),
    ("draft_pending", "Draft pending"),
    ("followed_up", "Followed up"),
    ("replied", "Replied"),
    ("stopped", "Stopped"),
]
EXTRA_LANE = ("exhausted", "error")
STATUS_LABELS = dict(BOARD_COLUMNS) | {"exhausted": "Exhausted", "error": "Error"}
IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


# --------------------------------------------------------------------------------------------
# App state
# --------------------------------------------------------------------------------------------
class AgentStatus:
    def __init__(self) -> None:
        self.ok = True
        self.message = ""
        self.test_mode = False
        self.checked = False

    def up(self, test_mode: bool | None = None) -> None:
        self.ok, self.message, self.checked = True, "", True
        if test_mode is not None:
            self.test_mode = test_mode

    def down(self, message: str) -> None:
        self.ok, self.message, self.checked = False, message, True


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_for_startup()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_engine(settings.database_path)
        mock: MockAgent | None = None
        agent_transport = transport
        base_url = settings.agent_base_url
        if settings.mock_mode:
            mock = MockAgent(settings.agent_webhook_secret, event_delay=settings.mock_event_delay_seconds)
            mock.deliver = lambda raw, sig: _receive(app, raw, sig, simulated=True)
            agent_transport = httpx.ASGITransport(app=mock.app)
            base_url = "http://mock-agent.internal"
        app.state.mock = mock
        app.state.agent = AgentClient(
            base_url,
            settings.agent_api_key or "mock",
            transport=agent_transport,
            timeout=settings.agent_timeout_seconds,
        )
        app.state.status = AgentStatus()
        app.state.locks = defaultdict(asyncio.Lock)
        await _check_health(app)
        await _reconcile(app)
        yield
        await app.state.agent.aclose()

    app = FastAPI(title="Follow-Up Agent Demo Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="demo_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    limiter = auth.LoginRateLimiter(settings.login_attempts_per_minute)

    # ----------------------------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------------------------
    def agent() -> AgentClient:
        return app.state.agent

    def flash(request: Request, text: str, kind: str = "info") -> None:
        request.session.setdefault("flash", []).append({"kind": kind, "text": text})

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def mode_info() -> dict[str, str]:
        status: AgentStatus = app.state.status
        if settings.mock_mode:
            return {"key": "mock", "label": "Mock mode: all data SIMULATED"}
        if status.test_mode:
            return {"key": "test-live", "label": "Test-live: allowlisted test mailboxes"}
        return {"key": "live", "label": "Live: demo timing disabled"}

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        demo_on = db.get_setting("demo_mode", "off") == "on"
        base_ctx = {
            "csrf": auth.csrf_token(request),
            "flashes": request.session.pop("flash", []),
            "mode": mode_info(),
            "mock": settings.mock_mode,
            "demo_on": demo_on,
            "status": app.state.status,
            "status_labels": STATUS_LABELS,
            "scenario_guide": SCENARIO_GUIDE,
            "new_key": lambda: uuid.uuid4().hex,
            "path": request.url.path,
            "webhook_url": settings.webhook_url,
        }
        return templates.TemplateResponse(request, name, base_ctx | ctx, status_code=status_code)

    def describe(exc: Exception) -> str:
        if isinstance(exc, AgentUnavailable):
            return "The agent is unreachable right now. Nothing was sent; showing cached data."
        if isinstance(exc, AgentAuthError):
            return f"The agent refused this request ({exc.status_code}): {exc.message}"
        if isinstance(exc, AgentError):
            return f"Agent said: {exc.message}"
        return str(exc)

    def note_failure(exc: Exception) -> None:
        if isinstance(exc, AgentUnavailable):
            app.state.status.down(describe(exc))
        elif isinstance(exc, AgentAuthError):
            app.state.status.down(describe(exc))

    async def call(coro):
        """Run an agent call and keep the availability banner in sync."""
        try:
            result = await coro
        except (AgentUnavailable, AgentAuthError) as exc:
            note_failure(exc)
            raise
        app.state.status.up()
        return result

    async def idempotent(key: str, action: str, target: str | None, fn) -> tuple[str, Any]:
        """Call the agent once per Idempotency-Key, even across double clicks and retries.

        Returns ("ok", result), ("duplicate", None) or ("error", exception).
        """
        if not IDEM_RE.match(key or ""):
            raise HTTPException(400, "Missing or invalid idempotency key")
        async with app.state.locks[key]:
            with db.session() as s:
                row = s.get(db.ActionRow, key)
                if row and row.state != "failed":
                    return "duplicate", None
                row = row or db.ActionRow(idempotency_key=key, action=action, target_id=target)
                row.state, row.updated_at = "in_progress", db.utcnow()
                s.add(row)
                s.commit()
            try:
                result = await call(fn())
            except (AgentError, AgentUnavailable) as exc:
                _set_action(key, "failed", error=describe(exc))
                return "error", exc
            _set_action(key, "accepted" if action == "approve" else "done")
            return "ok", result

    # ----------------------------------------------------------------------------------------
    # Auth
    # ----------------------------------------------------------------------------------------
    async def require_login(request: Request) -> None:
        if not auth.is_authenticated(request, settings.session_max_age_seconds):
            if request.headers.get("hx-request"):
                raise HTTPException(401, headers={"HX-Redirect": "/login"})
            raise HTTPException(303, headers={"Location": "/login?expired=1"})

    authed = [Depends(require_login)]
    mutate = [Depends(require_login), Depends(auth.verify_csrf)]

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, expired: int = 0):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Your session expired. Please sign in again." if expired else None,
                "mode": mode_info(),
                "csrf": auth.csrf_token(request),
            },
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(...), csrf_token: str = Form("")):
        await auth.verify_csrf(request)
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Too many attempts. Wait a minute and try again.",
                    "mode": mode_info(),
                    "csrf": auth.csrf_token(request),
                },
                status_code=429,
            )
        if not auth.check_password(password, settings.demo_password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Wrong password.", "mode": mode_info(), "csrf": auth.csrf_token(request)},
                status_code=401,
            )
        auth.login(request)
        return redirect("/")

    @app.post("/logout", dependencies=mutate)
    async def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # ----------------------------------------------------------------------------------------
    # Pages
    # ----------------------------------------------------------------------------------------
    def recent_events(limit: int = 25) -> list[db.WebhookEventRow]:
        with db.session() as s:
            q = select(db.WebhookEventRow).order_by(db.WebhookEventRow.seq.desc()).limit(limit)  # type: ignore[union-attr]
            return list(s.exec(q))

    def board_ctx() -> dict:
        cards = db.list_cards()
        cols = {k: [c for c in cards if c.status == k] for k, _ in BOARD_COLUMNS}
        extra = [c for c in cards if c.status in EXTRA_LANE]
        return {"columns": BOARD_COLUMNS, "cols": cols, "extra": extra, "cards": cards}

    def counters() -> dict[str, int]:
        cards = db.list_cards()
        return {
            "waiting": sum(c.status in ("waiting", "followed_up") and not c.paused for c in cards),
            "drafts": sum(c.status == "draft_pending" for c in cards),
            "sent": sum(c.followup_count for c in cards),
            "replied": sum(c.status == "replied" for c in cards),
        }

    @app.get("/", response_class=HTMLResponse, dependencies=authed)
    async def overview(request: Request):
        cards = db.list_cards()
        runnable = [c for c in cards if c.status in ("waiting", "followed_up") and not c.paused][:6]
        return render(
            request,
            "overview.html",
            counters=counters(),
            runnable=runnable,
            activity=recent_events(10),
            events=recent_events(),
        )

    @app.get("/partials/counters", response_class=HTMLResponse, dependencies=authed)
    async def counters_partial(request: Request):
        return render(request, "partials/counters.html", counters=counters())

    @app.get("/tasks", response_class=HTMLResponse, dependencies=authed)
    async def tasks_page(request: Request):
        return render(request, "tasks.html", events=recent_events(), **board_ctx())

    @app.get("/partials/board", response_class=HTMLResponse, dependencies=authed)
    async def board_partial(request: Request):
        return render(request, "partials/board.html", **board_ctx())

    @app.post("/refresh", dependencies=mutate)
    async def refresh(request: Request, back: str = Form("/tasks")):
        ok = await _reconcile(app)
        flash(
            request,
            "Synced with the agent." if ok else app.state.status.message or "Sync failed.",
            "success" if ok else "error",
        )
        return redirect(back if back.startswith("/") and not back.startswith("//") else "/tasks")

    async def load_users() -> list:
        try:
            return await call(agent().list_users())
        except (AgentError, AgentUnavailable):
            return []

    @app.get("/tasks/new", response_class=HTMLResponse, dependencies=authed)
    async def new_task_page(request: Request):
        users = await load_users()
        return render(
            request,
            "task_new.html",
            users=users,
            scenarios=SCENARIOS,
            form={},
            errors=[],
            events=recent_events(),
            idem=uuid.uuid4().hex,
        )

    @app.get("/partials/threads", response_class=HTMLResponse, dependencies=authed)
    async def threads_partial(request: Request, owner_email: str = "", q: str = ""):
        users = await load_users()
        user = next((u for u in users if u.email == owner_email), None)
        threads, note = [], None
        if user:
            try:
                threads = await call(agent().list_threads(user.id, q))
            except AgentNotFound:
                note = "The agent has no thread picker yet. Paste a thread ID or leave it blank."
            except (AgentError, AgentUnavailable) as exc:
                note = describe(exc)
        return render(request, "partials/threads.html", threads=threads, note=note)

    @app.post("/tasks", dependencies=mutate)
    async def create_task(request: Request):
        form = dict((await request.form()).items())
        errors: list[str] = []
        scenario = str(form.get("scenario", ""))
        guide = SCENARIO_GUIDE.get(scenario)
        trigger = str(form.get("scenario_trigger_at") or "").strip()
        trigger_iso = None
        if trigger:
            try:
                dt = datetime.fromisoformat(trigger)
                if dt.tzinfo is None:
                    raise ValueError
                trigger_iso = dt.astimezone(UTC).isoformat()
            except ValueError:
                errors.append("Baseline time must be a valid date and time.")
        if guide is None:
            errors.append("Choose a scenario.")
        elif guide["requires_trigger"] and not trigger_iso:
            errors.append(f"{guide['label']} needs a baseline time ({guide['baseline']}).")
        thread_id = str(form.get("gmail_thread_id") or form.get("thread_pick") or "").strip() or None
        if scenario == "new_lead" and not (thread_id or trigger_iso):
            errors.append("New lead needs a thread or a lead created time.")
        if scenario == "custom" and not str(form.get("goal") or "").strip():
            errors.append("Custom scenario needs a goal.")

        def opt_int(name: str) -> int | None:
            v = str(form.get(name) or "").strip()
            if not v:
                return None
            try:
                return int(v)
            except ValueError:
                errors.append(f"{name.replace('_', ' ').capitalize()} must be a whole number.")
                return None

        payload = None
        if not errors:
            try:
                payload = TaskCreate(
                    external_id=str(form.get("external_id") or "").strip() or f"DEMO-{uuid.uuid4().hex[:6].upper()}",
                    owner_email=str(form.get("owner_email", "")),
                    scenario=scenario,  # type: ignore[arg-type]
                    client=Client(
                        name=str(form.get("client_name", "")).strip(),
                        email=str(form.get("client_email", "")).strip(),
                        company=str(form.get("client_company") or "").strip() or None,
                    ),
                    goal=str(form.get("goal") or "").strip() or None,
                    gmail_thread_id=thread_id,
                    notes=str(form.get("notes") or "").strip() or None,
                    wait_business_days=opt_int("wait_business_days"),
                    max_followups=opt_int("max_followups"),
                    send_mode=(str(form.get("send_mode")) or None) if form.get("send_mode") else None,  # type: ignore[arg-type]
                    scenario_trigger_at=trigger_iso,
                    trigger_type=str(guide["baseline"]) if guide and trigger_iso and guide["baseline"] else None,
                )
                if not payload.client.name:
                    errors.append("Client name is required.")
            except ValidationError as exc:
                errors.extend(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())

        idem_key = str(form.get("idem", ""))
        if not errors and payload:
            outcome, result = await idempotent(
                idem_key, "create_task", None, lambda: agent().create_task(payload, idempotency_key=idem_key)
            )
            if outcome == "ok":
                _upsert_card(result, simulated=settings.mock_mode)
                broadcaster.publish(Message("tasks"))
                flash(request, f"Task {result.external_id} created. It is now Waiting.", "success")
                return redirect(f"/tasks/{result.id}")
            if outcome == "duplicate":
                flash(request, "That task was already submitted; it was not created twice.", "info")
                return redirect("/tasks")
            errors.append(describe(result))
        users = await load_users()
        return render(
            request,
            "task_new.html",
            status_code=422,
            users=users,
            scenarios=SCENARIOS,
            form=form,
            errors=errors,
            events=recent_events(),
            idem=idem_key if IDEM_RE.match(idem_key) else uuid.uuid4().hex,
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse, dependencies=authed)
    async def task_detail(request: Request, task_id: str):
        task, error = None, None
        try:
            task = await call(agent().get_task(task_id))
            _upsert_card(task, simulated=settings.mock_mode)
        except AgentNotFound:
            raise HTTPException(404, "Task not found") from None
        except (AgentError, AgentUnavailable) as exc:
            error = describe(exc)
        with db.session() as s:
            card = s.get(db.TaskCard, task_id)
            events = list(
                s.exec(
                    select(db.WebhookEventRow)
                    .where(db.WebhookEventRow.agent_task_id == task_id)
                    .order_by(db.WebhookEventRow.seq.desc())
                )
            )  # type: ignore[union-attr]
        if task is None and card is None:
            raise HTTPException(404, "Task not found")
        rejected = bool(task and task.paused and (task.pause_reason or "") == "draft_rejected")
        return render(
            request,
            "task_detail.html",
            task=task,
            card=card,
            error=error,
            task_events=events,
            rejected=rejected,
            events=recent_events(),
        )

    @app.post("/tasks/{task_id}/preview", response_class=HTMLResponse, dependencies=mutate)
    async def preview(request: Request, task_id: str):
        try:
            p = await call(agent().preview(task_id))
            return render(request, "partials/preview.html", preview=p, error=None)
        except (AgentError, AgentUnavailable) as exc:
            return render(request, "partials/preview.html", preview=None, error=describe(exc))

    async def task_action(request: Request, task_id: str, action: str, fn, ok_text: str, back: str | None = None):
        form = await request.form()
        key = str(form.get("idem", ""))
        outcome, result = await idempotent(key, action, task_id, fn(key))
        if outcome == "ok":
            if hasattr(result, "status") and hasattr(result, "external_id"):
                _upsert_card(result, simulated=settings.mock_mode)
                broadcaster.publish(Message("tasks"))
            flash(request, ok_text, "success")
        elif outcome == "duplicate":
            flash(request, "Already submitted; not sent twice.", "info")
        else:
            flash(request, describe(result), "error")
        return redirect(back or f"/tasks/{task_id}")

    def run_message() -> str:
        demo_on = db.get_setting("demo_mode", "off") == "on"
        if settings.mock_mode:
            return "Check requested. The SIMULATED result will appear in the live panel in a few seconds."
        if demo_on and not app.state.status.test_mode:
            return (
                "Check requested. The agent's TEST_MODE is off, so normal business-day timing and send "
                "windows still apply; the demo switch cannot override them."
            )
        return "Check requested. If a follow-up is due, a draft will appear for approval."

    @app.post("/tasks/{task_id}/run", dependencies=mutate)
    async def run_now(request: Request, task_id: str, back: str = Form("")):
        return await task_action(
            request,
            task_id,
            "run",
            lambda k: lambda: agent().run(task_id, idempotency_key=k),
            run_message(),
            back if back.startswith("/") and not back.startswith("//") else None,
        )

    @app.post("/tasks/{task_id}/pause", dependencies=mutate)
    async def pause(request: Request, task_id: str):
        return await task_action(
            request, task_id, "pause", lambda k: lambda: agent().pause(task_id, idempotency_key=k), "Task paused."
        )

    @app.post("/tasks/{task_id}/resume", dependencies=mutate)
    async def resume(request: Request, task_id: str):
        return await task_action(
            request, task_id, "resume", lambda k: lambda: agent().resume(task_id, idempotency_key=k), "Task resumed."
        )

    @app.post("/tasks/{task_id}/stop", dependencies=mutate)
    async def stop(request: Request, task_id: str, reason: str = Form("")):
        return await task_action(
            request,
            task_id,
            "stop",
            lambda k: lambda: agent().stop(task_id, reason or "Stopped from demo dashboard", idempotency_key=k),
            "Task stopped.",
        )

    @app.post("/tasks/{task_id}/notes", dependencies=mutate)
    async def edit_notes(request: Request, task_id: str, notes: str = Form("")):
        return await task_action(
            request,
            task_id,
            "notes",
            lambda k: lambda: agent().update_task(task_id, {"notes": notes}, idempotency_key=k),
            "Notes saved.",
        )

    @app.post("/tasks/{task_id}/events", dependencies=mutate)
    async def external_event(request: Request, task_id: str, type: str = Form(...)):
        if type not in {"meeting_booked", "task_closed", "opted_out"}:
            raise HTTPException(400, "Unknown event type")

        def fn(k: str):
            body = {"type": type, "occurred_at": datetime.now(UTC).isoformat(), "source_event_id": k}
            return lambda: agent().post_event(task_id, body, idempotency_key=k)

        return await task_action(request, task_id, "event", fn, f"Sent {type.replace('_', ' ')} to the agent.")

    @app.post("/tasks/{task_id}/simulate-reply", dependencies=mutate)
    async def simulate_reply(request: Request, task_id: str):
        if not app.state.mock:
            raise HTTPException(404)
        await agent()._http.post(f"/mock/tasks/{task_id}/client-reply")
        flash(request, "SIMULATED: the client replied in the thread. Click Run check now to see the stop rule.", "info")
        return redirect(f"/tasks/{task_id}")

    # Drafts --------------------------------------------------------------------------------
    @app.get("/drafts", response_class=HTMLResponse, dependencies=authed)
    async def drafts_page(request: Request):
        drafts, error = [], None
        try:
            drafts = await call(agent().list_drafts())
        except (AgentError, AgentUnavailable) as exc:
            error = describe(exc)
        with db.session() as s:
            pending = list(
                s.exec(select(db.ActionRow).where(db.ActionRow.action == "approve", db.ActionRow.state == "accepted"))
            )
        cards = {c.agent_task_id: c for c in db.list_cards()}
        return render(
            request, "drafts.html", drafts=drafts, error=error, pending=pending, cards=cards, events=recent_events()
        )

    @app.post("/drafts/{draft_id}/approve", dependencies=mutate)
    async def approve(
        request: Request,
        draft_id: str,
        task_id: str = Form(""),
        subject: str = Form(""),
        body: str = Form(""),
        original_subject: str = Form(""),
        original_body: str = Form(""),
    ):
        form = await request.form()
        key = str(form.get("idem", ""))
        edited_subject = subject if subject and subject != original_subject else None
        edited_body = body.replace("\r\n", "\n") if body and body.replace("\r\n", "\n") != original_body else None
        outcome, result = await idempotent(
            key,
            "approve",
            task_id or None,
            lambda: agent().approve(draft_id, idempotency_key=key, subject=edited_subject, body=edited_body),
        )
        if outcome == "ok":
            flash(
                request,
                "Approved. Waiting for the agent to confirm the send"
                + (" (SIMULATED)." if settings.mock_mode else "."),
                "success",
            )
        elif outcome == "duplicate":
            flash(request, "Already approved; it will not be sent twice.", "info")
        else:
            flash(request, describe(result) + " The draft was not sent.", "error")
        return redirect("/drafts")

    @app.post("/drafts/{draft_id}/reject", dependencies=mutate)
    async def reject(request: Request, draft_id: str, task_id: str = Form(""), feedback: str = Form("")):
        form = await request.form()
        key = str(form.get("idem", ""))
        outcome, result = await idempotent(
            key, "reject", task_id or None, lambda: agent().reject(draft_id, feedback, idempotency_key=key)
        )
        if outcome == "ok":
            flash(
                request,
                "Draft rejected and kept in the audit history. The task is paused: update the notes "
                "and press Resume to get a new draft.",
                "success",
            )
        elif outcome == "duplicate":
            flash(request, "Already rejected.", "info")
        else:
            flash(request, describe(result), "error")
        return redirect("/drafts")

    # Team ----------------------------------------------------------------------------------
    @app.get("/team", response_class=HTMLResponse, dependencies=authed)
    async def team_page(request: Request):
        users, error = [], None
        try:
            users = await call(agent().list_users())
        except (AgentError, AgentUnavailable) as exc:
            error = describe(exc)
        return render(request, "team.html", users=users, error=error, events=recent_events())

    @app.post("/team/{user_id}/update", dependencies=mutate)
    async def team_update(request: Request, user_id: str, field: str = Form(...), value: str = Form(...)):
        if field == "send_mode" and value in ("approve", "auto"):
            changes: dict = {"send_mode": value}
        elif field == "paused" and value in ("true", "false"):
            changes = {"paused": value == "true"}
        else:
            raise HTTPException(400, "Invalid change")
        form = await request.form()
        key = str(form.get("idem", ""))
        outcome, result = await idempotent(
            key, "user_update", user_id, lambda: agent().update_user(user_id, changes, idempotency_key=key)
        )
        if outcome == "ok":
            flash(request, f"Updated {result.name}.", "success")
        elif outcome == "error":
            flash(request, describe(result), "error")
        return redirect("/team")

    @app.post("/team/{user_id}/connect", dependencies=mutate)
    async def team_connect(request: Request, user_id: str):
        try:
            url = await call(agent().connect_url(user_id))
        except (AgentError, AgentUnavailable) as exc:
            flash(request, describe(exc), "error")
            return redirect("/team")
        if not (url.startswith("https://") or (settings.mock_mode and url.startswith("/mock/"))):
            flash(request, "The agent returned an unexpected sign-in URL; not redirecting.", "error")
            return redirect("/team")
        return redirect(url)

    @app.get("/mock/connect/{user_id}", dependencies=authed)
    async def mock_connect(request: Request, user_id: str):
        if not app.state.mock:
            raise HTTPException(404)
        await agent()._http.post(f"/mock/users/{user_id}/connected")
        flash(request, "SIMULATED: Gmail connected. No Google account was used.", "info")
        return redirect("/team")

    # Demo mode -----------------------------------------------------------------------------
    @app.post("/demo-mode", dependencies=mutate)
    async def demo_mode(request: Request, back: str = Form("/")):
        on = db.get_setting("demo_mode", "off") != "on"
        db.set_setting("demo_mode", "on" if on else "off")
        await _check_health(app)
        if on and not settings.mock_mode and not app.state.status.test_mode:
            flash(
                request,
                "Demo mode is on, but the agent's TEST_MODE is off, so normal timing rules still apply. "
                "Run check now will not skip the business-day wait.",
                "warning",
            )
        else:
            flash(request, f"Demo mode {'on' if on else 'off'}.", "info")
        return redirect(back if back.startswith("/") and not back.startswith("//") else "/")

    # Live events ---------------------------------------------------------------------------
    @app.get("/events/stream", dependencies=authed)
    async def stream(request: Request, after: int = 0):
        last = request.headers.get("last-event-id")
        try:
            cursor = int(last) if last else after
        except ValueError:
            cursor = after

        async def gen():
            q = broadcaster.subscribe()
            try:
                # Replay anything missed while disconnected, from SQLite.
                with db.session() as s:
                    missed = list(
                        s.exec(
                            select(db.WebhookEventRow)
                            .where(db.WebhookEventRow.seq > cursor)
                            .order_by(db.WebhookEventRow.seq)
                        )
                    )  # type: ignore[operator]
                for row in missed:
                    yield _event_sse(request, row)
                if missed:
                    yield {"event": "tasks", "data": "replay"}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        continue
                    if msg.kind == "webhook":
                        with db.session() as s:
                            row = s.get(db.WebhookEventRow, int(msg.data))
                        if row:
                            yield _event_sse(request, row)
                    else:
                        yield {"event": msg.kind, "data": msg.data or "changed"}
            finally:
                broadcaster.unsubscribe(q)

        return EventSourceResponse(gen(), ping=15)

    def _event_sse(request: Request, row: db.WebhookEventRow) -> dict:
        html = templates.get_template("partials/event.html").render(e=row, mock=settings.mock_mode)
        return {"event": "webhook", "id": str(row.seq), "data": html}

    @app.post("/webhooks/agent")
    async def webhook(request: Request, background: BackgroundTasks):
        raw = await request.body()
        result = _receive(app, raw, request.headers.get("x-signature"), simulated=False, background=background)
        if result.status_code != 200:
            return JSONResponse(
                {"error": {"code": "rejected", "message": "Invalid webhook"}}, status_code=result.status_code
            )
        return {"ok": True, "duplicate": result.duplicate}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "agent_ok": app.state.status.ok, "mode": mode_info()["key"]}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (303, 307) and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if exc.status_code == 401 and exc.headers and "HX-Redirect" in exc.headers:
            return Response(status_code=401, headers=exc.headers)
        if request.url.path.startswith("/webhooks") or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                {"error": {"code": str(exc.status_code), "message": exc.detail}}, status_code=exc.status_code
            )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"code": exc.status_code, "detail": exc.detail, "mode": mode_info()},
            status_code=exc.status_code,
        )

    return app


# --------------------------------------------------------------------------------------------
# Module-level helpers shared with the mock deliverer
# --------------------------------------------------------------------------------------------
def _set_action(key: str, state: str, *, error: str | None = None) -> None:
    with db.session() as s:
        row = s.get(db.ActionRow, key)
        if row:
            row.state, row.error, row.updated_at = state, error, db.utcnow()
            s.add(row)
            s.commit()


def _upsert_card(task: Task, *, simulated: bool) -> None:
    with db.session() as s:
        card = s.get(db.TaskCard, task.id) or db.TaskCard(agent_task_id=task.id, external_id=task.external_id)
        card.external_id = task.external_id
        card.owner_email = task.owner_email
        if task.client:
            card.client_name, card.client_email, card.client_company = (
                task.client.name,
                task.client.email,
                task.client.company,
            )
        card.scenario = task.scenario
        card.status = task.status
        card.paused = task.paused
        card.followup_count = task.followup_count
        card.max_followups = task.max_followups
        card.next_check_at = task.next_check_at
        card.simulated = simulated or bool(getattr(task, "simulated", False))
        card.updated_at = db.utcnow()
        s.add(card)
        s.commit()


async def _check_health(app: FastAPI) -> None:
    try:
        h = await app.state.agent.health()
        app.state.status.up(test_mode=h.test_mode)
    except (AgentError, AgentUnavailable) as exc:
        app.state.status.down(
            "The agent is unreachable right now. Showing cached data."
            if isinstance(exc, AgentUnavailable)
            else f"Agent health check failed: {exc}"
        )


async def _reconcile(app: FastAPI) -> bool:
    """Re-sync every task card from GET /v1/tasks (startup and the Refresh button)."""
    settings: Settings = app.state.settings
    try:
        tasks = await app.state.agent.list_tasks()
    except (AgentError, AgentUnavailable) as exc:
        app.state.status.down(
            "The agent is unreachable right now. Showing cached data."
            if isinstance(exc, AgentUnavailable)
            else f"Agent refused the sync: {exc}"
        )
        return False
    app.state.status.up()
    ids = {t.id for t in tasks}
    for t in tasks:
        _upsert_card(t, simulated=settings.mock_mode)
    with db.session() as s:
        for card in s.exec(select(db.TaskCard)).all():
            if card.agent_task_id not in ids:
                s.delete(card)
        s.commit()
    broadcaster.publish(Message("tasks"))
    return True


async def _refresh_task(app: FastAPI, task_id: str) -> None:
    try:
        task = await app.state.agent.get_task(task_id)
    except Exception as exc:  # never let a background refresh crash the webhook path
        log.warning("Could not refresh task %s after webhook: %s", task_id, exc)
        return
    _upsert_card(task, simulated=app.state.settings.mock_mode)
    broadcaster.publish(Message("tasks"))


def _receive(
    app: FastAPI, raw: bytes, signature: str | None, *, simulated: bool, background: BackgroundTasks | None = None
) -> webhooks.WebhookResult:
    result = webhooks.process(raw, signature, app.state.settings.agent_webhook_secret, simulated=simulated)
    if result.needs_task_refresh:
        if background is not None:
            background.add_task(_refresh_task, app, result.needs_task_refresh)
        else:
            asyncio.get_running_loop().create_task(_refresh_task(app, result.needs_task_refresh))
    return result


def display_payload(raw: str) -> str:
    """Pretty JSON for the event panel, with email bodies truncated (B5 privacy)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw[:2000]

    def redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (v[:120] + "... (truncated)" if k == "body" and isinstance(v, str) and len(v) > 120 else redact(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [redact(v) for v in obj]
        return obj

    return json.dumps(redact(data), indent=2)[:4000]


def _fmt_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone(UTC).strftime("%b %d, %H:%M UTC")


templates.env.filters["pretty"] = display_payload
templates.env.filters["when"] = _fmt_time
templates.env.globals["json_loads"] = json.loads

app_factory = create_app
