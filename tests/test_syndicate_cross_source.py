"""
Unit tests for — cross-source syndicate-round detection (app/syndicate.py).

Distinct from test_syndicate.py, which tests its WITHIN-one-extraction-
result heuristic (_detect_syndicate_indices). This module generalizes that
signal across the whole investment_events table / across separate ingestion
runs — the case explicitly scoped out (see extraction/pipeline.py's
_detect_syndicate_indices docstring) and which turned out to be the dominant
real-world pattern (confirmed live 2026-07-13 UAT).
"""

from datetime import date
from unittest.mock import patch

import moneygraph.core.syndicate as syn


def _row(
    id_,
    event_date,
    delta_usd,
    investee_id="waymo",
    investee_name="Waymo LLC",
    investor_id="inv",
    investor_name="Investor",
):
    return {
        "id": id_,
        "event_date": event_date,
        "delta_usd": delta_usd,
        "investee_id": investee_id,
        "investee_name": investee_name,
        "investor_id": investor_id,
        "investor_name": investor_name,
    }


def test_cluster_by_date_single_cluster_within_tolerance():
    rows = [
        _row("e1", date(2026, 5, 28), 65_000_000_000),
        _row("e2", date(2026, 6, 1), 65_000_000_000),
    ]
    clusters = syn._cluster_by_date(rows, tolerance_days=10)
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_cluster_by_date_splits_on_large_gap():
    rows = [
        _row("e1", date(2026, 2, 1), 30_000_000_000),
        _row("e2", date(2026, 7, 1), 30_000_000_000),  # ~5 months later
    ]
    clusters = syn._cluster_by_date(rows, tolerance_days=10)
    assert len(clusters) == 2


def test_detect_syndicate_clusters_requires_min_coinvestors():
    rows = [
        _row("e1", date(2026, 6, 1), 20_000_000_000, investor_id="a", investor_name="A"),
        _row("e2", date(2026, 6, 2), 20_000_000_000, investor_id="b", investor_name="B"),
    ]
    with patch.object(syn, "query", return_value=rows):
        clusters = syn.detect_syndicate_clusters(min_coinvestors=3)
    assert clusters == []  # only 2 distinct investors, below threshold


def test_detect_syndicate_clusters_finds_real_cluster():
    rows = [
        _row("e1", date(2026, 6, 1), 20_000_000_000, investor_id="a", investor_name="A"),
        _row("e2", date(2026, 6, 2), 20_000_000_000, investor_id="b", investor_name="B"),
        _row("e3", date(2026, 6, 3), 20_000_000_000, investor_id="c", investor_name="C"),
    ]
    with patch.object(syn, "query", return_value=rows):
        clusters = syn.detect_syndicate_clusters(min_coinvestors=3)
    assert len(clusters) == 1
    assert clusters[0]["investee_name"] == "Waymo LLC"
    assert clusters[0]["delta_usd"] == 20_000_000_000
    assert set(clusters[0]["event_ids"]) == {"e1", "e2", "e3"}
    assert clusters[0]["investors"] == ["A", "B", "C"]


def test_detect_syndicate_clusters_different_amounts_not_grouped():
    rows = [
        _row("e1", date(2026, 6, 1), 20_000_000_000, investor_id="a", investor_name="A"),
        _row("e2", date(2026, 6, 2), 16_000_000_000, investor_id="b", investor_name="B"),
        _row("e3", date(2026, 6, 3), 20_000_000_000, investor_id="c", investor_name="C"),
    ]
    with patch.object(syn, "query", return_value=rows):
        clusters = syn.detect_syndicate_clusters(min_coinvestors=2)
    # e2's different amount must not join the $20B cluster
    for c in clusters:
        assert "e2" not in c["event_ids"]


def test_flag_syndicate_clusters_updates_value_status():
    rows = [
        _row("e1", date(2026, 6, 1), 20_000_000_000, investor_id="a", investor_name="A"),
        _row("e2", date(2026, 6, 2), 20_000_000_000, investor_id="b", investor_name="B"),
        _row("e3", date(2026, 6, 3), 20_000_000_000, investor_id="c", investor_name="C"),
    ]
    with patch.object(syn, "query", return_value=rows), patch.object(syn, "execute") as ex:
        result = syn.flag_syndicate_clusters(min_coinvestors=3)

    assert result["clusters_found"] == 1
    assert result["events_flagged"] == 3
    ex.assert_called_once()
    sql, params = ex.call_args[0]
    assert "value_status = 'estimated'" in sql
    assert "estimate_reason = 'syndicate_total'" in sql
    assert set(params[0]) == {"e1", "e2", "e3"}
