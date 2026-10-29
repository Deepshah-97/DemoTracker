"""End-to-end demo script (section 6) against the built-in SIMULATED agent."""

import re
import time

from sqlmodel import select

from app import db


def wait_for(predicate, timeout: float = 3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def card(task_id: str) -> db.TaskCard | None:
    with db.session() as s:
        return s.get(db.TaskCard, task_id)


def events(name: str | None = None) -> list[db.WebhookEventRow]:
    with db.session() as s:
        q = select(db.WebhookEventRow)
        if name:
            q = q.where(db.WebhookEventRow.event == name)
        return list(s.exec(q))


def test_mock_labels_everything_simulated(mock_dash):
    html = mock_dash.c.get("/tasks").text
    assert "Mock mode: all data SIMULATED" in html
    assert "SIM-001" in html and "SIM" in html


def test_full_demo_script_in_mock_mode(mock_dash):
    d = mock_dash
    # Create the task.
    r = d.post(
        "/tasks",
        {
            "owner_email": "sender@demo-company.test",
            "scenario": "post_demo",
            "client_name": "Client Test",
            "client_email": "client@client-demo.test",
            "notes": "Liked reporting. Asked about SSO.",
            "scenario_trigger_at": "2026-10-28T15:00:00Z",
            "gmail_thread_id": "sim-thread-demo",
            "idem": "mock-create-0001",
            "external_id": "DEMO-001",
        },
        follow_redirects=False,
    )
    task_id = r.headers["location"].rsplit("/", 1)[1]
    assert card(task_id).status == "waiting"

    # Preview shows context and does not create a draft.
    r = d.post(f"/tasks/{task_id}/preview")
    assert "Liked reporting" in r.text or "liked reporting" in r.text
    assert "No drafts waiting" in d.c.get("/drafts").text

    # Run check now -> draft.ready webhook (signed, verified, SIMULATED).
    d.post(f"/tasks/{task_id}/run", {"idem": "mock-run-000001"})
    assert wait_for(lambda: card(task_id).status == "draft_pending")
    ev = events("draft.ready")[-1]
    assert ev.simulated and ev.signature_valid

    # Edit one word and approve -> followup.sent.
    html = d.c.get("/drafts").text
    draft_id = re.search(r'action="/drafts/([^/]+)/approve"', html).group(1)
    body = re.search(r'name="original_body" value="([^"]*)"', html).group(1)
    import html as h

    body = h.unescape(body)
    d.post(
        f"/drafts/{draft_id}/approve",
        {
            "idem": "mock-approve-001",
            "task_id": task_id,
            "subject": "Re: Post Demo follow-up",
            "original_subject": "Re: Post Demo follow-up",
            "body": body.replace("Thanks", "Thank you"),
            "original_body": body,
        },
    )
    assert wait_for(lambda: card(task_id).status == "followed_up")
    assert card(task_id).followup_count == 1
    assert events("followup.sent")[-1].simulated

    # Second follow-up is the final one for post_demo -> exhausted (followup.sent then task.exhausted).
    d.post(f"/tasks/{task_id}/run", {"idem": "mock-run-000002"})
    assert wait_for(lambda: card(task_id).status == "draft_pending")
    html = d.c.get("/drafts").text
    draft_id = re.search(r'action="/drafts/([^/]+)/approve"', html).group(1)
    d.post(f"/drafts/{draft_id}/approve", {"idem": "mock-approve-002", "task_id": task_id})
    assert wait_for(lambda: card(task_id).status == "exhausted")
    names = [e.event for e in events() if e.agent_task_id == task_id]
    assert names[-2:] == ["followup.sent", "task.exhausted"]


def test_stop_rule_on_client_reply(mock_dash):
    d = mock_dash
    task_id = next(c.agent_task_id for c in db.list_cards() if c.external_id == "SIM-001")
    d.post(f"/tasks/{task_id}/simulate-reply")
    d.post(f"/tasks/{task_id}/run", {"idem": "mock-run-reply01"})
    assert wait_for(lambda: card(task_id).status == "replied")
    assert not [e for e in events("followup.sent") if e.agent_task_id == task_id]


def test_reply_after_approval_prevents_send(mock_dash):
    d = mock_dash
    task_id = next(c.agent_task_id for c in db.list_cards() if c.external_id == "SIM-001")
    d.post(f"/tasks/{task_id}/run", {"idem": "mock-run-race001"})
    assert wait_for(lambda: card(task_id).status == "draft_pending")
    draft_id = re.search(r'action="/drafts/([^/]+)/approve"', d.c.get("/drafts").text).group(1)
    d.post(f"/drafts/{draft_id}/approve", {"idem": "mock-approve-race", "task_id": task_id})
    d.post(f"/tasks/{task_id}/simulate-reply")
    assert wait_for(lambda: card(task_id).status == "replied")
    assert not [e for e in events("followup.sent") if e.agent_task_id == task_id]


def test_reject_pauses_and_resume_redrafts(mock_dash):
    d = mock_dash
    task_id = next(c.agent_task_id for c in db.list_cards() if c.external_id == "SIM-001")
    d.post(f"/tasks/{task_id}/run", {"idem": "mock-run-rej0001"})
    assert wait_for(lambda: card(task_id).status == "draft_pending")
    draft_id = re.search(r'action="/drafts/([^/]+)/reject"', d.c.get("/drafts").text).group(1)
    d.post(f"/drafts/{draft_id}/reject", {"idem": "mock-reject-0001", "task_id": task_id, "feedback": "shorter"})
    assert wait_for(lambda: card(task_id).paused)
    assert card(task_id).status == "waiting"
    assert "was rejected" in d.c.get(f"/tasks/{task_id}").text
    d.post(f"/tasks/{task_id}/resume", {"idem": "mock-resume-0001"})
    assert wait_for(lambda: card(task_id).status == "draft_pending")
    assert card(task_id).followup_count == 0


def test_meeting_booked_stops_task(mock_dash):
    d = mock_dash
    task_id = next(c.agent_task_id for c in db.list_cards() if c.external_id == "SIM-002")
    d.post(f"/tasks/{task_id}/events", {"idem": "mock-event-00001", "type": "meeting_booked"})
    assert wait_for(lambda: card(task_id).status == "stopped")
    assert events("task.stopped")


def test_mock_connect_gmail(mock_dash):
    r = mock_dash.post("/team/usr_alex/connect", follow_redirects=False)
    assert r.headers["location"] == "/mock/connect/usr_alex"
    r = mock_dash.c.get("/mock/connect/usr_alex")
    assert "SIMULATED: Gmail connected" in r.text


def test_mock_agent_has_no_network_clients():
    import app.mock_agent as m

    src = open(m.__file__).read()
    assert "googleapiclient" not in src and "anthropic" not in src and "httpx.AsyncClient" not in src
