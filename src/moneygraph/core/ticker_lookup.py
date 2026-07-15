"""
Ticker lookup — SEC company_tickers.json bulk lookup.

Feeds the review-queue approve-form pre-fill: for a candidate already known
to be public (`candidates.facts.is_public is True`), look up a real ticker
by name so the reviewer doesn't have to type it.

The bulk file (https://www.sec.gov/files/company_tickers.json) lists every
SEC-registered ticker (~10k rows of {cik_str, ticker, title}), is free, needs
no key, and has no meaningful rate limit at this scale — but it IS a single
multi-MB static file, so it's fetched at most once per `max_age_days` and
cached on disk (same pattern as edgar.py's submissions cache), plus memoized
in-process so a batch of candidates in one enrichment run doesn't re-parse
the ~10k rows per candidate.

Match strategy — same "never guess wrong" bar used throughout this codebase
(see enrichment.py's Wikidata disambiguation guard, resolve.py's fuzzy-match
threshold):
  1. exact match on the normalized company title (reuses resolve.normalize()
     rather than a second name-normalizer)
  2. fuzzy fallback: Levenshtein distance <= 1 on the normalized title — the
     same confidence bar resolve.py's Pass 4 uses to auto-register an alias
     without human review
  3. no confident match -> None (blank ticker beats a wrong one)
"""

import json
import logging
import os
import time
from pathlib import Path

import requests
from rapidfuzz.distance import Levenshtein

from .resolve import normalize

logger = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_DATA_DIR = Path("/app/data")
_TIMEOUT = 30
_MAX_FUZZY_DISTANCE = 1

# In-process memo of the built {normalized_title: ticker} index — avoids
# re-parsing the ~10k-row file for every candidate in a single backfill run.
_index_cache: dict[str, str] | None = None


def _cache_path() -> Path:
    p = _DATA_DIR / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p / "company_tickers.json"


def _fetch_raw(max_age_days: int = 30) -> dict:
    """Return the raw SEC company_tickers.json dict, from disk cache if
    fresh enough, else fetched and re-cached. Raises on a hard failure with
    no usable cache (caller decides how to handle — lookup_ticker() treats
    it as 'no match', same as any other enrichment source failure)."""
    cache = _cache_path()
    if cache.exists():
        age_days = (time.time() - cache.stat().st_mtime) / 86400
        if age_days < max_age_days:
            logger.info("ticker index cache hit  (%.1fd old)", age_days)
            return json.loads(cache.read_text(encoding="utf-8"))

    # SEC fair-use policy 403s any User-Agent it can't parse as "name email@domain"
    # (confirmed live 2026-07-11 — a plain "MoneyGraph/1.0 (...)" string, no
    # '@', was rejected outright). Reuses the same EDGAR_USER_AGENT env var
    # edgar.py already sends to data.sec.gov, rather than adding a second
    # config knob for the same www.sec.gov fair-use requirement.
    resp = requests.get(
        _TICKERS_URL,
        headers={"User-Agent": os.environ.get("EDGAR_USER_AGENT", "")},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    cache.write_text(json.dumps(data), encoding="utf-8")
    logger.info("ticker index fetched   (%d rows)", len(data))
    return data


def _build_index(max_age_days: int = 30) -> dict[str, dict]:
    raw = _fetch_raw(max_age_days=max_age_days)
    index: dict[str, dict] = {}
    for row in raw.values():
        title = row.get("title")
        ticker = row.get("ticker")
        if not title or not ticker:
            continue
        norm = normalize(title)
        # First hit wins on a normalized-name collision (rare; SEC's file
        # isn't otherwise ordered by relevance, but a collision here means
        # two nearly-identical titles, so either is a reasonable pick).
        if norm and norm not in index:
            cik_raw = row.get("cik_str")
            # Keep the raw (unpadded) string form — matches how CIKs
            # are already stored on existing `nodes` rows (e.g. "1045810",
            # not "0000001045810"). Zero-padding, if ever needed, is a
            # presentation concern for a caller, not this index.
            cik = str(cik_raw) if cik_raw is not None else None
            index[norm] = {"ticker": ticker, "cik": cik}
    return index


def _get_index(max_age_days: int = 30, force_refresh: bool = False) -> dict[str, dict]:
    global _index_cache
    if _index_cache is None or force_refresh:
        _index_cache = _build_index(max_age_days=max_age_days)
    return _index_cache


def lookup_ticker_and_cik(name: str) -> tuple[str | None, str | None]:
    """
    Return (ticker, cik) for `name` from the SEC bulk file, or (None, None) if
    nothing matches with enough confidence. Same match strategy as
    lookup_ticker() (exact normalized-title match, then Levenshtein <= 1
    fallback) — see module docstring. Never raises.

    Added for (dark_horse auto-promotion): a confirmed CIK is the
    strongest available "this is a real SEC filer now" signal, and the
    existing lookup_ticker() discarded the CIK the bulk file already carries.
    """
    try:
        index = _get_index()
    except Exception:
        logger.exception("ticker lookup: failed to load/build SEC ticker index")
        return None, None

    norm = normalize(name)
    if not norm:
        return None, None

    exact = index.get(norm)
    if exact:
        return exact["ticker"], exact["cik"]

    best_entry: dict | None = None
    best_dist: int | None = None
    for cand_norm, entry in index.items():
        d = Levenshtein.distance(norm, cand_norm)
        if best_dist is None or d < best_dist:
            best_dist = d
            best_entry = entry

    if best_dist is not None and best_dist <= _MAX_FUZZY_DISTANCE and best_entry is not None:
        return best_entry["ticker"], best_entry["cik"]
    return None, None


def lookup_ticker(name: str) -> str | None:
    """
    Return a confident ticker for `name`, or None if nothing matches with
    enough confidence. Never raises — any lookup/network failure degrades to
    "no match" (blank ticker), consistent with the rest of the enrichment
    pipeline's "fall back to no-enrichment rather than guess wrong" stance.

    Thin wrapper around lookup_ticker_and_cik for pre-existing
    ticker-only callers (candidate approve-form pre-fill).
    """
    ticker, _cik = lookup_ticker_and_cik(name)
    return ticker
