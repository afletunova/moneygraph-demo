"""
EDGAR API client.

Two-stage caching:
  1. Submissions index (data.sec.gov/submissions/CIK{}.json) — refreshed at most
     once per `submissions_max_age_days` (default: snapshot_frequency_days = 7).
  2. Individual filing documents — write-once; cache hit = no re-download.

Cache layout on disk:
  /app/data/cache/{cik}/submissions.json
  /app/data/cache/{cik}/{form_type_safe}/{accession}.{ext}

Rate limit: 10 req/s per EDGAR fair-use policy; enforced via 120ms inter-request delay.
"""

import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{}.json"
_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
_RATE_LIMIT = 0.12  # seconds

# In scope: 8-K and SC 13D/13G.
# 10-K footnotes, 10-Q, 13F-HR are not fetched yet.
FETCH_FORM_TYPES = {"8-K", "SC 13D", "SC 13G"}

_DATA_DIR = Path("/app/data")
_last_req: float = 0.0


def _get(url: str) -> requests.Response:
    global _last_req
    gap = _RATE_LIMIT - (time.monotonic() - _last_req)
    if gap > 0:
        time.sleep(gap)
    resp = requests.get(
        url,
        headers={"User-Agent": os.environ.get("EDGAR_USER_AGENT", "")},
        timeout=30,
    )
    _last_req = time.monotonic()
    resp.raise_for_status()
    return resp


def _cache_dir(cik: str, form_type: str | None = None) -> Path:
    base = _DATA_DIR / "cache" / cik
    p = base / form_type.replace(" ", "_") if form_type else base
    p.mkdir(parents=True, exist_ok=True)
    return p


def fetch_submissions(cik: str, max_age_days: int = 7) -> dict:
    """
    Return the EDGAR submissions JSON for a CIK.

    Uses the cached file if it exists and is younger than max_age_days.
    Otherwise fetches from data.sec.gov and overwrites the cache.
    """
    cache = _cache_dir(cik) / "submissions.json"
    if cache.exists():
        age_days = (time.time() - cache.stat().st_mtime) / 86400
        if age_days < max_age_days:
            logger.info("submissions cache hit  CIK %s (%.1fd old)", cik, age_days)
            return json.loads(cache.read_text(encoding="utf-8"))

    url = _SUBMISSIONS_URL.format(cik.zfill(10))
    data = _get(url).json()
    cache.write_text(json.dumps(data), encoding="utf-8")
    logger.info("submissions fetched   CIK %s", cik)
    return data


def recent_filings(
    cik: str,
    lookback_days: int,
    submissions_max_age_days: int = 7,
) -> list[dict]:
    """
    Return filing metadata for FETCH_FORM_TYPES within the lookback window.

    Reads only the 'recent' array in submissions.json (~40 most recent filings),
    which is sufficient for a weekly pipeline with a ≤30-day lookback.
    """
    data = fetch_submissions(cik, max_age_days=submissions_max_age_days)
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    cutoff = date.today() - timedelta(days=lookback_days)

    results = []
    for i, form in enumerate(forms):
        if form not in FETCH_FORM_TYPES:
            continue
        try:
            if date.fromisoformat(dates[i]) < cutoff:
                continue
        except (ValueError, IndexError):
            continue
        results.append(
            {
                "form": form,
                "filing_date": dates[i],
                "accession_number": accns[i] if i < len(accns) else None,
                "primary_document": docs[i] if i < len(docs) else None,
                "cik": cik,
            }
        )

    return results


def download_filing(
    cik: str,
    form_type: str,
    accession_number: str,
    primary_document: str,
) -> str | None:
    """
    Download and cache the primary document for a filing.

    Stored at: data/cache/{cik}/{form_type_safe}/{accession}.{ext}
    Original file extension is preserved (raw HTML/XML — not converted to text).
    Returns cached content on subsequent calls (write-once).
    Returns None on network/HTTP error (logged, not raised).
    """
    acc = accession_number.replace("-", "")
    ext = Path(primary_document).suffix.lstrip(".") or "htm"
    cache = _cache_dir(cik, form_type) / f"{acc}.{ext}"

    if cache.exists():
        logger.info("filing cache hit      CIK %s  %s", cik, acc)
        return cache.read_text(encoding="utf-8", errors="replace")

    url = _FILING_URL.format(cik=int(cik), accession=acc, doc=primary_document)
    try:
        text = _get(url).text
        cache.write_text(text, encoding="utf-8")
        return text
    except requests.HTTPError as exc:
        logger.warning("HTTP %s fetching %s", exc.response.status_code, url)
        return None
    except Exception as exc:
        logger.warning("download failed for %s: %s", url, exc)
        return None
