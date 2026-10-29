"""SQLite cache: task cards, received webhook events and idempotent dashboard actions.

The agent is the source of truth; this cache only makes pages fast and keeps the event
history across restarts.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import Engine, event
from sqlmodel import Field, Session, SQLModel, create_engine, select


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskCard(SQLModel, table=True):
    __tablename__ = "task_cards"

    agent_task_id: str = Field(primary_key=True)
    external_id: str = Field(index=True)
    owner_email: str = ""
    client_name: str = ""
    client_email: str = ""
    client_company: str | None = None
    scenario: str = ""
    status: str = Field(default="waiting", index=True)
    paused: bool = False
    followup_count: int = 0
    max_followups: int = 0
    next_check_at: str | None = None
    simulated: bool = False
    updated_at: str = Field(default_factory=utcnow)


class WebhookEventRow(SQLModel, table=True):
    __tablename__ = "webhook_events"

    # Autoincrement sequence doubles as the SSE event id for replay after reconnects.
    seq: int | None = Field(default=None, primary_key=True)
    event_id: str = Field(unique=True, index=True)
    event: str
    agent_task_id: str | None = Field(default=None, index=True)
    signature_valid: bool = True
    simulated: bool = False
    payload: str
    received_at: str = Field(default_factory=utcnow)


class ActionRow(SQLModel, table=True):
    """One row per Idempotency-Key the dashboard has used, so double clicks call the agent once."""

    __tablename__ = "actions"

    idempotency_key: str = Field(primary_key=True)
    action: str
    target_id: str | None = None
    # in_progress -> accepted (agent confirmed the request) -> confirmed (webhook arrived) | failed
    state: str = "in_progress"
    result: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str


_engine: Engine | None = None


def init_engine(path: str) -> Engine:
    global _engine
    url = "sqlite://" if path == ":memory:" else f"sqlite:///{path}"
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if path == ":memory:":
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    SQLModel.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised")
    return _engine


def session() -> Session:
    return Session(get_engine())


def get_session() -> Iterator[Session]:
    with session() as s:
        yield s


def get_setting(key: str, default: str = "") -> str:
    with session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with session() as s:
        row = s.get(Setting, key) or Setting(key=key, value=value)
        row.value = value
        s.add(row)
        s.commit()


def list_cards() -> list[TaskCard]:
    with session() as s:
        return list(s.exec(select(TaskCard).order_by(TaskCard.updated_at.desc())))  # type: ignore[attr-defined]
