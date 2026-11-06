#!/usr/bin/env bash
# One-time Google Cloud setup for the Mail Assistant. Run BEFORE the first deploy.
# Nothing here deploys code. Safe to re-run: existing resources are left as they are.
#
#   PROJECT_ID=my-project REGION=asia-south1 ./infra/setup.sh
#
# Secrets are read from your keyboard (never from files or arguments), so they do not
# end up in shell history.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=mail-assistant-prod}"
REGION="${REGION:-asia-south1}"   # Mumbai: same region as the Supabase project
RUNTIME_SA="api-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="internal-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

# --- 1. Enable the APIs we use --------------------------------------------------------
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  gmail.googleapis.com \
  calendar-json.googleapis.com

# --- 2. Service accounts ---------------------------------------------------------------
# api-runtime:      identity of the `api` Cloud Run service (reads secrets).
# internal-invoker: used by Pub/Sub and Cloud Scheduler to call /internal/* with OIDC.
create_service_account() {
  local name="$1" display="$2"
  if gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    echo "Service account ${name} already exists"
  else
    gcloud iam service-accounts create "$name" --display-name="$display"
  fi
}
create_service_account api-runtime "Mail Assistant API runtime"
create_service_account internal-invoker "Mail Assistant internal invoker (Pub/Sub, Scheduler)"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None >/dev/null

# --- 3. Secrets (values typed in, never stored in this repo) ---------------------------
SECRETS=(
  SESSION_SECRET            # python -c "import secrets;print(secrets.token_urlsafe(48))"
  TOKEN_ENCRYPTION_KEY      # python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
  GOOGLE_API_KEY            # Gemini + embeddings
  GROQ_API_KEY              # only needed if LLM_PROVIDER=groq (type anything otherwise)
  GOOGLE_OAUTH_CLIENT_SECRET
  DATABASE_URL              # Supabase transaction pooler, postgresql+asyncpg://...:6543/postgres
  CHECKPOINT_DATABASE_URL   # Supabase session pooler, postgresql://...:5432/postgres
)
for name in "${SECRETS[@]}"; do
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo "Secret ${name} already exists (add a new version in the console to change it)"
    continue
  fi
  read -r -s -p "Value for ${name}: " value
  echo
  printf '%s' "$value" | gcloud secrets create "$name" --replication-policy=automatic --data-file=-
  unset value
done

# --- 4. Pub/Sub topics for Gmail push --------------------------------------------------
for topic in gmail-push gmail-push-dlq; do
  if gcloud pubsub topics describe "$topic" >/dev/null 2>&1; then
    echo "Topic ${topic} already exists"
  else
    gcloud pubsub topics create "$topic"
  fi
done

# Gmail publishes new-mail notifications to the topic as this Google-owned account.
gcloud pubsub topics add-iam-policy-binding gmail-push \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" >/dev/null

echo
echo "Done. Next: create the Cloud Run services from GitHub (see infra/README.md),"
echo "then run infra/post-deploy.sh once the api service has a URL."
echo "Set on the api service: PUBSUB_TOPIC=projects/${PROJECT_ID}/topics/gmail-push"
echo "                        INTERNAL_INVOKER_SA=${INVOKER_SA}"
