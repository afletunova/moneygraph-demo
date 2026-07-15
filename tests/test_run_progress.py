"""
Unit tests for Runs-tab live progress + websearch cost estimate +
percent-complete/ETA.

Pure/mocked throughout: no DB, no network, no OpenAI.
Covers:
  - db.bump_run_counters: SQL shape (increment, not absolute SET) +
    the column allowlist rejecting an unknown kwarg (incl. units_processed).
  - db.set_run_total_units: absolute SET SQL shape.
  - pipeline.py::_fetch_all_nodes bumps nodes_processed once per node
    when given a run_id (EDGAR fetch phase live progress).
  - extraction/pipeline.py::_process_results bumps units_processed once per
    filing (always) plus events/candidates/edges (only when nonzero);
    run_extract_phase sets total_units = filings post idempotency-skip.
  - extraction/websearch.py::run_websearch_phase bumps nodes_processed +
    units_processed + search_calls_made once per node actually searched
    (never for a stale-skipped node), plus per-node events/candidates/edges;
    total_units = nodes that will actually be searched (stale-skips
    excluded up front).
  - extraction/rss.py::run_rss_phase bumps units_processed once per matched
    article (always) plus events/candidates/edges (only when nonzero);
    total_units = matched entries (pre-fetch).
  - common._estimate_websearch_cost: search_calls_made * per-call rate
    (currently $0 — Brave free tier), rounded, and tolerant of None/0.
  - common._run_progress: percent-complete + ETA, incl. edge cases (total
    unknown/0, not running, just-started/no-samples-yet).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from moneygraph.api.common import _estimate_websearch_cost, _run_progress
from moneygraph.db import bump_run_counters, set_run_total_units

# ---------------------------------------------------------------------------
# bump_run_counters — SQL shape + allowlist
# ---------------------------------------------------------------------------


def test_bump_run_counters_builds_increment_sql():
    with patch("moneygraph.db.execute") as ex:
        bump_run_counters("run-1", nodes_processed=1, edges_created=2)
    assert ex.call_count == 1
    sql, params = ex.call_args[0]
    assert "nodes_processed = nodes_processed + %s" in sql
    assert "edges_created = edges_created + %s" in sql
    assert "WHERE id = %s" in sql
    # params order matches kwargs insertion order (Python 3.7+ dict order), run_id last
    assert params == (1, 2, "run-1")


def test_bump_run_counters_no_deltas_is_noop():
    with patch("moneygraph.db.execute") as ex:
        bump_run_counters("run-1")
    ex.assert_not_called()


def test_bump_run_counters_rejects_unknown_column():
    with patch("moneygraph.db.execute") as ex:
        with pytest.raises(ValueError, match="unknown column"):
            bump_run_counters("run-1", not_a_real_column=1)
    ex.assert_not_called()


def test_bump_run_counters_accepts_units_processed():
    with patch("moneygraph.db.execute") as ex:
        bump_run_counters("run-1", units_processed=1)
    ex.assert_called_once()
    sql, params = ex.call_args[0]
    assert "units_processed = units_processed + %s" in sql
    assert params == (1, "run-1")


# ---------------------------------------------------------------------------
# set_run_total_units — absolute SET, not an increment
# ---------------------------------------------------------------------------


def test_set_run_total_units_builds_absolute_set_sql():
    with patch("moneygraph.db.execute") as ex:
        set_run_total_units("run-1", 42)
    ex.assert_called_once_with("UPDATE pipeline_runs SET total_units = %s WHERE id = %s", (42, "run-1"))


# ---------------------------------------------------------------------------
# EDGAR fetch phase — nodes_processed bumped once per node
# ---------------------------------------------------------------------------


def test_fetch_all_nodes_bumps_per_node_when_run_id_given():
    from moneygraph import pipeline as top_pipeline

    nodes = [
        {"id": "n1", "name": "Acme", "cik": "0001"},
        {"id": "n2", "name": "Beta", "cik": "0002"},
    ]
    with (
        patch.object(top_pipeline, "recent_filings", return_value=[]),
        patch.object(top_pipeline, "download_filing"),
        patch.object(top_pipeline, "bump_run_counters") as bump,
    ):
        processed, errors = top_pipeline._fetch_all_nodes(nodes, 30, 7, run_id="run-42")

    assert processed == 2
    assert errors == []
    assert bump.call_count == 2
    for call in bump.call_args_list:
        args, kwargs = call
        assert args[0] == "run-42"
        assert kwargs == {"nodes_processed": 1}


def test_fetch_all_nodes_no_bump_without_run_id():
    from moneygraph import pipeline as top_pipeline

    nodes = [{"id": "n1", "name": "Acme", "cik": "0001"}]
    with (
        patch.object(top_pipeline, "recent_filings", return_value=[]),
        patch.object(top_pipeline, "download_filing"),
        patch.object(top_pipeline, "bump_run_counters") as bump,
    ):
        top_pipeline._fetch_all_nodes(nodes, 30, 7)

    bump.assert_not_called()


def test_fetch_all_nodes_skips_bump_on_per_node_failure():
    from moneygraph import pipeline as top_pipeline

    nodes = [{"id": "n1", "name": "Bad", "cik": "0001"}]
    with (
        patch.object(top_pipeline, "recent_filings", side_effect=RuntimeError("boom")),
        patch.object(top_pipeline, "bump_run_counters") as bump,
    ):
        processed, errors = top_pipeline._fetch_all_nodes(nodes, 30, 7, run_id="run-42")

    assert processed == 0
    assert len(errors) == 1
    bump.assert_not_called()


# ---------------------------------------------------------------------------
# EDGAR extract phase — bump once per filing
# ---------------------------------------------------------------------------


def test_process_results_bumps_once_per_filing():
    from moneygraph.ingest.extraction import pipeline as extraction_pipeline
    from moneygraph.ingest.extraction.backend import ExtractionResult

    meta_lookup = {
        "run-1:0001:acc1": {
            "url": "u1",
            "form_type": "8-K",
            "date": "2026-01-01",
            "node_name": "Acme",
            "content_hash": None,
            "sidecar_path": None,
            "cik": "0001",
            "accession": "acc1",
        },
    }
    results = [
        ExtractionResult(
            custom_id="run-1:0001:acc1",
            events=[
                {"investor": "A", "investee": "B", "amount_usd": 100, "event_type": "investment"},
                {"investor": "C", "investee": "D", "amount_usd": 200, "event_type": "investment"},
            ],
        ),
    ]

    # First event: logged + new edge. Second event: only a candidate (unresolved).
    with (
        patch.object(
            extraction_pipeline,
            "_process_event",
            side_effect=[(True, False, True), (False, True, False)],
        ),
        patch.object(extraction_pipeline, "bump_run_counters") as bump,
        patch.object(extraction_pipeline, "_upsert_processed_filing"),
    ):
        events_logged, candidates_found, edges_created = extraction_pipeline._process_results(
            results, meta_lookup, "run-1"
        )

    assert (events_logged, candidates_found, edges_created) == (1, 1, 1)
    bump.assert_called_once_with(
        "run-1",
        units_processed=1,
        events_logged=1,
        candidates_found=1,
        edges_created=1,
    )


def test_process_results_bumps_units_processed_only_when_filing_yields_nothing():
    """Units_processed must move even for a zero-event filing — a
    filing that was attempted but produced nothing is still real work done,
    and percent-complete must not stall on zero-yield filings."""
    from moneygraph.ingest.extraction import pipeline as extraction_pipeline
    from moneygraph.ingest.extraction.backend import ExtractionResult

    meta_lookup = {
        "run-1:0001:acc1": {
            "url": "u1",
            "form_type": "8-K",
            "date": "2026-01-01",
            "node_name": "Acme",
            "content_hash": None,
            "sidecar_path": None,
            "cik": "0001",
            "accession": "acc1",
        },
    }
    results = [ExtractionResult(custom_id="run-1:0001:acc1", events=[])]

    with (
        patch.object(extraction_pipeline, "bump_run_counters") as bump,
        patch.object(extraction_pipeline, "_upsert_processed_filing"),
    ):
        extraction_pipeline._process_results(results, meta_lookup, "run-1")

    bump.assert_called_once_with("run-1", units_processed=1)


# ---------------------------------------------------------------------------
# EDGAR extract phase — total_units set once, from post-idempotency-skip count
# ---------------------------------------------------------------------------


def test_run_extract_phase_sets_total_units_to_request_count():
    from moneygraph.ingest.extraction import pipeline as extraction_pipeline
    from moneygraph.ingest.extraction.backend import ExtractionJob

    fake_requests = [MagicMock(), MagicMock(), MagicMock()]
    fake_backend = MagicMock()
    fake_backend.submit.return_value = ExtractionJob(mode="realtime", batch_id=None)
    fake_backend.harvest.return_value = []

    with (
        patch.object(extraction_pipeline, "_scan_cache", return_value=(fake_requests, {})),
        patch.object(extraction_pipeline, "get_backend", return_value=fake_backend),
        patch.object(extraction_pipeline, "set_run_total_units") as set_total,
    ):
        extraction_pipeline.run_extract_phase("run-1", "realtime")

    set_total.assert_called_once_with("run-1", 3)


def test_run_extract_phase_sets_total_units_zero_when_no_filings():
    from moneygraph.ingest.extraction import pipeline as extraction_pipeline

    with (
        patch.object(extraction_pipeline, "_scan_cache", return_value=([], {})),
        patch.object(extraction_pipeline, "set_run_total_units") as set_total,
    ):
        result = extraction_pipeline.run_extract_phase("run-1", "realtime")

    set_total.assert_called_once_with("run-1", 0)
    assert result == (0, 0, 0)


# ---------------------------------------------------------------------------
# Websearch phase — nodes_processed / search_calls_made per attempted node
# ---------------------------------------------------------------------------


def test_websearch_bumps_nodes_and_search_calls_only_for_searched_nodes():
    from moneygraph.ingest.extraction import websearch as ws

    nodes = [
        {"id": "n1", "name": "FreshCo"},  # skipped as fresh
        {"id": "n2", "name": "StaleCo"},  # actually searched
    ]

    def fake_recent(node_id, days):
        return node_id == "n1"

    with (
        patch.object(ws, "search_node", return_value=[]),
        patch.object(ws, "_node_recently_searched", side_effect=fake_recent),
        patch.object(ws, "_mark_node_websearched"),
        patch.object(ws, "bump_run_counters") as bump,
        patch.object(ws, "set_run_total_units") as set_total,
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ws.run_websearch_phase("run-1", nodes=nodes, stale_days=14)

    # Total_units excludes the stale-skipped node (n1) — only n2 is
    # real (billed/timed) work.
    set_total.assert_called_once_with("run-1", 1)

    # Only StaleCo (n2) should generate a nodes_processed/search_calls_made bump.
    node_bumps = [c for c in bump.call_args_list if "search_calls_made" in c.kwargs]
    assert len(node_bumps) == 1
    args, kwargs = node_bumps[0]
    assert args[0] == "run-1"
    assert kwargs["nodes_processed"] == 1
    assert kwargs["units_processed"] == 1
    assert kwargs["search_calls_made"] == len(ws._QUERY_TEMPLATES)


def test_websearch_bumps_event_counts_per_node():
    from moneygraph.ingest.extraction import websearch as ws

    node = [{"id": "n1", "name": "Acme"}]

    class _R:
        url = "u1"
        content_hash = "h1"

    with (
        patch.object(ws, "search_node", return_value=[_R()]),
        patch.object(ws, "_node_recently_searched", return_value=False),
        patch.object(ws, "_mark_node_websearched"),
        patch.object(ws, "_get_processed_web_source", return_value=None),
        patch.object(ws, "_process_web_result", return_value=(1, 2, 3)),
        patch.object(ws, "_upsert_processed_web_source"),
        patch.object(ws, "bump_run_counters") as bump,
        patch.object(ws, "set_run_total_units"),
        patch.object(ws, "query"),
        patch.object(ws, "execute"),
    ):
        ev, cand, edges = ws.run_websearch_phase("run-1", nodes=node, stale_days=0)

    assert (ev, cand, edges) == (1, 2, 3)
    event_bumps = [c for c in bump.call_args_list if "events_logged" in c.kwargs]
    assert len(event_bumps) == 1
    assert event_bumps[0].kwargs == {
        "events_logged": 1,
        "candidates_found": 2,
        "edges_created": 3,
    }


# ---------------------------------------------------------------------------
# RSS phase — bump once per matched/processed article
# ---------------------------------------------------------------------------


def test_rss_bumps_once_per_processed_article():
    from moneygraph.ingest.extraction import rss as rss_mod

    feed = {"name": "TestFeed", "url": "http://example.com/feed"}
    entry = rss_mod.FeedEntry(
        url="http://example.com/a",
        title="t",
        summary="Acme raised money",
        published_at="2026-01-01",
    )

    with (
        patch.object(rss_mod, "_build_node_matcher", return_value=__import__("re").compile(r"acme")),
        patch.object(rss_mod, "fetch_feed_entries", return_value=[entry]),
        patch.object(rss_mod, "_get_processed_web_source", return_value=None),
        patch.object(rss_mod, "_fetch_page", return_value=("Acme raised money from investors.", None)),
        patch.object(rss_mod, "_is_paywalled", return_value=False),
        patch.object(rss_mod, "_process_web_result", return_value=(1, 0, 1)),
        patch.object(rss_mod, "_upsert_processed_web_source"),
        patch.object(rss_mod, "set_run_total_units") as set_total,
        patch.object(rss_mod, "bump_run_counters") as bump,
    ):
        ev, cand, edges = rss_mod.run_rss_phase("run-1", feeds=[feed])

    assert (ev, cand, edges) == (1, 0, 1)
    set_total.assert_called_once_with("run-1", 1)  # one matched entry
    bump.assert_called_once_with(
        "run-1",
        units_processed=1,
        events_logged=1,
        edges_created=1,
    )


def test_rss_bumps_units_processed_only_when_article_yields_nothing():
    """Units_processed must move even for a zero-yield article."""
    from moneygraph.ingest.extraction import rss as rss_mod

    feed = {"name": "TestFeed", "url": "http://example.com/feed"}
    entry = rss_mod.FeedEntry(
        url="http://example.com/a",
        title="t",
        summary="Acme raised money",
        published_at="2026-01-01",
    )

    with (
        patch.object(rss_mod, "_build_node_matcher", return_value=__import__("re").compile(r"acme")),
        patch.object(rss_mod, "fetch_feed_entries", return_value=[entry]),
        patch.object(rss_mod, "_get_processed_web_source", return_value=None),
        patch.object(rss_mod, "_fetch_page", return_value=("Acme raised money from investors.", None)),
        patch.object(rss_mod, "_is_paywalled", return_value=False),
        patch.object(rss_mod, "_process_web_result", return_value=(0, 0, 0)),
        patch.object(rss_mod, "_upsert_processed_web_source"),
        patch.object(rss_mod, "set_run_total_units"),
        patch.object(rss_mod, "bump_run_counters") as bump,
    ):
        rss_mod.run_rss_phase("run-1", feeds=[feed])

    bump.assert_called_once_with("run-1", units_processed=1)


def test_rss_total_units_excludes_unmatched_entries():
    """Total_units is matched entries only — unmatched feed noise
    never costs a fetch and must not inflate the denominator."""
    from moneygraph.ingest.extraction import rss as rss_mod

    feed = {"name": "TestFeed", "url": "http://example.com/feed"}
    matched = rss_mod.FeedEntry(url="http://example.com/a", title="Acme deal", summary="", published_at=None)
    unmatched = rss_mod.FeedEntry(url="http://example.com/b", title="unrelated", summary="", published_at=None)

    with (
        patch.object(rss_mod, "_build_node_matcher", return_value=__import__("re").compile(r"acme")),
        patch.object(rss_mod, "fetch_feed_entries", return_value=[matched, unmatched]),
        patch.object(rss_mod, "_get_processed_web_source", return_value=None),
        patch.object(rss_mod, "_fetch_page", return_value=("Acme raised money.", None)),
        patch.object(rss_mod, "_is_paywalled", return_value=False),
        patch.object(rss_mod, "_process_web_result", return_value=(0, 0, 0)),
        patch.object(rss_mod, "_upsert_processed_web_source"),
        patch.object(rss_mod, "set_run_total_units") as set_total,
        patch.object(rss_mod, "bump_run_counters"),
    ):
        rss_mod.run_rss_phase("run-1", feeds=[feed])

    set_total.assert_called_once_with("run-1", 1)


# ---------------------------------------------------------------------------
# Cost estimate — pure function
# ---------------------------------------------------------------------------


def test_estimate_websearch_cost_basic():
    assert _estimate_websearch_cost(51) == pytest.approx(0.0)


def test_estimate_websearch_cost_zero_and_none():
    assert _estimate_websearch_cost(0) == 0.0
    assert _estimate_websearch_cost(None) == 0.0


# ---------------------------------------------------------------------------
# _run_progress: percent-complete + ETA, computed on read
# ---------------------------------------------------------------------------


def test_run_progress_not_running_shows_nothing():
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=60)
    result = _run_progress("completed", started, total_units=10, units_processed=10, now=now)
    assert result == {"percent_complete": None, "eta_seconds": None}


def test_run_progress_total_unknown_shows_nothing():
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=60)
    result = _run_progress("running", started, total_units=None, units_processed=5, now=now)
    assert result == {"percent_complete": None, "eta_seconds": None}


def test_run_progress_total_zero_shows_nothing():
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=60)
    result = _run_progress("running", started, total_units=0, units_processed=0, now=now)
    assert result == {"percent_complete": None, "eta_seconds": None}


def test_run_progress_just_started_no_eta_yet():
    """Zero samples so far — percent can still be shown (0%), but no rate to
    project an ETA from. No fabricated/smoothed number."""
    now = datetime.now(timezone.utc)
    started = now  # just started, no elapsed samples
    result = _run_progress("running", started, total_units=10, units_processed=0, now=now)
    assert result["percent_complete"] == 0.0
    assert result["eta_seconds"] is None


def test_run_progress_midway_computes_percent_and_eta():
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=30)  # 30s elapsed
    # 3 of 12 units done in 30s → 10s/unit → 9 remaining → 90s ETA
    result = _run_progress("running", started, total_units=12, units_processed=3, now=now)
    assert result["percent_complete"] == 25.0
    assert result["eta_seconds"] == 90


def test_run_progress_caps_percent_at_100_when_overcounted():
    """Defensive: processed should never exceed total, but if it somehow did
    (reconciliation race), percent must cap at 100 rather than exceed it."""
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=10)
    result = _run_progress("running", started, total_units=5, units_processed=7, now=now)
    assert result["percent_complete"] == 100.0


def test_run_progress_no_started_at_shows_percent_but_no_eta():
    result = _run_progress("running", None, total_units=10, units_processed=2)
    assert result["percent_complete"] == 20.0
    assert result["eta_seconds"] is None
