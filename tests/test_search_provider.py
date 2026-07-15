"""
Unit tests for search_provider.py (— Brave Search discovery backend).

NO real HTTP calls — requests.get is mocked everywhere (same pattern as
test_enrichment.py's mocked Wikidata calls).
Covers:
  - missing API key: graceful no-op (empty list, no request made).
  - successful search: results parsed into {url, title, snippet} dicts,
    HTML highlight markup stripped from the snippet.
  - HTTP error (401 / 429 / other): graceful empty list, no raise.
  - unexpected response shapes (not a dict / no 'web' key / no results list):
    graceful empty list.
"""

from unittest.mock import MagicMock, patch

import requests

from moneygraph.ingest.extraction import search_provider as sp


def _env_with_key(monkeypatch, key="test-key-123"):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", key)


# ---------------------------------------------------------------------------
# Missing / blank API key
# ---------------------------------------------------------------------------


def test_search_returns_empty_when_key_missing(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with patch.object(sp.requests, "get") as get_mock:
        results = sp.search("Acme Corp investment")
    assert results == []
    get_mock.assert_not_called()


def test_search_returns_empty_when_key_blank(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "   ")
    with patch.object(sp.requests, "get") as get_mock:
        results = sp.search("Acme Corp investment")
    assert results == []
    get_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Successful search
# ---------------------------------------------------------------------------


def test_search_success_parses_results(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {
        "web": {
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "Example A",
                    "description": "Acme raised <strong>$10M</strong> from Foo.",
                },
                {
                    "url": "https://example.com/b",
                    "title": "Example B",
                    "description": "No highlight here.",
                },
            ]
        }
    }
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp) as get_mock:
        results = sp.search("Acme Corp investment")

    assert len(results) == 2
    assert results[0] == {
        "url": "https://example.com/a",
        "title": "Example A",
        "snippet": "Acme raised $10M from Foo.",
    }
    assert results[1]["snippet"] == "No highlight here."

    # key sent as header, not query param
    _, kwargs = get_mock.call_args
    assert kwargs["headers"]["X-Subscription-Token"] == "test-key-123"
    assert kwargs["params"]["q"] == "Acme Corp investment"


def test_search_skips_result_missing_url(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"web": {"results": [{"title": "No URL", "description": "x"}]}}
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


def test_search_handles_401(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.status_code = 401
    err = requests.exceptions.HTTPError(response=fake_resp)
    fake_resp.raise_for_status.side_effect = err

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


def test_search_handles_429(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    err = requests.exceptions.HTTPError(response=fake_resp)
    fake_resp.raise_for_status.side_effect = err

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


def test_search_handles_generic_exception(monkeypatch):
    _env_with_key(monkeypatch)
    with patch.object(sp.requests, "get", side_effect=RuntimeError("network down")):
        results = sp.search("query")

    assert results == []


# ---------------------------------------------------------------------------
# Unexpected response shapes
# ---------------------------------------------------------------------------


def test_search_handles_non_dict_response(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = ["not", "a", "dict"]
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


def test_search_handles_missing_web_key(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"unexpected": "shape"}
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


def test_search_handles_missing_results_list(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"web": {"no_results_key": True}}
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []


def test_search_returns_empty_list_when_no_results(monkeypatch):
    _env_with_key(monkeypatch)
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"web": {"results": []}}
    fake_resp.raise_for_status.return_value = None

    with patch.object(sp.requests, "get", return_value=fake_resp):
        results = sp.search("query")

    assert results == []
