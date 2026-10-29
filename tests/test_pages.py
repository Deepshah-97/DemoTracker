import httpx
from fastapi.testclient import TestClient

from app import db
from app.agent_models import TASK_STATUSES
from app.main import create_app

from .conftest import make_settings, task_json


def test_all_statuses_render(dash, agent_mock):
    tasks = [task_json(f"tsk_{s}", s, external_id=f"EXT-{s}") for s in TASK_STATUSES]
    tasks.append(task_json("tsk_paused", "waiting", paused=True, external_id="EXT-paused"))
    tasks.append(task_json("tsk_more", "followed_up", followup_count=1, max_followups=3, external_id="EXT-more"))
    agent_mock.get("/v1/tasks").respond(json={"data": tasks, "next_cursor": None})
    dash.post("/refresh", {"back": "/tasks"})
    html = dash.c.get("/tasks").text
    for s in TASK_STATUSES:
        assert f"EXT-{s}" in html
    assert "Exhausted / Error" in html
    assert "Paused" in html
    assert "More follow-ups possible" in html


def test_pagination_followed_on_refresh(dash, agent_mock):
    def pages(request):
        if request.url.params.get("cursor") == "p2":
            return httpx.Response(200, json={"data": [task_json("tsk_b")], "next_cursor": None})
        return httpx.Response(200, json={"data": [task_json("tsk_a")], "next_cursor": "p2"})

    agent_mock.get("/v1/tasks").mock(side_effect=pages)
    dash.post("/refresh", {"back": "/tasks"})
    ids = {c.agent_task_id for c in db.list_cards()}
    assert ids == {"tsk_a", "tsk_b"}


def test_missed_webhook_recovered_on_refresh(dash, agent_mock):
    agent_mock.get("/v1/tasks").respond(json={"data": [task_json(status="replied")], "next_cursor": None})
    dash.post("/refresh", {"back": "/tasks"})
    with db.session() as s:
        assert s.get(db.TaskCard, "tsk_1").status == "replied"


def test_agent_down_shows_banner_and_keeps_cache(dash, agent_mock):
    agent_mock.get("/v1/tasks").mock(side_effect=httpx.ConnectError("refused"))
    agent_mock.get("/v1/tasks/tsk_1").mock(side_effect=httpx.ConnectError("refused"))
    r = dash.post("/refresh", {"back": "/tasks"})
    assert "Agent unavailable" in r.text
    assert "Priya Shah" in r.text  # cached card still shown
    r = dash.c.get("/tasks/tsk_1")
    assert r.status_code == 200 and "Showing the cached card" in r.text


def test_agent_5xx_and_auth_errors_shown(dash, agent_mock):
    agent_mock.get("/v1/drafts").respond(503)
    assert "unreachable" in dash.c.get("/drafts").text
    agent_mock.get("/v1/users").respond(403, json={"error": {"code": "forbidden", "message": "not your users"}})
    r = dash.c.get("/team")
    assert "refused this request (403)" in r.text and "not your users" in r.text


def test_agent_unreachable_at_startup_does_not_crash(agent_mock):
    agent_mock.get("/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    agent_mock.get("/v1/tasks").mock(side_effect=httpx.ConnectError("refused"))
    with TestClient(create_app(make_settings())) as c:
        from .conftest import Dash

        d = Dash(c)
        d.login()
        assert "Agent unavailable" in c.get("/").text


def test_live_mode_without_test_mode_explains_timing(agent_mock):
    agent_mock.get("/v1/health").respond(json={"status": "ok", "test_mode": False})
    run = agent_mock.post("/v1/tasks/tsk_1/run").respond(202, json={"task_id": "tsk_1", "accepted": True})
    agent_mock.get("/v1/tasks/tsk_1").respond(json=task_json())
    with TestClient(create_app(make_settings())) as c:
        from .conftest import Dash

        d = Dash(c)
        d.login()
        r = d.post("/demo-mode", {"back": "/"})
        assert "Live: demo timing disabled" in r.text
        assert "TEST_MODE is off" in r.text
        r = d.post("/tasks/tsk_1/run", {"idem": "run-live-key-01"})
        assert run.called and "normal business-day timing" in r.text


def test_test_live_mode_label(dash):
    assert "Test-live" in dash.c.get("/").text


def test_team_connect_redirects_only_to_https(dash, agent_mock):
    agent_mock.get("/v1/users/usr_1/connect").respond(
        json={"url": "https://accounts.google.com/o/oauth2?x", "expires_at": "2026-10-29T12:00:00Z"}
    )
    r = dash.post("/team/usr_1/connect", follow_redirects=False)
    assert r.headers["location"].startswith("https://accounts.google.com/")
    agent_mock.get("/v1/users/usr_1/connect").respond(
        json={"url": "javascript:alert(1)", "expires_at": "2026-10-29T12:00:00Z"}
    )
    r = dash.post("/team/usr_1/connect", follow_redirects=False)
    assert r.headers["location"] == "/team"


def test_thread_picker_falls_back_when_endpoint_missing(dash, agent_mock):
    agent_mock.get("/v1/users/usr_1/threads").respond(404, json={"error": {"code": "not_found", "message": "x"}})
    r = dash.c.get("/partials/threads", params={"owner_email": "sender@company.test"})
    assert "Paste a thread ID" in r.text


def test_repeated_cursor_does_not_loop_forever(dash, agent_mock):
    agent_mock.get("/v1/tasks").respond(json={"data": [task_json("tsk_a")], "next_cursor": "same"})
    r = dash.post("/refresh", {"back": "/tasks"})
    assert "repeated pagination cursor" in r.text
