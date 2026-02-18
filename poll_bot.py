"""Simple polling mode for the bot — processes commands locally.

Use this when the Cloudflare Worker webhook is down.
Ctrl+C to stop.
"""
import json
import os
import sys
import time
import urllib.request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN environment variable required", file=sys.stderr)
    sys.exit(1)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT = 30  # seconds (long polling)


def get_updates(offset: int | None = None) -> list[dict]:
    params: dict = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        f"{API}/getUpdates", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as resp:
            result = json.loads(resp.read())
            return result.get("result", [])
    except Exception as e:
        print(f"[poll] getUpdates error: {e}", file=sys.stderr)
        return []


def process_update(update: dict) -> None:
    os.environ["TELEGRAM_UPDATE"] = json.dumps(update)
    try:
        from newsfeed.run_command import main
        main()
    except SystemExit:
        pass  # run_command calls sys.exit
    except Exception as e:
        print(f"[poll] Error processing update {update.get('update_id')}: {e}",
              file=sys.stderr)


def main() -> None:
    print("[poll] Bot polling started. Ctrl+C to stop.")
    offset = None
    while True:
        updates = get_updates(offset)
        for u in updates:
            uid = u.get("update_id", 0)
            msg = u.get("message", {})
            text = msg.get("text", "")
            user = msg.get("from", {}).get("username", "?")
            print(f"[poll] Processing update {uid} from @{user}: {text[:50]}")
            process_update(u)
            offset = uid + 1
        if not updates:
            time.sleep(1)  # Brief pause before next long poll


if __name__ == "__main__":
    # Ensure src is on path
    src = os.path.join(os.path.dirname(__file__), "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    main()
