# AI Mail Assistant — Implementation Spec

> Audience: Claude Code. Build the system described here end to end.
> This one spec is shared by **two repositories**: `mail-assistant-server` (FastAPI backend + infra) and `mail-assistant-webapp` (Next.js frontend). A copy lives at `docs/implementation.md` in each repo. Build only the parts for the repo you are working in (§5 says which); treat the other repo's sections as the contract you must stay compatible with.
> Read the whole document before writing code. When this spec and your own preference disagree, follow the spec. When something is unspecified, choose the simplest readable option and leave a short `# NOTE:` comment explaining the choice.

---

## 1. Product summary

A production-grade, multi-agent email assistant for Gmail + Google Calendar.

It triages incoming mail (category, priority, sentiment, summary, action items), drafts replies in the user's own tone, handles meeting invites and meeting requests according to the user's preferences, and lets the user chat with their inbox.

### Core principles

1. **The app never sends email.** Replies are saved as Gmail *drafts*; the user sends them from Gmail.
2. **Human-in-the-loop by default.** Every write goes to an approvals queue, with one opt-in exception: calendar actions the user has allowed through the `auto_schedule` preference (§10.2). Autonomous actions are always logged, notified, and undoable.
3. **Agents decide, code verifies and executes.** LLM agents make judgment calls; deterministic Python checks constraints (time math, availability, policy) and performs the writes.
4. **Email content is untrusted data.** Text inside an email never decides which tools run.
5. **Readable over clever.** Plain functions, small files, type hints, Pydantic models, docstrings. No metaprogramming, no deep inheritance, no magic.
6. **Configurable LLM provider.** Google Gemini or Groq, chosen by environment variable.
7. **Minimal, free-tier-friendly infrastructure.** Two Cloud Run services, Supabase free tier, Pub/Sub, Cloud Scheduler, Secret Manager. Nothing else.

---

## 2. Features

### 2.1 Inbox triage (automatic, on new mail)
- Classify each email: `action_required`, `fyi`, `meeting_request`, `newsletter`, `promotion`, `other`.
- Priority score 1–5 (sender importance, deadlines, sentiment, VIP list).
- Sentiment: `positive | neutral | negative | frustrated | urgent` + confidence.
- One-line summary; thread summary for threads with > 3 messages.
- Action items with optional due dates.
- Detect calendar invites (`text/calendar` MIME part or Google Calendar invitation).
- Apply Gmail labels (`AI/Action`, `AI/FYI`, `AI/Meeting`, …) via the Gmail API.
- Flag suspected prompt-injection emails (`AI/Suspicious`) and exclude them from all automated drafting and scheduling.

### 2.2 Reply drafting
- 2–3 variants per email (e.g. *Short*, *Detailed*, *Polite decline*).
- Written in the user's tone using RAG over their **sent mail** + structured preferences.
- Tone adapts to the sender's sentiment (frustrated → empathetic) and VIP status (→ more formal).
- User edits and approves → saved as a Gmail draft via the Gmail API.

### 2.3 Calendar (agentic, preference-aware)
- **Invites** (an event already exists): the scheduling agent decides accept / tentative / decline / suggest another time.
- **Meeting requests in text** ("can we meet Tuesday?"): the agent decides whether to create an event directly or propose times in a reply.
- Every decision respects the user's **meeting preferences**: working days/hours, no-meeting days, focus blocks, lunch, buffers, minimum notice, daily meeting cap, preferred times of day, default duration, and free-text preference notes (e.g. "client calls only on Tue/Thu mornings").
- **Auto-scheduling** (`auto_schedule`: `off | trusted | all`): when allowed and safe, the action is executed immediately (accept invite / add event) and the user is notified with an Undo button; otherwise it goes to the approvals queue.
- Sender-proposed times that break a rule are explained ("Tue 9:00 is inside your focus block").
- Every suggested or booked slot shows a short "why" reason.
- Today/week agenda view; auto-added events are marked.

### 2.4 Preferences & personal knowledge
- **Structured preferences** (always loaded): signature, sign-off, tone, language, timezone, VIP senders, meeting preferences, auto-scheduling policy.
- **Preference notes** (free text, embedded for RAG): e.g. "When clients ask for discounts, say I need to check with finance."
- **Sent-mail examples** (embedded for RAG): style examples for drafting.
- Preferences page shows everything the assistant knows; user can edit it.
- Memory agent suggests new preferences learned from draft edits; user accepts/rejects them.

### 2.5 Attachments & shared documents
- **Not indexed for content.** Store metadata only: file name, MIME type, size, sender, date, message/thread id.
- Searchable by name/sender/type via Postgres full-text search (e.g. "the invoice PDF from Ravi").

### 2.6 Chat with your inbox
- Chat page + slide-over: "What did the client say about the deadline?", "Find the PDF Ravi sent", "Draft a reply to Priya's last email", "Find me 30 minutes with Arun next week".
- Streams intermediate steps ("Searching mail…", "Checking calendar…").
- Chat is read-only; anything that writes is handed to the email graph (same guardrails, same policy).

### 2.7 Dashboard, digest, notifications
- Dashboard: counts by category, priority queue, sentiment overview, pending approvals.
- Daily digest: needs-attention list + today's meetings, shown in-app.
- Notifications (bell): auto-scheduled actions (with Undo), new approvals, reconnect warnings.

### 2.8 Safety, transparency, control
- Approvals queue (drafts, events, invite responses).
- Activity log: every agent action, tool, reason, outcome.
- Disconnect Google; delete all my data.

---

## 3. Architecture

### 3.1 Deployment: two Cloud Run services

| Service | Contents | Access |
|---|---|---|
| `web` (repo `mail-assistant-webapp`) | Next.js (App Router) + shadcn/ui. Proxies `/api/*` to the backend at runtime so cookies are first-party. | Public |
| `api` (repo `mail-assistant-server`) | FastAPI. User routes under `/api/*`; internal routes under `/internal/*` (called by Pub/Sub and Cloud Scheduler). | Public URL; `/internal/*` requires Google-signed OIDC tokens |

Google Cloud services used (and why):

| Service | Why |
|---|---|
| Cloud Run | Hosts `web` and `api`; each deployed automatically from its own GitHub repo (Cloud Build, using the repo's `Dockerfile`). Artifact Registry stores the built images automatically — no manual setup. |
| Pub/Sub | Gmail `watch` can only deliver new-mail notifications to a Pub/Sub topic. One topic + one push subscription to `api`. |
| Cloud Scheduler | Gmail watches expire after 7 days and must be renewed; also a periodic catch-up sync and the daily digest. Two jobs total. |
| Secret Manager | API keys, DB URLs, encryption keys — mounted into Cloud Run as env vars. |

External: Supabase (Postgres + pgvector), Gmail API + Google Calendar API (OAuth, per-user tokens), Gemini API or Groq API.

Not used (deliberately): MCP servers, Cloud Tasks, Terraform, Workload Identity Federation, local Docker.

### 3.2 Request flows

**New mail (background):**
```
Gmail ──watch──▶ Pub/Sub topic ──push (OIDC)──▶ api POST /internal/gmail-push
    → take a per-user lock
    → history.list from stored history_id → new message ids
    → for each new message (max MAX_EMAILS_PER_RUN): parse, store, run the email graph
    → save history_id, return 200
```
- If the per-user lock is already held, return `503` so Pub/Sub redelivers later.
- If more messages remain than `MAX_EMAILS_PER_RUN`, process the cap and return 200; the `sync-sweep` job (every 10 min) finishes the rest.
- Pub/Sub retries on non-2xx with exponential backoff; after 5 attempts the message goes to a dead-letter topic.

**User approval:**
```
UI decides ─▶ api POST /api/approvals/{id}/decide
    → resume the paused LangGraph run with Command(resume=decision)
    → create Gmail draft / Calendar event / invite response via the Gmail and Calendar APIs
```

**Chat:**
```
UI ─▶ api POST /api/chat (SSE) → chat agent (read-only tools) → streams steps + final answer
```

### 3.3 Cloud Run rules (important)
- **Do all work inside the request.** CPU is throttled after the response is sent. Never use FastAPI `BackgroundTasks` or fire-and-forget `asyncio.create_task` for real work.
- Approval waits never hold a request open: LangGraph `interrupt()` checkpoints to Postgres and the request ends.
- `api`: concurrency 20, request timeout 600s, min instances 0, max instances 3.
- `web`: concurrency 80, timeout 300s, min instances 0.
- Pub/Sub subscription ack deadline 600s (matches the `api` timeout).

---

## 4. Tech stack

### Backend (`mail-assistant-server`)
- **Python 3.12, managed by uv.** The developer's system Python may be older; `uv python install 3.12` provides a project-only interpreter. Commit `.python-version` containing `3.12` and set `requires-python = ">=3.12,<3.13"` in `pyproject.toml`. The container uses `python:3.12-slim`.
- FastAPI, Uvicorn, Pydantic v2, pydantic-settings
- SQLAlchemy 2.0 (async) + asyncpg, Alembic migrations, pgvector
- LangChain v1 (`langchain`, `langchain-core`), **LangGraph**, `langgraph-checkpoint-postgres` (+ `psycopg[binary]`)
- `langchain-google-genai` (Gemini chat + embeddings), `langchain-groq` (Groq chat)
- `google-auth`, `google-auth-oauthlib`, `google-api-python-client` (OAuth + Gmail API + Calendar API)
- `cryptography` (Fernet) for token encryption
- `structlog` for JSON logs; optional LangSmith tracing via env
- Dev: pytest, pytest-asyncio, respx, ruff, mypy

### Frontend (`mail-assistant-webapp`)
- Next.js (latest stable, App Router, TypeScript strict), `output: "standalone"`
- Tailwind CSS v4, **shadcn/ui**, lucide-react icons
- TanStack Query, zod, react-hook-form
- next-themes, sonner, motion, cmdk (shadcn `Command`)
- `openapi-typescript` + `openapi-fetch` for a typed API client generated from the server's OpenAPI schema (§5.3)
- pnpm; Playwright for e2e

### Infra & delivery
- `infra/setup.sh` and `infra/post-deploy.sh` (in `mail-assistant-server`): commented `gcloud` commands (no Terraform).
- Deployment: Cloud Run **continuous deployment from GitHub**, one Cloud Build trigger per repo (Dockerfile build).
- CI: GitHub Actions in each repo runs lint/type-check/tests on pull requests only (no Google Cloud credentials needed). Protect `main` in both repos so merges require green CI; merging to `main` triggers that repo's deployment.

---

## 5. Repositories

Two GitHub repositories, each with its own CI, Cloud Run service, and deploy trigger.

| Repo | Contains | Deploys to |
|---|---|---|
| `mail-assistant-server` | FastAPI backend, agents, LangGraph, migrations, prompts, evals, `infra/` scripts, committed `openapi.json` | Cloud Run service `api` |
| `mail-assistant-webapp` | Next.js + shadcn frontend, generated API types, Playwright tests | Cloud Run service `web` |

Spec sections by repo:
- **Server:** §6 (server env), §7–§14, §16 (server items), §17 (unit/integration/graph/evals), §18.1–§18.5, §19.1, §20.1.
- **Webapp:** §6 (webapp env), §15, §16 (frontend items), §17 (e2e), §18.1, §18.2, §19.2, §20.2.
- **Both:** §1–§5, §13 (the API contract), §21.

### 5.1 `mail-assistant-server`

```
mail-assistant-server/
├── README.md
├── .env.example
├── openapi.json                     # GENERATED by scripts/export_openapi.py — committed, checked in CI
├── docs/
│   └── implementation.md
├── .github/workflows/ci.yml          # lint, types, tests, OpenAPI drift check
├── .python-version              # 3.12
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── entrypoint.sh                # alembic upgrade head, then uvicorn
├── alembic/
├── prompts/                     # one .md file per prompt
│   ├── triage.md
│   ├── draft.md
│   ├── scheduling.md
│   ├── guardrail_input.md
│   ├── guardrail_output.md
│   ├── chat.md
│   ├── memory.md
│   └── digest.md
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── logging.py
│   ├── errors.py
│   ├── security/
│   │   ├── session.py           # app session cookie (JWT)
│   │   ├── oidc.py              # verify Google OIDC for /internal/*
│   │   └── crypto.py            # Fernet encrypt/decrypt tokens
│   ├── models/                  # SQLAlchemy tables
│   ├── schemas/                 # Pydantic API + LLM output models
│   ├── llm/
│   │   ├── provider.py          # get_chat_model(), get_embeddings()
│   │   ├── prompts.py           # load_prompt(name)
│   │   └── retry.py             # call_with_retry()
│   ├── google/
│   │   ├── oauth.py             # consent URL, code exchange, token refresh
│   │   ├── clients.py           # build Gmail/Calendar clients per user, run_google()
│   │   ├── gmail_api.py         # typed Gmail API wrappers (sync, search, labels, drafts)
│   │   ├── calendar_api.py      # typed Calendar API wrappers (free/busy, events, RSVP)
│   │   └── mime.py              # MIME → clean text, attachments, invite detection
│   ├── agents/                  # one file per agent
│   │   ├── triage.py
│   │   ├── drafting.py
│   │   ├── scheduling.py
│   │   ├── guardrail.py
│   │   ├── chat.py
│   │   ├── memory.py
│   │   └── digest.py
│   ├── tools/                   # plain LangChain @tool functions (read-only)
│   │   ├── __init__.py          # CHAT_TOOLS, SCHEDULING_TOOLS, DIGEST_TOOLS
│   │   ├── context.py           # AgentContext (user_id, timezone, context)
│   │   ├── gmail_tools.py       # search_threads, get_thread
│   │   ├── calendar_tools.py    # check_availability, find_free_slots, find_invite_event, list_events
│   │   ├── search_tools.py      # search_attachments, search_my_sent_mail
│   │   └── flow_tools.py        # start_draft_flow, start_schedule_flow
│   ├── graphs/
│   │   ├── state.py
│   │   ├── email_graph.py
│   │   └── checkpointer.py
│   ├── services/
│   │   ├── sync.py              # history sync, per-user lock, dedupe
│   │   ├── slots.py             # pure-Python availability + slot generation
│   │   ├── autonomy.py          # auto_schedule policy gate
│   │   ├── retrieval.py         # preferences + RAG lookup
│   │   ├── embeddings.py
│   │   ├── approvals.py
│   │   ├── notifications.py
│   │   └── activity.py
│   └── routes/
│       ├── auth.py
│       ├── emails.py
│       ├── approvals.py
│       ├── calendar.py
│       ├── chat.py
│       ├── preferences.py
│       ├── attachments.py
│       ├── notifications.py
│       ├── dashboard.py
│       ├── activity.py
│       ├── account.py
│       ├── health.py
│       └── internal.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── evals/
├── scripts/
│   ├── seed_demo.py
│   └── export_openapi.py            # writes openapi.json from app.main:app
└── infra/
    ├── setup.sh                     # one-time project setup (before first deploy)
    ├── post-deploy.sh               # Pub/Sub subscription + Scheduler jobs (needs api URL)
    └── README.md                    # console steps that can't be scripted (OAuth consent, test users, budget)
```

### 5.2 `mail-assistant-webapp`

```
mail-assistant-webapp/
├── README.md
├── .env.example
├── docs/
│   └── implementation.md
├── .github/workflows/ci.yml          # lint, types, build, generated-types drift check
├── Dockerfile
├── package.json
├── next.config.ts
├── components.json
├── openapi.json                     # copy of the server schema used for generation
└── src/
    ├── app/
    │   ├── (marketing)/page.tsx
    │   ├── (app)/layout.tsx
    │   ├── (app)/inbox/page.tsx
    │   ├── (app)/inbox/[id]/page.tsx
    │   ├── (app)/approvals/page.tsx
    │   ├── (app)/calendar/page.tsx
    │   ├── (app)/chat/page.tsx
    │   ├── (app)/files/page.tsx
    │   ├── (app)/preferences/page.tsx
    │   ├── (app)/activity/page.tsx
    │   ├── (app)/settings/page.tsx
    │   ├── onboarding/page.tsx
    │   └── api/[...path]/route.ts   # runtime proxy to API_URL
    ├── components/
    ├── lib/
    │   ├── api/
    │   │   ├── schema.d.ts          # GENERATED by `pnpm gen:api` — do not edit
    │   │   └── client.ts            # openapi-fetch client + error handling
    │   ├── query-keys.ts
    │   └── sse.ts
    └── hooks/
```

### 5.3 API contract between the repos

The FastAPI OpenAPI schema is the single source of truth for request/response shapes.

**Server side**
- Every route declares `response_model` and typed request bodies, and has a stable `operation_id` (e.g. `list_emails`, `decide_approval`) set via a custom `generate_unique_id_function`.
- `uv run python -m scripts.export_openapi` writes `openapi.json` at the repo root (sorted keys, so diffs are readable). Commit it with every API change.
- CI regenerates it and fails if it differs from the committed file.
- SSE endpoints (`/api/chat`) document their event payloads as Pydantic models (`ChatStepEvent`, `ChatTokenEvent`, `ChatDoneEvent`, `ChatErrorEvent`) attached to the schema's `components` so the webapp gets types for them too.

**Webapp side**
- `pnpm gen:api` copies the schema from `OPENAPI_SOURCE` (default `../mail-assistant-server/openapi.json`; can also be a URL) into `openapi.json`, then runs `openapi-typescript openapi.json -o src/lib/api/schema.d.ts`. Both files are committed.
- `src/lib/api/client.ts` creates one `openapi-fetch` client (`baseUrl: "/api"` — the runtime proxy) with typed paths, cookie credentials, CSRF header, and a single error mapper. No hand-written response types.
- CI checks out `mail-assistant-server` (`actions/checkout` with `repository:`; a read-only `SERVER_REPO_TOKEN` secret if the server repo is private), regenerates the types, and fails if `schema.d.ts` changed — so the webapp can't merge against an outdated contract.

**Change rules**
- API changes are **additive** by default (new optional fields, new endpoints). Deploy the server first, then the webapp.
- Breaking changes go in two steps: add the new shape → update and deploy the webapp → remove the old shape in a later server release.
- Version tags: tag server releases `server-vX.Y.Z`; the webapp README records the server version its `openapi.json` came from.

---

## 6. Configuration

All config via environment variables. Locally from `.env` (git-ignored); on Cloud Run, secrets come from Secret Manager mounted as env vars.

**`mail-assistant-webapp`** needs only:

```dotenv
API_URL=http://localhost:8000        # server URL, read at runtime by the proxy route (never exposed to the browser)
OPENAPI_SOURCE=../mail-assistant-server/openapi.json   # used by `pnpm gen:api` only
```

**`mail-assistant-server`** (`app/config.py`, pydantic-settings):

```dotenv
# --- App ---
APP_ENV=development                  # development | production
APP_BASE_URL=http://localhost:3000   # public web URL (OAuth redirect + cookies)
SESSION_SECRET=                      # secret
TOKEN_ENCRYPTION_KEY=                # secret; Fernet key
CORS_ORIGINS=http://localhost:3000
MAX_EMAILS_PER_RUN=10

# --- LLM provider ---
LLM_PROVIDER=google                  # google | groq
LLM_FAST_MODEL=                      # fast model for triage, checks, scheduling
LLM_SMART_MODEL=                     # stronger model for drafting and chat
LLM_TEMPERATURE=0.2
GOOGLE_API_KEY=                      # secret; required for google provider AND embeddings
GROQ_API_KEY=                        # secret; required if LLM_PROVIDER=groq

# --- Embeddings (always Google; Groq has no embeddings API) ---
EMBEDDING_MODEL=
EMBEDDING_DIM=768

# --- Database (Supabase) ---
DATABASE_URL=                        # secret; transaction pooler (postgresql+asyncpg://...)
CHECKPOINT_DATABASE_URL=             # secret; SESSION pooler (postgresql://...) for LangGraph + Alembic

# --- Google OAuth / APIs ---
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=          # secret
GOOGLE_OAUTH_REDIRECT_PATH=/api/auth/google/callback
GCP_PROJECT_ID=
PUBSUB_TOPIC=projects/<project>/topics/gmail-push
INTERNAL_INVOKER_SA=                 # service account email used by Pub/Sub + Scheduler
INTERNAL_AUDIENCE=                   # api service URL (OIDC audience)

# --- Observability (optional) ---
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LOG_LEVEL=INFO
```

Validation in `Settings`:
- `LLM_PROVIDER=groq` requires `GROQ_API_KEY`.
- `GOOGLE_API_KEY` always required (embeddings).
- In production, fail fast at startup if any secret is missing.

---

## 7. LLM provider abstraction

One small file. Every agent calls `get_chat_model("fast" | "smart")`; nothing else knows which provider is active.

```python
# app/llm/provider.py
from functools import lru_cache
from typing import Literal

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.config import settings

ModelTier = Literal["fast", "smart"]


def get_chat_model(tier: ModelTier = "fast") -> BaseChatModel:
    """Return the chat model for the configured provider.

    'fast' is for triage, checks and scheduling; 'smart' is for drafting and chat.
    """
    model_name = settings.llm_fast_model if tier == "fast" else settings.llm_smart_model

    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            temperature=settings.llm_temperature,
            google_api_key=settings.google_api_key,
        )

    if settings.llm_provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=model_name,
            temperature=settings.llm_temperature,
            api_key=settings.groq_api_key,
        )

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


@lru_cache
def get_embeddings() -> Embeddings:
    """Embeddings always use Google (Groq has no embeddings endpoint)."""
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(
        model=settings.embedding_model,
        google_api_key=settings.google_api_key,
    )
```

Rules:
- Structured outputs use `model.with_structured_output(PydanticModel)`.
- Prompts live in `prompts/*.md` (server repo), loaded with `load_prompt("triage")`.
- Every LLM call goes through `call_with_retry()` (timeout + 2 retries with exponential backoff on rate-limit/5xx).
- Free tiers rate-limit aggressively: keep prompts short, cap email bodies at ~6,000 characters (head + tail), reuse the triage output in later agents instead of re-reading the email.

---

## 8. Google integration

### 8.1 OAuth (single flow: sign-in + Gmail + Calendar)
- FastAPI owns the flow: `GET /api/auth/google/login` → Google consent → `GET /api/auth/google/callback` (both reached through the web proxy).
- Scopes (request only what's used):
  - `openid`, `email`, `profile`
  - `https://www.googleapis.com/auth/gmail.modify` (read, labels, drafts, watch)
  - `https://www.googleapis.com/auth/calendar.events`
  - `https://www.googleapis.com/auth/calendar.readonly` (calendar list, free/busy)
- `access_type=offline`, `prompt=consent`, `state` + PKCE.
- Tokens Fernet-encrypted in `oauth_tokens`; `get_valid_access_token(user_id)` refreshes before expiry.
- Testing-mode apps get refresh tokens that expire after 7 days: on `invalid_grant`, set `users.status = needs_reconnect` and show a "Reconnect Google" banner.
- After callback: upsert user, set httpOnly `session` cookie (JWT, 7 days, `SameSite=Lax`, `Secure` in prod), start Gmail `watch`, redirect to `/onboarding` or `/inbox`.

### 8.2 Google API clients (`google/clients.py`)

The app talks to Google only through the official **Gmail API** and **Google Calendar API** using `google-api-python-client`. No MCP servers are used.

```python
# app/google/clients.py
import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.google.oauth import get_valid_access_token


async def get_gmail(user_id: str) -> Any:
    """Build a Gmail API client authorised with the user's own token."""
    token = await get_valid_access_token(user_id)
    return build("gmail", "v1", credentials=Credentials(token), cache_discovery=False)


async def get_calendar(user_id: str) -> Any:
    """Build a Calendar API client authorised with the user's own token."""
    token = await get_valid_access_token(user_id)
    return build("calendar", "v3", credentials=Credentials(token), cache_discovery=False)


async def run_google(request: Any) -> Any:
    """Execute a blocking googleapiclient request without blocking the event loop."""
    return await asyncio.to_thread(request.execute)
```

- Every Google call goes through `run_google()`, which also retries 429/5xx with exponential backoff (2 retries) and maps 401/`invalid_grant` to `NeedsReconnect`.
- Clients are built per request so each user's own token is used; never cache a client across users.

### 8.3 Gmail and Calendar API wrappers

Thin, typed async functions — one per API operation — that return small Pydantic models (never raw API dicts). Agents' tools and execution nodes both call these.

`google/gmail_api.py`:

| Function | Gmail API call | Used by |
|---|---|---|
| `start_watch(user_id)` / `stop_watch(user_id)` | `users.watch` (label `INBOX`, Pub/Sub topic) / `users.stop` | onboarding, `daily-maintenance`, disconnect |
| `list_history(user_id, start_history_id)` | `users.history.list` | sync |
| `get_message(user_id, message_id)` | `users.messages.get(format=full)` | sync |
| `list_sent(user_id, limit=200)` | `users.messages.list(q="in:sent")` | onboarding embeddings |
| `search_threads(user_id, query, limit)` | `users.threads.list(q=…)` + `threads.get(format=metadata)` | chat, digest |
| `get_thread(user_id, thread_id)` | `users.threads.get(format=full)` | chat, drafting context |
| `ensure_labels(user_id)` | `users.labels.list` / `labels.create` for `AI/*` labels | onboarding |
| `apply_labels(user_id, message_id, add, remove)` | `users.messages.modify` | labeling node |
| `create_draft(user_id, thread_id, to, subject, body, in_reply_to)` | `users.drafts.create` (RFC 2822 message, base64url, `threadId`, `In-Reply-To`/`References` headers) | execution node only |

`google/calendar_api.py`:

| Function | Calendar API call | Used by |
|---|---|---|
| `get_busy(user_id, start, end)` | `freebusy.query` (primary calendar) | `slots.py` via `check_availability` / `find_free_slots` |
| `list_events(user_id, start, end)` | `events.list(singleEvents=True, orderBy="startTime")` | agenda, chat, digest, daily meeting cap |
| `find_event_by_ical_uid(user_id, ical_uid)` | `events.list(iCalUID=…)` | invite handling |
| `create_event(user_id, event)` | `events.insert` (`conferenceDataVersion=1` + `createRequest` for a Meet link, `sendUpdates="all"`) | execution node only |
| `respond_to_invite(user_id, event_id, response)` | `events.patch` setting the user's own attendee `responseStatus` (`accepted`, `tentative`, `declined`, `needsAction`), `sendUpdates="all"` | execution node only |
| `delete_event(user_id, event_id)` | `events.delete(sendUpdates="all")` | Undo |

Rules:
- There is **no send-email function**. Do not add one.
- Write functions (`create_draft`, `apply_labels`, `create_event`, `respond_to_invite`, `delete_event`) are never wrapped as LLM tools. Only graph execution nodes call them, after the approval/autonomy gate (labels are applied by the deterministic labeling node).

### 8.3.1 Agent tools (`app/tools/`)

Plain LangChain `@tool` functions wrapping the **read** functions above. The user id comes from the runtime context, never from the model.

```python
# app/tools/gmail_tools.py
from langchain.tools import ToolRuntime, tool

from app.google import gmail_api
from app.tools.context import AgentContext


@tool
async def search_threads(
    query: str, runtime: ToolRuntime[AgentContext], max_results: int = 10
) -> list[dict]:
    """Search the user's Gmail using Gmail search syntax (e.g. 'from:ravi invoice newer_than:30d')."""
    threads = await gmail_api.search_threads(runtime.context.user_id, query, max_results)
    return [thread.model_dump() for thread in threads]
```

- `AgentContext` is a small dataclass (`user_id`, `timezone`, `context` from retrieval) passed via `agent.ainvoke(..., context=AgentContext(...))`. Fall back to a closure-based tool factory (`make_gmail_tools(user_id)`) if the installed LangChain version lacks `ToolRuntime`.
- Tool results are trimmed (snippets, capped bodies) to keep prompts small.
- Tool sets per agent are explicit lists in `app/tools/__init__.py`: `CHAT_TOOLS`, `SCHEDULING_TOOLS`, `DIGEST_TOOLS`.

| Module | Tools |
|---|---|
| `gmail_tools.py` | `search_threads`, `get_thread` |
| `calendar_tools.py` | `get_meeting_preferences`, `check_availability`, `find_free_slots`, `find_invite_event`, `list_events` |
| `search_tools.py` | `search_attachments`, `search_my_sent_mail` |
| `flow_tools.py` | `start_draft_flow`, `start_schedule_flow` (chat only) |

### 8.3.2 Quotas
Both APIs are free; Gmail uses per-user quota units (e.g. `messages.get` and `threads.get` cost more than `list`). Prefer `format=metadata` for lists, fetch full bodies only when needed, and never page through more than `MAX_EMAILS_PER_RUN` messages per sync.

### 8.4 MIME parsing (`mime.py`)
- Prefer `text/plain`; fall back to HTML → text.
- Strip quoted replies and common signatures.
- Decode headers (From, To, Cc, Subject, Date, Message-ID, In-Reply-To).
- Attachment metadata only (filename, mimeType, size, attachmentId) — never download bodies.
- **Invite detection:** if a `text/calendar` part exists, parse `UID`, `METHOD`, `DTSTART`, `DTEND`, `ORGANIZER`, `SUMMARY` into `invite: InviteInfo | None` (small manual parser or the `icalendar` package).

### 8.5 Sync service (`services/sync.py`)
- `sync_user(user_id)` is used by `/internal/gmail-push`, `/internal/sync-sweep`, and `POST /api/sync` (same code path everywhere, including local dev).
- Per-user lock: `pg_try_advisory_xact_lock(hashtext(user_id))`. If not acquired: push route returns 503 (Pub/Sub retries), sweep skips the user, manual sync returns "already syncing".
- Idempotency: skip messages already in `emails`; skip graph runs if `email_insights` exists.
- Advance `history_id` only after the processed messages are stored.

---

## 9. Data model (Supabase Postgres)

Enable extensions `vector` and `pg_trgm`. All tables: `id uuid pk default gen_random_uuid()`, `created_at`, `updated_at`. User-owned tables: `user_id uuid not null references users(id) on delete cascade` + index.

| Table | Key columns |
|---|---|
| `users` | `email unique`, `name`, `picture_url`, `status` (`active`, `needs_reconnect`), `onboarded bool` |
| `oauth_tokens` | `user_id unique`, `access_token_enc`, `refresh_token_enc`, `expires_at`, `scopes text[]` |
| `sync_state` | `user_id unique`, `history_id`, `watch_expires_at`, `last_synced_at` |
| `emails` | `gmail_id` (unique per user), `thread_id`, `from_email`, `from_name`, `to`, `cc`, `subject`, `snippet`, `body_text` (truncated), `received_at`, `is_sent bool`, `invite jsonb null` |
| `email_insights` | `email_id unique`, `category`, `priority`, `sentiment`, `sentiment_score`, `summary`, `action_items jsonb`, `meeting_intent jsonb null`, `suspicious bool`, `model`, `provider` |
| `attachments` | `email_id`, `filename`, `mime_type`, `size_bytes`, `gmail_attachment_id`, `search tsvector` (generated from filename + sender + subject, GIN) |
| `sent_embeddings` | `email_id`, `chunk`, `embedding vector(768)` (HNSW, cosine) |
| `preferences` | `user_id unique`, `signature`, `sign_off`, `tone`, `language`, `timezone`, `vip_senders text[]`, `meeting_preferences jsonb` (validated by `MeetingPreferences`, §10.2) |
| `preference_notes` | `text`, `source` (`user`, `learned`), `status` (`active`, `suggested`, `rejected`), `embedding vector(768)` |
| `approvals` | `kind` (`draft`, `event`, `invite_response`), `email_id null`, `thread_id` (LangGraph), `payload jsonb`, `warnings jsonb`, `status` (`pending`, `approved`, `rejected`, `expired`, `stale`), `decided_at`, `result jsonb` |
| `scheduling_actions` | `email_id`, `action`, `calendar_event_id`, `mode` (`auto`, `approved`), `decision jsonb`, `undo_until`, `undone bool` |
| `notifications` | `type`, `title`, `body`, `link`, `read bool`, `undo_action_id null` |
| `activity_log` | `actor`, `action`, `tool null`, `email_id null`, `detail jsonb`, `status` (`ok`, `error`, `blocked`, `auto`) |
| `chat_messages` | `session_id`, `role`, `content`, `steps jsonb` |
| `digests` | `date`, `content jsonb` |

Notes:
- Enable RLS on all tables with no public policies; the backend is the only client (database password via Secret Manager). The Supabase anon key is not used.
- `body_text` truncated to ~20k chars; `daily-maintenance` sets it to `NULL` after 30 days (insights remain).

---

## 10. Agents

Each agent is one file in `app/agents/` exposing plain `async def` functions with typed inputs and Pydantic outputs. Agents never import each other; the graph wires them together. **No agent ever holds a write tool.**

| Agent | Function(s) | Model tier | Tools (read-only) | Output |
|---|---|---|---|---|
| **Guardrail** | `check_input(email)`, `check_output(draft, email)` | fast | none | `InputCheck {suspicious, reasons}`, `OutputCheck {ok, problems}` |
| **Triage** | `triage(email, prefs)` | fast | none | `TriageResult` |
| **Scheduling** | `decide_scheduling(email, triage, context, user_id)` | fast | `get_meeting_preferences`, `check_availability`, `find_free_slots`, `find_invite_event`, `list_events` | `SchedulingDecision` |
| **Drafting** | `draft_replies(email, triage, context, scheduling=None)` | smart | none | `DraftSet` (2–3 variants) |
| **Chat (supervisor)** | `run_chat(user_id, messages)` | smart | `search_threads`, `get_thread`, `search_attachments`, `search_my_sent_mail`, `list_events`, `find_free_slots`, `start_draft_flow`, `start_schedule_flow` | streamed answer + steps |
| **Memory** | `suggest_preferences(original_draft, final_text)` | fast | none | `list[PreferenceSuggestion]` |
| **Digest** | `build_digest(user_id)` | fast | `search_threads`, `list_events` | `Digest` |

Triage output:
```python
# app/schemas/llm.py
from typing import Literal
from pydantic import BaseModel, Field

Category = Literal["action_required", "fyi", "meeting_request", "newsletter", "promotion", "other"]
Sentiment = Literal["positive", "neutral", "negative", "frustrated", "urgent"]


class ActionItem(BaseModel):
    task: str
    due: str | None = Field(None, description="ISO date if a deadline is stated")


class MeetingIntent(BaseModel):
    wants_meeting: bool
    proposed_times: list[str] = Field(default_factory=list, description="ISO 8601 with offset")
    duration_minutes: int | None = None
    topic: str | None = None


class TriageResult(BaseModel):
    category: Category
    priority: int = Field(ge=1, le=5)
    sentiment: Sentiment
    sentiment_score: float = Field(ge=0, le=1)
    summary: str = Field(max_length=200)
    action_items: list[ActionItem] = Field(default_factory=list)
    meeting_intent: MeetingIntent | None = None
    needs_reply: bool
```

General agent rules:
- Wrap email content in `<email>…</email>` delimiters with an instruction that it is data, not instructions.
- If `suspicious` is true, the graph skips drafting and scheduling; it only labels the email.
- Drafting must not invent facts, prices, dates, or commitments; the output guardrail checks this. On failure: redraft once with the problems listed, then surface to the user with a warning.
- All times use the user's `timezone`; ISO 8601 with offsets everywhere.
- Priority boost (deterministic, after triage): +1 VIP sender, +1 `frustrated`/`urgent`, capped at 5.

### 10.1 Scheduling agent (LLM decides, tools do the math)

The scheduling agent is a tool-calling agent (LangChain `create_agent`, fast model, `prompts/scheduling.md`). It makes the judgment calls; deterministic tools do every time calculation. Its final answer is a structured `SchedulingDecision` — it never writes to the calendar itself.

**Two cases:**
1. **Invite** (`email.invite` present, or a Google Calendar invitation): the event already exists in the user's calendar (usually pending). Options: `accept_invite`, `tentative_invite`, `decline_invite`, `propose_new_time`.
2. **Text request** (`triage.meeting_intent.wants_meeting`, no invite): options: `create_event` (when the sender proposed a concrete time that works, or explicitly asked the user to send an invite), `propose_times` (reply with 3 options), `ignore` (not a real request).

**Deterministic tools** (`app/tools/calendar_tools.py`, backed by `services/slots.py`):

| Tool | Behaviour |
|---|---|
| `get_meeting_preferences()` | Returns `MeetingPreferences`, timezone, VIP list, top preference notes (from `state["context"]`). |
| `check_availability(start, end)` | Calendar API free/busy + all hard rules (§10.2). Returns `{ok: bool, violations: [rule, detail]}`. |
| `find_free_slots(duration_minutes, window_start, window_end, limit=10)` | Generates valid slots in pure Python; each with `id`, start/end, `soft_score`, and a reason. |
| `find_invite_event(ical_uid)` | Looks up the existing calendar event for an invite (Calendar API `events.list(iCalUID=…)`). |

Decision model:
```python
# app/schemas/scheduling.py
from typing import Literal
from pydantic import BaseModel, Field

SchedulingAction = Literal[
    "accept_invite",
    "tentative_invite",
    "decline_invite",
    "propose_new_time",
    "create_event",
    "propose_times",
    "ignore",
]


class Slot(BaseModel):
    start: str  # ISO 8601 with offset
    end: str
    reason: str  # shown to the user, e.g. "Morning, matches your preference"


class SchedulingDecision(BaseModel):
    action: SchedulingAction
    event_id: str | None = None  # existing event for invite actions
    slot: Slot | None = None  # for create_event
    proposed_slots: list[Slot] = Field(default_factory=list, max_length=3)
    title: str | None = None
    attendees: list[str] = Field(default_factory=list)
    agenda: str | None = None
    add_video_link: bool = True
    needs_reply: bool  # e.g. propose_times / decline need a draft
    reason: str  # one-line explanation for the user
    confidence: float = Field(ge=0, le=1)
```

**Verification after the agent (`validate_decision` node, plain Python):**
- For `accept_invite`, `tentative_invite`, and `create_event`: re-run `check_availability` on the actual time. If it fails, attach the violations as warnings and force the approval path (never trust the model's claim that a slot is free).
- Every proposed slot must also pass `check_availability`; drop any that don't and top up from `find_free_slots`.
- Reject decisions with unknown actions or missing required fields; fall back to the approval path with a warning.

**Prompt guidance (`scheduling.md`):** use the tools rather than calculating times yourself; respect hard rules absolutely; use preference notes and sender context for soft choices; VIPs can bend soft preferences; when unsure, choose `propose_times` or the approval path; explain the decision in one line.

### 10.2 Meeting preferences & autonomy policy

```python
# app/schemas/preferences.py
from datetime import time
from typing import Literal
from pydantic import BaseModel, Field

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeRange(BaseModel):
    start: time
    end: time


class FocusBlock(BaseModel):
    day: Weekday
    start: time
    end: time


class MeetingPreferences(BaseModel):
    # --- hard rules (checked in code, never violated by auto actions) ---
    working_days: list[Weekday] = ["mon", "tue", "wed", "thu", "fri"]
    working_hours: TimeRange = TimeRange(start=time(9, 30), end=time(18, 0))
    no_meeting_days: list[Weekday] = []
    focus_blocks: list[FocusBlock] = []
    lunch_break: TimeRange | None = TimeRange(start=time(13, 0), end=time(14, 0))
    buffer_minutes: int = Field(10, ge=0, le=60)
    min_notice_hours: int = Field(4, ge=0, le=72)
    max_meetings_per_day: int = Field(6, ge=1, le=20)
    search_horizon_days: int = Field(10, ge=1, le=30)

    # --- soft preferences (used by the agent and for ranking) ---
    preferred_times: list[Literal["morning", "afternoon", "evening"]] = ["morning"]
    default_duration_minutes: int = Field(30, ge=15, le=240)
    add_video_link: bool = True

    # --- autonomy ---
    auto_schedule: Literal["off", "trusted", "all"] = "trusted"
    trusted_domains: list[str] = []  # e.g. the user's company domain
    auto_actions_per_day: int = Field(5, ge=0, le=20)
    undo_window_hours: int = Field(24, ge=1, le=72)
    vip_can_override_focus: bool = False
```

**Slot generation (`services/slots.py`)** — pure Python, fully unit-tested:
1. Window starts at `now + min_notice_hours` (or around sender-proposed dates) and ends at `search_horizon_days`.
2. Busy intervals from Calendar API `freebusy.query`, each expanded by `buffer_minutes`.
3. Step through the window in 15-minute increments; keep a slot only if it is on a working day, not a no-meeting day, fully inside working hours, outside lunch and focus blocks (unless VIP + `vip_can_override_focus`), outside buffered busy times, and on a day below `max_meetings_per_day`.
4. `soft_score`: +2 inside a preferred time of day, +1 earlier in the window, +1 matches a sender-proposed time.

**Autonomy gate (`services/autonomy.py`)** — plain Python, runs after `validate_decision`:

Auto-execute only if **all** are true:
- `auto_schedule` is `trusted` or `all`.
- Action is `accept_invite`, `tentative_invite`, or `create_event` (declines and proposals always go to approval, because they need a reply or carry social cost).
- The email is not flagged suspicious and validation produced no warnings.
- `confidence >= 0.7`.
- Today's auto actions < `auto_actions_per_day`.
- For `trusted`: sender is a VIP, **or** in `trusted_domains`, **or** the user has emailed this address before (`emails.is_sent` to that address).

Otherwise → approval queue.

On auto-execution: perform the Calendar API write (`create_event` or `respond_to_invite`), insert `scheduling_actions` (`mode=auto`, `undo_until`), create a notification with **Undo**, log activity with status `auto`. Note in the UI that accepting an invite notifies the organizer.

**Undo** (`POST /api/scheduling-actions/{id}/undo`, within `undo_window_hours`): delete an auto-created event; for an accepted invite, change the response to `tentative` (`needsAction` is also supported by `events.patch`; prefer it). Log the undo.

---

## 11. LangGraph: email flow

### 11.1 State
```python
# app/graphs/state.py
from typing import TypedDict

from app.schemas.llm import DraftSet, InputCheck, OutputCheck, TriageResult
from app.schemas.scheduling import SchedulingDecision


class EmailState(TypedDict, total=False):
    user_id: str
    email_id: str
    email: dict  # parsed email incl. optional invite info
    input_check: InputCheck
    triage: TriageResult
    context: dict  # preferences + meeting prefs + RAG examples + notes
    scheduling: SchedulingDecision
    scheduling_warnings: list[str]
    auto_approved: bool
    drafts: DraftSet
    output_check: OutputCheck
    redraft_count: int
    decision: dict  # user's approval payload from the UI
    result: dict  # ids of created draft / event / response
    errors: list[str]
```

### 11.2 Graph
```
START
  → guardrail_input
  → triage
  → apply_labels                         (Gmail API, plain code)
  → route                                (plain Python)
       ├─ suspicious / newsletter / promotion / no reply needed ─▶ END
       ├─ invite or meeting request ─▶ retrieve_context
       │      → scheduling_agent         (LLM + deterministic tools)
       │      → validate_decision        (plain Python)
       │      → autonomy_gate            (plain Python)
       │           ├─ auto ─▶ execute_calendar_action → notify → END
       │           │          (if decision.needs_reply, continue to draft_replies first)
       │           └─ approval ─▶ [draft_replies → guardrail_output, if decision.needs_reply]
       │                          → await_approval
       └─ needs reply ─▶ retrieve_context → draft_replies → guardrail_output → await_approval
await_approval   (interrupt(); writes an `approvals` row; graph pauses)
  → execute_decision                     (Gmail draft and/or calendar action via the APIs)
  → learn_from_edit                      (Memory agent, only if the user edited a draft)
  → END
```

- `route`, `validate_decision`, `autonomy_gate`, `execute_*`, `notify` contain no LLM calls.
- `guardrail_output` loops back to `draft_replies` at most once (`redraft_count`).
- Before executing an approved calendar action, re-run `check_availability`; if the slot is no longer free, mark the approval `stale` and re-run the scheduling agent.
- `await_approval` uses `interrupt({...})`; the approvals route resumes with `graph.ainvoke(Command(resume=decision), config={"configurable": {"thread_id": approval.thread_id}})`.
- `thread_id = f"email:{user_id}:{email_id}"`.
- Checkpointer: `AsyncPostgresSaver` on `CHECKPOINT_DATABASE_URL` (Supabase **session** pooler; the transaction pooler breaks prepared statements). Call `setup()` once at startup.
- Every node writes one `activity_log` row.
- Approvals older than 7 days are marked `expired` by `daily-maintenance`.

### 11.3 Chat agent
- LangChain v1 `create_agent` with the smart model and `prompts/chat.md`.
- Tools (`CHAT_TOOLS`): `search_threads`, `get_thread`, `search_attachments`, `search_my_sent_mail`, `list_events`, `find_free_slots`, `start_draft_flow(email_id)`, `start_schedule_flow(email_id)`.
- `start_*_flow` runs the email graph from `retrieve_context` for that email, so chat-initiated actions pass through the same validation, autonomy gate, and approvals.
- Stream with `astream(..., stream_mode=["updates", "messages"])` → SSE events `step`, `token`, `done`, `error`.
- Persist messages in `chat_messages`.

---

## 12. RAG & retrieval (`services/retrieval.py`)

`get_context(user_id, email) -> dict` returns:
1. **Structured preferences + meeting preferences** — always included.
2. **Top 3 preference notes** — pgvector cosine on `preference_notes` (`status='active'`).
3. **Top 3 sent-mail examples** — pgvector cosine on `sent_embeddings`; +0.1 score if the recipient domain matches.

Embedding pipeline (`services/embeddings.py`):
- Onboarding: embed the last 200 sent emails (quotes stripped, ≤ 1,500 chars, one chunk per email).
- Sync: embed new messages with label `SENT`.
- Batch ≤ 50 per call; throttle for free-tier limits.

Attachment search: `plainto_tsquery` on the generated `tsvector` + `pg_trgm` similarity on filename. No content indexing.

---

## 13. API endpoints

User routes (session cookie required unless noted):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | health (public) |
| GET | `/api/auth/google/login` | start OAuth (public) |
| GET | `/api/auth/google/callback` | finish OAuth (public) |
| POST | `/api/auth/logout` | clear session |
| GET | `/api/me` | current user + status |
| GET | `/api/emails?category=&priority=&sentiment=&q=&cursor=` | inbox list with insights |
| GET | `/api/emails/{id}` | email + insights + thread + attachments + scheduling decision |
| POST | `/api/emails/{id}/draft` | start draft flow manually |
| POST | `/api/emails/{id}/schedule` | start scheduling flow manually |
| POST | `/api/sync` | sync now (same code as the push handler) |
| GET | `/api/approvals?status=pending` | approvals queue |
| POST | `/api/approvals/{id}/decide` | `{action: approve|reject, variant_index?, edited_body?, slot_index?, response?}` |
| POST | `/api/scheduling-actions/{id}/undo` | undo an auto action within the window |
| GET | `/api/calendar/agenda?from=&to=` | events via the Calendar API (auto-added ones flagged) |
| POST | `/api/chat` | SSE stream |
| GET | `/api/chat/sessions`, `/api/chat/sessions/{id}` | history |
| GET/PUT | `/api/preferences` | structured + meeting preferences + autonomy |
| GET/POST/PATCH/DELETE | `/api/preferences/notes` | notes; PATCH accepts/rejects suggestions |
| GET | `/api/attachments?q=` | file name search |
| GET | `/api/notifications`, POST `/api/notifications/read` | notification bell |
| GET | `/api/dashboard` | counts, sentiment breakdown, pending approvals, auto actions today |
| GET | `/api/activity?cursor=` | activity log |
| GET | `/api/digest/today` | latest digest |
| POST | `/api/account/disconnect` | revoke Google token, stop watch |
| DELETE | `/api/account` | delete all user data |

Internal routes (Google OIDC required via `require_internal_caller`):

| Method | Path | Caller | Does |
|---|---|---|---|
| POST | `/internal/gmail-push` | Pub/Sub push subscription | `sync_user` for the mailbox in the message |
| POST | `/internal/sync-sweep` | Scheduler, every 10 min | catch-up `sync_user` for users not synced in 10+ min; build today's digest for users whose local time has passed 8:00 and have none yet |
| POST | `/internal/daily-maintenance` | Scheduler, daily | renew Gmail watches expiring within 2 days; expire old approvals; apply retention |

OIDC check: `google.oauth2.id_token.verify_oauth2_token(token, request, audience=INTERNAL_AUDIENCE)`, then require `email == INTERNAL_INVOKER_SA` and `email_verified`. Return 401 otherwise.

Errors: one exception handler returning `{error: {code, message}}`. Never leak stack traces, tokens, or email content.

---

## 14. Backend code style (must follow)

- Files ≤ ~200 lines; functions ≤ ~40 lines. Split when larger.
- Type hints and a one-line docstring (what + why) on every public function.
- Routes are thin: validate → call a `services/` function → return a schema.
- Policy and time math live in plain Python modules (`slots.py`, `autonomy.py`) with no LLM or I/O inside the core functions, so they are trivially unit-testable.
- No global mutable state except cached settings/engine/model factories.
- `Depends` for `db`, `current_user`, `require_internal_caller`.
- Plain names: `get_email`, `save_draft`, `check_availability`. No abbreviations.
- Early returns over nested `if`s.
- Small custom exceptions (`NotFound`, `NeedsReconnect`, `LLMUnavailable`, `SlotUnavailable`) mapped to HTTP codes in `errors.py`.
- `structlog` key-value logs; never log email bodies or tokens.
- Use Python 3.12 features freely (`datetime.UTC`, `StrEnum`, `typing.Self`, `TaskGroup`).
- `ruff` (format + lint) and `mypy --strict` on `app/` must pass.

---

## 15. Frontend: modern UI/UX

### 15.1 Design direction
- Calm productivity aesthetic (Linear / Superhuman style): neutral surfaces, one accent color, generous whitespace, 14px base UI text, Inter or Geist.
- Light / dark / system themes (next-themes).
- Rounded-lg cards, subtle borders over heavy shadows, 150–200 ms transitions; respect `prefers-reduced-motion`.
- Responsive: three-pane on desktop, list → detail on mobile, bottom nav on small screens.
- Accessible: keyboard reachable, visible focus rings, ARIA labels on icon buttons; color is never the only signal (chips have icon + text).

### 15.2 App shell
- Sidebar: Inbox (category sub-filters + counts), Approvals (badge), Calendar, Chat, Files, Preferences, Activity, Settings.
- Top bar: command palette trigger (`⌘K`), sync status ("Synced 2m ago"), notification bell, theme toggle, avatar menu.
- Global "Reconnect Google" banner when `status=needs_reconnect`.

### 15.3 Pages
- **Landing / sign-in:** value proposition, "Continue with Google", privacy note ("drafts only — nothing is sent without you").
- **Onboarding (stepper):** 1) account confirmed, 2) timezone + meeting preferences (working days/hours, no-meeting days, focus blocks, lunch, buffer, preferred times, default duration), 3) **auto-scheduling** (Off / Trusted senders — recommended / All non-suspicious; trusted domains; daily limit) with a plain explanation of each, 4) tone + signature + sign-off, 5) VIP senders (suggest frequent correspondents), 6) "Learning your writing style…" progress.
- **Inbox (three-pane):**
  - List: sender, subject, AI summary, category chip, priority dots, sentiment chip, time, attachment icon, calendar icon for invites/requests. Filters: category, priority ≥ N, sentiment, unread. Infinite scroll + skeletons.
  - Detail: sanitised email (sandboxed iframe) + attachments.
  - AI panel: summary, action items, sentiment with explanation, **scheduling card** (decision, reason, slot, "Auto-added ✓ · Undo" or "Waiting for approval"), buttons "Draft reply" / "Find a time". Suspicious emails show a warning card and disable automation.
- **Approvals:** grouped by email. Draft cards: variant tabs, inline editor, guardrail warnings, "Save to Gmail drafts" / "Reject". Event cards: slot tiles with reasons and conflict indicators, editable title/attendees. Invite cards: Accept / Tentative / Decline with the agent's recommendation highlighted and its reason. Optimistic updates + toasts.
- **Calendar:** today + week agenda; auto-added events marked with a small "AI" badge; pending proposals as dashed ghost events.
- **Chat:** full page + slide-over (`⌘J`); streamed step timeline; email citations as chips; suggested prompts on empty state.
- **Files:** attachment metadata table with debounced search.
- **Preferences:** sections for tone & signature, meeting preferences, **auto-scheduling**, VIPs, and "What I've learned" (notes with accept/reject/add/edit/delete).
- **Activity:** timeline with agent icon, action, tool, status badge (`auto` actions highlighted), expandable detail, Undo where still allowed.
- **Settings:** LLM provider (read-only), disconnect Google, delete all data (type "delete" to confirm).

### 15.4 Interaction details
- Shortcuts: `j/k` next/prev, `Enter` open, `d` draft, `t` find a time, `a` approvals, `⌘K` palette, `⌘J` chat, `?` help.
- Notifications for auto actions show the action, reason, and an Undo button (toast + bell).
- Empty states: icon + one helpful sentence + action. Skeletons while loading. Inline retry on errors.
- Collapsible dashboard strip at the top of Inbox: category counts, sentiment bar, pending approvals, auto actions today.

### 15.5 Frontend code rules
- Server components for layout/static parts; client components only where interactive.
- One typed API client (`lib/api/client.ts`, `openapi-fetch` over the generated `schema.d.ts`, §5.3) — never hand-write response types; zod only for form validation. TanStack Query for server state; keys in `lib/query-keys.ts`.
- `lib/sse.ts`: `fetch` + `ReadableStream` (POST bodies supported).
- `app/api/[...path]/route.ts` proxies all methods to `process.env.API_URL` **at runtime**, forwarding cookies and streaming bodies (SSE). No build-time API URL.
- Use shadcn components (Button, Card, Badge, Tabs, Dialog, Sheet, Command, Table, Skeleton, Tooltip, Form, Textarea, Select, Switch, RadioGroup, Separator, ScrollArea, Sonner) instead of custom primitives.

---

## 16. Security checklist

- [ ] OAuth tokens Fernet-encrypted at rest; key in Secret Manager.
- [ ] Session cookie httpOnly, Secure (prod), SameSite=Lax; CSRF double-submit header on state-changing routes.
- [ ] `/internal/*` verifies OIDC token, audience, and invoker service account.
- [ ] Email content delimited as data in every prompt; agents hold read-only tools only; Google write functions are called only by execution nodes after the approval/autonomy gate; no send function exists.
- [ ] Auto-scheduling limited by policy (trusted senders by default, daily cap, confidence threshold, no suspicious mail, availability re-checked) and always undoable.
- [ ] Email HTML sanitised (DOMPurify) in a sandboxed iframe; remote images blocked by default.
- [ ] Per-user rate limits on `/api/chat` and draft/schedule triggers (slowapi).
- [ ] No email bodies, tokens, or PII in logs or traces (LangSmith inputs/outputs hidden or tracing off in prod).
- [ ] Supabase RLS on all tables, no public policies, anon key unused.
- [ ] Service accounts: `api-runtime` (Secret Manager accessor) for the `api` service; `internal-invoker` (Cloud Run invoker on `api`) used by Pub/Sub and Scheduler.
- [ ] Disconnect revokes the token and stops the watch; delete removes all rows.

---

## 17. Testing & evaluation

- **Unit:**
  - `slots.py`: every hard rule has a test (working hours, working/no-meeting days, focus blocks, lunch, buffers, min notice, daily cap, VIP override, timezone + DST edges).
  - `autonomy.py`: each policy level × trusted/untrusted sender × confidence × daily cap × suspicious flag × action type.
  - `validate_decision`: rejects busy slots, drops invalid proposals, downgrades to approval.
  - Google wrappers: `create_draft` builds a correct RFC 2822 reply (threadId, `In-Reply-To`, `References`, base64url); `respond_to_invite` only changes the user's own attendee entry; `run_google` retries and maps auth errors.
  - Tool sets: no write function is reachable from `CHAT_TOOLS`, `SCHEDULING_TOOLS` or `DIGEST_TOOLS` (assert by name).
  - MIME parsing (incl. `text/calendar` invites), quote stripping, priority boost, routing, OIDC dependency, token crypto, provider factory (both providers).
- **Integration:** routes against Postgres + pgvector (Supabase local or a Postgres container in CI); Google API wrappers and LLM calls replaced with fakes (in-memory fake `gmail_api`/`calendar_api` modules, `langchain_core` fake chat models).
- **Graph tests:** fake models; assert routing, auto path vs approval path, interrupt creates an approval, resume executes, guardrail loop stops after one retry, stale slot triggers re-proposal.
- **Evals (`tests/evals/`):** 40+ golden emails with expected category, sentiment, needs_reply, meeting_intent; 15+ scheduling cases (invites and text requests) with expected action and expected auto-vs-approval outcome under a fixed preferences profile and fake calendar. Include 5 prompt-injection emails that must be flagged and never auto-scheduled. Thresholds: category ≥ 85%, sentiment ≥ 80%, scheduling action ≥ 80%, **zero** hard-rule violations. Runnable per provider: `uv run python -m tests.evals.run --provider groq`.
- **E2E (Playwright):** mocked sign-in, inbox renders, approve a draft, undo an auto-added event, chat streams.

---

## 18. Deployment

### 18.1 Containers (built by Cloud Build — no local Docker needed)
- `mail-assistant-server/Dockerfile`: multi-stage; builder installs uv and runs `uv sync --frozen --no-dev`; runtime `python:3.12-slim`, non-root user, `entrypoint.sh`.
- `mail-assistant-server/entrypoint.sh`: `alembic upgrade head` (Alembic `env.py` takes a Postgres advisory lock so concurrent instance starts are safe), then `exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers`.
- `mail-assistant-webapp/Dockerfile`: multi-stage `pnpm build` with `output: "standalone"`; runtime `node server.js` on `$PORT`; non-root.

### 18.2 Continuous deployment (Cloud Run ↔ GitHub)
- In the Cloud Run console, create each service with **"Continuously deploy from a repository"**:
  - `api` ← repo `mail-assistant-server`, branch `main`, build type **Dockerfile**, source location `/Dockerfile`.
  - `web` ← repo `mail-assistant-webapp`, branch `main`, build type **Dockerfile**, source location `/Dockerfile`.
- Install the Cloud Build GitHub app on both repos when connecting them.
- Set service settings from §3.3, service accounts from §16, env vars from §6 (secrets referenced from Secret Manager).
- Images land in Artifact Registry automatically.

### 18.3 `infra/setup.sh` (server repo; run once, before the first deploy)
Readable, commented `gcloud` commands:
1. Set project and region variables.
2. Enable APIs: `run`, `cloudbuild`, `artifactregistry`, `pubsub`, `cloudscheduler`, `secretmanager`, `gmail`, `calendar-json`.
3. Create service accounts `api-runtime` and `internal-invoker`; grant `api-runtime` `roles/secretmanager.secretAccessor`.
4. Create secrets (read values from stdin, never hard-coded): `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `GOOGLE_OAUTH_CLIENT_SECRET`, `DATABASE_URL`, `CHECKPOINT_DATABASE_URL`.
5. Create Pub/Sub topics `gmail-push` and `gmail-push-dlq`; grant `roles/pubsub.publisher` on `gmail-push` to `gmail-api-push@system.gserviceaccount.com`.

### 18.4 `infra/post-deploy.sh` (server repo; run once after `api` has a URL)
1. Grant `internal-invoker` `roles/run.invoker` on the `api` service.
2. Create push subscription on `gmail-push` → `${API_URL}/internal/gmail-push`, OIDC with `internal-invoker` (audience `API_URL`), ack deadline 600s, exponential retry, dead-letter `gmail-push-dlq` after 5 attempts. Grant the Pub/Sub service agent the roles the docs require for dead-lettering and OIDC token creation.
3. Create Scheduler jobs (HTTP, OIDC with `internal-invoker`):
   - `sync-sweep`: `*/10 * * * *` → `POST /internal/sync-sweep`
   - `daily-maintenance`: `30 2 * * *` (Asia/Kolkata) → `POST /internal/daily-maintenance`

### 18.5 `infra/README.md` (manual console steps)
- OAuth consent screen (External, Testing, add test users) and OAuth web client with redirect URIs for localhost and the `web` URL.
- Budget alert on the billing account.
- Branch protection on `main` in **both** repos requiring their CI workflow.
- In `mail-assistant-webapp`, add the `SERVER_REPO_TOKEN` secret (fine-grained, read-only contents on `mail-assistant-server`) if the server repo is private.

### 18.6 CI (pull requests only, one workflow per repo)
- **`mail-assistant-server`:** `uv sync`, `ruff check`, `ruff format --check`, `mypy`, `pytest` (Postgres + pgvector service container), **OpenAPI drift check** (`export_openapi` then `git diff --exit-code openapi.json`). Evals with real models run manually (`workflow_dispatch`) to save free-tier quota.
- **`mail-assistant-webapp`:** `pnpm install`, lint, type-check, build, **contract check** (checkout server repo → `pnpm gen:api` → `git diff --exit-code src/lib/api/schema.d.ts openapi.json`), Playwright e2e against a mocked API.

---

## 19. Local development

Clone both repos side by side so the default `OPENAPI_SOURCE` path works:

```
workspace/
├── mail-assistant-server/
└── mail-assistant-webapp/
```

### 19.1 Server
- `uv python install 3.12` (once; doesn't touch the system Python).
- Database: a Supabase dev project (or Supabase local via CLI).
- `uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload`
- After changing any route or schema: `uv run python -m scripts.export_openapi` and commit `openapi.json`.
- Gmail push can't reach localhost: use **Sync now** (`POST /api/sync`), which runs the exact same `sync_user` code as production.
- `uv run python -m scripts.seed_demo` seeds a demo user with sample emails, insights, approvals, and auto actions for UI work without Google.
- Use a separate Google Cloud project + OAuth client for development (separate test-user pool).

### 19.2 Webapp
- `pnpm install && pnpm gen:api && pnpm dev` (with `API_URL=http://localhost:8000`).
- Re-run `pnpm gen:api` whenever the server's `openapi.json` changes.
- For UI work without the server or Google, run the server with `seed_demo` data, or use the Playwright mock API (`pnpm dev:mock`, MSW handlers generated from the same schema types).

---

## 20. Build order

Build and verify each step before moving on; each step ends with passing tests. Server steps come first in each pair so the webapp always builds against a real, committed `openapi.json`.

### 20.1 `mail-assistant-server`
1. Scaffold (uv + Python 3.12), settings, logging, errors, health route, Dockerfile, `export_openapi.py`, CI with drift check.
2. Database models + Alembic migrations (pgvector, tsvector, advisory-lock `env.py`), Supabase connection.
3. Google OAuth, encrypted tokens, session cookie, `/api/me`.
4. Gmail API sync (`sync_user`, per-user lock, MIME parsing incl. invites, attachment metadata); `POST /api/sync`; email list/detail routes.
5. LLM provider factory + retry helper; Triage + Guardrail (input) agents; email graph up to labeling; evals harness.
6. Preferences (structured, meeting, autonomy) + notes routes; sent-mail embeddings; retrieval service.
7. Drafting agent, output guardrail, approval interrupt/resume; approvals routes; Gmail drafts.
8. `slots.py` + calendar tools; scheduling agent; `validate_decision`; autonomy gate; auto-execution, notifications, Undo; invite handling; agenda route.
9. Chat agent with SSE streaming (typed events in the schema); attachment search.
10. Internal routes with OIDC (`gmail-push`, `sync-sweep`, `daily-maintenance`); watch renewal; digest.
11. Memory agent; activity, dashboard, notification routes.
12. `infra/setup.sh`, `infra/post-deploy.sh`, `infra/README.md`; Cloud Run continuous deployment; security checklist; README.

### 20.2 `mail-assistant-webapp`
1. Scaffold (Next.js, Tailwind v4, shadcn, theme), Dockerfile, CI with contract check, `pnpm gen:api`, typed client, runtime proxy route.
2. Landing, sign-in, app shell, `/api/me`, reconnect banner (needs server step 3).
3. Inbox list/detail with Sync now (server step 4); insight chips and AI panel (server step 5).
4. Onboarding + Preferences pages (server step 6).
5. Approvals page with draft editor (server step 7).
6. Scheduling card, invite cards, Calendar page, notifications with Undo (server step 8).
7. Chat page + slide-over, Files page (server step 9).
8. Activity page, dashboard strip, notification bell, keyboard shortcuts, command palette (server step 11).
9. Playwright e2e, accessibility pass, Cloud Run continuous deployment, README.

---

## 21. Definition of done

- Each repo deploys its service automatically from its own `main` via the Cloud Run GitHub connection; `/api/health` is green.
- The webapp's generated types match the deployed server's `openapi.json` (both CI contract checks green).
- A test user can: sign in → onboard → receive an email → see it triaged within ~1 minute → approve a draft (appears in Gmail Drafts) → receive a calendar invite from a trusted sender that is auto-accepted with a notification and working Undo → receive a meeting request from an untrusted sender that lands in Approvals with 3 preference-respecting slots → ask the chat about past emails and get a cited answer.
- Switching `LLM_PROVIDER` between `google` and `groq` works without code changes; evals pass for both.
- Zero hard-rule violations in scheduling evals; all prompt-injection emails flagged and never auto-actioned.
- Security checklist complete; no secrets in the repo; logs contain no email content.
- Each repo's README covers its own setup and links to the other repo; the server README also covers env vars, architecture, the autonomy policy, and Google OAuth testing-mode limits (100 test users, 7-day refresh tokens, verification + CASA required for public production).
