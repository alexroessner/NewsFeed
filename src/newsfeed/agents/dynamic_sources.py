"""Dynamic source management — discover, validate, and register custom RSS feeds.

Users can add their own RSS sources via ``/source add <url>``.  This module
handles the full lifecycle:

1. **Discovery** — given a website URL, find the RSS/Atom feed
   (checks ``<link rel="alternate">``, common paths, or direct feed URL).
2. **Validation** — ensures the URL is safe (HTTPS, no private IPs) and
   returns parseable RSS/Atom XML.
3. **Agent creation** — wraps the feed as a ``GenericRSSAgent`` with low
   initial trust (tier 3) so it can participate in the pipeline.

Safety constraints:
- HTTPS only
- Private/reserved/metadata IP blocking (reuses webhook IP validation)
- Feed must parse as valid RSS 2.0 or Atom
- Per-user cap: 10 custom sources
- Source names: alphanumeric + hyphens, 1-30 chars
- Feed URLs: max 512 chars
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from newsfeed.agents._xml_safe import ParseError, safe_fromstring
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from newsfeed.agents.rss_generic import GenericRSSAgent
from newsfeed.delivery.webhook import _check_hostname_ip

log = logging.getLogger(__name__)

# Limits
MAX_CUSTOM_SOURCES_PER_USER = 10
_MAX_URL_LEN = 512
_MAX_NAME_LEN = 30
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,29}$")
_MAX_PROBE_BYTES = 256 * 1024  # 256 KB — just enough to check if it's valid XML
_PROBE_TIMEOUT = 8  # seconds

# Common RSS/Atom feed paths to try during auto-discovery
_COMMON_FEED_PATHS = [
    "/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/feeds/posts/default", "/index.xml", "/blog/feed",
    "/feed/rss", "/feed/atom", "/?feed=rss2",
]

# Evidence baseline for custom sources (low trust — tier 3)
_CUSTOM_EVIDENCE_BASELINE = 0.40
_CUSTOM_PREDICTION_BASELINE = 0.30


@dataclass
class FeedProbeResult:
    """Result of probing a URL for a valid RSS/Atom feed."""
    valid: bool
    feed_url: str = ""
    feed_title: str = ""
    item_count: int = 0
    error: str = ""


def validate_source_name(name: str) -> tuple[bool, str]:
    """Validate a custom source name."""
    if not name:
        return False, "Source name is required."
    if len(name) > _MAX_NAME_LEN:
        return False, f"Source name too long (max {_MAX_NAME_LEN} characters)."
    if not _NAME_RE.match(name):
        return False, "Source name must be alphanumeric (hyphens and underscores allowed)."
    return True, ""


def validate_feed_url(url: str) -> tuple[bool, str]:
    """Validate a feed URL is safe to fetch.

    Reuses the webhook IP validation to block private/reserved/metadata IPs.
    """
    if not url:
        return False, "URL is required."
    if len(url) > _MAX_URL_LEN:
        return False, f"URL too long (max {_MAX_URL_LEN} characters)."
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format."
    if parsed.scheme not in ("https",):
        return False, "Only HTTPS URLs are allowed."
    if not parsed.hostname:
        return False, "URL must include a hostname."
    hostname = parsed.hostname.lower()
    if hostname in ("localhost",):
        return False, "Localhost URLs are not allowed."
    # Reuse webhook IP hardening
    blocked, reason = _check_hostname_ip(hostname)
    if blocked:
        return False, reason
    return True, ""


def _probe_feed(url: str) -> FeedProbeResult:
    """Fetch a URL and check if it contains valid RSS 2.0 or Atom XML.

    Returns a FeedProbeResult with validity, title, and item count.
    """
    try:
        req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
        with urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            data = resp.read(_MAX_PROBE_BYTES + 1)
        if len(data) > _MAX_PROBE_BYTES:
            return FeedProbeResult(valid=False, error="Response too large for probe.")
    except (URLError, OSError) as e:
        return FeedProbeResult(valid=False, error=f"Fetch failed: {e}")

    try:
        root = safe_fromstring(data)
    except ParseError:
        return FeedProbeResult(valid=False, error="Not valid XML.")

    # Check for RSS 2.0 (<rss> with <channel>/<item>)
    if root.tag == "rss" or root.tag.endswith("}rss"):
        channel = root.find("channel")
        if channel is not None:
            title_el = channel.find("title")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            items = list(channel.iter("item"))
            return FeedProbeResult(
                valid=True, feed_url=url,
                feed_title=title, item_count=len(items),
            )

    # Check for Atom (<feed> with <entry>)
    # Atom uses namespace: http://www.w3.org/2005/Atom
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if root.tag == "feed" or root.tag == f"{atom_ns}feed":
        title_el = root.find(f"{atom_ns}title") or root.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        entries = list(root.iter(f"{atom_ns}entry")) or list(root.iter("entry"))
        return FeedProbeResult(
            valid=True, feed_url=url,
            feed_title=title, item_count=len(entries),
        )

    # Check for RDF/RSS 1.0
    if "rdf" in root.tag.lower() or "rss" in root.tag.lower():
        items = list(root.iter("item"))
        if items:
            return FeedProbeResult(
                valid=True, feed_url=url,
                feed_title="", item_count=len(items),
            )

    return FeedProbeResult(valid=False, error="Not a recognized RSS/Atom feed.")


def _discover_feed_from_html(url: str) -> str | None:
    """Try to find an RSS/Atom feed link in a webpage's HTML.

    Looks for <link rel="alternate" type="application/rss+xml" href="...">
    or the Atom equivalent.
    """
    try:
        req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
        with urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "xml" in content_type or "rss" in content_type or "atom" in content_type:
                # The URL itself might be a feed
                return url
            data = resp.read(_MAX_PROBE_BYTES)
    except (URLError, OSError):
        return None

    text = data.decode("utf-8", errors="replace")

    # Look for <link rel="alternate" type="application/rss+xml" href="...">
    link_pattern = re.compile(
        r'<link[^>]*\brel=["\']alternate["\'][^>]*'
        r'\btype=["\']application/(rss\+xml|atom\+xml)["\'][^>]*'
        r'\bhref=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    match = link_pattern.search(text)
    if match:
        href = html_mod.unescape(match.group(2))
        return urljoin(url, href)

    # Also check reversed attribute order (type before rel)
    link_pattern_rev = re.compile(
        r'<link[^>]*\btype=["\']application/(rss\+xml|atom\+xml)["\'][^>]*'
        r'\bhref=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    match = link_pattern_rev.search(text)
    if match:
        href = html_mod.unescape(match.group(2))
        return urljoin(url, href)

    return None


def discover_feed(url: str) -> FeedProbeResult:
    """Discover and validate an RSS/Atom feed from a URL.

    Tries in order:
    1. Direct probe (is the URL itself a feed?)
    2. HTML link discovery (parse <link rel="alternate"> from HTML)
    3. Common feed paths (/rss, /feed, /atom.xml, etc.)

    Returns a FeedProbeResult. If valid, feed_url contains the confirmed feed URL.
    """
    # Validate URL safety first
    valid, error = validate_feed_url(url)
    if not valid:
        return FeedProbeResult(valid=False, error=error)

    # 1. Try the URL directly
    result = _probe_feed(url)
    if result.valid:
        return result

    # 2. Try HTML link discovery
    discovered = _discover_feed_from_html(url)
    if discovered and discovered != url:
        # Validate the discovered URL too
        d_valid, d_error = validate_feed_url(discovered)
        if d_valid:
            result = _probe_feed(discovered)
            if result.valid:
                return result

    # 3. Try common feed paths
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port and parsed.port not in (80, 443):
        base = f"{base}:{parsed.port}"

    for path in _COMMON_FEED_PATHS:
        candidate = base + path
        if candidate == url:
            continue
        c_valid, _ = validate_feed_url(candidate)
        if not c_valid:
            continue
        result = _probe_feed(candidate)
        if result.valid:
            return result

    return FeedProbeResult(
        valid=False,
        error="Could not find an RSS/Atom feed at this URL. "
              "Try providing the direct feed URL (e.g. https://example.com/rss).",
    )


def create_custom_agent(
    name: str,
    feed_url: str,
    user_id: str,
    topics: list[str] | None = None,
) -> GenericRSSAgent:
    """Create a GenericRSSAgent for a user's custom source.

    Custom sources get low initial trust (evidence_baseline=0.40) and are
    tagged with the user's ID in the agent_id for isolation.
    """
    agent_id = f"custom_{name}_{user_id[:8]}"
    topic = topics[0] if topics else "general"

    return GenericRSSAgent(
        agent_id=agent_id,
        source=f"custom:{name}",
        mandate=f"User-added custom RSS source: {name}",
        feeds={"main": feed_url},
        topic_map={"main": topic},
        evidence_baseline=_CUSTOM_EVIDENCE_BASELINE,
        prediction_baseline=_CUSTOM_PREDICTION_BASELINE,
        max_feeds=1,
    )
