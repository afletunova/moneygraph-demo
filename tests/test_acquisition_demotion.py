"""
Unit tests for — acquisition/delisting demotion candidates.

Covers: detection finds a `public` node with an incoming `subsidiary` edge,
does NOT fire on private/dark_horse nodes or non-subsidiary edges, the
flag-only (not auto-apply) confidence gate never writes `nodes.type`, the
flagging is idempotent per evidence edge, and the whole-graph sweep tallies
correctly.

No live DB, no network — `query`/`execute` are mocked, matching the
convention used throughout this test suite (see test_dark_horse_promotion.py,
test_nodes.py).
"""

from unittest.mock import patch

import moneygraph.core.enrichment as enrichment
from moneygraph.core.enrichment import (
    check_acquisition_demotion_evidence,
    flag_acquisition_demotion_candidate,
    sweep_acquisition_demotion_candidates,
)

_EVIDENCE_EDGE_ROW = {
    "edge_id": "edge-1",
    "amount_usd": 7_000_000_000,
    "acquirer_node_id": "acquirer-1",
    "acquirer_name": "Pfizer Inc.",
}


# ---------------------------------------------------------------------------
# check_acquisition_demotion_evidence() — detection
# ---------------------------------------------------------------------------


def test_no_evidence_when_node_missing():
    with patch.object(enrichment, "query", return_value=[]):
        result = check_acquisition_demotion_evidence("node-1")
    assert result is None


def test_no_evidence_when_node_not_public():
    def _query(sql, params=None):
        if "FROM nodes WHERE id" in sql:
            return [{"type": "private", "name": "Waymo LLC"}]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        result = check_acquisition_demotion_evidence("node-1")
    assert result is None


def test_no_evidence_when_public_but_no_subsidiary_edge():
    def _query(sql, params=None):
        if "FROM nodes WHERE id" in sql:
            return [{"type": "public", "name": "NVIDIA Corporation"}]
        if "edge_type = 'subsidiary'" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        result = check_acquisition_demotion_evidence("node-1")
    assert result is None


def test_fires_on_public_node_with_incoming_subsidiary_edge():
    def _query(sql, params=None):
        if "FROM nodes WHERE id" in sql:
            return [{"type": "public", "name": "Metsera, Inc."}]
        if "edge_type = 'subsidiary'" in sql:
            return [_EVIDENCE_EDGE_ROW]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        result = check_acquisition_demotion_evidence("node-1")

    assert result == {
        "node_id": "node-1",
        "name": "Metsera, Inc.",
        "evidence_edge_id": "edge-1",
        "acquirer_node_id": "acquirer-1",
        "acquirer_name": "Pfizer Inc.",
        "amount_usd": 7_000_000_000,
    }


def test_query_filters_on_to_node_id_and_subsidiary_edge_type():
    """A non-subsidiary edge_type (e.g. 'ownership') or an edge where this
    node is the ACQUIRER (from_node_id), not the investee, must not surface
    — verified by asserting the query shape itself, since the DB layer is
    mocked and would return whatever we tell it to regardless."""
    captured = {}

    def _query(sql, params=None):
        if "FROM nodes WHERE id" in sql:
            return [{"type": "public", "name": "Some Co"}]
        captured["sql"] = sql
        captured["params"] = params
        return []

    with patch.object(enrichment, "query", side_effect=_query):
        check_acquisition_demotion_evidence("node-1")

    assert "e.to_node_id = %s" in captured["sql"]
    assert "e.edge_type = 'subsidiary'" in captured["sql"]
    assert captured["params"] == ("node-1",)


# ---------------------------------------------------------------------------
# flag_acquisition_demotion_candidate() — flag-only, never auto-applies
# ---------------------------------------------------------------------------


def test_flag_returns_none_when_no_evidence():
    with patch.object(enrichment, "query", return_value=[{"type": "private", "name": "X"}]):
        with patch.object(enrichment, "execute") as mock_execute:
            result = flag_acquisition_demotion_candidate("node-1")
    assert result is None
    mock_execute.assert_not_called()


def test_flag_writes_meta_marker_never_writes_type():
    """The confidence/asymmetry judgment call: unlike the dark_horse
    promotion signal's near-certain CIK/ticker match, edge_type is
    model-derived and known to be unverified — so this flags
    for review, it never writes nodes.type. Asserted directly: execute() is
    only ever called with an UPDATE ... SET meta = ..., never SET type =."""

    def _query(sql, params=None):
        if "FROM nodes WHERE id" in sql and "meta" not in sql:
            return [{"type": "public", "name": "Metsera, Inc."}]
        if "edge_type = 'subsidiary'" in sql:
            return [_EVIDENCE_EDGE_ROW]
        if "SELECT meta FROM nodes" in sql:
            return [{"meta": {}}]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch.object(enrichment, "execute") as mock_execute:
            result = flag_acquisition_demotion_candidate("node-1")

    assert result["flagged"] is True
    assert result["evidence_edge_id"] == "edge-1"
    mock_execute.assert_called_once()
    sql = mock_execute.call_args.args[0]
    assert "SET meta = COALESCE" in sql
    assert "SET type" not in sql
    marker = mock_execute.call_args.args[1][0].adapted
    assert marker["acquisition_demotion_candidate"]["reason"] == "subsidiary_edge_incoming"
    assert marker["acquisition_demotion_candidate"]["source"] == "pipeline_auto"
    assert marker["acquisition_demotion_candidate"]["evidence_edge_id"] == "edge-1"


def test_flag_is_idempotent_for_same_evidence_edge():
    already_flagged_meta = {
        "acquisition_demotion_candidate": {
            "reason": "subsidiary_edge_incoming",
            "evidence_edge_id": "edge-1",
            "acquirer_node_id": "acquirer-1",
            "acquirer_name": "Pfizer Inc.",
            "at": "2026-07-11T00:00:00+00:00",
            "source": "pipeline_auto",
        }
    }

    def _query(sql, params=None):
        if "edge_type = 'subsidiary'" in sql:
            return [_EVIDENCE_EDGE_ROW]
        if "SELECT meta FROM nodes" in sql:
            return [{"meta": already_flagged_meta}]
        if "FROM nodes WHERE id" in sql:
            return [{"type": "public", "name": "Metsera, Inc."}]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch.object(enrichment, "execute") as mock_execute:
            result = flag_acquisition_demotion_candidate("node-1")

    assert result == {
        "flagged": False,
        "reason": "already_flagged",
        **{
            "node_id": "node-1",
            "name": "Metsera, Inc.",
            "evidence_edge_id": "edge-1",
            "acquirer_node_id": "acquirer-1",
            "acquirer_name": "Pfizer Inc.",
            "amount_usd": 7_000_000_000,
        },
    }
    mock_execute.assert_not_called()


def test_flag_refires_when_evidence_edge_changes():
    """A different subsidiary edge (e.g. a re-acquisition by a different
    buyer) than what's already flagged should write a fresh marker, not be
    treated as already-resolved."""
    stale_meta = {
        "acquisition_demotion_candidate": {
            "evidence_edge_id": "old-edge",
            "reason": "subsidiary_edge_incoming",
            "acquirer_node_id": "old-acquirer",
            "acquirer_name": "Old Acquirer Co",
            "at": "2020-01-01T00:00:00+00:00",
            "source": "pipeline_auto",
        }
    }

    def _query(sql, params=None):
        if "edge_type = 'subsidiary'" in sql:
            return [_EVIDENCE_EDGE_ROW]  # edge-1, different from "old-edge"
        if "SELECT meta FROM nodes" in sql:
            return [{"meta": stale_meta}]
        if "FROM nodes WHERE id" in sql:
            return [{"type": "public", "name": "Metsera, Inc."}]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch.object(enrichment, "execute") as mock_execute:
            result = flag_acquisition_demotion_candidate("node-1")

    assert result["flagged"] is True
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# sweep_acquisition_demotion_candidates() — whole-graph on-demand sweep
# ---------------------------------------------------------------------------


def test_sweep_tallies_flagged_and_already_flagged(monkeypatch):
    monkeypatch.setattr(
        enrichment,
        "query",
        lambda *a, **kw: [{"id": "n1"}, {"id": "n2"}],
    )

    def _fake_flag(node_id):
        if node_id == "n1":
            return {"flagged": True, "evidence_edge_id": "e1"}
        return {"flagged": False, "reason": "already_flagged", "evidence_edge_id": "e2"}

    monkeypatch.setattr(enrichment, "flag_acquisition_demotion_candidate", _fake_flag)

    counts = sweep_acquisition_demotion_candidates()

    assert counts == {"checked": 2, "flagged": 1, "already_flagged": 1, "skipped": 0}


def test_sweep_query_only_targets_public_nodes_with_subsidiary_edges():
    captured = {}

    def _query(sql, params=None):
        captured["sql"] = sql
        return []

    with patch.object(enrichment, "query", side_effect=_query):
        sweep_acquisition_demotion_candidates()

    assert "n.type = 'public'" in captured["sql"]
    assert "e.edge_type = 'subsidiary'" in captured["sql"]
