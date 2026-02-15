"""Article enrichment — fetch full articles and generate real summaries.

RSS feeds only provide 100-200 char teasers. This module reads the actual
articles and produces summaries substantial enough that users don't need
to click through.

Pipeline position: runs AFTER expert council selection, so only the final
10 selected stories get enriched (not all 45+ candidates).

Two summarization modes:
- Extractive (always available): pulls the most information-dense paragraphs
- LLM-backed (when API key configured): generates a proper narrative summary
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

from newsfeed.models.domain import CandidateItem

log = logging.getLogger(__name__)

# ── HTML text extraction ──────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NAV_RE = re.compile(
    r"<(nav|header|footer|aside|form|menu|iframe|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\n{3,}")
_SENTENCE_END_RE = re.compile(r"[.!?]\s")

# Common boilerplate patterns to strip
_BOILERPLATE = re.compile(
    r"(cookie|subscribe|sign up|newsletter|advertisement|read more|"
    r"share this|follow us|related articles|recommended|most popular|"
    r"copyright \d{4}|all rights reserved|terms of service|privacy policy|"
    r"hide caption|toggle caption|enlarge this image|image source|"
    r"getty images|reuters/|ap photo|screenshot by|"
    r"click here|tap here|swipe|download the app|"
    r"more on this story|you may also like|sponsored content)",
    re.IGNORECASE,
)


def extract_article_text(html_content: str) -> str:
    """Extract clean article text from raw HTML.

    Uses a lightweight readability-style approach:
    1. Remove scripts, styles, nav elements, comments
    2. Extract text from <p> and <article> tags (main content)
    3. Fall back to full text extraction if <p> tags yield too little
    4. Filter out boilerplate paragraphs
    """
    # Remove noise
    text = _SCRIPT_RE.sub("", html_content)
    text = _STYLE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    text = _NAV_RE.sub("", text)

    # Try to extract from <article> first (most reliable for news sites)
    article_match = re.search(
        r"<article[^>]*>(.*?)</article>", text, re.DOTALL | re.IGNORECASE
    )
    if article_match:
        text = article_match.group(1)

    # Extract <p> tag content — the core article paragraphs
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", text, re.DOTALL | re.IGNORECASE)

    if paragraphs:
        cleaned = []
        for p in paragraphs:
            p_text = _TAG_RE.sub("", p).strip()
            p_text = _decode_entities(p_text)
            # Skip short fragments and boilerplate
            if len(p_text) < 40:
                continue
            if _BOILERPLATE.search(p_text):
                continue
            cleaned.append(p_text)
        if cleaned:
            return "\n\n".join(cleaned)

    # Fallback: strip all tags and return raw text
    raw = _TAG_RE.sub(" ", text)
    raw = _decode_entities(raw)
    raw = _WHITESPACE_RE.sub("\n\n", raw).strip()
    # Take the middle portion (skip header/footer noise)
    lines = [l.strip() for l in raw.split("\n") if len(l.strip()) > 40]
    return "\n\n".join(lines[:30])


def _decode_entities(text: str) -> str:
    """Decode HTML entities."""
    import html
    text = html.unescape(text)
    # Clean up common unicode artifacts
    text = text.replace("\xa0", " ")
    text = text.replace("\u200b", "")
    text = re.sub(r"  +", " ", text)
    return text.strip()


# ── Article fetching ──────────────────────────────────────────────

_SAFE_FETCH_SCHEMES = frozenset({"http", "https"})

# Per-domain rate limiting: avoid hammering the same news site when multiple
# articles are fetched concurrently.  Minimum 0.5s between requests to any
# single domain.  Thread-safe via lock.
_domain_lock = threading.Lock()
_domain_last_access: dict[str, float] = {}
_DOMAIN_MIN_INTERVAL = 0.5  # seconds
_DOMAIN_CACHE_MAX = 500  # prevent unbounded growth


def _throttle_domain(url: str) -> None:
    """Sleep if needed to enforce per-domain minimum interval."""
    hostname = urlparse(url).hostname or ""
    if not hostname:
        return
    with _domain_lock:
        # Evict stale entries if cache grows too large
        if len(_domain_last_access) > _DOMAIN_CACHE_MAX:
            _domain_last_access.clear()
        last = _domain_last_access.get(hostname, 0.0)
        now = time.monotonic()
        wait = _DOMAIN_MIN_INTERVAL - (now - last)
        _domain_last_access[hostname] = max(now, last + _DOMAIN_MIN_INTERVAL)
    if wait > 0:
        time.sleep(wait)


def fetch_article(url: str, timeout: int = 8) -> str:
    """Fetch article HTML from a URL. Returns empty string on failure."""
    if not url or url.startswith("https://example.com"):
        return ""
    # Only allow http/https — block file://, ftp://, data://, gopher:// etc.
    scheme = url.split(":", 1)[0].lower().strip() if ":" in url else ""
    if scheme not in _SAFE_FETCH_SCHEMES:
        log.debug("Blocked fetch for non-http scheme: %s", scheme)
        return ""
    _throttle_domain(url)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; NewsFeed/1.0)",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type.lower() and "text" not in content_type.lower():
                return ""
            raw = resp.read()
            # Try UTF-8 first, fall back to latin-1
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        log.debug("Article fetch failed for %s: %s", url[:80], e)
        return ""


# ── Extractive summarization ──────────────────────────────────────

def extractive_summary(article_text: str, target_chars: int = 500) -> str:
    """Generate a summary by selecting the most information-dense paragraphs.

    Strategy:
    - News articles follow the inverted pyramid: most important info first
    - Take the opening paragraphs up to target length
    - Prefer paragraphs with named entities (capitalized words), numbers, quotes
    """
    if not article_text:
        return ""

    paragraphs = [p.strip() for p in article_text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""

    # Score each paragraph for information density
    scored: list[tuple[float, int, str]] = []
    for i, para in enumerate(paragraphs):
        if len(para) < 30:
            continue
        score = _paragraph_score(para, i, len(paragraphs))
        scored.append((score, i, para))

    # Sort by score but preserve some ordering (top paragraphs get position boost)
    scored.sort(key=lambda x: x[0], reverse=True)

    # Select paragraphs up to target length, then reorder by position
    selected: list[tuple[int, str]] = []
    total = 0
    for score, idx, para in scored:
        if total + len(para) > target_chars * 1.2:
            # If we have enough, stop. Allow slight overshoot for coherence.
            if total >= target_chars * 0.6:
                break
        selected.append((idx, para))
        total += len(para)
        if total >= target_chars:
            break

    if not selected:
        # Just take the first paragraph if scoring found nothing
        return paragraphs[0][:target_chars]

    # Reorder by original position for narrative flow
    selected.sort(key=lambda x: x[0])

    result = " ".join(para for _, para in selected)

    # Trim to target at sentence boundary
    if len(result) > target_chars:
        cut = result[:target_chars].rfind(". ")
        if cut > target_chars * 0.5:
            result = result[:cut + 1]
        else:
            result = result[:target_chars - 3] + "..."

    return result


def _paragraph_score(para: str, position: int, total: int) -> float:
    """Score a paragraph for information density."""
    score = 0.0

    # Position: inverted pyramid — first paragraphs are most important
    position_weight = max(0.1, 1.0 - (position / max(total, 1)) * 0.7)
    score += position_weight * 3.0

    # Length: prefer substantial paragraphs (50-300 chars)
    if 50 < len(para) < 300:
        score += 1.0
    elif len(para) >= 300:
        score += 0.5

    # Named entities: capitalized multi-word phrases suggest proper nouns
    caps = len(re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", para))
    score += min(2.0, caps * 0.3)

    # Numbers: dates, statistics, amounts indicate factual content
    numbers = len(re.findall(r"\b\d[\d,.]*\b", para))
    score += min(1.5, numbers * 0.3)

    # Quotes: direct quotes carry source attribution
    if '"' in para or "\u201c" in para:
        score += 1.0

    # Penalize boilerplate
    if _BOILERPLATE.search(para):
        score -= 5.0

    return score


# ── LLM-backed summarization ─────────────────────────────────────

def llm_summary(
    article_text: str,
    title: str,
    source: str,
    api_key: str,
    model: str = "claude-sonnet-4-5-20250929",
    base_url: str = "https://api.anthropic.com/v1",
    target_chars: int = 500,
) -> str:
    """Generate a summary using Anthropic Claude. Falls back to extractive on failure."""
    if not api_key or not article_text:
        return extractive_summary(article_text, target_chars)

    # Truncate article to fit in context (keep first ~4000 chars)
    article_truncated = article_text[:4000]

    system_prompt = (
        "You are a news summarizer for a personal intelligence briefing. "
        "Write a concise but complete summary of the article — enough that "
        "the reader does NOT need to click through to the original. "
        "Include key facts, names, numbers, and quotes. "
        "Write in plain prose, no bullet points. "
        f"Target length: {target_chars} characters."
    )

    user_message = (
        f"Article: \"{title}\" from {source}\n\n"
        f"{article_truncated}\n\n"
        f"Summarize this article in about {target_chars} characters."
    )

    try:
        body = json.dumps({
            "model": model,
            "max_tokens": 300,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result.get("content", [{}])[0].get("text", "")
        if text and len(text) > 50:
            return text.strip()

    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as e:
        log.warning("Anthropic summary failed for %s: %s", title[:60], e)

    return extractive_summary(article_text, target_chars)


def gemini_summary(
    article_text: str,
    title: str,
    source: str,
    api_key: str,
    model: str = "gemini-2.0-flash",
    target_chars: int = 500,
) -> str:
    """Generate a summary using Google Gemini. Falls back to extractive on failure."""
    if not api_key or not article_text:
        return extractive_summary(article_text, target_chars)

    article_truncated = article_text[:4000]

    prompt = (
        f"You are a news summarizer for a personal intelligence briefing. "
        f"Summarize this article in about {target_chars} characters — enough that "
        f"the reader does NOT need to read the original article. "
        f"Include key facts, names, numbers, and quotes. "
        f"Write in plain prose, no bullet points or headers.\n\n"
        f"Article: \"{title}\" from {source}\n\n"
        f"{article_truncated}"
    )

    # API key in header, not URL — URLs leak into logs, proxies, and error traces
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent"
    )

    try:
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 400,
                "temperature": 0.3,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        # Extract text from Gemini response
        candidates = result.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                text = parts[0].get("text", "")
                if text and len(text) > 50:
                    return text.strip()

    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as e:
        log.warning("Gemini summary failed for %s: %s", title[:60], e)

    return extractive_summary(article_text, target_chars)


# ── Batch enrichment (the public API) ────────────────────────────

class ArticleEnricher:
    """Enriches selected candidates by fetching and summarizing full articles.

    Runs after expert council selection — only the final selected stories
    get fetched, keeping total latency manageable.

    Summarization priority:
    1. Gemini API (fast, generous free tier — best for bulk summarization)
    2. Anthropic Claude (high quality, requires paid key)
    3. Extractive (always available, no API key needed)
    """

    def __init__(
        self,
        llm_api_key: str = "",
        llm_model: str = "claude-sonnet-4-5-20250929",
        llm_base_url: str = "https://api.anthropic.com/v1",
        gemini_api_key: str = "",
        gemini_model: str = "gemini-2.0-flash",
        fetch_timeout: int = 8,
        max_workers: int = 5,
        target_summary_chars: int = 500,
    ) -> None:
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._llm_base_url = llm_base_url
        self._gemini_api_key = gemini_api_key
        self._gemini_model = gemini_model
        self._fetch_timeout = fetch_timeout
        self._max_workers = max_workers
        self._target_chars = target_summary_chars

        # Log which summarization backend is active
        if gemini_api_key:
            log.info("Article enrichment: using Gemini (%s)", gemini_model)
        elif llm_api_key:
            log.info("Article enrichment: using Anthropic (%s)", llm_model)
        else:
            log.info("Article enrichment: using extractive (no LLM key)")

    def enrich(self, candidates: list[CandidateItem]) -> list[CandidateItem]:
        """Fetch articles and replace RSS teasers with real summaries.

        Fetches articles in parallel for speed. If a fetch or summary
        fails, the original RSS description is preserved.
        """
        if not candidates:
            return candidates

        # Parallel fetch
        article_texts: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(fetch_article, c.url, self._fetch_timeout): c.candidate_id
                for c in candidates
                if c.url
            }
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    article_texts[cid] = future.result()
                except Exception:
                    article_texts[cid] = ""

        # Summarize
        enriched_count = 0
        for c in candidates:
            raw_html = article_texts.get(c.candidate_id, "")
            if not raw_html:
                continue

            article_text = extract_article_text(raw_html)
            if len(article_text) < 100:
                # Extraction failed — keep original
                continue

            # Generate summary — try Gemini first, then Anthropic, then extractive
            summary = self._summarize(article_text, c.title, c.source)

            if summary and len(summary) > len(c.summary):
                c.summary = summary
                enriched_count += 1

        log.info(
            "Article enrichment: %d/%d candidates enriched",
            enriched_count, len(candidates),
        )
        return candidates

    def _summarize(self, article_text: str, title: str, source: str) -> str:
        """Generate a summary using the best available backend."""
        if self._gemini_api_key:
            return gemini_summary(
                article_text, title, source,
                self._gemini_api_key, self._gemini_model,
                self._target_chars,
            )
        if self._llm_api_key:
            return llm_summary(
                article_text, title, source,
                self._llm_api_key, self._llm_model, self._llm_base_url,
                self._target_chars,
            )
        return extractive_summary(article_text, self._target_chars)
