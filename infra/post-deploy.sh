#!/usr/bin/env bash
# Run ONCE after the `api` Cloud Run service exists and has a URL.
# Creates the Gmail push subscription and the two Cloud Scheduler jobs, all authenticated
# with Google-signed OIDC tokens for the internal-invoker service account.
#
#   PROJECT_ID=my-project REGION=asia-south1 ./infra/post-deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-api}"
INVOKER_SA="internal-invoker@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
API_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
echo "api URL: ${API_URL}"
echo "Set INTERNAL_AUDIENCE=${API_URL} on the api service if you have not already."

# --- 1. Let the internal invoker call the api service ----------------------------------
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:${INVOKER_SA}" --role="roles/run.invoker" >/dev/null

# --- 2. Pub/Sub permissions: mint OIDC tokens, dead-letter, acknowledge ----------------
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/iam.serviceAccountTokenCreator" >/dev/null
gcloud pubsub topics add-iam-policy-binding gmail-push-dlq \
  --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.publisher" >/dev/null

# --- 3. Push subscription: Gmail notification -> POST /internal/gmail-push --------------
# ack deadline 600s = the api request timeout; after 5 failed deliveries -> gmail-push-dlq.
if gcloud pubsub subscriptions describe gmail-push-sub >/dev/null 2>&1; then
  echo "Subscription gmail-push-sub already exists"
else
  gcloud pubsub subscriptions create gmail-push-sub \
    --topic=gmail-push \
    --push-endpoint="${API_URL}/internal/gmail-push" \
    --push-auth-service-account="$INVOKER_SA" \
    --push-auth-token-audience="$API_URL" \
    --ack-deadline=600 \
    --min-retry-delay=10s \
    --max-retry-delay=600s \
    --dead-letter-topic=gmail-push-dlq \
    --max-delivery-attempts=5
fi
gcloud pubsub subscriptions add-iam-policy-binding gmail-push-sub \
  --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.subscriber" >/dev/null

# Keep dead letters somewhere you can inspect them.
if ! gcloud pubsub subscriptions describe gmail-push-dlq-sub >/dev/null 2>&1; then
  gcloud pubsub subscriptions create gmail-push-dlq-sub --topic=gmail-push-dlq
fi

# --- 4. Cloud Scheduler jobs -----------------------------------------------------------
create_job() {
  local name="$1" schedule="$2" path="$3"
  if gcloud scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1; then
    echo "Scheduler job ${name} already exists"
    return
  fi
  gcloud scheduler jobs create http "$name" \
    --location="$REGION" \
    --schedule="$schedule" \
    --time-zone="Asia/Kolkata" \
    --uri="${API_URL}${path}" \
    --http-method=POST \
    --oidc-service-account-email="$INVOKER_SA" \
    --oidc-token-audience="$API_URL" \
    --attempt-deadline=600s
}
create_job sync-sweep "*/10 * * * *" /internal/sync-sweep              # catch-up sync + digests
create_job daily-maintenance "30 2 * * *" /internal/daily-maintenance  # watches, expiry, retention

echo
echo "Done. Test with: gcloud scheduler jobs run sync-sweep --location=${REGION}"
