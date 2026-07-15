"""
Unit tests for the SEC ticker bulk-lookup (app/ticker_lookup.py,).

`_fetch_raw` (the only function that touches network/disk) is patched
directly in every test, so these never hit the real SEC endpoint and never
touch the filesystem — matching the mocked-HTTP convention used throughout
this test suite (see test_enrichment.py).
"""

import moneygraph.core.ticker_lookup as ticker_lookup
from moneygraph.core.ticker_lookup import lookup_ticker

_RAW = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}


def _reset_index_cache():
    ticker_lookup._index_cache = None


def test_lookup_ticker_exact_match(monkeypatch):
    _reset_index_cache()
    monkeypatch.setattr(ticker_lookup, "_fetch_raw", lambda **kw: _RAW)
    assert lookup_ticker("Apple Inc.") == "AAPL"
    # normalize() strips the corporate suffix — a bare "Nvidia" should still
    # exact-match "NVIDIA CORP" once both sides are normalized.
    assert lookup_ticker("Nvidia") == "NVDA"


def test_lookup_ticker_fuzzy_match_within_distance(monkeypatch):
    _reset_index_cache()
    monkeypatch.setattr(ticker_lookup, "_fetch_raw", lambda **kw: _RAW)
    # "Alphabet" (missing trailing "t" typo-adjacent) — distance 1 from
    # normalized "alphabet" via a single-char edit ("alphabe" -> "alphabet").
    assert lookup_ticker("Alphabe") == "GOOGL"


def test_lookup_ticker_no_confident_match_stays_none(monkeypatch):
    _reset_index_cache()
    monkeypatch.setattr(ticker_lookup, "_fetch_raw", lambda **kw: _RAW)
    assert lookup_ticker("Completely Unrelated Widget Co") is None


def test_lookup_ticker_empty_name_returns_none(monkeypatch):
    _reset_index_cache()
    monkeypatch.setattr(ticker_lookup, "_fetch_raw", lambda **kw: _RAW)
    assert lookup_ticker("") is None


def test_lookup_ticker_handles_fetch_failure_gracefully(monkeypatch):
    _reset_index_cache()

    def _boom(**kw):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(ticker_lookup, "_fetch_raw", _boom)
    assert lookup_ticker("Apple Inc.") is None


def test_lookup_ticker_index_is_memoized(monkeypatch):
    _reset_index_cache()
    calls = {"n": 0}

    def _counting_fetch(**kw):
        calls["n"] += 1
        return _RAW

    monkeypatch.setattr(ticker_lookup, "_fetch_raw", _counting_fetch)
    lookup_ticker("Apple Inc.")
    lookup_ticker("NVIDIA CORP")
    assert calls["n"] == 1
