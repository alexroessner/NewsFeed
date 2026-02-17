#!/bin/bash
# Deploy NewsFeed to Google Cloud Run
# Usage: ./deploy.sh <project-id> <region>

set -euo pipefail

PROJECT_ID="${1:?Usage: ./deploy.sh <project-id> <region>}"
REGION="${2:-us-central1}"
SERVICE_NAME="newsfeed-bot"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "Building and deploying NewsFeed to Cloud Run..."

# Build the image
gcloud builds submit \
    --tag "${IMAGE}" \
    --project "${PROJECT_ID}" \
    ../../

# Deploy to Cloud Run
gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --memory 512Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 3 \
    --timeout 300 \
    --set-env-vars "NEWSFEED_LOG_JSON=1,NEWSFEED_ENV=production" \
    --no-allow-unauthenticated

echo ""
echo "Deployed! Set secrets with:"
echo "  gcloud run services update ${SERVICE_NAME} --region ${REGION} --set-env-vars TELEGRAM_BOT_TOKEN=xxx"
echo ""
echo "View logs with:"
echo "  gcloud run services logs read ${SERVICE_NAME} --region ${REGION}"
