"""
Unit tests for the websearch cost cap + hang guard +
forward safety net (response id logging) + the Brave Search
discovery backend, which replaces the OpenAI Responses API web_search tool.

NO paid search calls / no real HTTP — search_node / search_provider.search /
the OpenAI client are mocked everywhere.
Covers:
  - _QUERY_TEMPLATES is the restored 5-template strategy (— Brave's
    free tier removed the per-call cost reason it was cut to 1).
  - run_websearch_phase skips nodes web-searched within stale_days, and searches
    (and stamps) stale / never-searched nodes.
  - stale_days=0 disables the skip (forces a full re-search).
  - hang guard: bounded OpenAI client; a search that raises / a node that
    exceeds its wall-clock budget is skipped and the run continues.
  - _extract_from_result sets store=True on the Chat Completions call
    and logs its response id (unchanged by — extraction backend is
    untouched).
  - search_node calls search_provider.search per template, dedupes
    URLs, fetches page text itself, and still applies the E8 paywall gate.
"""

from unittest.mock import MagicMock, patch

import moneygraph.ingest.extraction.pipeline as pipeline
import moneygraph.ingest.extraction.websearch as ws

# ---------------------------------------------------------------------------
# Template count — restored to 5 (Brave free tier, no per-call cost)
# ---------------------------------------------------------------------------


def test_five_query_templates():
    assert len(ws._QUERY_TEMPLATES) == 5


def test_templates_are_formattable():
    # every template must still accept a {name} placeholder
    for template in ws._QUERY_TEMPLATES:
        q = template.format(name="Acme")
        assert "Acme" in q


# ---------------------------------------------------------------------------
# Stale-node skip
# ---------------------------------------------------------------------------

_NODES = [
    {"id": "11111111-1111-1111-1111-111111111111", "name": "FreshCo"},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "StaleCo"},
]


def _run(stale_map, stale_days=14):
    """Run the phase with search_node mocked (0 results) and freshness stubbed.

    stale_map: node_id -> bool (True = recently searched → should skip).
    Returns (search_calls, marked_ids).
    """
    search_calls = []
    marked = []

    def fake_search(name):
        search_calls.append(name)
        return []  # no results — we only care about search vs skip

    def fake_recent(node_id, days):
        assert days == stale_days
        return stale_map.get(node_id, False)

    with (
        patch.object(ws, "search_node", side_effect=fake_search),
        patch.object(ws, "_node_recently_searched", side_effect=fake_recent),
        patch.object(ws, "_mark_node_websearched", side_effect=marked.append),
        patch.object(ws, "bump_run_counters"),
        patch.object(ws, "set_run_total_units"),
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ev, cand, edges = ws.run_websearch_phase("run-1", nodes=_NODES, stale_days=stale_days)

    return search_calls, marked, (ev, cand, edges)


def test_skips_fresh_node_searches_stale():
    fresh_id = _NODES[0]["id"]
    calls, marked, counts = _run({fresh_id: True})
    # FreshCo skipped, StaleCo searched
    assert calls == ["StaleCo"]
    # only the searched node gets stamped
    assert marked == [_NODES[1]["id"]]
    assert counts == (0, 0, 0)


def test_searches_all_when_none_fresh():
    calls, marked, _ = _run({})
    assert calls == ["FreshCo", "StaleCo"]
    assert marked == [_NODES[0]["id"], _NODES[1]["id"]]


def test_stale_days_zero_forces_all():
    # even if both "look" fresh, stale_days=0 disables the skip via _node_recently_searched
    # (which returns False for <=0); here we assert the phase still searches both.
    fresh_both = {_NODES[0]["id"]: True, _NODES[1]["id"]: True}

    def fake_recent(node_id, days):
        # mimic real helper: disabled at <= 0
        if days <= 0:
            return False
        return fresh_both.get(node_id, False)

    calls = []
    with (
        patch.object(ws, "search_node", side_effect=lambda n: calls.append(n) or []),
        patch.object(ws, "_node_recently_searched", side_effect=fake_recent),
        patch.object(ws, "_mark_node_websearched"),
        patch.object(ws, "bump_run_counters"),
        patch.object(ws, "set_run_total_units"),
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ws.run_websearch_phase("run-1", nodes=_NODES, stale_days=0)

    assert calls == ["FreshCo", "StaleCo"]


# ---------------------------------------------------------------------------
# _node_recently_searched — guard on stale_days <= 0
# ---------------------------------------------------------------------------


def test_recently_searched_disabled_at_zero():
    # no DB query should be issued when disabled
    with patch.object(ws, "query") as q:
        assert ws._node_recently_searched("n1", 0) is False
        q.assert_not_called()


def test_recently_searched_reads_fresh_flag():
    with patch.object(ws, "query", return_value=[{"fresh": True}]):
        assert ws._node_recently_searched("n1", 14) is True
    with patch.object(ws, "query", return_value=[{"fresh": False}]):
        assert ws._node_recently_searched("n1", 14) is False


# ---------------------------------------------------------------------------
# Hang guard — bounded OpenAI client
# ---------------------------------------------------------------------------


def test_openai_client_is_bounded():
    with patch.object(ws.openai, "OpenAI") as ctor:
        ws._openai_client()
    _, kwargs = ctor.call_args
    assert kwargs["timeout"] == ws._OPENAI_TIMEOUT_S
    assert kwargs["max_retries"] == ws._OPENAI_MAX_RETRIES
    # worst-case single call must stay under the per-node budget
    assert ws._OPENAI_TIMEOUT_S * (1 + ws._OPENAI_MAX_RETRIES) < ws._NODE_TIMEOUT_S


# ---------------------------------------------------------------------------
# Hang guard — a failing search skips the node and the run continues
# ---------------------------------------------------------------------------


def test_search_error_skips_node_and_continues():
    marked = []
    calls = []

    def fake_search(name):
        calls.append(name)
        if name == "FreshCo":
            raise TimeoutError("simulated hang → bounded timeout")
        return []

    with (
        patch.object(ws, "search_node", side_effect=fake_search),
        patch.object(ws, "_node_recently_searched", return_value=False),
        patch.object(ws, "_mark_node_websearched", side_effect=marked.append),
        patch.object(ws, "bump_run_counters"),
        patch.object(ws, "set_run_total_units"),
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ev, cand, edges = ws.run_websearch_phase("run-1", nodes=_NODES, stale_days=0)

    # both nodes attempted; the failing one did not abort the run
    assert calls == ["FreshCo", "StaleCo"]
    # failing node still stamped (so a re-run doesn't re-hang on it)
    assert marked == [_NODES[0]["id"], _NODES[1]["id"]]
    assert (ev, cand, edges) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Hang guard — per-node wall-clock budget skips the rest of a slow node
# ---------------------------------------------------------------------------


def test_node_budget_skips_remaining_results():
    node = [{"id": "aaaaaaaa-0000-0000-0000-000000000000", "name": "SlowCo"}]

    class _R:
        def __init__(self, u):
            self.url = u
            self.content_hash = "h-" + u

    r1 = _R("u1")
    r2 = _R("u2")
    processed = []

    # monotonic sequence: node_start=0, iter1 check=10 (<90 → process r1),
    # iter2 check=200 (>90 → break before processing r2).
    monotonic_vals = iter([0.0, 10.0, 200.0])

    with (
        patch.object(ws, "search_node", return_value=[r1, r2]),
        patch.object(ws, "_node_recently_searched", return_value=False),
        patch.object(ws, "_mark_node_websearched"),
        patch.object(ws, "_get_processed_web_source", return_value=None),
        patch.object(ws, "_process_web_result", side_effect=lambda r, rid: processed.append(r) or (0, 0, 0)),
        patch.object(ws, "_upsert_processed_web_source"),
        patch.object(ws.time, "monotonic", side_effect=lambda: next(monotonic_vals)),
        patch.object(ws, "bump_run_counters"),
        patch.object(ws, "set_run_total_units"),
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ws.run_websearch_phase("run-1", nodes=node, stale_days=0, node_timeout_s=90)

    # only the first result was processed; the budget broke the loop before r2
    assert processed == [r1]


# ---------------------------------------------------------------------------
# Search_node on the Brave Search backend (search_provider.py)
# ---------------------------------------------------------------------------


def test_search_node_calls_search_provider_and_fetches_pages():
    fake_hits = [
        {"url": "https://example.com/a", "title": "A", "snippet": "snip a"},
        {"url": "https://example.com/b", "title": "B", "snippet": "snip b"},
    ]
    with (
        patch.object(ws.search_provider, "search", return_value=fake_hits) as search_mock,
        patch.object(ws, "_fetch_page", return_value=("x" * 500, "2026-01-01")),
    ):
        results = ws.search_node("Acme Corp")

    # One call per query template (5 templates), each formatted with the node name
    assert search_mock.call_count == len(ws._QUERY_TEMPLATES)
    for (query_arg,), _ in search_mock.call_args_list:
        assert "Acme Corp" in query_arg

    # same two URLs come back from every template — dedup collapses them to one hit each
    assert [r.url for r in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0].title == "A"
    assert results[0].snippet == "snip a"
    assert results[0].domain == "example.com"


def test_search_node_dedupes_urls_across_templates():
    # same URL returned twice (e.g. two query templates) must only appear once
    dup_hits = [
        {"url": "https://example.com/a", "title": "A", "snippet": ""},
        {"url": "https://example.com/a", "title": "A dup", "snippet": ""},
    ]
    with (
        patch.object(ws.search_provider, "search", return_value=dup_hits),
        patch.object(ws, "_fetch_page", return_value=("x" * 500, None)),
    ):
        results = ws.search_node("Acme Corp")

    assert len(results) == 1


def test_search_node_skips_paywalled_result():
    hits = [{"url": "https://example.com/a", "title": "A", "snippet": ""}]
    with (
        patch.object(ws.search_provider, "search", return_value=hits),
        patch.object(ws, "_fetch_page", return_value=("", None)),
    ):
        results = ws.search_node("Acme Corp")

    assert results == []  # E8 paywall/empty gate drops it


def test_search_node_survives_search_provider_error():
    with patch.object(ws.search_provider, "search", side_effect=RuntimeError("boom")):
        results = ws.search_node("Acme Corp")

    assert results == []


def test_search_node_survives_empty_results():
    with patch.object(ws.search_provider, "search", return_value=[]):
        results = ws.search_node("Acme Corp")

    assert results == []


def test_extract_from_result_sets_store_true_and_logs():
    resp = MagicMock()
    resp.id = "resp_extract_1"
    resp.usage.prompt_tokens = 20
    resp.usage.completion_tokens = 8
    resp.choices = [MagicMock(message=MagicMock(content='{"events": []}'))]
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = resp

    result = ws.WebResult(
        url="https://example.com/article",
        domain="example.com",
        title="t",
        snippet="",
        page_text="Acme invested in Widget.",
        published_at=None,
        content_hash="deadbeef",
    )

    with (
        patch.object(ws, "_openai_client", return_value=fake_client),
        patch.object(pipeline, "execute") as ex,
    ):
        ws._extract_from_result(result)

    _, kwargs = fake_client.chat.completions.create.call_args
    assert kwargs["store"] is True

    ex.assert_called_once()
    args, _ = ex.call_args
    _, params = args
    endpoint, model, response_id, context_json = params
    assert endpoint == "chat.completions"
    assert response_id == "resp_extract_1"
    assert result.url in context_json
