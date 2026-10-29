"""Built-in fake agent for AGENT_BASE_URL=mock.

It serves the same v1.1 HTTP contract in-process (via httpx.ASGITransport), so the dashboard
exercises exactly the code path it uses against the real agent. Every record and event it
produces is SIMULATED: it never calls Gmail or Claude and never sends a real email.
"""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .webhooks import sign

Deliver = Callable[[bytes, str], None]

TERMINAL = {"replied", "stopped", "exhausted", "error"}
REQUIRES_TRIGGER = {"post_demo", "proposal_sent", "meeting_no_show", "custom"}
DEFAULTS = {
    "no_reply": (2, 3),
    "new_lead": (1, 3),
    "post_demo": (1, 2),
    "proposal_sent": (3, 3),
    "meeting_no_show": (0, 2),
    "custom": (2, 3),
}
PLACEHOLDER = re.compile(r"\[[A-Za-z ]+\]|\{[A-Za-z_ ]+\}")
SIM_MARK = "[SIMULATED by mock agent]"
AI_WORDS = re.compile(r"\b(AI|automated|bot|language model)\b", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class MockAgent:
    def __init__(self, webhook_secret: str, *, event_delay: float = 2.0, seed: bool = True) -> None:
        self.secret = webhook_secret
        self.delay = event_delay
        self.deliver: Deliver | None = None
        self.users: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.followups: dict[str, dict] = {}
        self.idem: dict[tuple[str, str], tuple[int, Any]] = {}
        self.client_replied: set[str] = set()
        self._bg: set[asyncio.Task] = set()
        self.app = self._build_app()
        self._seed_users()
        if seed:
            self._seed_tasks()

    # ----- seed data -------------------------------------------------------------------------
    def _seed_users(self) -> None:
        for uid, email, name, connected in [
            ("usr_sender", "sender@demo-company.test", "Sam Sender", True),
            ("usr_alex", "alex@demo-company.test", "Alex Rivera", False),
        ]:
            self.users[uid] = {
                "id": uid,
                "email": email,
                "name": name,
                "timezone": "America/New_York",
                "send_mode": "approve",
                "paused": False,
                "gmail_connected": connected,
                "gmail_status": "connected" if connected else "never_connected",
                "simulated": True,
            }

    def _seed_tasks(self) -> None:
        now = _now()
        samples = [
            (
                "SIM-001",
                "post_demo",
                "Priya Shah",
                "priya@client-demo.test",
                "Acme",
                "Liked the reporting view. Asked about SSO. Promised to send the pricing sheet.",
                "waiting",
                0,
            ),
            (
                "SIM-002",
                "proposal_sent",
                "Tom Becker",
                "tom@client-demo.test",
                "Northwind",
                "Sent proposal for 40 seats on Monday.",
                "followed_up",
                1,
            ),
            (
                "SIM-003",
                "no_reply",
                "Lena Ortiz",
                "lena@client-demo.test",
                "Globex",
                "Asked about onboarding timeline.",
                "exhausted",
                3,
            ),
        ]
        for ext, scen, name, email, company, notes, status, count in samples:
            t = self._new_task(
                {
                    "external_id": ext,
                    "owner_email": "sender@demo-company.test",
                    "scenario": scen,
                    "client": {"name": name, "email": email, "company": company},
                    "notes": notes,
                    "gmail_thread_id": f"sim-thread-{ext.lower()}",
                    "scenario_trigger_at": _iso(now - timedelta(days=2)),
                }
            )
            t["status"], t["followup_count"] = status, count
            if status == "exhausted":
                t["next_check_at"] = None
            self._timeline(t, "seeded", "Sample task created by the mock agent")

    # ----- helpers ----------------------------------------------------------------------------
    def _timeline(self, task: dict, type_: str, detail: str | None = None) -> None:
        task["timeline"].append({"at": _iso(_now()), "type": type_, "detail": detail})
        task["updated_at"] = _iso(_now())

    def _new_task(self, body: dict) -> dict:
        wait, max_f = DEFAULTS[body["scenario"]]
        owner = next(u for u in self.users.values() if u["email"] == body["owner_email"])
        now = _now()
        wait = body.get("wait_business_days", wait)
        task = {
            "id": "tsk_" + uuid.uuid4().hex[:12],
            "external_id": body["external_id"],
            "owner_user_id": owner["id"],
            "owner_email": owner["email"],
            "scenario": body["scenario"],
            "goal": body.get("goal"),
            "status": "waiting",
            "paused": bool(body.get("paused", False)),
            "pause_reason": None,
            "client": body["client"],
            "gmail_thread_id": body.get("gmail_thread_id"),
            "gmail_thread_url": None,
            "notes": body.get("notes"),
            "followup_count": 0,
            "max_followups": body.get("max_followups") or max_f,
            "wait_business_days": wait,
            "send_mode": body.get("send_mode"),
            "scenario_trigger_at": body.get("scenario_trigger_at"),
            "next_check_at": _iso(now + timedelta(days=max(wait, 0) or 0, hours=1)),
            "last_outbound_at": None,
            "last_inbound_at": None,
            "stop_reason": None,
            "error_message": None,
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "timeline": [],
            "simulated": True,
        }
        self.tasks[task["id"]] = task
        return task

    def _task_out(self, task: dict, detail: bool = True) -> dict:
        out = {k: v for k, v in task.items() if k != "timeline"}
        if detail:
            out["followups"] = sorted(
                (f for f in self.followups.values() if f["task_id"] == task["id"]),
                key=lambda f: f["sequence_number"],
            )
            out["timeline"] = list(task["timeline"])
        else:
            out.pop("notes", None)
        return out

    def _pending_draft(self, task_id: str) -> dict | None:
        return next(
            (
                f
                for f in self.followups.values()
                if f["task_id"] == task_id and f["status"] in {"drafted", "approved", "sending"}
            ),
            None,
        )

    def _compose(self, task: dict, seq: int) -> dict:
        first = task["client"]["name"].split()[0]
        notes = (task.get("notes") or "").strip()
        detail = notes.split(".")[0].strip() if notes else "our last conversation"
        lines = {
            1: f"Thanks again for your time. You mentioned this: {detail.lower()}. "
            "Would a quick call this week help you move to a decision?",
            2: f"One more thought on {detail.lower()}: I can put together a short summary for your team "
            "if that would be useful. Want me to send it over?",
            3: "I don't want to crowd your inbox. Should I check back next quarter instead?",
        }
        owner = self.users[task["owner_user_id"]]["name"].split()[0]
        body = f"Hi {first},\n\n{lines.get(seq, lines[3])}\n\nBest,\n{owner}\n\n{SIM_MARK}"
        return {
            "subject": f"Re: {task['scenario'].replace('_', ' ').title()} follow-up",
            "body": body,
            "needs_review": not notes,
            "reason": f"SIMULATED draft. Referenced: {detail}" if notes else "SIMULATED. Notes are empty.",
            "context_summary": f"SIMULATED context. Scenario {task['scenario']}, follow-up {seq} of "
            f"{task['max_followups']}. Notes: {notes[:160] or '(none)'}",
        }

    def _safety(self, subject: str, body: str) -> list[str]:
        failures = []
        body = body.replace(SIM_MARK, "")
        if PLACEHOLDER.search(subject + body):
            failures.append("placeholder text left in draft")
        if len(body.split()) > 150:
            failures.append("draft is over 150 words")
        if AI_WORDS.search(body):
            failures.append("draft mentions AI or automation")
        return failures

    # ----- webhooks ---------------------------------------------------------------------------
    def emit(self, event: str, task: dict | None, data: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "event": event,
            "occurred_at": _iso(_now()),
            "task_id": task["id"] if task else None,
            "external_id": task["external_id"] if task else None,
            "task_status": task["status"] if task else None,
            "paused": task["paused"] if task else None,
            "followup_count": task["followup_count"] if task else None,
            "next_check_at": task["next_check_at"] if task else None,
            "data": {**(data or {}), "simulated": True},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        if self.deliver:
            self.deliver(raw, sign(raw, self.secret))

    def later(self, fn: Callable[[], None]) -> None:
        async def run() -> None:
            await asyncio.sleep(self.delay)
            fn()

        t = asyncio.get_running_loop().create_task(run())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ----- engine steps -----------------------------------------------------------------------
    def _check(self, task_id: str) -> None:
        task = self.tasks[task_id]
        if task["status"] in TERMINAL or task["paused"] or task["status"] == "draft_pending":
            return
        if task_id in self.client_replied:
            self._mark_replied(task)
            return
        seq = task["followup_count"] + 1
        draft = self._compose(task, seq)
        f = {
            "id": "drf_" + uuid.uuid4().hex[:12],
            "task_id": task_id,
            "sequence_number": seq,
            "draft_revision": 1,
            "status": "drafted",
            "edited": False,
            "gmail_message_id": None,
            "scheduled_send_at": None,
            "created_at": _iso(_now()),
            "sent_at": None,
            **draft,
        }
        existing = next(
            (x for x in self.followups.values() if x["task_id"] == task_id and x["sequence_number"] == seq), None
        )
        if existing:  # one record per sequence: redraft in place
            task["timeline"].append(
                {
                    "at": _iso(_now()),
                    "type": "draft.revision_archived",
                    "detail": f"Revision {existing['draft_revision']}: {existing['subject']}",
                }
            )
            f["id"], f["draft_revision"] = existing["id"], existing["draft_revision"] + 1
        self.followups[f["id"]] = f
        mode = task["send_mode"] or self.users[task["owner_user_id"]]["send_mode"]
        if mode == "auto" and not f["needs_review"]:
            f["status"] = "approved"
            self._timeline(task, "draft.auto_approved", f"Follow-up {seq}")
            self._send(f["id"])
            return
        task["status"] = "draft_pending"
        self._timeline(task, "draft.ready", f"Follow-up {seq} drafted")
        self.emit(
            "draft.ready",
            task,
            {"draft_id": f["id"], "sequence_number": seq, "subject": f["subject"], "needs_review": f["needs_review"]},
        )

    def _mark_replied(self, task: dict) -> None:
        for f in self.followups.values():
            if f["task_id"] == task["id"] and f["status"] in {"drafted", "approved"}:
                f["status"] = "canceled"
        task["status"], task["next_check_at"] = "replied", None
        task["last_inbound_at"] = _iso(_now())
        self._timeline(task, "task.replied", "Client replied (simulated); follow-ups stopped")
        self.emit("task.replied", task, {"reason": "client_replied"})

    def _send(self, followup_id: str) -> None:
        f = self.followups[followup_id]
        task = self.tasks[f["task_id"]]
        if f["status"] != "approved":
            return
        # Pre-send recheck: terminal conditions outrank an approval click.
        if task["id"] in self.client_replied:
            self._mark_replied(task)
            return
        if task["status"] in TERMINAL or task["paused"]:
            f["status"] = "canceled"
            return
        f["status"], f["sent_at"] = "sent", _iso(_now())
        f["gmail_message_id"] = "sim-msg-" + uuid.uuid4().hex[:10]
        task["followup_count"] += 1
        task["last_outbound_at"] = f["sent_at"]
        final = task["followup_count"] >= task["max_followups"]
        task["status"] = "exhausted" if final else "followed_up"
        task["next_check_at"] = None if final else _iso(_now() + timedelta(days=task["wait_business_days"] or 1))
        self._timeline(task, "followup.sent", f"Follow-up {f['sequence_number']} sent (SIMULATED, no Gmail)")
        self.emit(
            "followup.sent",
            task,
            {
                "sequence_number": f["sequence_number"],
                "subject": f["subject"],
                "body": f["body"],
                "sent_at": f["sent_at"],
                "gmail_link": None,
            },
        )
        if final:
            self._timeline(task, "task.exhausted", "Final follow-up sent")
            self.emit("task.exhausted", task, {"sequence_number": f["sequence_number"]})

    def _stop(self, task: dict, reason: str, status: str = "stopped") -> None:
        for f in self.followups.values():
            if f["task_id"] == task["id"] and f["status"] in {"drafted", "approved"}:
                f["status"] = "canceled"
        task["status"], task["stop_reason"], task["next_check_at"] = status, reason, None
        self._timeline(task, "task.stopped", reason)
        self.emit("task.stopped", task, {"reason": reason})

    # ----- HTTP app ---------------------------------------------------------------------------
    def _build_app(self) -> FastAPI:  # noqa: C901 - one route per contract operation
        app = FastAPI(title="Mock Follow-Up Agent (SIMULATED)")
        agent = self

        def idem(key: str | None, scope: str, fn: Callable[[], JSONResponse]) -> JSONResponse:
            if key and (scope, key) in agent.idem:
                status, body = agent.idem[(scope, key)]
                return JSONResponse(body, status_code=status)
            resp = fn()
            if key and resp.status_code < 500:
                agent.idem[(scope, key)] = (resp.status_code, json.loads(resp.body))
            return resp

        def get_task(task_id: str) -> dict | None:
            return agent.tasks.get(task_id)

        @app.get("/v1/health")
        async def health() -> dict:
            return {"status": "ok", "test_mode": True, "version": "mock-1.1", "mock": True}

        @app.get("/v1/users")
        async def users() -> dict:
            return {"data": list(agent.users.values())}

        @app.patch("/v1/users/{user_id}")
        async def patch_user(user_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json()

            def do() -> JSONResponse:
                user = agent.users.get(user_id)
                if not user:
                    return _err(404, "not_found", "User not found")
                for k in ("send_mode", "paused", "timezone"):
                    if k in body:
                        user[k] = body[k]
                return JSONResponse(user)

            return idem(idempotency_key, f"user:{user_id}", do)

        @app.get("/v1/users/{user_id}/connect")
        async def connect(user_id: str):
            if user_id not in agent.users:
                return _err(404, "not_found", "User not found")
            return {"url": f"/mock/connect/{user_id}", "expires_at": _iso(_now() + timedelta(minutes=10))}

        @app.get("/v1/users/{user_id}/threads")
        async def threads(user_id: str, q: str = "", limit: int = 10):
            user = agent.users.get(user_id)
            if not user:
                return _err(404, "not_found", "User not found")
            if not user["gmail_connected"]:
                return _err(409, "gmail_disconnected", "Connect Gmail first")
            sample = [
                {
                    "thread_id": "sim-thread-demo",
                    "subject": "Product demo (SIMULATED)",
                    "last_sender": user["email"],
                    "date": _iso(_now() - timedelta(days=1)),
                },
                {
                    "thread_id": "sim-thread-reply",
                    "subject": "Pricing question (SIMULATED)",
                    "last_sender": user["email"],
                    "date": _iso(_now() - timedelta(days=3)),
                },
            ]
            return {"data": [t for t in sample if q.lower() in t["subject"].lower()][:limit]}

        @app.post("/v1/tasks")
        async def create(request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json()

            def do() -> JSONResponse:
                if body.get("scenario") not in DEFAULTS:
                    return _err(422, "invalid_scenario", "Unknown scenario")
                if not any(u["email"] == body.get("owner_email") for u in agent.users.values()):
                    return _err(403, "owner_not_authorized", "Owner is not a provisioned user for this client")
                if body["scenario"] in REQUIRES_TRIGGER and not body.get("scenario_trigger_at"):
                    return _err(422, "missing_baseline", f"scenario_trigger_at is required for {body['scenario']}")
                has_baseline = body.get("gmail_thread_id") or body.get("scenario_trigger_at")
                if body["scenario"] == "new_lead" and not has_baseline:
                    return _err(422, "missing_baseline", "new_lead needs a thread or lead_created_at")
                if body["scenario"] == "custom" and not body.get("goal"):
                    return _err(422, "missing_goal", "custom scenario needs a goal")
                if any(t["external_id"] == body.get("external_id") for t in agent.tasks.values()):
                    return _err(409, "duplicate_external_id", "external_id already exists")
                task = agent._new_task(body)
                agent._timeline(task, "task.created", "Created via API")
                return JSONResponse(agent._task_out(task), status_code=201)

            return idem(idempotency_key, "create", do)

        @app.get("/v1/tasks")
        async def list_tasks(
            status: str | None = None,
            owner_email: str | None = None,
            scenario: str | None = None,
            limit: int = 50,
            cursor: str | None = None,
        ):
            items = [
                t
                for t in agent.tasks.values()
                if (not status or t["status"] == status)
                and (not owner_email or t["owner_email"] == owner_email)
                and (not scenario or t["scenario"] == scenario)
            ]
            start = int(cursor or 0)
            page = items[start : start + limit]
            nxt = str(start + limit) if start + limit < len(items) else None
            return {"data": [agent._task_out(t, detail=False) for t in page], "next_cursor": nxt}

        @app.get("/v1/tasks/{task_id}")
        async def task_detail(task_id: str):
            task = get_task(task_id)
            return JSONResponse(agent._task_out(task)) if task else _err(404, "not_found", "Task not found")

        @app.patch("/v1/tasks/{task_id}")
        async def patch_task(task_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json()

            def do() -> JSONResponse:
                task = get_task(task_id)
                if not task:
                    return _err(404, "not_found", "Task not found")
                for k in ("notes", "goal", "wait_business_days", "max_followups", "send_mode"):
                    if k in body:
                        task[k] = body[k]
                agent._timeline(task, "task.updated", ", ".join(sorted(body)))
                return JSONResponse(agent._task_out(task))

            return idem(idempotency_key, f"patch:{task_id}", do)

        def simple_action(task_id: str, key: str | None, name: str, fn: Callable[[dict], JSONResponse | None]):
            def do() -> JSONResponse:
                task = get_task(task_id)
                if not task:
                    return _err(404, "not_found", "Task not found")
                result = fn(task)
                return result or JSONResponse(agent._task_out(task))

            return idem(key, f"{name}:{task_id}", do)

        @app.post("/v1/tasks/{task_id}/pause")
        async def pause(task_id: str, idempotency_key: str | None = Header(None)):
            def fn(task: dict) -> None:
                task["paused"], task["pause_reason"] = True, "Paused from dashboard"
                agent._timeline(task, "task.paused")

            return simple_action(task_id, idempotency_key, "pause", fn)

        @app.post("/v1/tasks/{task_id}/resume")
        async def resume(task_id: str, idempotency_key: str | None = Header(None)):
            def fn(task: dict) -> JSONResponse | None:
                if task["status"] in TERMINAL:
                    return _err(409, "terminal", f"Task is {task['status']}")
                was_rejected = task["pause_reason"] == "draft_rejected"
                task["paused"], task["pause_reason"] = False, None
                agent._timeline(task, "task.resumed")
                if was_rejected:
                    agent.later(lambda: agent._check(task_id))
                return None

            return simple_action(task_id, idempotency_key, "resume", fn)

        @app.post("/v1/tasks/{task_id}/stop")
        async def stop(task_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json() if await request.body() else {}

            def fn(task: dict) -> None:
                if task["status"] not in TERMINAL:
                    agent._stop(task, body.get("reason") or "Stopped from dashboard")

            return simple_action(task_id, idempotency_key, "stop", fn)

        @app.post("/v1/tasks/{task_id}/events")
        async def ext_event(task_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json()

            def fn(task: dict) -> JSONResponse | None:
                if body.get("type") not in {"meeting_booked", "task_closed", "opted_out"}:
                    return _err(422, "invalid_event", "Unknown event type")
                if task["status"] not in TERMINAL:
                    agent._stop(task, body["type"])
                return None

            return simple_action(task_id, idempotency_key or body.get("source_event_id"), "event", fn)

        @app.post("/v1/tasks/{task_id}/preview")
        async def preview(task_id: str):
            task = get_task(task_id)
            if not task:
                return _err(404, "not_found", "Task not found")
            if task["status"] in TERMINAL:
                return _err(409, "terminal", f"Task is {task['status']}")
            seq = task["followup_count"] + 1
            d = agent._compose(task, seq)
            return {**d, "safety_failures": agent._safety(d["subject"], d["body"]), "sequence_number": seq}

        @app.post("/v1/tasks/{task_id}/run")
        async def run(task_id: str, idempotency_key: str | None = Header(None)):
            def fn(task: dict) -> JSONResponse:
                if task["status"] in TERMINAL:
                    return _err(409, "terminal", f"Task is {task['status']}")
                if task["paused"]:
                    return _err(409, "paused", "Task is paused; resume it first")
                if task["status"] == "draft_pending":
                    return _err(409, "draft_pending", "A draft is already waiting for approval")
                agent._timeline(task, "run.requested", "Run check now (SIMULATED)")
                agent.later(lambda: agent._check(task_id))
                return JSONResponse(
                    {"task_id": task_id, "accepted": True, "test_mode": True, "message": "Check queued (SIMULATED)"},
                    status_code=202,
                )

            return simple_action(task_id, idempotency_key, "run", fn)

        @app.get("/v1/drafts")
        async def drafts():
            out = []
            for f in agent.followups.values():
                if f["status"] != "drafted":
                    continue
                task = agent.tasks[f["task_id"]]
                out.append(
                    {
                        **f,
                        "external_id": task["external_id"],
                        "client": task["client"],
                        "owner_email": task["owner_email"],
                    }
                )
            return {"data": out}

        @app.post("/v1/drafts/{draft_id}/approve")
        async def approve(draft_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json() if await request.body() else {}

            def do() -> JSONResponse:
                f = agent.followups.get(draft_id)
                if not f:
                    return _err(404, "not_found", "Draft not found")
                if f["status"] != "drafted":
                    return _err(409, "not_drafted", f"Draft is {f['status']}")
                subject, text = body.get("subject") or f["subject"], body.get("body") or f["body"]
                failures = agent._safety(subject, text)
                if failures:
                    return _err(422, "safety_check_failed", "; ".join(failures))
                f["edited"] = (subject, text) != (f["subject"], f["body"])
                f["subject"], f["body"], f["status"] = subject, text, "approved"
                task = agent.tasks[f["task_id"]]
                agent._timeline(
                    task, "draft.approved", f"Follow-up {f['sequence_number']}" + (" (edited)" if f["edited"] else "")
                )
                agent.later(lambda: agent._send(draft_id))
                return JSONResponse(f, status_code=202)

            return idem(idempotency_key, f"approve:{draft_id}", do)

        @app.post("/v1/drafts/{draft_id}/reject")
        async def reject(draft_id: str, request: Request, idempotency_key: str | None = Header(None)):
            body = await request.json() if await request.body() else {}

            def do() -> JSONResponse:
                f = agent.followups.get(draft_id)
                if not f:
                    return _err(404, "not_found", "Draft not found")
                if f["status"] != "drafted":
                    return _err(409, "not_drafted", f"Draft is {f['status']}")
                f["status"] = "rejected"
                task = agent.tasks[f["task_id"]]
                task["status"] = "followed_up" if task["followup_count"] else "waiting"
                task["paused"], task["pause_reason"] = True, "draft_rejected"
                agent._timeline(task, "draft.rejected", body.get("feedback") or "Rejected")
                agent.emit(
                    "draft.rejected",
                    task,
                    {"draft_id": draft_id, "sequence_number": f["sequence_number"], "feedback": body.get("feedback")},
                )
                return JSONResponse(f)

            return idem(idempotency_key, f"reject:{draft_id}", do)

        # Mock-only controls (not part of the real contract).
        @app.post("/mock/tasks/{task_id}/client-reply")
        async def client_reply(task_id: str):
            if task_id not in agent.tasks:
                return _err(404, "not_found", "Task not found")
            agent.client_replied.add(task_id)
            agent._timeline(agent.tasks[task_id], "mock.client_replied", "Client reply simulated in thread")
            return {"ok": True}

        @app.post("/mock/users/{user_id}/connected")
        async def mark_connected(user_id: str):
            user = agent.users.get(user_id)
            if not user:
                return _err(404, "not_found", "User not found")
            user["gmail_connected"], user["gmail_status"] = True, "connected"
            return user

        return app
