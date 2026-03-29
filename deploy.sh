#!/bin/bash
# deploy.sh — Deploy Saasy to Google Cloud Run
# Usage: ./deploy.sh [project-id] [region]
#
# Key flags that enable true autonomous operation:
#   --cpu-always-allocated   CPU is never throttled between requests; background
#                            threads (build, reels, email pipelines) keep running
#                            even when no user is connected.
#   --min-instances 1        Instance is never shut down; no cold starts and no
#                            risk of losing in-flight jobs to scale-to-zero.
#   --max-instances 1        Single instance keeps _active_builds in-memory state
#                            consistent. Upgrade to Redis/Firestore job registry
#                            before removing this if you need horizontal scale.
#   --timeout 3600           WebSocket connections (build live-stream) can stay
#                            open for up to 1 hour.

set -e

PROJECT="${1:-$(gcloud config get-value project)}"
REGION="${2:-us-central1}"
SERVICE="saasy"
IMAGE="gcr.io/$PROJECT/$SERVICE"

echo "Deploying $SERVICE to $PROJECT / $REGION..."

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 1 \
  --cpu 2 \
  --memory 2Gi \
  --timeout 3600 \
  --concurrency 80 \
  --cpu-always-allocated \
  --port 8080

echo ""
echo "Done. Service URL:"
gcloud run services describe "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --format "value(status.url)"
