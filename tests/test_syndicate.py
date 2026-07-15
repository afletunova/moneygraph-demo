"""
Unit tests for — syndicate-round overcount detection.

Pure: no DB, no network. Exercises _detect_syndicate_indices (the within-result
grouping heuristic) directly, plus _process_event's value_status/estimate_reason
computation via a scripted fake DB cursor (mirrors test_link.py's pattern).
"""

from unittest.mock import MagicMock, patch

from moneygraph.ingest.extraction.pipeline import (
    SYNDICATE_MIN_COINVESTORS,
    _detect_syndicate_indices,
)

# ---------------------------------------------------------------------------
# _detect_syndicate_indices — pure grouping logic
# ---------------------------------------------------------------------------


def _waymo_style_events(n=13, amount=16_000_000_000, date="2026-02-02"):
    return [
        {
            "investor": f"Investor {i}",
            "investee": "Waymo LLC",
            "amount_usd": amount,
            "date": date,
            "excerpt": "...",
        }
        for i in range(n)
    ]


def test_syndicate_group_flagged_at_or_above_threshold():
    events = _waymo_style_events(n=13)
    idxs = _detect_syndicate_indices(events)
    assert idxs == set(range(13))


def test_below_threshold_not_flagged():
    events = _waymo_style_events(n=SYNDICATE_MIN_COINVESTORS - 1)
    idxs = _detect_syndicate_indices(events)
    assert idxs == set()


def test_exactly_at_threshold_flagged():
    events = _waymo_style_events(n=SYNDICATE_MIN_COINVESTORS)
    idxs = _detect_syndicate_indices(events)
    assert idxs == set(range(SYNDICATE_MIN_COINVESTORS))


def test_mixed_result_only_syndicate_group_flagged():
    # 3 co-investors sharing one round total (syndicate) + 1 unrelated
    # single-investor event in the SAME extraction result.
    events = _waymo_style_events(n=3) + [
        {
            "investor": "Solo VC",
            "investee": "OtherCo",
            "amount_usd": 500_000_000,
            "date": "2026-02-02",
            "excerpt": "...",
        },
    ]
    idxs = _detect_syndicate_indices(events)
    assert idxs == {0, 1, 2}
    assert 3 not in idxs


def test_different_amounts_not_grouped():
    # Same investee/date but different amounts per investor -> a real
    # multi-tranche pattern (Flag 1 territory), NOT a syndicate overcount.
    events = [
        {"investor": "A", "investee": "OpenAI", "amount_usd": 30_000_000_000, "date": "2026-01-01"},
        {"investor": "B", "investee": "OpenAI", "amount_usd": 22_500_000_000, "date": "2026-01-01"},
        {"investor": "C", "investee": "OpenAI", "amount_usd": 40_000_000_000, "date": "2026-01-01"},
    ]
    idxs = _detect_syndicate_indices(events)
    assert idxs == set()


def test_zero_or_missing_amount_never_grouped():
    events = [
        {
            "investor": f"Investor {i}",
            "investee": "SomeCo",
            "amount_usd": None,
            "date": "2026-01-01",
        }
        for i in range(5)
    ]
    idxs = _detect_syndicate_indices(events)
    assert idxs == set()


def test_delta_usd_used_when_present_over_amount_usd():
    events = [
        {
            "investor": f"Investor {i}",
            "investee": "SomeCo",
            "delta_usd": 9_000_000_000,
            "amount_usd": 1,
            "date": "2026-01-01",
        }
        for i in range(3)
    ]
    idxs = _detect_syndicate_indices(events)
    assert idxs == {0, 1, 2}


def test_same_investor_repeated_does_not_inflate_group():
    # Same investor name repeated (e.g. dedup artifact) should not count as
    # multiple distinct co-investors.
    events = [
        {
            "investor": "Same VC",
            "investee": "SomeCo",
            "amount_usd": 5_000_000_000,
            "date": "2026-01-01",
        }
        for _ in range(5)
    ]
    idxs = _detect_syndicate_indices(events)
    assert idxs == set()  # only 1 distinct investor


def test_custom_threshold_respected():
    events = _waymo_style_events(n=2)
    assert _detect_syndicate_indices(events, min_coinvestors=2) == {0, 1}
    assert _detect_syndicate_indices(events, min_coinvestors=3) == set()


# ---------------------------------------------------------------------------
# _process_event — value_status / estimate_reason wiring
# ---------------------------------------------------------------------------


def _fake_conn(edge_id="edge-1", event_id="ev-1", source_id="src-1"):
    """Scripted fake cursor/connection mirroring test_link.py's _FakeCursor."""
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (edge_id, True),  # edges INSERT ... RETURNING id, is_new
        (event_id,),  # investment_events INSERT ... RETURNING id
        (source_id,),  # sources INSERT ... RETURNING id
    ]
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _event_insert_call(cur):
    for c in cur.execute.call_args_list:
        sql, params = c.args
        if "INSERT INTO investment_events" in sql:
            return sql, params
    raise AssertionError("no investment_events INSERT found")


def test_force_estimate_reason_overrides_actual_amount():
    """A syndicate-flagged event with a real numeric amount is still written
    as value_status='estimated', estimate_reason='syndicate_total' — NOT
    'actual', even though amount_usd is present and nonzero."""
    from moneygraph.ingest.extraction import pipeline

    conn, cur = _fake_conn()
    resolved = MagicMock(resolved=True, node_id="node-1")
    with (
        patch.object(pipeline, "get_conn", return_value=conn),
        patch.object(pipeline, "resolve", return_value=resolved),
    ):
        event = {
            "investor": "Bessemer Venture Partners",
            "investee": "Waymo LLC",
            "amount_usd": 16_000_000_000,
            "date": "2026-02-02",
            "excerpt": "raised $16 billion",
        }
        filing_meta = {
            "url": "https://waymo.com/blog/x",
            "form_type": "WEB",
            "date": "2026-02-02",
            "source_tier": 3,
        }
        logged, candidate, new_edge = pipeline._process_event(
            event,
            filing_meta,
            "run-1",
            write_news_feed=False,
            force_estimate_reason="syndicate_total",
        )

    assert logged is True
    _, params = _event_insert_call(cur)
    # (edge_id, delta_usd, event_type, event_date, source_url, source_tier,
    #  filing_type, confidence, raw_excerpt, value_status, estimate_reason,
    #  discovery_source)
    value_status, estimate_reason = params[9], params[10]
    assert value_status == "estimated"
    assert estimate_reason == "syndicate_total"


def test_canonical_key_targets_source_url_not_event_date():
    """Regression: the investment_events ON CONFLICT target must key on
    source_url, not event_date. event_date is an LLM extraction output, not a
    stable fact — a re-processed source assigning a slightly different date on
    a second pass must collide (update in place) rather than insert a
    duplicate that silently inflates the edge's summed total (confirmed live
    2026-07-13: 40 edges, ~$352.1B phantom, worst case Anthropic at $286B of
    it — see db/migrations/020_canonical_key_source_url.sql)."""
    from moneygraph.ingest.extraction import pipeline

    conn, cur = _fake_conn()
    resolved = MagicMock(resolved=True, node_id="node-1")
    with (
        patch.object(pipeline, "get_conn", return_value=conn),
        patch.object(pipeline, "resolve", return_value=resolved),
    ):
        event = {
            "investor": "Dragoneer Investment Group",
            "investee": "Anthropic",
            "amount_usd": 65_000_000_000,
            "date": "2026-05-28",
            "excerpt": "raised $65 billion in Series H",
        }
        filing_meta = {
            "url": "https://www.anthropic.com/news/series-h",
            "form_type": "WEB",
            "date": "2026-05-28",
            "source_tier": 3,
        }
        pipeline._process_event(event, filing_meta, "run-1", write_news_feed=False)

    sql, _ = _event_insert_call(cur)
    assert "ON CONFLICT (edge_id, event_type, source_url, delta_usd)" in sql
    assert "event_date" not in sql[sql.index("ON CONFLICT") : sql.index("DO UPDATE")]


def test_no_force_reason_defaults_to_actual_when_amount_present():
    from moneygraph.ingest.extraction import pipeline

    conn, cur = _fake_conn()
    resolved = MagicMock(resolved=True, node_id="node-1")
    with (
        patch.object(pipeline, "get_conn", return_value=conn),
        patch.object(pipeline, "resolve", return_value=resolved),
    ):
        event = {
            "investor": "Nvidia",
            "investee": "Anthropic",
            "amount_usd": 5_000_000_000,
            "date": "2026-01-01",
            "excerpt": "invested $5 billion",
        }
        filing_meta = {
            "url": "https://example.com/x",
            "form_type": "WEB",
            "date": "2026-01-01",
            "source_tier": 3,
        }
        pipeline._process_event(event, filing_meta, "run-1", write_news_feed=False)

    _, params = _event_insert_call(cur)
    assert params[9] == "actual"
    assert params[10] is None


def test_no_amount_defaults_to_estimated_no_amount_reason():
    from moneygraph.ingest.extraction import pipeline

    conn, cur = _fake_conn()
    resolved = MagicMock(resolved=True, node_id="node-1")
    with (
        patch.object(pipeline, "get_conn", return_value=conn),
        patch.object(pipeline, "resolve", return_value=resolved),
    ):
        event = {
            "investor": "Nvidia",
            "investee": "Anthropic",
            "amount_usd": None,
            "date": "2026-01-01",
            "excerpt": "invested an undisclosed amount",
        }
        filing_meta = {
            "url": "https://example.com/x",
            "form_type": "WEB",
            "date": "2026-01-01",
            "source_tier": 3,
        }
        pipeline._process_event(event, filing_meta, "run-1", write_news_feed=False)

    _, params = _event_insert_call(cur)
    assert params[9] == "estimated"
    assert params[10] == "no_amount"
