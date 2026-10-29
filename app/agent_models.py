"""Typed models for the agent's v1.1 OpenAPI contract (contract/openapi.yaml).

Models accept unknown extra fields so additive agent changes never break the dashboard.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

TaskStatus = Literal["waiting", "draft_pending", "followed_up", "replied", "stopped", "exhausted", "error"]
Scenario = Literal["no_reply", "new_lead", "post_demo", "proposal_sent", "meeting_no_show", "custom"]
SendMode = Literal["approve", "auto"]

TASK_STATUSES: tuple[str, ...] = TaskStatus.__args__  # type: ignore[attr-defined]
SCENARIOS: tuple[str, ...] = Scenario.__args__  # type: ignore[attr-defined]

# Scenario guidance for the New Task form (spec A4): which baseline timestamp is needed.
SCENARIO_GUIDE: dict[str, dict[str, str | bool | None]] = {
    "no_reply": {
        "label": "No reply",
        "baseline": None,
        "requires_trigger": False,
        "help": "Baseline is the last message the owner sent in the thread. Pick or paste the thread.",
    },
    "new_lead": {
        "label": "New lead",
        "baseline": "lead_created_at",
        "requires_trigger": False,
        "help": "Baseline is when the lead was created or the inquiry arrived. Required if there is no thread yet.",
    },
    "post_demo": {
        "label": "Post demo",
        "baseline": "demo_completed_at",
        "requires_trigger": True,
        "help": "Baseline is when the demo finished. Put what was discussed and promised in the notes.",
    },
    "proposal_sent": {
        "label": "Proposal sent",
        "baseline": "proposal_sent_at",
        "requires_trigger": True,
        "help": "Baseline is when the proposal or quote was sent.",
    },
    "meeting_no_show": {
        "label": "Meeting no-show",
        "baseline": "no_show_recorded_at",
        "requires_trigger": True,
        "help": "Baseline is when the no-show was recorded. Follow-up goes out at the next permitted slot that day.",
    },
    "custom": {
        "label": "Custom",
        "baseline": "scenario_trigger_at",
        "requires_trigger": True,
        "help": "Give a goal in plain words, a wait and a baseline time.",
    },
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Client(_Model):
    name: str
    email: str
    company: str | None = None


class Followup(_Model):
    id: str
    task_id: str
    sequence_number: int
    status: str
    draft_revision: int | None = None
    subject: str | None = None
    body: str | None = None
    needs_review: bool = False
    reason: str | None = None
    edited: bool = False
    gmail_message_id: str | None = None
    scheduled_send_at: str | None = None
    created_at: str | None = None
    sent_at: str | None = None


class TimelineEntry(_Model):
    at: str
    type: str
    detail: str | None = None


class Task(_Model):
    id: str
    external_id: str
    owner_user_id: str = ""
    owner_email: str = ""
    scenario: str
    status: str
    paused: bool = False
    pause_reason: str | None = None
    client: Client | None = None
    gmail_thread_id: str | None = None
    followup_count: int = 0
    max_followups: int = 0
    wait_business_days: int | None = None
    send_mode: str | None = None
    scenario_trigger_at: str | None = None
    next_check_at: str | None = None
    last_outbound_at: str | None = None
    stop_reason: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class TaskDetail(Task):
    notes: str | None = None
    goal: str | None = None
    gmail_thread_url: str | None = None
    followups: list[Followup] = []
    timeline: list[TimelineEntry] = []


class TaskPage(_Model):
    data: list[Task]
    next_cursor: str | None = None


class Draft(_Model):
    id: str
    task_id: str
    external_id: str | None = None
    sequence_number: int
    subject: str
    body: str
    needs_review: bool = False
    reason: str | None = None
    context_summary: str | None = None
    client: Client | None = None
    owner_email: str | None = None
    created_at: str | None = None


class Preview(_Model):
    subject: str
    body: str
    needs_review: bool = False
    reason: str | None = None
    context_summary: str | None = None
    safety_failures: list[str] = []
    sequence_number: int | None = None


class RunResult(_Model):
    task_id: str
    accepted: bool
    test_mode: bool | None = None
    message: str | None = None


class User(_Model):
    id: str
    email: str
    name: str
    timezone: str | None = None
    send_mode: str = "approve"
    paused: bool = False
    gmail_connected: bool = False
    gmail_status: str | None = None


class ThreadSummary(_Model):
    thread_id: str
    subject: str
    last_sender: str | None = None
    date: str | None = None


class Health(_Model):
    status: str
    test_mode: bool = False
    version: str | None = None


class TaskCreate(BaseModel):
    external_id: str
    owner_email: str
    scenario: Scenario
    client: Client
    goal: str | None = None
    gmail_thread_id: str | None = None
    notes: str | None = None
    wait_business_days: int | None = None
    max_followups: int | None = None
    send_mode: SendMode | None = None
    scenario_trigger_at: str | None = None
    trigger_type: str | None = None
    paused: bool = False
