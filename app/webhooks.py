"""Webhook receiver: signature check over raw bytes, dedupe by event_id, card update, SSE publish."""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from . import db
from .agent_models import TASK_STATUSES
from .broadcast import Message, broadcaster

log = logging.getLogger("dashboard.webhooks")

KNOWN_EVENTS = {
    "draft.ready",
    "draft.rejected",
    "followup.sent",
    "task.replied",
    "task.stopped",
    "task.exhausted",
    "task.error",
    "user.disconnected",
    "daily.summary",
}


def sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256=") :]
    return hmac.compare_digest(sign(raw, secret), signature.strip().lower())


@dataclass
class WebhookResult:
    status_code: int
    duplicate: bool = False
    row: db.WebhookEventRow | None = None
    needs_task_refresh: str | None = None


def _apply_to_card(payload: dict) -> str | None:
    """Update the cached card from the event. Returns the task id if a full refresh is needed."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    with db.session() as s:
        card = s.get(db.TaskCard, task_id)
        if card is None:
            return task_id
        status = payload.get("task_status")
        if status in TASK_STATUSES:
            card.status = status
        if isinstance(payload.get("paused"), bool):
            card.paused = payload["paused"]
        if isinstance(payload.get("followup_count"), int):
            card.followup_count = payload["followup_count"]
        next_check = payload.get("next_check_at", ...)
        if next_check is None or isinstance(next_check, str):
            card.next_check_at = next_check
        card.updated_at = db.utcnow()
        s.add(card)
        s.commit()
    # Events that don't carry the full state get a background detail fetch.
    return task_id if "task_status" not in payload else None


def _confirm_actions(payload: dict) -> None:
    """Mark pending approve actions confirmed once the agent reports the outcome."""
    event = payload.get("event")
    if event not in {"followup.sent", "task.replied", "task.stopped", "task.error", "task.exhausted", "draft.rejected"}:
        return
    task_id = payload.get("task_id")
    with db.session() as s:
        from sqlmodel import select

        rows = s.exec(
            select(db.ActionRow).where(db.ActionRow.target_id == task_id, db.ActionRow.state == "accepted")
        ).all()
        for row in rows:
            row.state = "confirmed" if event == "followup.sent" else "superseded"
            row.result = event
            row.updated_at = db.utcnow()
            s.add(row)
        s.commit()


def process(raw: bytes, signature: str | None, secret: str, *, simulated: bool = False) -> WebhookResult:
    if not verify(raw, signature, secret):
        # Never stored or broadcast; visible only in the server log.
        log.warning("Rejected webhook with invalid signature (%d bytes)", len(raw))
        return WebhookResult(401)
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("Rejected signed webhook with invalid JSON")
        return WebhookResult(400)
    if not isinstance(payload, dict) or not isinstance(payload.get("event_id"), str):
        return WebhookResult(400)

    event = str(payload.get("event", "unknown"))
    if event not in KNOWN_EVENTS:
        log.info("Received unknown webhook event %r; storing for display", event)
    is_simulated = simulated or bool((payload.get("data") or {}).get("simulated"))
    row = db.WebhookEventRow(
        event_id=payload["event_id"],
        event=event,
        agent_task_id=payload.get("task_id") if isinstance(payload.get("task_id"), str) else None,
        signature_valid=True,
        simulated=is_simulated,
        payload=raw.decode("utf-8", errors="replace"),
    )
    # Store first, then broadcast (B3).
    with db.session() as s:
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return WebhookResult(200, duplicate=True)
        s.refresh(row)

    refresh = _apply_to_card(payload)
    _confirm_actions(payload)
    broadcaster.publish(Message("webhook", str(row.seq)))
    broadcaster.publish(Message("tasks"))
    return WebhookResult(200, row=row, needs_task_refresh=refresh)
