"""
Stock price widget data.

Data source decision: **Yahoo Finance's unofficial chart endpoint**
(`query1.finance.yahoo.com/v8/finance/chart/{ticker}`), not Stooq.

Why: Stooq's public CSV endpoint (`stooq.com/q/d/l/`) now sits behind a
JS proof-of-work challenge page (verified live 2026-07-10 — returns an HTML
page with a crypto.subtle.digest() challenge, not CSV), so it is no longer
usable headlessly without a browser. Yahoo's chart endpoint requires no API
key and has no documented rate limit for this scale (a handful of node-detail
panel opens).

One cache row per (ticker, UI range) — NOT one shared "max" series sliced
client-side. This matters: Yahoo silently coarsens granularity for long
`range` values when you fix `interval=1d` yourself — `range=max&interval=1d`
for AAPL returns only ~168 points (roughly monthly-spaced) covering its whole
trading history, not daily bars. Slicing that series down to "last 366 days"
for the 1y view would return ~5 points instead of ~251 trading days (verified
live 2026-07-10). Instead, each UI range is fetched with Yahoo's OWN `range`
param, which internally picks the right granularity per window
(range=1y&interval=1d -> 251 points; range=5y&interval=1d -> ~1254; range=max
-> Yahoo's own ~168-point coarse history, which is the correct behaviour for
an all-time view, not a bug). "1d" uses interval=5m for real intraday bars
(not faked from daily closes) — Yahoo provides this for free.

No ticker (private/dark_horse nodes) is not this module's problem — callers
check `node.ticker` before calling and render "no price data" themselves.

exchange-suffix mapping for non-US listings
-----------------------------------------------------
Yahoo's chart endpoint needs an exchange-suffixed symbol for anything not
US-listed (e.g. `9988.HK`, not `9988` — the bare form 404s/returns a
different result). `yahoo_symbol()` below maps this codebase's `node_tickers`
`(exchange, ticker)` pair to the symbol Yahoo actually expects. Every mapped
suffix was verified LIVE against the real endpoint on 2026-07-11 (not
guessed):
  - HKG (Hong Kong)  -> `.HK`  — `9988.HK`     confirmed (Alibaba Group
    Holding Ltd, HKD, regularMarketPrice ~110 HKD — a DIFFERENT security from
    the NYSE ADR `BABA`, ~112 USD, confirmed distinct via the same query).
  - LON (London)     -> `.L`   — `HSBA.L`      confirmed (HSBC, LSE, GBp).
  - TYO (Tokyo)      -> `.T`   — `7203.T`       confirmed (Toyota Motor, JPX, JPY).
  - SHA (Shanghai)   -> `.SS`  — `600519.SS`    confirmed (Kweichow Moutai, SHH, CNY).
  - SHE (Shenzhen)   -> `.SZ`  — `000002.SZ`    confirmed (Vanke, SHZ, CNY).
  - ETR (Frankfurt/Xetra) -> `.DE` — `BMW.DE`   confirmed 2026-07-13 (Bayerische
    Motoren Werke AG, exchangeName GER, EUR — added after UAT flagged "no price
    data for BMW Group", root cause was this exchange missing from the map).
  - NYSE              -> no suffix — `MS`       confirmed 2026-07-13 (Morgan
    Stanley, exchangeName NYQ, USD) — added alongside ETR: without an entry,
    NYSE fell through to the same bare-ticker fallback and happened to work by
    accident (correct result, spurious warning); now explicit and verified
    like every other entry instead of relying on the fallback path.
  - FRA (Frankfurt floor, distinct from Xetra/ETR) -> `.F` — `SSU.F` confirmed
    2026-07-13 (Samsung Electronics Co., Ltd., exchangeName FRA, EUR).
  - OTCMKTS / ''      -> no suffix — `TYIDY`     confirmed (Toyota Industries,
    PNK/OTC Markets OTCPK, USD) — Yahoo carries US OTC tickers bare, same as
    a normal US listing. `''` (empty-string exchange, this codebase's
    node_tickers sentinel for "no exchange qualifier") is treated the same
    way — the pre-existing, still-overwhelmingly-common case.

An exchange with no entry in `_YAHOO_EXCHANGE_SUFFIX` is a genuine gap, not a
guess: `yahoo_symbol()` logs a warning and falls back to the bare ticker
rather than fabricating a suffix, since a wrong suffix would silently fetch a
DIFFERENT company's price — worse than showing no price data at all.
"""

import logging
from datetime import datetime, timezone

import requests

from ..db import execute, query

logger = logging.getLogger(__name__)

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_USER_AGENT = "Mozilla/5.0 (MoneyGraph/1.0 personal project)"
_TIMEOUT = 10

_INTRADAY_TTL_SECS = 15 * 60  # 5-min bars — refresh every ~15 min
_DAILY_TTL_SECS = 24 * 3600  # daily OHLC (1m/1y/5y/max) — refresh at most once/day

# UI range -> (cache bucket == range itself, Yahoo `range` param, Yahoo `interval` param, TTL)
_RANGE_CONFIG = {
    "1d": ("1d", "5m", _INTRADAY_TTL_SECS),
    "1m": ("1mo", "1d", _DAILY_TTL_SECS),
    "1y": ("1y", "1d", _DAILY_TTL_SECS),
    "5y": ("5y", "1d", _DAILY_TTL_SECS),
    "max": ("max", "1d", _DAILY_TTL_SECS),
}


# Exchange (as stored in node_tickers) -> Yahoo Finance symbol
# suffix. See module docstring for live-verification evidence per entry.
# '' (this codebase's "no exchange qualifier" sentinel) and the explicit
# 'OTCMKTS' qualifier both map to "no suffix" — Yahoo carries both plain US
# listings and US OTC Markets tickers unsuffixed.
_YAHOO_EXCHANGE_SUFFIX = {
    "": "",
    "OTCMKTS": "",
    "HKG": ".HK",
    "LON": ".L",
    "TYO": ".T",
    "SHA": ".SS",
    "SHE": ".SZ",
    "ETR": ".DE",
    "NYSE": "",
    "FRA": ".F",
}


def yahoo_symbol(ticker: str, exchange: str | None) -> str:
    """Map a (ticker, exchange) pair to the symbol Yahoo's chart endpoint
    expects. Returns the bare ticker unchanged (with a warning logged) for an
    exchange this module doesn't have a verified mapping for — see module
    docstring: a silently-wrong suffix could fetch a different company's
    price entirely, which is worse than falling back to the bare ticker.
    """
    exch = (exchange or "").strip().upper()
    suffix = _YAHOO_EXCHANGE_SUFFIX.get(exch)
    if suffix is None:
        logger.warning(
            "yahoo_symbol: no verified suffix mapping for exchange=%r (ticker=%s) — "
            "using bare ticker as a fallback, may return no/wrong data",
            exchange,
            ticker,
        )
        return ticker
    return f"{ticker}{suffix}"


def _fetch_yahoo(ticker: str, range_: str, interval: str) -> dict | None:
    url = _YAHOO_CHART_URL.format(ticker=ticker)
    try:
        resp = requests.get(
            url,
            params={"range": range_, "interval": interval},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("yahoo chart fetch failed  ticker=%s range=%s interval=%s", ticker, range_, interval)
        return None

    result = (payload.get("chart") or {}).get("result")
    if not result:
        err = (payload.get("chart") or {}).get("error")
        logger.info("yahoo chart: no result  ticker=%s error=%s", ticker, err)
        return None

    r = result[0]
    timestamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []

    points = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue  # Yahoo pads gaps (market closed) with nulls — skip them
        points.append(
            {
                "t": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "open": opens[i] if i < len(opens) else None,
                "high": highs[i] if i < len(highs) else None,
                "low": lows[i] if i < len(lows) else None,
                "close": close,
                "volume": volumes[i] if i < len(volumes) else None,
            }
        )

    if not points:
        return None

    return {"points": points, "fetched_at": datetime.now(timezone.utc).isoformat()}


def _cache_get(ticker: str, bucket: str) -> tuple[dict | None, float]:
    """Returns (data, age_seconds). data is None if no cache row."""
    rows = query(
        "SELECT data, fetched_at FROM stock_price_cache WHERE ticker = %s AND bucket = %s",
        (ticker, bucket),
    )
    if not rows:
        return None, float("inf")
    age = (datetime.now(timezone.utc) - rows[0]["fetched_at"]).total_seconds()
    return rows[0]["data"], age


def _cache_put(ticker: str, bucket: str, data: dict) -> None:
    import psycopg2.extras

    execute(
        """
        INSERT INTO stock_price_cache (ticker, bucket, data, fetched_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (ticker, bucket) DO UPDATE SET
            data = EXCLUDED.data, fetched_at = NOW()
        """,
        (ticker, bucket, psycopg2.extras.Json(data)),
    )


def get_price_history(ticker: str, range_: str) -> dict:
    """
    Returns {"ticker", "range", "points": [...], "stale": bool}.
    `points` is [] if the ticker has no data available (bad/delisted ticker,
    or Yahoo unreachable with no cache to fall back on) — callers render this
    as "no price data", not an error. `stale` is True when a fetch failed and
    the response is served from an expired cache row rather than a fresh one.
    """
    if range_ not in _RANGE_CONFIG:
        range_ = "1y"

    yahoo_range, yahoo_interval, ttl_secs = _RANGE_CONFIG[range_]
    bucket = range_

    cached, age = _cache_get(ticker, bucket)
    if cached is not None and age < ttl_secs:
        return {"ticker": ticker, "range": range_, "points": cached["points"], "stale": False}

    fresh = _fetch_yahoo(ticker, yahoo_range, yahoo_interval)
    if fresh is not None:
        _cache_put(ticker, bucket, fresh)
        return {"ticker": ticker, "range": range_, "points": fresh["points"], "stale": False}

    if cached is not None:
        # Fetch failed — stale cache beats nothing.
        return {"ticker": ticker, "range": range_, "points": cached["points"], "stale": True}

    return {"ticker": ticker, "range": range_, "points": [], "stale": False}
