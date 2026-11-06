# Deploying the Mail Assistant

Two Cloud Run services, each deployed automatically from its own GitHub repo:

| Service | Repo | Access |
|---|---|---|
| `api` | `mail-assistant-server` | public URL; `/internal/*` needs a Google OIDC token |
| `web` | `mail-assistant-webapp` | public |

Order: **1 → 2 → 3 → 4 → 5**. Use region `asia-south1` (Mumbai), the same as Supabase.

## 1. OAuth consent screen and client (console)

1. **Google Auth Platform → Branding**: app name, support email.
2. **Audience**: External, **Testing**. Add every tester under *Test users* (max 100).
3. **Data access**: `openid`, `userinfo.email`, `userinfo.profile`, `gmail.modify`,
   `calendar.events`, `calendar.readonly`.
4. **Clients → Web application**:
   - JavaScript origins: `http://localhost:3000`, `https://<web-url>`
   - Redirect URIs: `http://localhost:3000/api/auth/google/callback`,
     `https://<web-url>/api/auth/google/callback`

Testing-mode limits: 100 test users, and **refresh tokens expire after 7 days** (users see
"Reconnect Google"). Public production needs Google verification and a CASA security
assessment for the Gmail scope.

## 2. One-time project setup

```bash
PROJECT_ID=<project> REGION=asia-south1 ./infra/setup.sh
```
Enables APIs, creates the `api-runtime` and `internal-invoker` service accounts, creates the
secrets (typed in, never stored), and the `gmail-push` / `gmail-push-dlq` topics.

## 3. Cloud Run services from GitHub (console)

Cloud Run → **Create service** → **Continuously deploy from a repository** (install the
Cloud Build GitHub app on both repos when asked):

| Setting | `api` | `web` |
|---|---|---|
| Repository / branch | `mail-assistant-server` / `main` | `mail-assistant-webapp` / `main` |
| Build type | Dockerfile, `/Dockerfile` | Dockerfile, `/Dockerfile` |
| Authentication | Allow unauthenticated | Allow unauthenticated |
| Service account | `api-runtime@<project>.iam.gserviceaccount.com` | default |
| Concurrency / timeout | 20 / 600 s | 80 / 300 s |
| Min / max instances | 0 / 3 | 0 / default |

**`api` environment** — plain variables:

```
APP_ENV=production
APP_BASE_URL=https://<web-url>
CORS_ORIGINS=https://<web-url>
LLM_PROVIDER=google
LLM_FAST_MODEL=gemini-3.1-flash-lite
LLM_SMART_MODEL=gemini-3.1-flash-lite
EMBEDDING_MODEL=gemini-embedding-001
GOOGLE_OAUTH_CLIENT_ID=<client id>
GCP_PROJECT_ID=<project>
PUBSUB_TOPIC=projects/<project>/topics/gmail-push
INTERNAL_INVOKER_SA=internal-invoker@<project>.iam.gserviceaccount.com
INTERNAL_AUDIENCE=https://<api-url>        # known after the first deploy
LANGSMITH_TRACING=false
```
…and **secrets** (Variables & Secrets → Reference a secret → exposed as env var):
`SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`,
`GOOGLE_OAUTH_CLIENT_SECRET`, `DATABASE_URL`, `CHECKPOINT_DATABASE_URL` (version `latest`).

In production the api refuses to start if any required secret is missing. Migrations run
automatically at startup (`entrypoint.sh`, guarded by a Postgres advisory lock).

**`web` environment**: `API_URL=https://<api-url>`.

## 4. Push subscription and scheduler jobs

```bash
PROJECT_ID=<project> REGION=asia-south1 ./infra/post-deploy.sh
```
Creates `gmail-push-sub` (push → `/internal/gmail-push`, OIDC, ack 600 s, dead-letter after
5 attempts) and the scheduler jobs `sync-sweep` (every 10 min) and `daily-maintenance`
(02:30 Asia/Kolkata).

## 5. Guard rails (console / GitHub)

- **Budget alert** on the billing account (e.g. ₹500/month, alerts at 50/90/100%).
- **Branch protection** on `main` in both repos: require the `CI` workflow to pass.
- In `mail-assistant-webapp`, add the `SERVER_REPO_TOKEN` secret (fine-grained token,
  read-only *Contents* on `mail-assistant-server`) if the server repo is private.
- Check: `curl https://<api-url>/api/health` → `{"status":"ok"}`, then
  `gcloud scheduler jobs run sync-sweep --location=asia-south1` and watch the logs.
