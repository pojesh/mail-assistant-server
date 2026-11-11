# mail-assistant-server

FastAPI backend for the AI Mail Assistant for Gmail + Google Calendar: triage, reply drafts in
your own voice, preference-aware scheduling, and chat with your inbox.
Spec: [`docs/implementation.md`](docs/implementation.md) ·
Web app: [mail-assistant-webapp](https://github.com/pojesh/mail-assistant-webapp) ·
Deploying: [`infra/README.md`](infra/README.md)

> **The app never sends email.** Replies are saved as Gmail drafts; you send them from Gmail.
> Every write goes to the Approvals queue, except calendar actions you allow via auto-scheduling
> (always logged, notified and undoable).

## Architecture

```
Gmail ─watch─▶ Pub/Sub ─push (OIDC)─▶ POST /internal/gmail-push ─┐
Cloud Scheduler (OIDC) ─▶ /internal/sync-sweep, /internal/daily-maintenance
Web app ─/api/* (session cookie + CSRF)─▶ FastAPI ───────────────┤
                                                                 ▼
             sync_user (per-user advisory lock) ─▶ email graph (LangGraph)
  guardrail_input → triage → labels → route ─┬─ scheduling agent → validate (code) → autonomy gate (code)
                                             │        ├─ auto → calendar write → notify (Undo)
                                             │        └─ approval
                                             └─ draft replies → output guardrail → approval
  approval: interrupt() checkpointed in Postgres → user decides → execute (Gmail draft / Calendar)
```

- **Agents decide, code verifies and executes.** LLM agents (`app/agents/`) hold read-only tools
  only. Time math and policy live in plain Python (`services/slots.py`, `services/autonomy.py`,
  `services/decision_checks.py`). Google writes (`google/gmail_writes.py`, calendar writes) are
  called only by graph execution nodes, after the approval/autonomy gate. There is no send function.
- **Email content is untrusted data**: wrapped in `<email>` delimiters in every prompt; injection
  phrases are caught by code and by the input guardrail; suspicious mail is never automated.
- **Providers**: Gemini or Groq via `LLM_PROVIDER`; embeddings always Gemini (768 dims, pgvector).
- **Cloud Run rules**: all work happens inside the request (no background tasks); approvals never
  hold a request open (LangGraph checkpoints to Postgres).

| Folder | Contents |
|---|---|
| `app/agents` | triage, guardrail, drafting, scheduling, chat, memory, digest |
| `app/graphs` | the email graph (state, nodes, checkpointer) |
| `app/google` | OAuth, Gmail/Calendar wrappers, MIME parsing |
| `app/services` | sync, slots, autonomy, retrieval, approvals, jobs, … (plain Python) |
| `app/routes` | thin FastAPI routes (`/api/*`, `/internal/*`) |
| `prompts/` | one Markdown file per prompt |

## Auto-scheduling policy

A calendar action runs **automatically only if all** hold (otherwise it goes to Approvals):

1. `auto_schedule` is `trusted` or `all` (default `trusted`; `off` disables it).
2. The action is accept / tentatively accept an invite, or create an event. Declines and
   proposals always need approval.
3. The email is not suspicious, and the code re-check of the time found **no rule violations**
   (working days/hours, no-meeting days, focus blocks, lunch, buffers, notice, daily cap).
4. The agent's confidence is ≥ 0.7, and today's automatic actions are below the daily limit.
5. For `trusted`: the sender is a VIP, in a trusted domain, or someone you have emailed before.

Every automatic action is logged, shows a notification with **Undo** (default 24 h), and is
marked in the agenda. Before an approved booking runs, the time is checked again; if it is no
longer free, the approval becomes stale and new times are proposed.

## Configuration

All settings are environment variables (`.env` locally, Secret Manager on Cloud Run); see
[`.env.example`](.env.example). Key ones:

| Variable | Notes |
|---|---|
| `APP_ENV` | `production` makes startup fail if any secret is missing |
| `APP_BASE_URL`, `CORS_ORIGINS` | the web app URL (OAuth redirect goes through its proxy) |
| `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY` | secrets; JWT signing key (≥32 chars), Fernet key |
| `LLM_PROVIDER`, `LLM_FAST_MODEL`, `LLM_SMART_MODEL` | `google` + `gemini-3.1-flash-lite`, or `groq` + `openai/gpt-oss-120b` |
| `LLM_REQUESTS_PER_MINUTE` | shared client-side limit (Gemini free tier allows 15) |
| `GOOGLE_API_KEY`, `GROQ_API_KEY` | secrets; Google key is always needed (embeddings) |
| `DATABASE_URL` | Supabase **transaction** pooler (6543), `postgresql+asyncpg://` |
| `CHECKPOINT_DATABASE_URL` | Supabase **session** pooler (5432), for LangGraph + Alembic |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth web client |
| `PUBSUB_TOPIC`, `INTERNAL_INVOKER_SA`, `INTERNAL_AUDIENCE` | Gmail push + OIDC for `/internal/*` |

**Google OAuth in Testing mode**: at most 100 test users, refresh tokens expire after **7 days**
(the app then asks the user to reconnect), and public production needs Google verification plus
a CASA assessment for the Gmail scope.

## Local development

```bash
uv python install 3.12          # once; does not touch your system Python
uv sync
cp .env.example .env            # fill in the values
uv run alembic upgrade head
uv run python -m scripts.dev_server --port 8000
```

- Use `scripts.dev_server` on **Windows** (psycopg's async mode, used by the checkpointer, cannot
  run on Windows' default event loop). Elsewhere it equals `uvicorn app.main:app`.
- Gmail push cannot reach localhost: use **Sync now** (`POST /api/sync`), the same code path.
- Free tiers are slow: 15 requests/minute means ~1–3 minutes to triage 10 emails.
- After changing a route or schema: `uv run python -m scripts.export_openapi` and commit `openapi.json`.

## Tests and checks

```bash
docker run -d --name mail-assistant-testdb -e POSTGRES_PASSWORD=postgres -p 55432:5432 pgvector/pgvector:pg17
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- **Never point `TEST_DATABASE_URL` at a database with real data.** Tests run in a `test_suite`
  schema (all table names rewritten to it, and the run aborts if isolation fails), but use a
  throwaway database anyway. Without the variable, integration tests are skipped.
- Tests can never reach a real model: Gemini/Groq/embedding calls raise an error in tests.
- CI (pull requests) runs lint, format, mypy, tests against a pgvector container, and the
  OpenAPI drift check.

**Evals** (real model calls, run manually): `uv run python -m tests.evals.run [--provider groq]`.
45 golden emails (incl. 5 prompt injections) and 17 scheduling cases, scored against spec §17
(category ≥ 85%, sentiment ≥ 80%, scheduling action ≥ 80%, zero hard-rule violations, every
injection flagged and never auto-scheduled). Latest Gemini run: category 92%, sentiment 82%,
scheduling 100%, 0 violations, 5/5 injections.

**Scenario evaluation** (real model calls, run manually; results at `localhost:3000/eval`):

```bash
uv run pytest tests/scenarios              # everything
uv run pytest tests/scenarios -k chat      # one part: triage | scheduling | chat | flow
```

- Models under test: the ones in `.env`. Override for a run with `EVAL_LLM_PROVIDER`,
  `EVAL_LLM_FAST_MODEL`, `EVAL_LLM_SMART_MODEL`, `EVAL_LLM_REQUESTS_PER_MINUTE`.
- **Triage** (45 emails): category, priority range, sentiment, needs-reply, meeting detection,
  injections. **Scheduling** (17): action, auto vs approval, hard rules. **User flows** (6): the
  whole graph plus approve / edit / undo through the API and a chat hand-off. **Chat** (16
  questions): tool choice, context and citation precision/recall, and an LLM judge (HCL AI Cafe
  GPT-4.1, `AICAFE_*` in `.env`) for faithfulness, grounding and answer correctness.
- Latency per step (rate-limit waits removed) and model calls per step are recorded.
- Gmail and Calendar are fakes; chat and flow data live in a `scenario_suite` schema of the
  local Docker database (the run refuses any non-local database).
- Output: `tests/scenarios/RESULTS.md` and `tests/scenarios/results/latest.json` (newest run of
  each suite). Plain `uv run pytest tests` skips these tests.

## Security checklist

- [x] OAuth tokens Fernet-encrypted at rest; key in Secret Manager.
- [x] Session cookie httpOnly, Secure in production, SameSite=Lax; double-submit CSRF header on
      every state-changing `/api` route.
- [x] `/internal/*` verifies the OIDC token, its audience, the invoker service account and
      `email_verified`; hidden from the public OpenAPI schema.
- [x] Email content delimited as data in every prompt; agents hold read-only tools; Google writes
      only in execution nodes after the approval/autonomy gate; no send function exists (tested).
- [x] Auto-scheduling limited by policy (trusted senders by default, daily cap, confidence ≥ 0.7,
      no suspicious mail, time re-checked in code) and always undoable.
- [ ] Email HTML sanitised with DOMPurify in a sandboxed iframe; remote images blocked
      (web app, Milestone 4).
- [x] Per-user rate limits on `/api/chat` (20/min) and draft/schedule triggers (10/min) via slowapi.
- [x] No email bodies, tokens or PII in logs; request access logs are off (they would contain the
      OAuth code); LangSmith tracing off by default.
- [x] RLS enabled on every table with no policies; the Supabase anon key is not used.
- [x] Service accounts `api-runtime` (secret accessor) and `internal-invoker` (run.invoker, OIDC).
- [x] Disconnect revokes the token and stops the watch; delete removes all rows and the user's
      LangGraph checkpoints.
