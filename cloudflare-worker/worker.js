/**
 * Cloudflare Worker — Telegram Webhook → GitHub Actions bridge
 *
 * Receives Telegram webhook POSTs, validates the secret token,
 * and triggers a GitHub Actions repository_dispatch event to process
 * the command. Response goes back to the user from the GH Actions run.
 *
 * Environment variables (set in Cloudflare dashboard):
 *   WEBHOOK_SECRET  — shared secret set when registering the Telegram webhook
 *   GITHUB_TOKEN    — GitHub personal access token (repo scope)
 *   GITHUB_REPO     — owner/repo, e.g. "alexroessner/NewsFeed"
 *
 * Deploy:
 *   npx wrangler deploy
 */

export default {
  async fetch(request, env) {
    // Only accept POST from Telegram
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    // Validate Telegram's secret token header (set via setWebhook secret_token param)
    // SECURITY: reject if WEBHOOK_SECRET is not configured OR if it doesn't match
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.WEBHOOK_SECRET || secret !== env.WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad JSON", { status: 400 });
    }

    // Validate Telegram update structure — must have update_id and at least
    // one recognized field (message, callback_query, etc.) to prevent
    // arbitrary payload injection into GitHub Actions dispatches.
    if (
      typeof update !== "object" ||
      update === null ||
      typeof update.update_id !== "number" ||
      (!update.message && !update.callback_query && !update.edited_message)
    ) {
      return new Response("Invalid Telegram update", { status: 422 });
    }

    // Fire repository_dispatch to GitHub Actions
    const resp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `token ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github.v3+json",
          "User-Agent": "NewsFeed-Webhook-Worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          event_type: "telegram_update",
          client_payload: { update },
        }),
      }
    );

    if (!resp.ok) {
      console.error(`GitHub dispatch failed: ${resp.status} ${await resp.text()}`);
      // Still return 200 to Telegram so it doesn't retry
      return new Response("OK", { status: 200 });
    }

    // Return 200 immediately — Telegram expects a fast response
    return new Response("OK", { status: 200 });
  },
};
