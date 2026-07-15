"""
Unit tests for entity enrichment (app/enrichment.py).

All HTTP calls are mocked — these test parsing/merge/disambiguation logic,
not live Wikidata/EDGAR responses. No live DB, no network.
"""

from unittest.mock import MagicMock, patch

import pytest

from moneygraph.core.enrichment import (
    _search_candidates,
    enrich,
    fetch_edgar_facts,
    fetch_wikidata_facts,
)


@pytest.fixture(autouse=True)
def _no_real_ticker_lookup(monkeypatch):
    """Enrich now attaches a ticker for is_public candidates via
    ticker_lookup.lookup_ticker() (SEC bulk-file match). Stub it out to a
    no-op for every test in this file by default (no network, no on-disk
    index build) — the ticker-specific tests below override this per-test
    to exercise the actual attach/skip behaviour."""
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker", lambda name: None)


def _resp(json_data):
    """Fake requests.Response: .raise_for_status() no-ops, .json() returns json_data."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = json_data
    return m


# ---------------------------------------------------------------------------
# fetch_wikidata_facts() — happy path
# ---------------------------------------------------------------------------

_SEARCH_APPLE = {"search": [{"id": "Q312", "label": "Apple Inc."}]}

_ENTITY_APPLE_CLAIMS = {
    "entities": {
        "Q312": {
            "id": "Q312",
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q891723"}}}}],  # public company
                "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1976-04-01T00:00:00Z"}}}}],
                "P159": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q95"}}}}],
                "P452": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q11661"}}}}],
                "P414": [{"mainsnak": {"datavalue": {"value": {"id": "Q13677"}}}}],
                # Org's own P17 deliberately different from the HQ entity's P17
                # below, so the happy-path test proves the HQ-entity value wins.
                "P17": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q999999"}}}}],
            },
            "descriptions": {"en": {"value": "American technology company"}},
        }
    }
}

_LABEL_CUPERTINO_WITH_COUNTRY = {
    "entities": {
        "Q95": {
            "labels": {"en": {"value": "Cupertino"}},
            "claims": {
                "P17": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q30"}}}}],
            },
        }
    }
}
_LABEL_SOFTWARE_INDUSTRY = {"entities": {"Q11661": {"labels": {"en": {"value": "consumer electronics"}}}}}
_LABEL_USA = {"entities": {"Q30": {"labels": {"en": {"value": "United States of America"}}}}}


def _apple_side_effect(url, params=None, headers=None, timeout=None):
    action = params.get("action")
    if action == "wbsearchentities":
        return _resp(_SEARCH_APPLE)
    if action == "wbgetentities":
        ids = params.get("ids", "")
        if ids == "Q312":
            return _resp(_ENTITY_APPLE_CLAIMS)
        if ids == "Q95":
            return _resp(_LABEL_CUPERTINO_WITH_COUNTRY)
        if ids == "Q11661":
            return _resp(_LABEL_SOFTWARE_INDUSTRY)
        if ids == "Q30":
            return _resp(_LABEL_USA)
    raise AssertionError(f"unexpected call: {params}")


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_happy_path(mock_get):
    mock_get.side_effect = _apple_side_effect
    facts = fetch_wikidata_facts("Apple Inc.")
    assert facts is not None
    assert facts["wikidata_qid"] == "Q312"
    assert facts["founded"] == 1976
    assert facts["sector"] == "consumer electronics"
    assert facts["headquarters"] == "Cupertino"
    assert facts["is_public"] is True
    assert facts["short_description"] == "American technology company"
    assert facts["source"] == "wikidata"
    # HQ entity's own P17 (Q30 -> "United States of America") wins
    # over the org's own P17 (Q999999, deliberately unresolved/different).
    assert facts["country"] == "United States of America"


# ---------------------------------------------------------------------------
# Fetch_wikidata_facts — falls back to org's own P17 when the HQ
# entity has no P17 of its own
# ---------------------------------------------------------------------------

_ENTITY_FALLBACKCO_CLAIMS = {
    "entities": {
        "Q400": {
            "id": "Q400",
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],  # business
                "P159": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q77"}}}}],
                "P17": [{"mainsnak": {"datavalue": {"value": {"entity-type": "item", "id": "Q183"}}}}],
            },
        }
    }
}
# HQ entity has a label but NO P17 claim of its own.
_LABEL_HQ_NO_COUNTRY = {"entities": {"Q77": {"labels": {"en": {"value": "Some City"}}, "claims": {}}}}
_LABEL_GERMANY = {"entities": {"Q183": {"labels": {"en": {"value": "Germany"}}}}}


def _fallbackco_side_effect(url, params=None, headers=None, timeout=None):
    action = params.get("action")
    if action == "wbsearchentities":
        return _resp({"search": [{"id": "Q400", "label": "FallbackCo"}]})
    if action == "wbgetentities":
        ids = params.get("ids", "")
        if ids == "Q400":
            return _resp(_ENTITY_FALLBACKCO_CLAIMS)
        if ids == "Q77":
            return _resp(_LABEL_HQ_NO_COUNTRY)
        if ids == "Q183":
            return _resp(_LABEL_GERMANY)
    raise AssertionError(f"unexpected call: {params}")


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_country_falls_back_to_org_p17(mock_get):
    mock_get.side_effect = _fallbackco_side_effect
    facts = fetch_wikidata_facts("FallbackCo")
    assert facts is not None
    assert facts["country"] == "Germany"


# ---------------------------------------------------------------------------
# Fetch_wikidata_facts — no P159 and no org P17 -> country stays None
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_country_none_when_unresolvable(mock_get):
    mock_get.side_effect = _privateco_side_effect
    facts = fetch_wikidata_facts("PrivateCo")
    assert facts is not None
    assert facts["country"] is None


# ---------------------------------------------------------------------------
# fetch_wikidata_facts() — disambiguation guard: non-business QID rejected
# ---------------------------------------------------------------------------

_SEARCH_ARM = {"search": [{"id": "Q999", "label": "Arm (anatomy)"}]}

_ENTITY_ARM_NOT_BUSINESS = {
    "entities": {
        "Q999": {
            "id": "Q999",
            "claims": {
                # P31 = human body part, not in the business allowlist
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q841423"}}}}],
            },
            "descriptions": {"en": {"value": "upper limb of the human body"}},
        }
    }
}


def _arm_side_effect(url, params=None, headers=None, timeout=None):
    action = params.get("action")
    if action == "wbsearchentities":
        return _resp(_SEARCH_ARM)
    if action == "wbgetentities":
        return _resp(_ENTITY_ARM_NOT_BUSINESS)
    raise AssertionError(f"unexpected call: {params}")


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_rejects_non_business_qid(mock_get):
    mock_get.side_effect = _arm_side_effect
    facts = fetch_wikidata_facts("Arm")
    assert facts is None


# ---------------------------------------------------------------------------
# fetch_wikidata_facts() — total API failure → None
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_api_failure_returns_none(mock_get):
    mock_get.side_effect = ConnectionError("network unreachable")
    facts = fetch_wikidata_facts("Whatever Corp")
    assert facts is None


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_no_search_results_returns_none(mock_get):
    mock_get.return_value = _resp({"search": []})
    facts = fetch_wikidata_facts("Zybertronics LLC")
    assert facts is None


# ---------------------------------------------------------------------------
# _search_candidates — bug fix: legal-suffix mismatch
#
# wbsearchentities does near-exact label matching; a raw name carrying a
# corporate suffix ("Klarna Inc.") reliably misses Wikidata's suffix-free
# label ("Klarna"). Confirmed live while investigating a 0/104 candidate
# backfill run. _search_candidates now retries
# with the suffix stripped (resolve.py's _SUFFIX_RE, case preserved) only
# when the raw search comes back empty.
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.requests.get")
def test_search_candidates_falls_back_to_suffix_stripped_name(mock_get):
    calls = []

    def side_effect(url, params=None, headers=None, timeout=None):
        calls.append(params["search"])
        if params["search"] == "Klarna Inc.":
            return _resp({"search": []})
        if params["search"] == "Klarna":
            return _resp({"search": [{"id": "Q1234567", "label": "Klarna"}]})
        raise AssertionError(f"unexpected search term: {params['search']}")

    mock_get.side_effect = side_effect
    qids = _search_candidates("Klarna Inc.")
    assert qids == ["Q1234567"]
    assert calls == ["Klarna Inc.", "Klarna"]  # raw first, stripped fallback second


@patch("moneygraph.core.enrichment.requests.get")
def test_search_candidates_no_retry_when_raw_name_already_hits(mock_get):
    def side_effect(url, params=None, headers=None, timeout=None):
        assert params["search"] == "Apple Inc."  # only one call expected — no fallback
        return _resp({"search": [{"id": "Q312", "label": "Apple Inc."}]})

    mock_get.side_effect = side_effect
    qids = _search_candidates("Apple Inc.")
    assert qids == ["Q312"]


@patch("moneygraph.core.enrichment.requests.get")
def test_search_candidates_both_searches_empty_returns_empty(mock_get):
    mock_get.return_value = _resp({"search": []})
    qids = _search_candidates("Zybertronics LLC")
    assert qids == []


@patch("moneygraph.core.enrichment.requests.get")
def test_search_candidates_no_fallback_when_no_suffix_to_strip(mock_get):
    """A name with no recognizable corporate suffix (e.g. a bare person/brand
    name) shouldn't trigger a second, identical search call."""
    calls = []

    def side_effect(url, params=None, headers=None, timeout=None):
        calls.append(params["search"])
        return _resp({"search": []})

    mock_get.side_effect = side_effect
    qids = _search_candidates("Zybertronics")
    assert qids == []
    assert calls == ["Zybertronics"]  # no second call — nothing to strip


# ---------------------------------------------------------------------------
# _wikidata_get — observability fix: retry-exhaustion under
# sustained 429s is now logged distinctly from a generic request failure, so
# it's distinguishable after the fact from a confirmed empty search result.
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.logger")
@patch("moneygraph.core.enrichment.time.sleep", return_value=None)
@patch("moneygraph.core.enrichment.requests.get")
def test_wikidata_get_logs_distinct_warning_on_retry_exhaustion(mock_get, mock_sleep, mock_logger):
    from moneygraph.core.enrichment import _wikidata_get

    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    mock_get.return_value = resp

    result = _wikidata_get({"action": "wbsearchentities", "search": "Whatever"})

    assert result is None
    # Last warning call (after retries exhausted) must be the distinct
    # "exhausted ... 429" message, not the generic per-retry warning nor the
    # unrelated "request failed" exception log (which isn't hit at all here —
    # this path returns None directly, without raising).
    warning_msgs = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert any("exhausted" in msg and "429" in msg for msg in warning_msgs)
    mock_logger.exception.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_wikidata_facts() — is_public is None when no P414 (stock exchange) claim
# ---------------------------------------------------------------------------

_SEARCH_PRIVATECO = {"search": [{"id": "Q500", "label": "PrivateCo"}]}

_ENTITY_PRIVATECO_NO_LISTING = {
    "entities": {
        "Q500": {
            "id": "Q500",
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5225895"}}}}],  # privately held company
                "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+2015-06-01T00:00:00Z"}}}}],
                # no P159, no P452, no P414
            },
            "descriptions": {"en": {"value": "a private software company"}},
        }
    }
}


def _privateco_side_effect(url, params=None, headers=None, timeout=None):
    action = params.get("action")
    if action == "wbsearchentities":
        return _resp(_SEARCH_PRIVATECO)
    if action == "wbgetentities":
        return _resp(_ENTITY_PRIVATECO_NO_LISTING)
    raise AssertionError(f"unexpected call: {params}")


@patch("moneygraph.core.enrichment.requests.get")
def test_fetch_wikidata_facts_is_public_none_without_stock_exchange_claim(mock_get):
    mock_get.side_effect = _privateco_side_effect
    facts = fetch_wikidata_facts("PrivateCo")
    assert facts is not None
    assert facts["is_public"] is None
    assert facts["headquarters"] is None
    assert facts["sector"] is None
    assert facts["founded"] == 2015


# ---------------------------------------------------------------------------
# fetch_edgar_facts()
# ---------------------------------------------------------------------------


@patch("moneygraph.ingest.edgar.fetch_submissions")
def test_fetch_edgar_facts_extracts_sic_description(mock_submissions):
    mock_submissions.return_value = {"sicDescription": "Computer Communications Equipment"}
    facts = fetch_edgar_facts("0000320193")
    assert facts == {
        "is_public": True,
        "founded": None,
        "sector": "Computer Communications Equipment",
        "headquarters": None,
        "country": "United States",
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }


# ---------------------------------------------------------------------------
# Fetch_edgar_facts — CIK -> country shortcut
# ---------------------------------------------------------------------------


@patch("moneygraph.ingest.edgar.fetch_submissions")
def test_fetch_edgar_facts_country_united_states_shortcut(mock_submissions):
    mock_submissions.return_value = {
        "sicDescription": "Computer Communications Equipment",
        "addresses": {"business": {"isForeignLocation": 0}},
    }
    facts = fetch_edgar_facts("0000320193")
    assert facts["country"] == "United States"


@patch("moneygraph.ingest.edgar.fetch_submissions")
def test_fetch_edgar_facts_country_none_when_flagged_foreign(mock_submissions):
    mock_submissions.return_value = {
        "sicDescription": "Software",
        "addresses": {"business": {"isForeignLocation": 1}},
    }
    facts = fetch_edgar_facts("0000000003")
    assert facts["country"] is None


@patch("moneygraph.ingest.edgar.fetch_submissions")
def test_fetch_edgar_facts_returns_none_on_failure(mock_submissions):
    mock_submissions.side_effect = Exception("HTTP 404")
    assert fetch_edgar_facts("0000000000") is None


@patch("moneygraph.ingest.edgar.fetch_submissions")
def test_fetch_edgar_facts_returns_none_without_sic_description(mock_submissions):
    mock_submissions.return_value = {}
    assert fetch_edgar_facts("0000320193") is None


# ---------------------------------------------------------------------------
# enrich() — merge precedence: EDGAR wins is_public/sector, Wikidata fills rest
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_merges_edgar_and_wikidata(mock_wd, mock_edgar):
    mock_wd.return_value = {
        "is_public": None,
        "founded": 1976,
        "sector": "consumer electronics",  # should be overridden by EDGAR
        "headquarters": "Cupertino",
        "country": "United States of America",  # should be overridden by EDGAR
        "short_description": "American technology company",
        "wikidata_qid": "Q312",
        "source": "wikidata",
    }
    mock_edgar.return_value = {
        "is_public": True,
        "founded": None,
        "sector": "Computer Communications Equipment",
        "headquarters": None,
        "country": "United States",
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }
    facts = enrich("Apple Inc.", cik="0000320193")
    assert facts["source"] == "both"
    assert facts["is_public"] is True  # EDGAR wins
    assert facts["sector"] == "Computer Communications Equipment"  # EDGAR wins
    assert facts["country"] == "United States"  # EDGAR wins
    assert facts["founded"] == 1976  # filled by Wikidata
    assert facts["headquarters"] == "Cupertino"  # filled by Wikidata
    assert facts["short_description"] == "American technology company"
    assert facts["wikidata_qid"] == "Q312"


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_country_falls_back_to_wikidata_when_edgar_unresolved(mock_wd, mock_edgar):
    """EDGAR wins only when it actually resolves a country (e.g. a
    flagged-foreign filer leaves EDGAR's country None) — Wikidata's answer
    should still come through rather than being clobbered with None."""
    mock_wd.return_value = {
        "is_public": None,
        "founded": None,
        "sector": None,
        "headquarters": "Berlin",
        "country": "Germany",
        "short_description": None,
        "wikidata_qid": "Q500",
        "source": "wikidata",
    }
    mock_edgar.return_value = {
        "is_public": True,
        "founded": None,
        "sector": "Software",
        "headquarters": None,
        "country": None,  # flagged foreign, unresolved
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }
    facts = enrich("ForeignFiler GmbH", cik="0000000009")
    assert facts["country"] == "Germany"


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_no_cik_skips_edgar(mock_wd, mock_edgar):
    mock_wd.return_value = {
        "is_public": None,
        "founded": 2015,
        "sector": None,
        "headquarters": None,
        "country": None,
        "short_description": "A private company",
        "wikidata_qid": "Q1",
        "source": "wikidata",
    }
    facts = enrich("PrivateCo")
    mock_edgar.assert_not_called()
    assert facts["source"] == "wikidata"


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_both_sources_fail_returns_none(mock_wd, mock_edgar):
    mock_wd.return_value = None
    mock_edgar.return_value = None
    assert enrich("Zybertronics LLC", cik="0000000001") is None


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_edgar_only(mock_wd, mock_edgar):
    mock_wd.return_value = None
    mock_edgar.return_value = {
        "is_public": True,
        "founded": None,
        "sector": "Software",
        "headquarters": None,
        "country": "United States",
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }
    facts = enrich("SomeCorp", cik="0000000002")
    assert facts["source"] == "edgar"
    assert facts["sector"] == "Software"
    assert facts["country"] == "United States"


# ---------------------------------------------------------------------------
# Enrich — ticker attach for public companies
# ---------------------------------------------------------------------------


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_attaches_ticker_when_public(mock_wd, mock_edgar, monkeypatch):
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker", lambda name: "AAPL")
    mock_wd.return_value = None
    mock_edgar.return_value = {
        "is_public": True,
        "founded": None,
        "sector": "Software",
        "headquarters": None,
        "country": "United States",
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }
    facts = enrich("Apple Inc.", cik="0000320193")
    assert facts["ticker"] == "AAPL"


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_no_ticker_when_not_public(mock_wd, mock_edgar, monkeypatch):
    # Even if a lookup WOULD return something, is_public isn't True here, so
    # _add_ticker must not even call it / must not attach a ticker key.
    lookups = []

    def _spy(name):
        lookups.append(name)
        return "SHOULD_NOT_APPEAR"

    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker", _spy)
    mock_wd.return_value = {
        "is_public": None,
        "founded": 2015,
        "sector": None,
        "headquarters": None,
        "country": None,
        "short_description": "A private company",
        "wikidata_qid": "Q1",
        "source": "wikidata",
    }
    mock_edgar.return_value = None
    facts = enrich("PrivateCo")
    assert "ticker" not in facts
    assert lookups == []


@patch("moneygraph.core.enrichment.fetch_edgar_facts")
@patch("moneygraph.core.enrichment.fetch_wikidata_facts")
def test_enrich_no_ticker_when_lookup_finds_no_confident_match(mock_wd, mock_edgar, monkeypatch):
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker", lambda name: None)
    mock_wd.return_value = None
    mock_edgar.return_value = {
        "is_public": True,
        "founded": None,
        "sector": "Software",
        "headquarters": None,
        "country": "United States",
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }
    facts = enrich("SomeObscureNewlyPublicCo", cik="0000000099")
    assert "ticker" not in facts
