import threading

import httpx
from sqlmodel import select

from app import db

from .conftest import event, task_json

CREATE_FORM = {
    "owner_email": "sender@company.test",
    "scenario": "post_demo",
    "client_name": "Priya Shah",
    "client_email": "priya@client.test",
    "client_company": "Acme",
    "notes": "Liked reporting",
    "scenario_trigger_at": "2026-10-28T15:00:00.000Z",
    "idem": "create-key-0001",
}


def test_create_task_calls_agent_once_on_double_click(dash, agent_mock):
    route = agent_mock.post("/v1/tasks").respond(201, json=task_json("tsk_9", external_id="DEMO-9"))
    r1 = dash.post("/tasks", CREATE_FORM, follow_redirects=False)
    r2 = dash.post("/tasks", CREATE_FORM, follow_redirects=False)
    assert route.call_count == 1
    assert r1.headers["location"] == "/tasks/tsk_9"
    assert r2.headers["location"] == "/tasks"
    req = route.calls[0].request
    assert req.headers["Idempotency-Key"] == "create-key-0001"
    assert req.headers["Authorization"] == "Bearer dash-key"
    import json

    body = json.loads(req.content)
    assert body["scenario_trigger_at"].startswith("2026-10-28T15:00:00")
    assert body["trigger_type"] == "demo_completed_at"


def test_concurrent_double_click_calls_agent_once(dash, agent_mock):
    calls = []

    def slow(request):
        calls.append(request)
        return httpx.Response(201, json=task_json("tsk_c"))

    agent_mock.post("/v1/tasks").mock(side_effect=slow)
    form = CREATE_FORM | {"idem": "concurrent-key-01"}
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(dash.post("/tasks", form, follow_redirects=False)))
        for _ in range(2)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1


def test_create_requires_scenario_baseline(dash, agent_mock):
    route = agent_mock.post("/v1/tasks").respond(201, json=task_json())
    r = dash.post("/tasks", CREATE_FORM | {"scenario_trigger_at": "", "idem": "create-key-0002"})
    assert r.status_code == 422
    assert "needs a baseline time" in r.text
    assert not route.called


def test_agent_validation_error_shown(dash, agent_mock):
    agent_mock.post("/v1/tasks").respond(
        422, json={"error": {"code": "missing_baseline", "message": "No valid baseline for post_demo"}}
    )
    r = dash.post("/tasks", CREATE_FORM | {"idem": "create-key-0003"})
    assert r.status_code == 422
    assert "No valid baseline for post_demo" in r.text


def test_failed_create_can_be_retried_with_same_key(dash, agent_mock):
    route = agent_mock.post("/v1/tasks")
    route.side_effect = [httpx.ConnectError("down"), httpx.Response(201, json=task_json("tsk_r"))]
    form = CREATE_FORM | {"idem": "retry-key-000001"}
    r1 = dash.post("/tasks", form)
    assert "unreachable" in r1.text
    r2 = dash.post("/tasks", form, follow_redirects=False)
    assert r2.headers["location"] == "/tasks/tsk_r"
    assert [c.request.headers["Idempotency-Key"] for c in route.calls] == ["retry-key-000001"] * 2


def test_approve_twice_calls_agent_once_and_shows_pending(dash, agent_mock):
    route = agent_mock.post("/v1/drafts/drf_1/approve").respond(
        202, json={"id": "drf_1", "task_id": "tsk_1", "sequence_number": 1, "status": "approved"}
    )
    form = {
        "idem": "approve-key-0001",
        "task_id": "tsk_1",
        "subject": "Re: Demo",
        "body": "Hi Priya",
        "original_subject": "Re: Demo",
        "original_body": "Hi Priya",
    }
    dash.post("/drafts/drf_1/approve", form)
    r = dash.post("/drafts/drf_1/approve", form)
    assert route.call_count == 1
    assert "Already approved" in r.text
    assert "waiting for the agent to confirm" in r.text
    # Unedited approve sends no body.
    assert route.calls[0].request.content in (b"", b"null")

    # followup.sent confirms the pending action and moves the card.
    dash.webhook(event("followup.sent", task_status="followed_up", followup_count=1))
    r = dash.c.get("/drafts")
    assert "waiting for the agent to confirm" not in r.text
    with db.session() as s:
        assert s.get(db.TaskCard, "tsk_1").status == "followed_up"


def test_edited_draft_sends_edits_and_failed_check_never_shows_sent(dash, agent_mock):
    route = agent_mock.post("/v1/drafts/drf_2/approve").respond(
        422, json={"error": {"code": "safety_check_failed", "message": "placeholder text left in draft"}}
    )
    form = {
        "idem": "approve-key-0002",
        "task_id": "tsk_1",
        "subject": "Re: Demo",
        "body": "Hi [Name]",
        "original_subject": "Re: Demo",
        "original_body": "Hi Priya",
    }
    r = dash.post("/drafts/drf_2/approve", form)
    import json

    assert json.loads(route.calls[0].request.content) == {"body": "Hi [Name]"}
    assert "placeholder text left in draft" in r.text
    assert "The draft was not sent" in r.text
    assert "waiting for the agent to confirm" not in r.text
    with db.session() as s:
        row = s.get(db.ActionRow, "approve-key-0002")
    assert row.state == "failed"


def test_reject_once_and_explains_resume(dash, agent_mock):
    route = agent_mock.post("/v1/drafts/drf_3/reject").respond(
        200, json={"id": "drf_3", "task_id": "tsk_1", "sequence_number": 1, "status": "rejected"}
    )
    form = {"idem": "reject-key-00001", "task_id": "tsk_1", "feedback": "too pushy"}
    r = dash.post("/drafts/drf_3/reject", form)
    dash.post("/drafts/drf_3/reject", form)
    assert route.call_count == 1
    assert "press Resume" in r.text


def test_task_actions_use_idempotency_keys(dash, agent_mock):
    agent_mock.get("/v1/tasks/tsk_1").respond(json=task_json())
    run = agent_mock.post("/v1/tasks/tsk_1/run").respond(202, json={"task_id": "tsk_1", "accepted": True})
    pause = agent_mock.post("/v1/tasks/tsk_1/pause").respond(json=task_json(paused=True))
    stop = agent_mock.post("/v1/tasks/tsk_1/stop").respond(json=task_json(status="stopped"))
    ev = agent_mock.post("/v1/tasks/tsk_1/events").respond(json=task_json(status="stopped"))
    for url, form in [
        ("run", {"idem": "run-key-000001"}),
        ("run", {"idem": "run-key-000001"}),
        ("pause", {"idem": "pause-key-00001"}),
        ("stop", {"idem": "stop-key-000001"}),
        ("events", {"idem": "event-key-00001", "type": "meeting_booked"}),
    ]:
        dash.post(f"/tasks/tsk_1/{url}", form, follow_redirects=False)
    assert run.call_count == 1 and pause.call_count == 1 and stop.call_count == 1
    import json

    body = json.loads(ev.calls[0].request.content)
    assert body["type"] == "meeting_booked" and body["source_event_id"] == "event-key-00001"
    with db.session() as s:
        assert s.get(db.TaskCard, "tsk_1").status == "stopped"


def test_missing_idempotency_key_rejected(dash, agent_mock):
    route = agent_mock.post("/v1/tasks/tsk_1/run").respond(202, json={"task_id": "tsk_1", "accepted": True})
    r = dash.post("/tasks/tsk_1/run", {})
    assert r.status_code == 400
    assert not route.called


def test_preview_is_read_only(dash, agent_mock):
    route = agent_mock.post("/v1/tasks/tsk_1/preview").respond(
        json={
            "subject": "Re: Demo",
            "body": "Hi Priya, SSO...",
            "needs_review": False,
            "reason": "SSO",
            "context_summary": "Demo notes",
            "sequence_number": 1,
        }
    )
    r = dash.post("/tasks/tsk_1/preview")
    assert route.called and "Not sent, not saved" in r.text and "SSO" in r.text
    with db.session() as s:
        assert list(s.exec(select(db.ActionRow))) == []
