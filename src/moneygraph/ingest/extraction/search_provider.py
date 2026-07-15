"""
Generic web-search backend for websearch discovery.

Replaces the OpenAI Responses API `web_search_preview` tool ($0.025/call,
the large majority of per-run websearch cost) with Brave
Search's free-tier REST API. Brave was chosen over raw Google/Bing scraping
(ToS risk, brittle) because it has a genuine free tier (2,000 queries/month
at the time of writing, 1 query/sec) and a simple JSON REST interface —
same "pick the free option that works, document why" pattern already used
for Wikidata (enrichment.py) and EDGAR.

NOTE: no live API key was available while writing this module.
The response shape below is built from Brave's documented Web Search API
contract (`web.results[].{url,title,description,...}`) and is written
defensively — unexpected shapes are handled gracefully, not assumed. Live
verification against a real key is still needed.

Public:
  search(query: str) -> list[dict]   # each: {url, title, snippet}

Fails gracefully (empty list + logged warning) on: missing/invalid API key,
HTTP errors, timeouts, and unexpected response shapes — mirrors how
enrichment.py's `_wikidata_get` degrades to None on failure rather than
raising, so a search-provider outage skips a node instead of crashing a run.
"""

from __future__ import annotations

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# Brave's `description` field carries inline highlight markup (<strong>...
# </strong> around matched terms) — strip it since nothing downstream
# consumes HTML (WebResult.snippet is currently unused metadata; the actual
# extraction input is the fetched page_text, not this snippet).
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_BRAVE_SEARCH_API = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 10
_MAX_RESULTS = 10


def _api_key() -> str | None:
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    return key or None


def search(query: str) -> list[dict]:
    """
    Run `query` against the Brave Search API. Returns a list of
    {"url": str, "title": str, "snippet": str} dicts, best-effort ordered
    by relevance (Brave's own ranking). Returns [] on any failure — missing
    key, HTTP error, timeout, or a response shape that doesn't match the
    documented contract — logging a clear reason each time so a run degrades
    (skips discovery for that query) instead of crashing.
    """
    key = _api_key()
    if key is None:
        logger.warning(
            "search_provider: BRAVE_SEARCH_API_KEY not set — skipping search  query=%r",
            query,
        )
        return []

    try:
        resp = requests.get(
            _BRAVE_SEARCH_API,
            params={"q": query, "count": _MAX_RESULTS},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 401:
            logger.warning(
                "search_provider: Brave API rejected the key (401) — check BRAVE_SEARCH_API_KEY  query=%r",
                query,
            )
        elif status == 429:
            logger.warning(
                "search_provider: Brave API rate limit hit (429)  query=%r",
                query,
            )
        else:
            logger.warning("search_provider: Brave API HTTP error %s  query=%r", status, query)
        return []
    except Exception as exc:
        logger.warning("search_provider: request failed  query=%r  error=%s", query, exc)
        return []

    if not isinstance(data, dict):
        logger.warning("search_provider: unexpected response shape (not a dict)  query=%r", query)
        return []

    web = data.get("web")
    if not isinstance(web, dict):
        logger.info("search_provider: no 'web' results block  query=%r", query)
        return []

    raw_results = web.get("results")
    if not isinstance(raw_results, list):
        logger.info("search_provider: no results list  query=%r", query)
        return []

    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        description = item.get("description") or ""
        results.append(
            {
                "url": url,
                "title": item.get("title") or "",
                "snippet": _HTML_TAG_RE.sub("", description),
            }
        )

    logger.info("search_provider: query=%r  results=%d", query, len(results))
    return results
