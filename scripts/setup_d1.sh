#!/usr/bin/env bash
# ============================================================
# One-time setup: Create Cloudflare D1 database for NewsFeed analytics
#
# Prerequisites:
#   - Node.js installed
#   - npx available
#   - Logged into Cloudflare: npx wrangler login
#
# After running this script, add these GitHub secrets:
#   CLOUDFLARE_ACCOUNT_ID  → Your Cloudflare account ID (shown below)
#   CLOUDFLARE_API_TOKEN   → Create at https://dash.cloudflare.com/profile/api-tokens
#                            (needs D1 read+write permissions)
#   D1_DATABASE_ID         → The database UUID output by this script
# ============================================================

set -euo pipefail

DB_NAME="newsfeed-analytics"

echo "=== Creating Cloudflare D1 database: $DB_NAME ==="
echo ""

# Create the D1 database
npx wrangler d1 create "$DB_NAME" 2>&1 | tee /tmp/d1_create_output.txt

echo ""
echo "=== Database created! ==="
echo ""
echo "Copy the database_id from the output above and add these GitHub secrets:"
echo ""
echo "  CLOUDFLARE_ACCOUNT_ID  → Your account ID from: npx wrangler whoami"
echo "  CLOUDFLARE_API_TOKEN   → Create at: https://dash.cloudflare.com/profile/api-tokens"
echo "                           Required permissions: D1 Edit"
echo "  D1_DATABASE_ID         → The UUID from the output above"
echo ""
echo "The schema will be auto-created on first use. No manual SQL needed."
