"""Shared demo login, session expiry, login rate limiting and CSRF protection.

Demo-only: one shared password is never production multi-user authorization (B5).
"""

import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

SESSION_AUTH = "auth_at"
SESSION_CSRF = "csrf"


class LoginRateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True


def check_password(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def login(request: Request) -> None:
    request.session.clear()
    request.session[SESSION_AUTH] = int(time.time())
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)


def is_authenticated(request: Request, max_age: int) -> bool:
    at = request.session.get(SESSION_AUTH)
    if not isinstance(at, int) or time.time() - at > max_age:
        return False
    return True


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Accept the token from the X-CSRF-Token header (HTMX) or a csrf_token form field."""
    expected = request.session.get(SESSION_CSRF)
    given = request.headers.get("x-csrf-token")
    if not given:
        form = await request.form()
        given = str(form.get("csrf_token", ""))
    if not expected or not given or not hmac.compare_digest(expected, given):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")
