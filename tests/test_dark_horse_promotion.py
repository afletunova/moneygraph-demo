"""
Unit tests for — dark_horse auto-promotion.

Covers: promotion fires on confirmed CIK / ticker / is_public evidence, does
NOT fire without any of those, never auto-demotes to private, and respects
the ticker/CIK dedup guard (blocks + logs rather than silently overwriting).

No live DB, no network — `query` and `main.update_node` are mocked, matching
the convention used throughout this test suite (see test_enrichment.py,
test_nodes.py).
"""

from unittest.mock import patch

import pytest

import moneygraph.core.enrichment as enrichment
from moneygraph.core.enrichment import _dark_horse_promotion_signal, check_dark_horse_promotion

# ---------------------------------------------------------------------------
# _dark_horse_promotion_signal() — evidence gate
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_ticker_lookup(monkeypatch):
    """Default: no ticker/CIK match. Individual tests override this."""
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", lambda name: (None, None))


def test_signal_none_when_nothing_confirms():
    assert _dark_horse_promotion_signal("Some Dark Horse", None) is None
    assert _dark_horse_promotion_signal("Some Dark Horse", {"is_public": None}) is None


def test_signal_fires_on_is_public_true():
    sig = _dark_horse_promotion_signal("Some Dark Horse", {"is_public": True, "ticker": "DHRS"})
    assert sig == {"reason": "is_public", "ticker": "DHRS", "cik": None}


def test_signal_fires_on_confirmed_cik(monkeypatch):
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", lambda name: ("DHRS", "1234567"))
    sig = _dark_horse_promotion_signal("Some Dark Horse", None)
    assert sig == {"reason": "cik_confirmed", "ticker": "DHRS", "cik": "1234567"}


def test_signal_fires_on_ticker_only_when_no_cik(monkeypatch):
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", lambda name: ("DHRS", None))
    sig = _dark_horse_promotion_signal("Some Dark Horse", None)
    assert sig == {"reason": "ticker_confirmed", "ticker": "DHRS", "cik": None}


def test_signal_ticker_lookup_failure_is_not_fatal(monkeypatch):
    def _boom(name):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", _boom)
    assert _dark_horse_promotion_signal("Some Dark Horse", None) is None


# ---------------------------------------------------------------------------
# check_dark_horse_promotion() — end to end (query + update_node mocked)
# ---------------------------------------------------------------------------


def _node_type_row(type_: str):
    return [{"type": type_}]


def test_not_dark_horse_is_a_noop():
    with patch.object(enrichment, "query", return_value=_node_type_row("private")):
        result = check_dark_horse_promotion("node-1", "Some Private Co", {"is_public": True})
    assert result is None


def test_missing_node_is_a_noop():
    with patch.object(enrichment, "query", return_value=[]):
        result = check_dark_horse_promotion("node-1", "Ghost", {"is_public": True})
    assert result is None


def test_dark_horse_no_evidence_does_not_promote():
    with patch.object(enrichment, "query", return_value=_node_type_row("dark_horse")):
        result = check_dark_horse_promotion("node-1", "Atom Computing", None)
    assert result is None


def test_dark_horse_promotes_on_confirmed_cik(monkeypatch):
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", lambda name: ("DHRS", "1234567"))

    def _query(sql, params=None):
        if "SELECT type::text" in sql:
            return _node_type_row("dark_horse")
        if "SELECT id::text, name FROM nodes" in sql:
            return []  # no collision
        raise AssertionError(f"unexpected query: {sql}")

    fake_node_detail = {"id": "node-1", "type": "public"}
    with patch.object(enrichment, "query", side_effect=_query):
        with patch("moneygraph.api.routers.nodes.update_node", return_value=fake_node_detail) as mock_update:
            result = check_dark_horse_promotion("node-1", "Atom Computing", None)

    assert result == {
        "promoted": True,
        "reason": "cik_confirmed",
        "ticker": "DHRS",
        "cik": "1234567",
    }
    mock_update.assert_called_once()
    called_node_id, called_body = mock_update.call_args.args
    assert called_node_id == "node-1"
    assert called_body.type == "public"
    # NodeUpdateBody's cik validator zero-pads to 10 digits (existing
    # behaviour) — the signal's raw "1234567" becomes "0001234567" on write.
    assert called_body.cik == "0001234567"
    assert called_body.meta_patch["auto_promotion"]["source"] == "pipeline_auto"
    assert called_body.meta_patch["auto_promotion"]["from"] == "dark_horse"
    assert called_body.meta_patch["auto_promotion"]["to"] == "public"


def test_dark_horse_promotes_on_is_public_true_even_without_ticker_or_cik():
    def _query(sql, params=None):
        if "SELECT type::text" in sql:
            return _node_type_row("dark_horse")
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch("moneygraph.api.routers.nodes.update_node", return_value={"id": "node-1"}) as mock_update:
            result = check_dark_horse_promotion("node-1", "MGX", {"is_public": True})

    assert result["promoted"] is True
    assert result["reason"] == "is_public"
    called_body = mock_update.call_args.args[1]
    assert called_body.ticker is None
    assert called_body.cik is None


def test_dark_horse_never_auto_demotes_to_private():
    """No code path in check_dark_horse_promotion ever sets type='private' —
    the only NodeUpdateBody it constructs hardcodes type='public'."""

    def _query(sql, params=None):
        if "SELECT type::text" in sql:
            return _node_type_row("dark_horse")
        if "SELECT id::text, name FROM nodes" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch("moneygraph.api.routers.nodes.update_node", return_value={"id": "node-1"}) as mock_update:
            check_dark_horse_promotion("node-1", "Gradium", {"is_public": True})

    called_body = mock_update.call_args.args[1]
    assert called_body.type == "public"
    assert called_body.type != "private"


def test_dedup_guard_blocks_ticker_cik_collision(monkeypatch):
    """Blocked, logged (not silently overridden), and update_node is never
    called — the app.* loggers are configured with propagate=False (see
    main.py), which defeats pytest's caplog, so the log call is asserted
    directly on enrichment.logger instead."""
    monkeypatch.setattr("moneygraph.core.ticker_lookup.lookup_ticker_and_cik", lambda name: ("DHRS", "1234567"))

    def _query(sql, params=None):
        if "SELECT type::text" in sql:
            return _node_type_row("dark_horse")
        if "SELECT id::text, name FROM nodes" in sql:
            return [{"id": "existing-node", "name": "Already Known Public Co"}]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch("moneygraph.api.routers.nodes.update_node") as mock_update:
            with patch.object(enrichment.logger, "warning") as mock_warn:
                result = check_dark_horse_promotion("node-1", "Atom Computing", None)

    assert result == {
        "promoted": False,
        "reason": "collision",
        "collides_with_node_id": "existing-node",
        "collides_with_name": "Already Known Public Co",
    }
    mock_update.assert_not_called()
    assert mock_warn.call_count == 1
    assert "BLOCKED" in mock_warn.call_args.args[0]


def test_update_rejected_surfaces_as_not_promoted():
    from fastapi.responses import JSONResponse

    def _query(sql, params=None):
        if "SELECT type::text" in sql:
            return _node_type_row("dark_horse")
        if "SELECT id::text, name FROM nodes" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(enrichment, "query", side_effect=_query):
        with patch(
            "moneygraph.api.routers.nodes.update_node",
            return_value=JSONResponse(status_code=404, content={"error": "not found"}),
        ):
            result = check_dark_horse_promotion("node-1", "Atom Computing", {"is_public": True})

    assert result == {"promoted": False, "reason": "update_rejected"}


# ---------------------------------------------------------------------------
# enrich_all_nodes() wiring — promotion check runs per node, no misfire
# ---------------------------------------------------------------------------


def test_enrich_all_nodes_calls_promotion_check_and_counts_it(monkeypatch):
    monkeypatch.setattr(enrichment, "query", lambda *a, **kw: [{"id": "n1", "name": "Atom Computing", "cik": None}])
    monkeypatch.setattr(enrichment, "enrich", lambda name, cik: None)
    monkeypatch.setattr(enrichment, "_ENRICH_THROTTLE_SECS", 0)

    calls = []

    def _fake_promo(node_id, name, facts):
        calls.append((node_id, name, facts))
        return {"promoted": True, "reason": "cik_confirmed", "ticker": "DHRS", "cik": "1"}

    monkeypatch.setattr(enrichment, "check_dark_horse_promotion", _fake_promo)

    counts = enrichment.enrich_all_nodes(mode="all")

    assert calls == [("n1", "Atom Computing", None)]
    assert counts["dark_horse_promoted"] == 1
    assert counts.get("dark_horse_blocked", 0) == 0


def test_enrich_all_nodes_no_misfire_when_promotion_returns_none(monkeypatch):
    monkeypatch.setattr(enrichment, "query", lambda *a, **kw: [{"id": "n1", "name": "Diraq", "cik": None}])
    monkeypatch.setattr(enrichment, "enrich", lambda name, cik: None)
    monkeypatch.setattr(enrichment, "_ENRICH_THROTTLE_SECS", 0)
    monkeypatch.setattr(enrichment, "check_dark_horse_promotion", lambda *a, **kw: None)

    counts = enrichment.enrich_all_nodes(mode="all")

    assert "dark_horse_promoted" not in counts
    assert "dark_horse_blocked" not in counts
    assert counts["skipped"] == 1
