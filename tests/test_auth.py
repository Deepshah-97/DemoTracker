import time

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import make_settings


def test_pages_require_login(agent_mock):
    with TestClient(create_app(make_settings())) as c:
        for path in ["/", "/tasks", "/drafts", "/team", "/tasks/new"]:
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/login")
        r = c.get("/tasks", headers={"HX-Request": "true"}, follow_redirects=False)
        assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_wrong_password_and_rate_limit(agent_mock):
    with TestClient(create_app(make_settings(login_attempts_per_minute=3))) as c:
        import re

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        codes = [c.post("/login", data={"password": "nope", "csrf_token": csrf}).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]


def test_session_expires(agent_mock, monkeypatch):
    with TestClient(create_app(make_settings(session_max_age_seconds=60))) as c:
        from .conftest import Dash

        d = Dash(c)
        d.login()
        assert c.get("/", follow_redirects=False).status_code == 200
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 120)
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and "expired" in r.headers["location"]


def test_csrf_required_for_mutations(dash, agent_mock):
    route = agent_mock.post("/v1/tasks/tsk_1/run").respond(202, json={"task_id": "tsk_1", "accepted": True})
    r = dash.c.post("/tasks/tsk_1/run", data={"idem": "run-key-nocsrf1"})
    assert r.status_code == 403
    r = dash.c.post("/tasks/tsk_1/run", data={"idem": "run-key-nocsrf2", "csrf_token": "wrong"})
    assert r.status_code == 403
    assert not route.called


def test_secure_cookie_flags():
    import respx
    from fastapi.testclient import TestClient as TC

    with respx.mock(base_url="https://agent.test", assert_all_called=False) as m:
        m.get("/v1/health").respond(json={"status": "ok"})
        m.get("/v1/tasks").respond(json={"data": []})
        with TC(create_app(make_settings(cookie_secure=True)), base_url="https://testserver") as c:
            r = c.get("/login")
            cookie = r.headers["set-cookie"].lower()
            assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_api_key_never_sent_to_browser(dash):
    for path in ["/", "/tasks", "/drafts", "/team", "/tasks/new"]:
        html = dash.c.get(path).text
        assert "dash-key" not in html
        assert "test-webhook-secret" not in html


def test_settings_validation():
    import pytest

    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(make_settings(session_secret="short"))
    with pytest.raises(RuntimeError, match="https"):
        create_app(make_settings(agent_base_url="http://evil.example"))
    with pytest.raises(RuntimeError, match="AGENT_API_KEY"):
        create_app(make_settings(agent_api_key=""))
    make_settings(agent_base_url="http://localhost:8000").validate_for_startup()
