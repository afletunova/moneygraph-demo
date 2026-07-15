"""
Web search ingest for private seed nodes.

Public:
  run_websearch_phase(run_id, nodes=None) → (events_logged, candidates_found, edges_created)
  search_node(node_name) → list[WebResult]

Search backend: Brave Search API (search_provider.py) —
switched off the OpenAI Responses API web_search_preview tool, which cost
$0.025/call and dominated per-run websearch cost. Discovery only: page text is still fetched ourselves
via `_fetch_page` (E1 gate requires real page text, and Brave doesn't bundle
a page fetch), and event extraction is still a plain chat completion on
_WEB_MODEL (`_extract_from_result`, unchanged — not the cost driver).
Write path: _process_event() in pipeline.py — unchanged.
Idempotency: processed_web_sources (url PK + content_hash).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import openai
import requests as http_requests

from ...db import bump_run_counters, execute, query, set_run_total_units
from . import search_provider
from .pipeline import _detect_syndicate_indices, _log_openai_response, _process_event, _strip_html
from .prompt import WEB_SYSTEM_PROMPT, build_web_user_content, parse_extraction_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cheap and sufficient for this extraction shape (short excerpt -> small
# structured JSON); env-overridable rather than escalation-tiered like the
# EDGAR realtime backend, since web-sourced text is short enough that a
# stronger model isn't needed here.
_WEB_MODEL = os.environ.get("OPENAI_PRIMARY_MODEL", "gpt-4o-mini")

# Tier-3 domains: press wires (primary sources, no paywall) + reliable open tech/finance outlets.
# bloomberg.com / ft.com / wsj.com / reuters.com removed — 403 in practice.
# Press wires (businesswire, prnewswire, globenewswire) are primary company announcements;
# arguably tier 2, but web floor stays at 3 for now (revisit if it matters in practice).
_TIER3_DOMAINS = {
    "businesswire.com",
    "prnewswire.com",
    "globenewswire.com",
    "cnbc.com",
    "techcrunch.com",
    "venturebeat.com",
    "axios.com",
}

# Restored to the original 5-template design now that discovery has moved
# off the paid OpenAI web_search tool ($0.025/call) onto Brave Search's free
# tier (~2,000 queries/month) — the $-per-call reason this was ever cut down
# to 1 template no longer applies; Brave calls are free up to the tier ceiling.
# 3 general phrasings (catch different journalistic framings of the same deal)
# + 2 site-forced templates (press-wire primary sources, open tech/finance
# outlets that don't 403) — same shape as the original design.
# Quota math: a one-time backfill over the 91 no-CIK zero-edge nodes at 5
# templates/node = 455 calls, comfortably inside the ~2,000/month free tier
# even stacked with routine traffic. For *ongoing* runs this does NOT reduce
# to "455/month" — every full un-skipped run costs 5×(# private nodes), so if
# this is left running on a schedule, keep the stale_days skip (default 14)
# in place; a full recurring backfill (not just a refresh of a handful of
# stale nodes) at high node counts (e.g. after a large seed-list expansion)
# could approach the ceiling and should be re-budgeted then, not assumed safe
# forever just because it fits today's node count.
_QUERY_TEMPLATES = [
    "{name} investment stake 2025 2026",
    "{name} funding round announced",
    "{name} strategic partnership equity",
    '"{name}" site:businesswire.com OR site:prnewswire.com OR site:globenewswire.com',
    '"{name}" funding site:techcrunch.com OR site:venturebeat.com OR site:cnbc.com',
]

_PAYWALL_MARKERS = [
    "subscribe to read",
    "subscribe to continue",
    "subscribe for full access",
    "sign in to read",
    "create an account to read",
    "to continue reading",
    "premium content",
]

_MIN_PAGE_TEXT_LEN = 300

# Freshness skip: avoid re-searching a node that was searched recently on a
# refresh run. Originally an OpenAI-$/call cost cap; kept afterwards as a
# Brave free-tier quota guard (5 templates × N nodes adds up on a recurring
# schedule even at $0/call — see quota note above). Env-tunable;
# 0 (or negative) disables the skip (forces a full re-search of every node).
_DEFAULT_STALE_DAYS = int(os.environ.get("WEBSEARCH_STALE_DAYS", "14"))

# Hang guard (2026-07-09): the OpenAI SDK defaults to a ~600s timeout with
# retries, so one unresponsive search/extraction call can wedge the whole run
# with no log output (reproduced at "Bessemer Venture Partners"). Bound every
# OpenAI call, and cap total per-node wall-clock as a backstop.
#   worst single call ~= _OPENAI_TIMEOUT_S * (1 + _OPENAI_MAX_RETRIES) + backoff
#   => 30 * 2 ~= 60s < node budget 90s.
_OPENAI_TIMEOUT_S = float(os.environ.get("WEBSEARCH_OPENAI_TIMEOUT_S", "30"))
_OPENAI_MAX_RETRIES = int(os.environ.get("WEBSEARCH_OPENAI_MAX_RETRIES", "1"))
_NODE_TIMEOUT_S = float(os.environ.get("WEBSEARCH_NODE_TIMEOUT_S", "90"))


def _openai_client() -> "openai.OpenAI":
    """OpenAI client with a bounded timeout + capped retries.

    search discovery no longer goes through OpenAI (moved to Brave
    Search, search_provider.py) — this client is now only used for the
    extraction (Chat Completions) call, but keeps its bounded
    timeout/retries so a hung socket there still can't wedge a node.
    """
    return openai.OpenAI(timeout=_OPENAI_TIMEOUT_S, max_retries=_OPENAI_MAX_RETRIES)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WebResult:
    url: str
    domain: str
    title: str
    snippet: str
    page_text: str
    published_at: str | None  # ISO string or None
    content_hash: str  # SHA-256 of page_text


# ---------------------------------------------------------------------------
# Domain → tier
# ---------------------------------------------------------------------------


def tier_for_domain(domain: str) -> int:
    """Return source tier for a web domain. Floor is always 3."""
    d = domain.lower()
    if d.startswith("www."):
        d = d[4:]
    return 3 if d in _TIER3_DOMAINS else 4


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------


def _get_processed_web_source(url: str) -> dict | None:
    rows = query(
        "SELECT content_hash FROM processed_web_sources WHERE url = %s",
        (url,),
    )
    return rows[0] if rows else None


def _node_recently_searched(node_id: str, stale_days: int) -> bool:
    """True if this node was web-searched within the last `stale_days` days.

    Freshness is tracked by nodes.last_websearched_at. A skip is
    disabled when stale_days <= 0. A node with a NULL timestamp (never searched)
    is never fresh.
    """
    if stale_days <= 0:
        return False
    rows = query(
        """SELECT (last_websearched_at IS NOT NULL
                   AND last_websearched_at > NOW() - (%s || ' days')::interval) AS fresh
           FROM nodes WHERE id = %s""",
        (stale_days, node_id),
    )
    return bool(rows and rows[0]["fresh"])


def _mark_node_websearched(node_id: str) -> None:
    """Stamp nodes.last_websearched_at = NOW() so future refreshes can skip it."""
    execute("UPDATE nodes SET last_websearched_at = NOW() WHERE id = %s", (node_id,))


def _upsert_processed_web_source(url: str, content_hash: str, run_id: str, events_count: int) -> None:
    execute(
        """INSERT INTO processed_web_sources (url, content_hash, run_id, events_count)
           VALUES (%s, %s, %s::uuid, %s)
           ON CONFLICT (url) DO UPDATE
             SET content_hash  = EXCLUDED.content_hash,
                 run_id        = EXCLUDED.run_id,
                 events_count  = EXCLUDED.events_count,
                 processed_at  = NOW()""",
        (url, content_hash, run_id, events_count),
    )


# ---------------------------------------------------------------------------
# Page fetching + paywall check
# ---------------------------------------------------------------------------

_DATE_META_RE = re.compile(
    r'<meta[^>]+(?:property|name)\s*=\s*["\']'
    r"(?:article:published_time|article:modified_time|pubdate|date|DC\.date|og:updated_time)"
    r'["\'][^>]+content\s*=\s*["\']([^"\']+)["\']'
    r'|<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)\s*=\s*["\']'
    r"(?:article:published_time|article:modified_time|pubdate|date|DC\.date|og:updated_time)"
    r'["\']',
    re.IGNORECASE,
)
_TIME_TAG_RE = re.compile(r'<time[^>]+datetime\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
# JSON-LD / embedded JSON: "datePublished" (standard) or "publishedOn" (Sanity/Next.js)
_JSONLD_DATE_RE = re.compile(r'"(?:datePublished|publishedOn)"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _parse_date_str(raw: str) -> str | None:
    """Normalise any ISO-ish date string to YYYY-MM-DD. Returns None on failure."""
    try:
        # strip timezone offset / Z, keep up to seconds
        cleaned = raw.strip().rstrip("Z")
        # handle -04:00 / +00:00 style offsets
        cleaned = re.sub(r"[+-]\d{2}:\d{2}$", "", cleaned)
        dt = datetime.fromisoformat(cleaned[:19])
        return dt.date().isoformat()
    except ValueError:
        return None


def _extract_published_at(html: str) -> str | None:
    """Extract publication date from JSON-LD, meta tags, or <time> elements."""
    # JSON-LD first — most reliable on press wires and company IR pages
    for pattern in (_JSONLD_DATE_RE, _DATE_META_RE, _TIME_TAG_RE):
        m = pattern.search(html)
        if m:
            raw = next(g for g in m.groups() if g)
            result = _parse_date_str(raw)
            if result:
                logger.debug("date extracted: %s (raw=%s)", result, raw[:40])
                return result
    logger.debug("no date found in page")
    return None


def _fetch_page(url: str) -> tuple[str, str | None]:
    """Fetch a URL. Returns (stripped_text, published_at_iso_or_None)."""
    try:
        resp = http_requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MoneyGraph/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        published_at = _extract_published_at(resp.text)
        return _strip_html(resp.text), published_at
    except Exception as exc:
        logger.warning("fetch failed %s: %s", url, exc)
        return "", None


def _is_paywalled(text: str) -> bool:
    """Return True if text is too short or matches known paywall markers (E8)."""
    if len(text) < _MIN_PAGE_TEXT_LEN:
        return True
    low = text.lower()
    return any(m in low for m in _PAYWALL_MARKERS)


# ---------------------------------------------------------------------------
# Verbatim excerpt gate (E1)
# ---------------------------------------------------------------------------


def _excerpt_verbatim(excerpt: str, page_text: str) -> bool:
    """Return True if excerpt (whitespace-normalized) is a substring of page_text."""
    norm_e = re.sub(r"\s+", " ", excerpt).strip()
    norm_p = re.sub(r"\s+", " ", page_text)
    return bool(norm_e) and norm_e in norm_p


# ---------------------------------------------------------------------------
# Search: Brave Search API (search_provider.py) → WebResult list
# ---------------------------------------------------------------------------


def search_node(node_name: str) -> list[WebResult]:
    """
    Run the query template(s) for node_name via search_provider.search()
    (Brave Search API, — replaces the OpenAI Responses API web_search
    tool). Deduplicates URLs across templates. Fetches page text for each
    discovered URL ourselves (search_provider only returns url/title/snippet,
    no bundled page fetch). Returns only results that have non-paywalled
    page text.
    """
    seen_urls: set[str] = set()
    results: list[WebResult] = []

    for template in _QUERY_TEMPLATES:
        q = template.format(name=node_name)
        try:
            hits = search_provider.search(q)
        except Exception as exc:
            logger.warning("search_provider.search failed  node=%s  query=%r  error=%s", node_name, q, exc)
            continue

        for hit in hits:
            url = hit.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc or url
            title: str = hit.get("title") or ""
            snippet: str = hit.get("snippet") or ""

            page_text, published_at = _fetch_page(url)
            if _is_paywalled(page_text):
                logger.info("E8 skip (paywall/empty)  %s", url)
                continue

            content_hash = hashlib.sha256(page_text.encode()).hexdigest()
            results.append(
                WebResult(
                    url=url,
                    domain=domain,
                    title=title,
                    snippet=snippet,
                    page_text=page_text,
                    published_at=published_at,
                    content_hash=content_hash,
                )
            )

    logger.info("search_node  node=%s  urls_found=%d", node_name, len(results))
    return results


# ---------------------------------------------------------------------------
# Extract events from a single WebResult
# ---------------------------------------------------------------------------


def _extract_from_result(result: WebResult) -> list[dict]:
    """Call chat completions on page_text using the web-specific prompt."""
    client = _openai_client()
    try:
        resp = client.chat.completions.create(
            model=_WEB_MODEL,
            max_tokens=2048,
            response_format={"type": "json_object"},
            # Chat Completions API defaults store=False — set explicitly so
            # this response is retrievable later (forward safety net).
            store=True,
            messages=[
                {"role": "system", "content": WEB_SYSTEM_PROMPT},
                {"role": "user", "content": build_web_user_content(result.page_text, result.url)},
            ],
        )
        logger.info(
            "web extract  tokens in=%d out=%d  %s",
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            result.url,
        )
        _log_openai_response(
            "chat.completions",
            _WEB_MODEL,
            resp.id,
            {"url": result.url, "domain": result.domain},
        )
        raw = resp.choices[0].message.content or ""
        return parse_extraction_response(raw)
    except Exception as exc:
        logger.warning("extraction failed  %s: %s", result.url, exc)
        return []


# ---------------------------------------------------------------------------
# Gate application + write
# ---------------------------------------------------------------------------


def _process_web_result(
    result: WebResult,
    run_id: str,
) -> tuple[int, int, int]:
    """Apply gates then call _process_event for each passing event."""
    events_logged = 0
    candidates_found = 0
    edges_created = 0

    events = _extract_from_result(result)

    source_tier = tier_for_domain(result.domain)
    filing_meta = {
        "form_type": "WEB",
        "url": result.url,
        "date": result.published_at,
        "discovery_source": "web",
        "source_tier": source_tier,
        "source_name": result.domain,
    }

    passing_events = []
    for event in events:
        investor = (event.get("investor") or "").strip()
        investee = (event.get("investee") or "").strip()

        # E10: self-reference
        from ...core.resolve import normalize as _norm

        if investor and investee and _norm(investor) == _norm(investee):
            logger.info("E10 drop (self-ref)  investor=%s", investor)
            continue

        # E1: verbatim excerpt gate (most important — anti-hallucination)
        excerpt = (event.get("excerpt") or "").strip()
        if not _excerpt_verbatim(excerpt, result.page_text):
            logger.info(
                "E1 reject (non-verbatim)  url=%s  excerpt=%r",
                result.url,
                excerpt[:120],
            )
            continue

        passing_events.append(event)

    # Detect syndicate-round events within this one page/result BEFORE
    # writing any of them — e.g. one press release naming N co-investors for
    # the same round with no per-investor breakdown (this is exactly how the
    # confirmed Waymo/xAI overcounts entered the DB: one web result, N events).
    syndicate_idxs = _detect_syndicate_indices(passing_events)

    for idx, event in enumerate(passing_events):
        force_reason = "syndicate_total" if idx in syndicate_idxs else None
        logged, candidate, new_edge = _process_event(event, filing_meta, run_id, force_estimate_reason=force_reason)
        if logged:
            events_logged += 1
        if candidate:
            candidates_found += 1
        if new_edge:
            edges_created += 1

    return events_logged, candidates_found, edges_created


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------


def run_websearch_phase(
    run_id: str,
    nodes: list[dict] | None = None,
    stale_days: int | None = None,
    node_timeout_s: float | None = None,
) -> tuple[int, int, int]:
    """
    Run web search for private seed nodes (cik IS NULL).

    nodes: list of {id, name} dicts. If None, queries DB for all private nodes.
    stale_days: skip any node web-searched within this many days (cost cap).
        Defaults to env WEBSEARCH_STALE_DAYS (14). Pass 0 to force a full
        re-search of every node.
    node_timeout_s: per-node wall-clock budget (env WEBSEARCH_NODE_TIMEOUT_S,
        default 90). If a node's processing exceeds it, the rest of that node is
        skipped and the run moves on — one node can never wedge the whole phase.
    Returns (events_logged, candidates_found, edges_created).
    """
    if stale_days is None:
        stale_days = _DEFAULT_STALE_DAYS
    if node_timeout_s is None:
        node_timeout_s = _NODE_TIMEOUT_S
    if nodes is None:
        nodes = query("SELECT id::text, name FROM nodes WHERE cik IS NULL ORDER BY name")
    if not nodes:
        logger.info("no private nodes found — skipping web search phase")
        return 0, 0, 0

    # Filter stale-skips UP FRONT so total_units reflects only real
    # (billed/timed) work — a node skipped for freshness is an instant no-op,
    # not something an ETA should count against.
    work_nodes = []
    skipped = 0
    for node in nodes:
        if _node_recently_searched(node["id"], stale_days):
            skipped += 1
            logger.info("web search skip (fresh <%dd)  node=%s", stale_days, node["name"])
        else:
            work_nodes.append(node)

    set_run_total_units(run_id, len(work_nodes))

    total_events = 0
    total_candidates = 0
    total_edges = 0
    timed_out = 0

    for node in work_nodes:
        node_name = node["name"]

        logger.info("web search  node=%s", node_name)
        node_start = time.monotonic()

        # A search attempt was made for this node — count it as
        # len(_QUERY_TEMPLATES) billed search calls regardless of outcome
        # (search_node's per-template try/except means a raised exception here
        # is an unexpected bug, not evidence the call was never sent/billed).
        # Skipped-as-fresh nodes above never reach this line, so they correctly
        # cost nothing.
        # Units_processed bumped in lockstep with nodes_processed —
        # nodes_processed IS the correct progress numerator for websearch
        # (unconditional per-attempted-node), units_processed just gives the
        # frontend one uniform field name across all three run types.
        bump_run_counters(
            run_id,
            nodes_processed=1,
            units_processed=1,
            search_calls_made=len(_QUERY_TEMPLATES),
        )

        try:
            results = search_node(node_name)
        except Exception as exc:
            # Bounded client means this is a real failure (or a bounded timeout),
            # not an indefinite hang. Stamp + move on so a re-run doesn't wedge
            # on the same node.
            logger.warning("web search errored  node=%s  error=%s — skipping", node_name, exc)
            _mark_node_websearched(node["id"])
            continue

        # Stamp freshness regardless of yield or later timeout — a node that
        # returns nothing (or times out mid-processing) must not be re-searched
        # (and re-billed / re-hung) on every refresh.
        _mark_node_websearched(node["id"])

        node_events = 0
        node_candidates = 0
        node_edges = 0
        for result in results:
            # Per-node wall-clock backstop: one node must never wedge the phase.
            if time.monotonic() - node_start > node_timeout_s:
                timed_out += 1
                logger.warning(
                    "web search node budget %.0fs exceeded — skipping rest of node=%s",
                    node_timeout_s,
                    node_name,
                )
                break

            # E11: URL + content_hash idempotency check
            existing = _get_processed_web_source(result.url)
            if existing and existing["content_hash"] == result.content_hash:
                logger.info("E11 skip (unchanged)  %s", result.url)
                continue

            ev, cand, edges = _process_web_result(result, run_id)
            total_events += ev
            total_candidates += cand
            total_edges += edges
            node_events += ev
            node_candidates += cand
            node_edges += edges

            _upsert_processed_web_source(result.url, result.content_hash, run_id, ev)

        # Live-progress bump — once per node (results already gated by
        # E11 idempotency above), same reconciliation pattern as pipeline.py.
        if node_events or node_candidates or node_edges:
            bump_run_counters(
                run_id,
                events_logged=node_events,
                candidates_found=node_candidates,
                edges_created=node_edges,
            )

    logger.info(
        "web search phase done  events=%d candidates=%d edges=%d  nodes_searched=%d skipped=%d timed_out=%d",
        total_events,
        total_candidates,
        total_edges,
        len(work_nodes),
        skipped,
        timed_out,
    )
    return total_events, total_candidates, total_edges
