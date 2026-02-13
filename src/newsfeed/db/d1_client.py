"""Lightweight Cloudflare D1 REST client — zero external dependencies.

Uses urllib.request (stdlib) to talk to the D1 HTTP API.
Drop-in replacement transport for AnalyticsDB when D1 credentials are present.

Env vars:
    CLOUDFLARE_ACCOUNT_ID  — Cloudflare account ID
    CLOUDFLARE_API_TOKEN   — API token with D1 read/write permissions
    D1_DATABASE_ID         — D1 database UUID
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = (1.0, 2.0, 4.0)


class D1Client:
    """Minimal Cloudflare D1 REST API client.

    Provides execute/query methods that mirror sqlite3.Connection semantics
    so AnalyticsDB can swap backends transparently.
    """

    def __init__(self, account_id: str, database_id: str, api_token: str) -> None:
        account_id = account_id.strip()
        database_id = database_id.strip()
        api_token = api_token.strip()
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.account_id = account_id
        self.database_id = database_id

    def _post(self, body: dict | list) -> list[dict]:
        """Send a query to D1 and return the result array.

        Retries on transient 403/429/5xx errors with exponential backoff.
        """
        data = json.dumps(body).encode("utf-8")

        for attempt in range(_MAX_RETRIES + 1):
            req = urllib.request.Request(
                self._url, data=data, headers=self._headers, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace")[:500]
                # Retry on transient errors (403 rate-limit, 429, 5xx)
                if e.code in (403, 429, 500, 502, 503) and attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[attempt]
                    log.warning("D1 API %d (attempt %d/%d), retrying in %.1fs: %s",
                                e.code, attempt + 1, _MAX_RETRIES + 1, wait, body_text[:120])
                    time.sleep(wait)
                    continue
                log.error("D1 API error %d: %s", e.code, body_text)
                raise
            except urllib.error.URLError as e:
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[attempt]
                    log.warning("D1 network error (attempt %d/%d), retrying in %.1fs: %s",
                                attempt + 1, _MAX_RETRIES + 1, wait, e.reason)
                    time.sleep(wait)
                    continue
                log.error("D1 network error: %s", e.reason)
                raise

            if not result.get("success"):
                errors = result.get("errors", [])
                log.error("D1 query failed: %s", errors)
                raise RuntimeError(f"D1 query failed: {errors}")

            return result.get("result", [])

        return []  # unreachable, but satisfies type checker

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a single SQL statement. Returns list of row dicts."""
        body = {"sql": sql}
        if params:
            body["params"] = [_convert_param(p) for p in params]

        results = self._post(body)
        if results and isinstance(results, list):
            return results[0].get("results", [])
        return []

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        """Execute a parameterized statement for each set of params.

        The D1 REST API only accepts single statements per request,
        so we iterate and send each individually.
        """
        if not params_list:
            return

        for params in params_list:
            self.execute(sql, params)

    def execute_script(self, script: str) -> None:
        """Execute a multi-statement SQL script (schema init).

        Splits on semicolons and sends each statement individually,
        since the D1 REST API only accepts single statements per request.
        Continues on failure so partial schema creation is possible
        (all DDL uses IF NOT EXISTS, so re-running is safe).
        """
        for raw in script.split(";"):
            # Strip comment-only lines first, then check if anything remains
            lines = [l for l in raw.split("\n") if not l.strip().startswith("--")]
            clean = "\n".join(lines).strip()
            if clean:
                try:
                    self.execute(clean)
                except Exception:
                    log.warning("D1 script statement failed (will retry on next init): %s",
                                clean.replace("\n", " ")[:100])

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read query and return list of row dicts."""
        return self.execute(sql, params)

    def ping(self) -> bool:
        """Test connectivity."""
        try:
            self.execute("SELECT 1 as ok")
            return True
        except Exception:
            return False


def _convert_param(value: Any) -> Any:
    """Convert Python values to D1-compatible JSON types."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str)):
        return value
    # Fallback: stringify
    return str(value)
