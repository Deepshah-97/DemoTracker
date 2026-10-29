# Follow-Up Agent demo dashboard

A small, standalone task tracker that plugs into the Follow-Up Agent API the same way the company's
real dashboard will. Stakeholders can watch the agent work live: create a task, preview the draft,
run a check, approve the draft and see every signed webhook arrive in the live event panel.

It uses only the agent's public v1.1 API (`contract/openapi.yaml`, owned by the agent repository) and
its webhooks. It never reads the agent database and never calls Gmail or Claude.

**Demo only.** One shared password, test mailboxes, no production data.

## Run it locally in mock mode (no agent needed)

```bash
uv sync
cp .env.example .env            # AGENT_BASE_URL=mock
uv run uvicorn --factory app.main:create_app --port 8001 --reload
```

Open http://localhost:8001 and sign in with `DEMO_PASSWORD`.

With `AGENT_BASE_URL=mock` the dashboard runs a built-in fake agent that serves the same HTTP
contract in-process and posts signed webhooks back a couple of seconds after each action. Every
record, draft, email and event is labelled **SIMULATED**, and the task page gets a
*Simulate client reply* button so the stop rule can be shown without Gmail.

## Modes

The top bar always shows which mode is active:

| Badge | When | Behaviour |
|---|---|---|
| Mock mode: all data SIMULATED | `AGENT_BASE_URL=mock` | Fake agent, no Gmail or Claude |
| Test-live | Real agent with `TEST_MODE` on (`GET /v1/health` → `test_mode: true`) | Run check now may skip the business-day wait, only for the agent's allowlisted test mailboxes |
| Live: demo timing disabled | Real agent with `TEST_MODE` off | Normal timing and send windows; the Demo mode switch shows an explanation instead of forcing anything |

The Demo mode switch grants no privilege. The agent decides what *Run check now* is allowed to do,
and it always enforces recipient checks, stop conditions, idempotency and approval.

## Configuration

| Variable | Purpose |
|---|---|
| `AGENT_BASE_URL` | `https://api.<agent-domain>`, or `mock` |
| `AGENT_API_KEY` | Key issued to the demo dashboard by the agent admin |
| `AGENT_WEBHOOK_SECRET` | Shared secret for verifying webhooks |
| `DEMO_PASSWORD` | Shared login password (use a strong one) |
| `SESSION_SECRET` | Signs the session cookie, at least 32 characters |
| `DATABASE_PATH` | SQLite file on a persistent volume |
| `PUBLIC_BASE_URL` | The dashboard's own URL; the webhook address shown in the panel |
| `COOKIE_SECURE` | `true` (default). Set `false` only for plain-HTTP local development |
| `SESSION_MAX_AGE_SECONDS` | Session expiry, default 8 hours |
| `LOGIN_ATTEMPTS_PER_MINUTE` | Login rate limit per IP, default 5 |

Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Connecting to the real agent

1. The agent admin creates an API client for the demo with
   `webhook_url = https://<demo-domain>/webhooks/agent`, scoped to the two test users, and hands over
   the API key and webhook secret.
2. Set `AGENT_BASE_URL`, `AGENT_API_KEY` and `AGENT_WEBHOOK_SECRET` and restart.
3. For a live demo the agent must run with `TEST_MODE` enabled and an allowlist containing exactly the
   Sender and Client test mailboxes.

## How it works

```
Browser ──HTMX──▶ FastAPI dashboard ──httpx + Bearer key + Idempotency-Key──▶ Agent API
   ▲                    │  ▲
   └──── SSE ───────────┘  └──── signed webhooks (POST /webhooks/agent) ──── Agent
```

* **Webhooks:** HMAC-SHA256 is checked over the raw bytes with a constant-time compare. Bad signatures
  get 401 and are only logged, never stored or shown. Valid events are stored in SQLite (deduplicated
  by `event_id`) *before* being broadcast. Unknown events are stored and shown. Event JSON is escaped,
  and long email bodies are truncated in the panel.
* **Live panel:** Server-Sent Events. After a reconnect the browser replays anything it missed from
  SQLite via `Last-Event-ID`.
* **Cards:** updated from each webhook's `task_status`; if an event lacks it, or the task is unknown,
  the task is fetched. *Refresh from agent* (and startup) re-syncs everything from `GET /v1/tasks`,
  following pagination, so a missed webhook is recovered.
* **No duplicates:** every write form carries its own Idempotency-Key. The dashboard records each key
  and calls the agent at most once per key, even on double clicks; a failed call can be retried with
  the same key. Approved drafts show *Sending...* until `followup.sent` arrives.
* **Agent down:** a red banner appears, cached cards and events stay visible, and nothing is shown as
  sent.

## Tests

```bash
uv run pytest
uv run ruff check app tests
```

The suite covers the spec's acceptance tests that can be automated: signature rejection,
duplicate events, double-click protection, card movement on webhooks, all statuses and pause
flags, rejected and exhausted flows, missed-webhook recovery, escaping, session expiry, CSRF,
rate limiting, agent downtime and 401/403 handling, and the full demo script against the mock
agent. Tests that need the real agent and Gmail (the 15-minute script end to end) are run by hand.

## Deploy (Railway)

Add this repository as a second service in the agent's Railway project, attach a small volume at
`/data`, set the variables above and expose it on a public HTTPS URL so the agent can reach
`/webhooks/agent`. Run a single instance: the idempotency locks and SSE fan-out are in-process.

Local development alongside the agent: run both with Docker Compose and point the agent's webhook
URL at `http://dashboard:8001/webhooks/agent`, or use a tunnel such as cloudflared or ngrok.

## Live demo script (15 minutes)

See section 6 of the build spec. In short: show the quiet thread, create a `post_demo` task with
notes, Preview draft, Run check now, edit one word and approve, show the email in the Client inbox,
then on a second thread have the Client reply and Run check now to show `task.replied` with no send.
Record a backup video the day before.
