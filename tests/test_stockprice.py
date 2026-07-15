"""
Unit tests for stock price widget data (app/stockprice.py).

Data source: Yahoo Finance's unofficial chart endpoint (see module docstring
for why — Stooq's free CSV endpoint is now behind a JS challenge page).
All HTTP calls are mocked — no live/paid network calls. DB cache reads/writes
(query/execute) are mocked too — no live DB.

Bucket == UI range itself ('1d'/'1m'/'1y'/'5y'/'max'), each fetched with
Yahoo's own `range` param rather than sliced from one shared "max" series —
see module docstring for why slicing was dropped (Yahoo coarsens `range=max`
to ~168 points, which would starve 1y/5y of real daily granularity).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import moneygraph.core.stockprice as sp


def _resp(json_data):
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = json_data
    return m


_CHART_OK = {
    "chart": {
        "result": [
            {
                "timestamp": [1700000000, 1700086400, 1700172800],
                "indicators": {
                    "quote": [
                        {
                            "close": [10.0, 11.0, 12.0],
                            "open": [9.5, 10.5, 11.5],
                            "high": [10.5, 11.5, 12.5],
                            "low": [9.0, 10.0, 11.0],
                            "volume": [100, 200, 300],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}

_CHART_NOT_FOUND = {
    "chart": {
        "result": None,
        "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"},
    }
}

_CHART_WITH_GAP = {
    "chart": {
        "result": [
            {
                "timestamp": [1700000000, 1700086400],
                "indicators": {
                    "quote": [
                        {
                            "close": [10.0, None],  # market-closed gap Yahoo pads with null
                            "open": [9.5, None],
                            "high": [10.5, None],
                            "low": [9.0, None],
                            "volume": [100, None],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


# ---------------------------------------------------------------------------
# yahoo_symbol — exchange -> Yahoo suffix mapping
#
# Every suffix below was verified live against the real Yahoo chart endpoint
# on 2026-07-11 (see module docstring): HKG/.HK (Alibaba 9988.HK vs the
# DIFFERENT NYSE ADR BABA), LON/.L (HSBA.L), TYO/.T (7203.T), SHA/.SS
# (600519.SS), SHE/.SZ (000002.SZ), OTCMKTS/no-suffix (TYIDY).
# ---------------------------------------------------------------------------


def test_yahoo_symbol_hkg_suffix():
    assert sp.yahoo_symbol("9988", "HKG") == "9988.HK"


def test_yahoo_symbol_london_suffix():
    assert sp.yahoo_symbol("HSBA", "LON") == "HSBA.L"


def test_yahoo_symbol_tokyo_suffix():
    assert sp.yahoo_symbol("7203", "TYO") == "7203.T"


def test_yahoo_symbol_shanghai_suffix():
    assert sp.yahoo_symbol("600519", "SHA") == "600519.SS"


def test_yahoo_symbol_shenzhen_suffix():
    assert sp.yahoo_symbol("000002", "SHE") == "000002.SZ"


def test_yahoo_symbol_frankfurt_xetra_suffix():
    # UAT 2026-07-13: BMW Group had "no price data" because ETR was missing
    # from the map entirely (not the node_tickers backfill gap, a separate
    # bug fixed in main.py's node-migration).
    assert sp.yahoo_symbol("BMW", "ETR") == "BMW.DE"


def test_yahoo_symbol_frankfurt_floor_suffix():
    # FRA (Frankfurt floor) is distinct from ETR (Xetra) — different suffix,
    # confirmed live 2026-07-13 (Samsung Electronics trades under both).
    assert sp.yahoo_symbol("SSU", "FRA") == "SSU.F"


def test_yahoo_symbol_nyse_no_suffix():
    # Added explicitly 2026-07-13 alongside ETR/FRA — previously NYSE fell
    # through to the unmapped-exchange fallback and happened to produce the
    # right answer by accident (bare ticker), now verified and intentional.
    assert sp.yahoo_symbol("MS", "NYSE") == "MS"


def test_yahoo_symbol_otcmkts_no_suffix():
    assert sp.yahoo_symbol("TYIDY", "OTCMKTS") == "TYIDY"


def test_yahoo_symbol_empty_exchange_no_suffix():
    # '' is this codebase's node_tickers sentinel for "no exchange qualifier"
    # (the pre-existing, still-common bare-ticker case).
    assert sp.yahoo_symbol("AAPL", "") == "AAPL"


def test_yahoo_symbol_none_exchange_no_suffix():
    assert sp.yahoo_symbol("AAPL", None) == "AAPL"


def test_yahoo_symbol_unmapped_exchange_falls_back_to_bare_ticker():
    # A genuinely unmapped exchange must NOT guess a suffix (a wrong suffix
    # could silently fetch a different company's price) — falls back to the
    # bare ticker and logs a warning instead.
    assert sp.yahoo_symbol("XYZ", "ASX") == "XYZ"


def test_yahoo_symbol_case_insensitive():
    assert sp.yahoo_symbol("9988", "hkg") == "9988.HK"


# ---------------------------------------------------------------------------
# _fetch_yahoo — parsing + gap handling + not-found
# ---------------------------------------------------------------------------


def test_fetch_yahoo_happy_path():
    with patch("moneygraph.core.stockprice.requests.get", return_value=_resp(_CHART_OK)):
        data = sp._fetch_yahoo("AAPL", "1y", "1d")
    assert data is not None
    assert len(data["points"]) == 3
    assert data["points"][0]["close"] == 10.0
    assert "fetched_at" in data


def test_fetch_yahoo_not_found_returns_none():
    with patch("moneygraph.core.stockprice.requests.get", return_value=_resp(_CHART_NOT_FOUND)):
        assert sp._fetch_yahoo("NOTAREALTICKER", "1y", "1d") is None


def test_fetch_yahoo_skips_null_close_gaps():
    with patch("moneygraph.core.stockprice.requests.get", return_value=_resp(_CHART_WITH_GAP)):
        data = sp._fetch_yahoo("AAPL", "1d", "5m")
    assert len(data["points"]) == 1  # the null-close row was dropped


def test_fetch_yahoo_http_error_returns_none():
    with patch("moneygraph.core.stockprice.requests.get", side_effect=Exception("boom")):
        assert sp._fetch_yahoo("AAPL", "1y", "1d") is None


# ---------------------------------------------------------------------------
# cache helpers — hit / miss
# ---------------------------------------------------------------------------


def test_cache_get_no_row():
    with patch.object(sp, "query", return_value=[]):
        data, age = sp._cache_get("AAPL", "1y")
    assert data is None
    assert age == float("inf")


def test_cache_get_returns_age():
    fetched = datetime.now(timezone.utc) - timedelta(seconds=30)
    with patch.object(sp, "query", return_value=[{"data": {"points": []}, "fetched_at": fetched}]):
        data, age = sp._cache_get("AAPL", "1y")
    assert data == {"points": []}
    assert 25 < age < 40


# ---------------------------------------------------------------------------
# get_price_history — per-range Yahoo params, cache hit/miss/stale-fallback,
# graceful "no data"
# ---------------------------------------------------------------------------


def test_get_price_history_uses_fresh_cache_within_ttl():
    cached = {"points": [{"close": 1}]}
    with patch.object(sp, "_cache_get", return_value=(cached, 10)):
        with patch.object(sp, "_fetch_yahoo") as fetch:
            result = sp.get_price_history("AAPL", "1y")
    fetch.assert_not_called()
    assert result == {"ticker": "AAPL", "range": "1y", "points": [{"close": 1}], "stale": False}


def test_get_price_history_refetches_when_stale_and_caches_result():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value={"points": [{"close": 2}]}) as fetch:
            with patch.object(sp, "_cache_put") as put:
                result = sp.get_price_history("AAPL", "1y")
    fetch.assert_called_once_with("AAPL", "1y", "1d")
    put.assert_called_once_with("AAPL", "1y", {"points": [{"close": 2}]})
    assert result["points"] == [{"close": 2}]
    assert result["stale"] is False


def test_get_price_history_falls_back_to_stale_cache_on_fetch_failure():
    stale_cached = {"points": [{"close": 3}]}
    with patch.object(sp, "_cache_get", return_value=(stale_cached, 999999)):
        with patch.object(sp, "_fetch_yahoo", return_value=None):
            result = sp.get_price_history("AAPL", "1y")
    assert result["points"] == [{"close": 3}]
    assert result["stale"] is True  # better than nothing, but flagged


def test_get_price_history_no_data_available_is_graceful():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value=None):
            result = sp.get_price_history("GHOSTCO", "1y")
    assert result == {"ticker": "GHOSTCO", "range": "1y", "points": [], "stale": False}


def test_get_price_history_unknown_range_falls_back_to_1y():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value={"points": []}) as fetch:
            with patch.object(sp, "_cache_put"):
                result = sp.get_price_history("AAPL", "bogus")
    assert result["range"] == "1y"
    fetch.assert_called_once_with("AAPL", "1y", "1d")


def test_get_price_history_1d_uses_intraday_yahoo_params():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value={"points": []}) as fetch:
            with patch.object(sp, "_cache_put"):
                sp.get_price_history("AAPL", "1d")
    fetch.assert_called_once_with("AAPL", "1d", "5m")


def test_get_price_history_5y_uses_5y_daily_yahoo_params():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value={"points": []}) as fetch:
            with patch.object(sp, "_cache_put"):
                sp.get_price_history("AAPL", "5y")
    fetch.assert_called_once_with("AAPL", "5y", "1d")


def test_get_price_history_max_uses_max_yahoo_range():
    with patch.object(sp, "_cache_get", return_value=(None, float("inf"))):
        with patch.object(sp, "_fetch_yahoo", return_value={"points": []}) as fetch:
            with patch.object(sp, "_cache_put"):
                sp.get_price_history("AAPL", "max")
    fetch.assert_called_once_with("AAPL", "max", "1d")
