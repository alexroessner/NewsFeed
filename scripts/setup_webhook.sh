#!/usr/bin/env bash
#
# Register Telegram webhook to point at your Cloudflare Worker.
#
# Usage:
#   ./scripts/setup_webhook.sh <WORKER_URL> <BOT_TOKEN> [WEBHOOK_SECRET]
#
# Example:
#   ./scripts/setup_webhook.sh https://newsfeed-telegram-webhook.YOUR_SUBDOMAIN.workers.dev \
#       8321195344:AAGVPmP2yzFHuM1EFXzYR1MlurTm7SToGm8 \
#       my-secret-token-123

set -euo pipefail

WORKER_URL="${1:?Usage: $0 <WORKER_URL> <BOT_TOKEN> [WEBHOOK_SECRET]}"
BOT_TOKEN="${2:?Usage: $0 <WORKER_URL> <BOT_TOKEN> [WEBHOOK_SECRET]}"
WEBHOOK_SECRET="${3:-}"

echo "Setting Telegram webhook..."
echo "  URL: ${WORKER_URL}"
echo "  Bot: ${BOT_TOKEN:0:10}..."

PARAMS="url=${WORKER_URL}&allowed_updates=[\"message\",\"callback_query\"]"
if [ -n "${WEBHOOK_SECRET}" ]; then
    PARAMS="${PARAMS}&secret_token=${WEBHOOK_SECRET}"
    echo "  Secret: (set)"
fi

RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook?${PARAMS}")
echo ""
echo "Response: ${RESPONSE}"

# Verify
echo ""
echo "Verifying..."
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
