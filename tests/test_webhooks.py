from sqlmodel import select

from app import db
from app.broadcast import broadcaster

from .conftest import event


def _events() -> list[db.WebhookEventRow]:
    with db.session() as s:
        return list(s.exec(select(db.WebhookEventRow)))


def test_bad_signature_rejected_with_401_not_stored_not_broadcast(dash, caplog):
    q = broadcaster.subscribe()
    try:
        r = dash.webhook(event("followup.sent", task_status="followed_up"), signature="deadbeef")
        assert r.status_code == 401
        assert _events() == []
        assert q.empty()
        assert "invalid signature" in caplog.text
        r = dash.webhook(event("followup.sent"), secret="wrong-secret")
        assert r.status_code == 401
        r = dash.c.post("/webhooks/agent", content=b"{}")  # no header
        assert r.status_code == 401
    finally:
        broadcaster.unsubscribe(q)


def test_signature_checked_over_raw_bytes(dash):
    # Same JSON, different whitespace: the signature of the original bytes must not verify.
    from app.webhooks import sign

    from .conftest import SECRET

    raw = b'{"event_id":"evt_x","event":"daily.summary"}'
    sig = sign(raw, SECRET)
    r = dash.c.post(
        "/webhooks/agent", content=b'{"event_id": "evt_x", "event": "daily.summary"}', headers={"X-Signature": sig}
    )
    assert r.status_code == 401
    r = dash.c.post("/webhooks/agent", content=raw, headers={"X-Signature": "sha256=" + sig})
    assert r.status_code == 200


def test_duplicate_event_id_stored_once(dash):
    e = event("draft.ready", event_id="evt_dup", task_status="draft_pending")
    assert dash.webhook(e).json() == {"ok": True, "duplicate": False}
    assert dash.webhook(e).json() == {"ok": True, "duplicate": True}
    assert len(_events()) == 1


def test_unknown_event_stored_and_shown(dash):
    assert dash.webhook(event("task.something_new", extra_field=[1, 2])).status_code == 200
    assert _events()[0].event == "task.something_new"
    assert "task.something_new" in dash.c.get("/").text


def test_untrusted_html_is_escaped(dash):
    evil = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    dash.webhook(event("draft.ready", data={"subject": evil, "body": evil}, task_status="draft_pending"))
    html = dash.c.get("/tasks").text
    assert "<script>alert" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_long_email_body_truncated_in_panel(dash):
    body = "Confidential " * 50
    dash.webhook(event("followup.sent", data={"body": body}, task_status="followed_up"))
    html = dash.c.get("/").text
    assert "(truncated)" in html
    assert body not in html


def test_webhook_updates_cards(dash):
    dash.webhook(event("followup.sent", task_status="followed_up", followup_count=1, data={"sequence_number": 1}))
    with db.session() as s:
        card = s.get(db.TaskCard, "tsk_1")
    assert card.status == "followed_up" and card.followup_count == 1

    dash.webhook(event("task.replied", task_status="replied"))
    with db.session() as s:
        assert s.get(db.TaskCard, "tsk_1").status == "replied"


def test_rejected_draft_marks_card_paused(dash):
    dash.webhook(event("draft.rejected", task_status="waiting", paused=True, data={"draft_id": "drf_1"}))
    with db.session() as s:
        card = s.get(db.TaskCard, "tsk_1")
    assert card.paused and card.status == "waiting"
    assert "Paused" in dash.c.get("/tasks").text


def test_exhausted_after_final_send(dash):
    dash.webhook(event("followup.sent", task_status="exhausted", followup_count=2))
    dash.webhook(event("task.exhausted", task_status="exhausted", followup_count=2))
    html = dash.c.get("/partials/board").text
    lane = html.split("Exhausted / Error")[1]
    assert "Priya Shah" in lane


def test_event_for_unknown_task_triggers_detail_fetch(dash, agent_mock):
    from .conftest import task_json

    route = agent_mock.get("/v1/tasks/tsk_new").respond(json=task_json("tsk_new", "draft_pending"))
    dash.webhook({"event_id": "evt_n", "event": "draft.ready", "task_id": "tsk_new"})
    assert route.called
    with db.session() as s:
        assert s.get(db.TaskCard, "tsk_new").status == "draft_pending"
