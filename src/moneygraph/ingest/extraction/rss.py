"""
RSS press-wire ingest.

Free, reliable discovery source. Polls public RSS feeds, keeps only entries that
mention a seed entity, then funnels matched articles through the existing web
gate/write path (verbatim-excerpt / paywall / self-reference / idempotency gates →
_process_event). RSS replaces the paid OpenAI web_search *discovery* layer;
everything downstream is reused from websearch.py.

Public:
  run_rss_phase(run_id, feeds=None) → (events_logged, candidates_found, edges_created)
  fetch_feed_entries(feed) → list[FeedEntry]
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass

import feedparser

from ...core.resolve import normalize
from ...db import bump_run_counters, query, set_run_total_units
from .websearch import (
    WebResult,
    _fetch_page,
    _get_processed_web_source,
    _is_paywalled,
    _parse_date_str,
    _process_web_result,
    _upsert_processed_web_source,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feed config
# ---------------------------------------------------------------------------
# Press wires = primary company announcements (Tier 1 trust, though
# floored at tier 3 today by the web>=3 rule — open question, revisit).
# Open tech/finance feeds = journalistic corroboration.
#
# NOTE: press-wire RSS endpoints are category-specific and drift; expect to tune
# these URLs after the first live run. feedparser degrades gracefully on a dead
# feed (returns empty entries), so a stale URL is a no-op, not a crash.
#
# Business Wire is intentionally NOT hardcoded: its feeds are per-selection
# tokenized URLs that can't be guessed. Set BUSINESSWIRE_RSS_URL in .env to
# enable it (generate at https://www.businesswire.com/portal/site/home/news/);
# unset → the feed is skipped.
_BASE_FEEDS: list[dict[str, str]] = [
    # Press wires (primary announcements)
    {"name": "PR Newswire", "url": "https://www.prnewswire.com/rss/news-releases-list.rss"},
    {
        "name": "GlobeNewswire",
        "url": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    },
    # Open tech / finance outlets (corroboration)
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "SiliconANGLE", "url": "https://siliconangle.com/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
]


def _load_feeds() -> list[dict[str, str]]:
    """Base feeds + Business Wire if BUSINESSWIRE_RSS_URL is configured."""
    feeds = list(_BASE_FEEDS)
    bw_url = os.getenv("BUSINESSWIRE_RSS_URL", "").strip()
    if bw_url:
        feeds.append({"name": "Business Wire", "url": bw_url})
    else:
        logger.info("BUSINESSWIRE_RSS_URL unset — Business Wire feed skipped")
    return feeds


_FEEDS: list[dict[str, str]] = _load_feeds()

# Terms shorter than this are dropped from the entity matcher to avoid false
# positives (e.g. the AT&T ticker alias "t"). Real company names clear this easily.
_MIN_TERM_LEN = 3


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FeedEntry:
    url: str
    title: str
    summary: str
    published_at: str | None  # ISO YYYY-MM-DD or None


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------


def fetch_feed_entries(feed: dict[str, str]) -> list[FeedEntry]:
    """Parse one RSS/Atom feed into FeedEntry rows. Never raises — logs and returns []."""
    try:
        parsed = feedparser.parse(feed["url"])
    except Exception as exc:  # feedparser rarely raises, but be safe
        logger.warning("feed parse failed  name=%s  error=%s", feed["name"], exc)
        return []

    if getattr(parsed, "bozo", False):
        logger.info(
            "feed bozo (malformed but usable)  name=%s  %s",
            feed["name"],
            getattr(parsed, "bozo_exception", ""),
        )

    entries: list[FeedEntry] = []
    for e in parsed.entries:
        url = getattr(e, "link", "") or ""
        if not url:
            continue
        title = getattr(e, "title", "") or ""
        summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        published_at = _entry_published_at(e)
        entries.append(FeedEntry(url=url, title=title, summary=summary, published_at=published_at))

    logger.info("feed parsed  name=%s  entries=%d", feed["name"], len(entries))
    return entries


def _entry_published_at(entry) -> str | None:
    """Pull a YYYY-MM-DD from the feed entry (free, no page fetch)."""
    # feedparser normalizes published/updated into *_parsed struct_time tuples
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            return f"{st.tm_year:04d}-{st.tm_mon:02d}-{st.tm_mday:02d}"
    # fall back to raw string fields
    for attr in ("published", "updated", "pubDate"):
        raw = getattr(entry, attr, None)
        if raw:
            parsed = _parse_date_str(raw)
            if parsed:
                return parsed
    return None


# ---------------------------------------------------------------------------
# Entity-match filter (the cost gate — replaces paid web_search discovery)
# ---------------------------------------------------------------------------


def _build_node_matcher() -> re.Pattern | None:
    """
    Build one alternation regex of normalized node names + aliases.
    Word-boundary matched against normalized entry text. Returns None if no terms.
    """
    terms: set[str] = set()

    for row in query("SELECT name FROM nodes"):
        t = normalize(row["name"])
        if len(t) >= _MIN_TERM_LEN:
            terms.add(t)

    for row in query("SELECT normalized_alias FROM node_aliases"):
        t = (row["normalized_alias"] or "").strip()
        if len(t) >= _MIN_TERM_LEN:
            terms.add(t)

    if not terms:
        return None

    # Longest-first so the alternation prefers the most specific match.
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    logger.info("entity matcher built  terms=%d", len(terms))
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


def _entry_matches(entry: FeedEntry, matcher: re.Pattern) -> str | None:
    """Return the first matched term if the entry mentions a seed entity, else None."""
    text = re.sub(r"\s+", " ", f"{entry.title} {entry.summary}").lower()
    m = matcher.search(text)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------


def run_rss_phase(
    run_id: str,
    feeds: list[dict[str, str]] | None = None,
) -> tuple[int, int, int]:
    """
    Poll RSS feeds, entity-filter, then run matched articles through the web
    gate/write path. Returns (events_logged, candidates_found, edges_created).

    feeds: defaults to _FEEDS. Pass a single-element list to test one feed.
    """
    feeds = feeds if feeds is not None else _FEEDS

    matcher = _build_node_matcher()
    if matcher is None:
        logger.info("no seed nodes — skipping RSS phase")
        return 0, 0, 0

    # Pass 1 — collect the matched, deduped entry list WITHOUT
    # fetching any page (cheap: feed title/summary only, already in memory).
    # This is the honest "total" — matched-but-not-yet-fetched entries are the
    # real work about to be attempted; unmatched entries never cost a fetch.
    matched_entries: list[FeedEntry] = []
    seen_urls: set[str] = set()
    for feed in feeds:
        for entry in fetch_feed_entries(feed):
            if entry.url in seen_urls:
                continue
            seen_urls.add(entry.url)
            if _entry_matches(entry, matcher):
                matched_entries.append(entry)

    set_run_total_units(run_id, len(matched_entries))

    total_events = 0
    total_candidates = 0
    total_edges = 0

    # Pass 2 — fetch + gate + write each matched entry.
    for entry in matched_entries:
        # E11: URL idempotency — cheap skip before any fetch.
        # (content_hash re-check below catches changed pages.)
        existing = _get_processed_web_source(entry.url)

        page_text, page_date = _fetch_page(entry.url)
        if _is_paywalled(page_text):
            logger.info("E8 skip (paywall/empty)  %s", entry.url)
            bump_run_counters(run_id, units_processed=1)
            continue

        content_hash = hashlib.sha256(page_text.encode()).hexdigest()
        if existing and existing["content_hash"] == content_hash:
            logger.info("E11 skip (unchanged)  %s", entry.url)
            bump_run_counters(run_id, units_processed=1)
            continue

        parsed = urllib.parse.urlparse(entry.url)
        domain = parsed.netloc or entry.url

        result = WebResult(
            url=entry.url,
            domain=domain,
            title=entry.title,
            snippet=entry.summary,
            page_text=page_text,
            published_at=entry.published_at or page_date,  # feed date wins; page meta fallback
            content_hash=content_hash,
        )

        logger.info("RSS process  %s", entry.url)
        ev, cand, edges = _process_web_result(result, run_id)
        total_events += ev
        total_candidates += cand
        total_edges += edges

        _upsert_processed_web_source(result.url, result.content_hash, run_id, ev)

        # Live-progress bump — once per matched article, so a
        # long RSS run shows real counts climbing via the existing 5s poll.
        # units_processed is always included (this entry was attempted
        # regardless of yield — the paywall/E11-skip branches above already
        # bumped it themselves since they `continue` before this line);
        # events/candidates/edges only when nonzero, same as before.
        deltas = {"units_processed": 1}
        if ev:
            deltas["events_logged"] = ev
        if cand:
            deltas["candidates_found"] = cand
        if edges:
            deltas["edges_created"] = edges
        bump_run_counters(run_id, **deltas)

    logger.info(
        "RSS phase done  events=%d candidates=%d edges=%d",
        total_events,
        total_candidates,
        total_edges,
    )
    return total_events, total_candidates, total_edges
