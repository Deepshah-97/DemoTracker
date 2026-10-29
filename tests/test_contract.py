"""The dashboard's typed models must match the agent's versioned OpenAPI contract."""

from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

from app import agent_models as m

SPEC = yaml.safe_load((Path(__file__).parent.parent / "contract" / "openapi.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]


def test_contract_is_valid_openapi():
    validate(SPEC)


def test_enums_match():
    assert set(m.TASK_STATUSES) == set(SCHEMAS["TaskStatus"]["enum"])
    assert set(m.SCENARIOS) == set(SCHEMAS["Scenario"]["enum"])


@pytest.mark.parametrize(
    "model,schema",
    [
        (m.Task, "Task"),
        (m.Draft, "Draft"),
        (m.User, "User"),
        (m.Followup, "Followup"),
        (m.Preview, "Preview"),
        (m.RunResult, "RunResult"),
        (m.ThreadSummary, "ThreadSummary"),
    ],
)
def test_model_fields_exist_in_contract(model, schema):
    props = set(SCHEMAS[schema]["properties"])
    assert set(model.model_fields) <= props, set(model.model_fields) - props


def test_task_create_fields_in_contract():
    props = set(SCHEMAS["TaskCreate"]["properties"])
    assert set(m.TaskCreate.model_fields) <= props


def test_contract_paths_used_by_client_exist():
    paths = set(SPEC["paths"])
    for p in [
        "/v1/tasks",
        "/v1/tasks/{task_id}",
        "/v1/tasks/{task_id}/run",
        "/v1/tasks/{task_id}/preview",
        "/v1/tasks/{task_id}/pause",
        "/v1/tasks/{task_id}/resume",
        "/v1/tasks/{task_id}/stop",
        "/v1/tasks/{task_id}/events",
        "/v1/drafts",
        "/v1/drafts/{draft_id}/approve",
        "/v1/drafts/{draft_id}/reject",
        "/v1/users",
        "/v1/users/{user_id}",
        "/v1/users/{user_id}/connect",
        "/v1/users/{user_id}/threads",
        "/v1/health",
    ]:
        assert p in paths


def test_webhook_events_known():
    from app.webhooks import KNOWN_EVENTS

    assert KNOWN_EVENTS == set(SCHEMAS["WebhookEventName"]["enum"])
