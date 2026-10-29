import json
import re
import uuid
from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.webhooks import sign

AGENT = "https://agent.test"
SECRET = "test-webhook-secret-0123456789abcdef"


def make_settings(**overrides) -> Settings:
    base = dict(
        agent_base_url=AGENT,
        agent_api_key="dash-key",
        agent_webhook_secret=SECRET,
        demo_password="pw",
        session_secret="s" * 40,
        database_path=":memory:",
        cookie_secure=False,
        mock_event_delay_seconds=0.05,
        public_base_url="https://demo.test",
    )
    return Settings(**(base | overrides))


def task_json(task_id: str = "tsk_1", status: str = "waiting", **kw) -> dict:
    return {
        "id": task_id,
        "external_id": kw.pop("external_id", f"DEMO-{task_id}"),
        "owner_user_id": "usr_1",
        "owner_email": "sender@company.test",
        "scenario": "post_demo",
        "status": status,
        "paused": False,
        "client": {"name": "Priya Shah", "email": "priya@client.test", "company": "Acme"},
        "followup_count": 0,
        "max_followups": 2,
        "wait_business_days": 1,
        "next_check_at": "2026-10-30T14:00:00Z",
        "created_at": "2026-10-29T10:00:00Z",
        "updated_at": "2026-10-29T10:00:00Z",
        "followups": [],
        "timeline": [],
        **kw,
    }


class Dash:
    def __init__(self, client: TestClient) -> None:
        self.c = client
        self.csrf = ""

    def login(self) -> None:
        r = self.c.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
        r = self.c.post("/login", data={"password": "pw", "csrf_token": csrf})
        assert r.status_code == 200
        self.csrf = re.search(r'"X-CSRF-Token": "([^"]+)"', r.text).group(1)

    def post(self, url: str, data: dict | None = None, **kw):
        return self.c.post(url, data={"csrf_token": self.csrf, **(data or {})}, **kw)

    def webhook(self, payload: dict, *, secret: str = SECRET, signature: str | None = None):
        raw = json.dumps(payload).encode()
        sig = signature if signature is not None else sign(raw, secret)
        return self.c.post(
            "/webhooks/agent", content=raw, headers={"X-Signature": sig, "Content-Type": "application/json"}
        )


def event(name: str, task_id: str | None = "tsk_1", **kw) -> dict:
    return {
        "event_id": kw.pop("event_id", "evt_" + uuid.uuid4().hex),
        "event": name,
        "task_id": task_id,
        "external_id": f"DEMO-{task_id}",
        "occurred_at": "2026-10-29T12:00:00Z",
        **kw,
    }


@pytest.fixture
def agent_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=AGENT, assert_all_called=False) as router:
        router.get("/v1/health").respond(json={"status": "ok", "test_mode": True})
        router.get("/v1/tasks").respond(json={"data": [task_json()], "next_cursor": None})
        router.get("/v1/users").respond(
            json={
                "data": [
                    {
                        "id": "usr_1",
                        "email": "sender@company.test",
                        "name": "Sam Sender",
                        "send_mode": "approve",
                        "paused": False,
                        "gmail_connected": True,
                    }
                ]
            }
        )
        router.get("/v1/drafts").respond(json={"data": []})
        router.get("/v1/tasks/tsk_1").respond(json=task_json())
        yield router


@pytest.fixture
def dash(agent_mock) -> Iterator[Dash]:
    app = create_app(make_settings())
    with TestClient(app) as c:
        d = Dash(c)
        d.login()
        yield d


@pytest.fixture
def mock_dash() -> Iterator[Dash]:
    app = create_app(make_settings(agent_base_url="mock", agent_api_key=""))
    with TestClient(app) as c:
        d = Dash(c)
        d.login()
        yield d
