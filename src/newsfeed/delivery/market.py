"""Market ticker — fetches live stock and crypto prices from free public APIs.

Sources:
- Crypto: CoinCap (no key), CoinGecko (no key), Binance (no key) as fallbacks
- Stocks/Indices: Yahoo Finance (no key, User-Agent required)

All fetches use stdlib urllib — no third-party dependencies.
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; NewsFeed/1.0)"
_TIMEOUT = 8

# ── Ticker symbols ────────────────────────────────────────────────────
# Default watchlists when the user hasn't set custom ones
DEFAULT_CRYPTO = ["bitcoin", "ethereum", "solana"]
DEFAULT_STOCKS = ["SPY", "QQQ"]
DEFAULT_INDICES = ["^GSPC", "^VIX"]

# Crypto symbol display mapping
_CRYPTO_SYMBOL = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "dogecoin": "DOGE", "cardano": "ADA", "ripple": "XRP",
    "polkadot": "DOT", "avalanche-2": "AVAX", "chainlink": "LINK",
    "litecoin": "LTC", "polygon": "MATIC", "uniswap": "UNI",
}

# Index display names
_INDEX_NAME = {
    "^GSPC": "S&P", "^DJI": "DOW", "^IXIC": "NDQ",
    "^VIX": "VIX", "^RUT": "R2K",
}


@dataclass(slots=True)
class TickerQuote:
    symbol: str
    price: float
    change_pct: float | None = None
    label: str = ""  # display label (e.g., "BTC", "S&P")


def _fetch_json(url: str, timeout: int = _TIMEOUT) -> dict | list | None:
    """Fetch JSON from a URL with error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        log.debug("Market fetch failed for %s: %s", url, e)
        return None


class MarketTicker:
    """Fetches live market data from free APIs."""

    def __init__(self, cache_seconds: int = 120) -> None:
        self._cache: dict[str, tuple[float, list[TickerQuote]]] = {}
        self._cache_ttl = cache_seconds

    def _cached(self, key: str) -> list[TickerQuote] | None:
        entry = self._cache.get(key)
        if entry and time.time() - entry[0] < self._cache_ttl:
            return entry[1]
        return None

    def _store(self, key: str, quotes: list[TickerQuote]) -> list[TickerQuote]:
        self._cache[key] = (time.time(), quotes)
        return quotes

    # ── Crypto via CoinCap (no key) ──────────────────────────────────

    def fetch_crypto(self, ids: list[str] | None = None) -> list[TickerQuote]:
        """Fetch crypto prices from CoinCap API."""
        ids = ids or DEFAULT_CRYPTO
        cache_key = f"crypto:{','.join(sorted(ids))}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        quotes: list[TickerQuote] = []

        def _fetch_coin(coin_id: str) -> TickerQuote | None:
            data = _fetch_json(f"https://api.coincap.io/v2/assets/{coin_id}")
            if data and isinstance(data.get("data"), dict):
                d = data["data"]
                try:
                    price = float(d.get("priceUsd", 0))
                    change = float(d.get("changePercent24Hr", 0))
                    label = _CRYPTO_SYMBOL.get(coin_id, d.get("symbol", coin_id.upper()))
                    return TickerQuote(symbol=coin_id, price=price,
                                      change_pct=change, label=label)
                except (ValueError, TypeError):
                    pass
            return None

        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            futures = {pool.submit(_fetch_coin, cid): cid for cid in ids}
            for future in as_completed(futures, timeout=12):
                try:
                    q = future.result()
                    if q:
                        quotes.append(q)
                except Exception:
                    pass

        if not quotes:
            # Fallback 1: try CoinGecko
            quotes = self._fetch_crypto_coingecko(ids)

        if not quotes:
            # Fallback 2: try Binance
            quotes = self._fetch_crypto_binance(ids)

        return self._store(cache_key, quotes)

    def _fetch_crypto_binance(self, ids: list[str]) -> list[TickerQuote]:
        """Fallback crypto fetch via Binance."""
        binance_map = {
            "bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
            "dogecoin": "DOGEUSDT", "cardano": "ADAUSDT", "ripple": "XRPUSDT",
            "avalanche-2": "AVAXUSDT", "chainlink": "LINKUSDT",
            "litecoin": "LTCUSDT", "polkadot": "DOTUSDT",
        }
        quotes: list[TickerQuote] = []
        for coin_id in ids:
            pair = binance_map.get(coin_id)
            if not pair:
                continue
            data = _fetch_json(f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}")
            if data and "lastPrice" in data:
                try:
                    price = float(data["lastPrice"])
                    change = float(data.get("priceChangePercent", 0))
                    label = _CRYPTO_SYMBOL.get(coin_id, coin_id.upper()[:4])
                    quotes.append(TickerQuote(symbol=coin_id, price=price,
                                              change_pct=change, label=label))
                except (ValueError, TypeError):
                    pass
        return quotes


    def _fetch_crypto_coingecko(self, ids: list[str]) -> list[TickerQuote]:
        """Fallback crypto fetch via CoinGecko free API."""
        ids_param = ",".join(ids)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_param}&vs_currencies=usd&include_24hr_change=true"
        data = _fetch_json(url)
        if not data or not isinstance(data, dict):
            return []
        quotes: list[TickerQuote] = []
        for coin_id in ids:
            coin_data = data.get(coin_id)
            if not coin_data:
                continue
            try:
                price = float(coin_data.get("usd", 0))
                change = float(coin_data.get("usd_24h_change", 0))
                label = _CRYPTO_SYMBOL.get(coin_id, coin_id.upper()[:4])
                quotes.append(TickerQuote(symbol=coin_id, price=price,
                                          change_pct=change, label=label))
            except (ValueError, TypeError):
                pass
        return quotes

    # ── Stocks/Indices via Yahoo Finance (no key) ────────────────────

    def fetch_stocks(self, tickers: list[str] | None = None) -> list[TickerQuote]:
        """Fetch stock/index quotes from Yahoo Finance."""
        tickers = tickers or DEFAULT_STOCKS
        cache_key = f"stocks:{','.join(sorted(tickers))}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        def _fetch_ticker(ticker: str) -> TickerQuote | None:
            encoded = ticker.replace("^", "%5E")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=1d&interval=1d"
            data = _fetch_json(url)
            if data and "chart" in data:
                try:
                    result = data["chart"]["result"][0]
                    meta = result.get("meta", {})
                    price = meta.get("regularMarketPrice", 0)
                    prev = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
                    change_pct = ((price - prev) / prev * 100) if prev else None
                    label = _INDEX_NAME.get(ticker, ticker.replace("^", ""))
                    return TickerQuote(symbol=ticker, price=price,
                                      change_pct=change_pct, label=label)
                except (KeyError, IndexError, TypeError, ZeroDivisionError):
                    pass
            return None

        quotes: list[TickerQuote] = []
        with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
            futures = {pool.submit(_fetch_ticker, t): t for t in tickers}
            for future in as_completed(futures, timeout=12):
                try:
                    q = future.result()
                    if q:
                        quotes.append(q)
                except Exception:
                    pass

        return self._store(cache_key, quotes)

    # ── Combined fetch ───────────────────────────────────────────────

    def fetch_all(self, crypto_ids: list[str] | None = None,
                  stock_tickers: list[str] | None = None) -> dict[str, list[TickerQuote]]:
        """Fetch both crypto and stock data."""
        return {
            "crypto": self.fetch_crypto(crypto_ids),
            "stocks": self.fetch_stocks(stock_tickers),
        }

    # ── Formatting ───────────────────────────────────────────────────

    @staticmethod
    def format_ticker_bar(quotes: list[TickerQuote]) -> str:
        """Format quotes as a compact ticker bar for Telegram HTML."""
        if not quotes:
            return ""

        parts: list[str] = []
        for q in quotes:
            # Format price compactly
            if q.price >= 10000:
                price_str = f"{q.price:,.0f}"
            elif q.price >= 100:
                price_str = f"{q.price:,.1f}"
            elif q.price >= 1:
                price_str = f"{q.price:,.2f}"
            else:
                price_str = f"{q.price:.4f}"

            # Change arrow and color
            if q.change_pct is not None:
                if q.change_pct > 0:
                    arrow = "\u25b2"  # ▲
                    sign = "+"
                elif q.change_pct < 0:
                    arrow = "\u25bc"  # ▼
                    sign = ""
                else:
                    arrow = "\u25ac"  # ▬
                    sign = ""
                change_str = f" {arrow}{sign}{q.change_pct:.1f}%"
            else:
                change_str = ""

            parts.append(f"<b>{html_mod.escape(q.label)}</b> ${price_str}{change_str}")

        return " \u2502 ".join(parts)  # │ separator
