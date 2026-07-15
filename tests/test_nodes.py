"""
Unit tests for — node detail panel backend: node read (GET /nodes/{id}),
node editing (POST /nodes/{id}/update, incl. type-transition validation), the
node-filtered /news query, and the price endpoint's graceful no-ticker path.

No live DB, no network — `query`/`execute`/`get_conn` are mocked throughout.
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import moneygraph.api.routers.nodes as main
from moneygraph.api.routers import graph as graph_mod
from moneygraph.api.routers import pipeline as pipeline_mod
from moneygraph.api.routers.graph import get_current_graph
from moneygraph.api.routers.nodes import (
    _ALLOWED_TYPE_TRANSITIONS,
    NodeTickerBody,
    NodeUpdateBody,
    add_node_ticker,
    delete_node_ticker,
    get_node_detail,
    get_node_price,
    list_node_tickers,
    list_nodes,
    split_ticker_field,
    update_node,
)
from moneygraph.api.routers.pipeline import _node_normalized_aliases, get_news

# ---------------------------------------------------------------------------
# NodeUpdateBody — field validation
# ---------------------------------------------------------------------------


def test_ticker_uppercased():
    assert NodeUpdateBody(ticker="aapl").ticker == "AAPL"


def test_ticker_allows_dot_and_hyphen():
    assert NodeUpdateBody(ticker="brk.b").ticker == "BRK.B"


def test_ticker_empty_string_becomes_none():
    assert NodeUpdateBody(ticker="").ticker is None


def test_ticker_rejects_bad_chars():
    with pytest.raises(ValidationError):
        NodeUpdateBody(ticker="AAPL!")


def test_ticker_rejects_too_long():
    with pytest.raises(ValidationError):
        NodeUpdateBody(ticker="A" * 11)


def test_cik_zero_padded():
    assert NodeUpdateBody(cik="320193").cik == "0000320193"


def test_cik_rejects_non_digits():
    with pytest.raises(ValidationError):
        NodeUpdateBody(cik="12ab34")


def test_cik_empty_string_becomes_none():
    assert NodeUpdateBody(cik="").cik is None


def test_sector_and_type_passthrough():
    body = NodeUpdateBody(type="public", sector="Technology")
    assert body.type == "public"
    assert body.sector == "Technology"


# ---------------------------------------------------------------------------
# Exchange-qualified ticker parsing (NodeUpdateBody.ticker /
# NodeTickerBody.ticker / split_ticker_field)
# ---------------------------------------------------------------------------


def test_ticker_exchange_qualified_with_colon_space():
    assert NodeUpdateBody(ticker="HKG: 9988").ticker == "HKG:9988"


def test_ticker_exchange_qualified_no_space():
    assert NodeUpdateBody(ticker="HKG:9988").ticker == "HKG:9988"


def test_ticker_exchange_qualified_lowercase_normalized():
    assert NodeUpdateBody(ticker="hkg: 9988").ticker == "HKG:9988"


def test_ticker_exchange_qualified_rejects_bad_ticker_portion():
    with pytest.raises(ValidationError):
        NodeUpdateBody(ticker="HKG: 99!!")


def test_ticker_bare_form_still_backward_compatible():
    # The original shape — no colon at all — must keep working
    # byte-for-byte (this is the actively-used common case: most nodes have
    # exactly one US-listed ticker).
    assert NodeUpdateBody(ticker="aapl").ticker == "AAPL"
    assert NodeUpdateBody(ticker="brk.b").ticker == "BRK.B"


def test_split_ticker_field_bare_has_empty_exchange():
    # '' (not None) is the node_tickers sentinel for "no exchange qualifier"
    # — see 017_node_tickers.sql for why NULL would break the UNIQUE
    # constraint for the common case.
    assert split_ticker_field("AAPL") == ("", "AAPL")


def test_split_ticker_field_exchange_qualified():
    assert split_ticker_field("HKG:9988") == ("HKG", "9988")


def test_split_ticker_field_none():
    assert split_ticker_field(None) == ("", None)


def test_node_ticker_body_bare():
    assert NodeTickerBody(ticker="baba").ticker == "BABA"
    assert NodeTickerBody(ticker="baba").is_primary is False


def test_node_ticker_body_exchange_qualified():
    body = NodeTickerBody(ticker="HKG: 9988", is_primary=True)
    assert body.ticker == "HKG:9988"
    assert body.is_primary is True


def test_node_ticker_body_empty_rejected():
    with pytest.raises(ValidationError):
        NodeTickerBody(ticker="")


# ---------------------------------------------------------------------------
# node.type transition rules (Yulia: "dark horse can become anything, private
# can go public")
# ---------------------------------------------------------------------------


def test_dark_horse_can_become_anything():
    assert _ALLOWED_TYPE_TRANSITIONS["dark_horse"] == {"public", "private", "dark_horse"}


def test_private_can_go_public_but_not_dark_horse():
    assert "public" in _ALLOWED_TYPE_TRANSITIONS["private"]
    assert "dark_horse" not in _ALLOWED_TYPE_TRANSITIONS["private"]


def test_public_can_go_private_but_not_dark_horse():
    # public -> private membership is necessary but NOT sufficient —
    # update_node additionally requires acquisition evidence (see the
    # section below). public -> dark_horse is still fully blocked, no
    # evidence path exists for it.
    assert _ALLOWED_TYPE_TRANSITIONS["public"] == {"public", "private"}
    assert "dark_horse" not in _ALLOWED_TYPE_TRANSITIONS["public"]


def _mock_conn_for_update(current_type: str):
    """A get_conn() mock whose cursor's first fetchone() returns current_type."""
    cur = MagicMock()
    cur.fetchone.return_value = (current_type,)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_update_node_blocks_disallowed_transition():
    conn, cur = _mock_conn_for_update("public")
    with patch.object(main, "get_conn", return_value=conn):
        resp = update_node("node-1", NodeUpdateBody(type="dark_horse"))
    assert resp.status_code == 400
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_update_node_allows_private_to_public():
    conn, cur = _mock_conn_for_update("private")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "_node_detail_row", return_value={"id": "node-1", "type": "public"}):
            resp = update_node("node-1", NodeUpdateBody(type="public", ticker="acme"))
    conn.commit.assert_called_once()
    assert resp == {"id": "node-1", "type": "public"}
    # UPDATE nodes ... executed with the uppercased ticker
    update_calls = [c for c in cur.execute.call_args_list if "UPDATE nodes" in c.args[0]]
    assert len(update_calls) == 1
    assert "ACME" in update_calls[0].args[1]


def test_update_node_allows_dark_horse_to_dark_horse_noop_type_but_updates_sector():
    conn, cur = _mock_conn_for_update("dark_horse")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
            update_node("node-1", NodeUpdateBody(sector="Fintech"))
    sector_calls = [c for c in cur.execute.call_args_list if "node_facts" in c.args[0]]
    assert len(sector_calls) == 1
    assert "Fintech" in sector_calls[0].args[1]


def test_update_node_404_when_missing():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = update_node("missing", NodeUpdateBody(type="public"))
    assert resp.status_code == 404
    conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# POST /nodes/{id}/update syncs the parsed ticker into node_tickers
# as this node's primary (and nodes.ticker stays the bare portion only).
# ---------------------------------------------------------------------------


def test_update_node_bare_ticker_syncs_node_tickers_primary_with_empty_exchange():
    conn, cur = _mock_conn_for_update("private")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
            update_node("node-1", NodeUpdateBody(type="public", ticker="acme"))

    # nodes.ticker gets the bare ticker (unchanged behaviour).
    node_update_calls = [c for c in cur.execute.call_args_list if "UPDATE nodes SET" in c.args[0]]
    assert "ACME" in node_update_calls[0].args[1]

    # node_tickers gets an upsert with exchange='' (the "no exchange" sentinel).
    insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO node_tickers" in c.args[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0].args[1] == ("node-1", "", "ACME")

    # any previously-primary row for this node is un-primaried first.
    unprimary_calls = [
        c
        for c in cur.execute.call_args_list
        if "UPDATE node_tickers" in c.args[0] and "is_primary = FALSE" in c.args[0]
    ]
    assert len(unprimary_calls) == 1


def test_update_node_exchange_qualified_ticker_splits_into_node_tickers():
    conn, cur = _mock_conn_for_update("private")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
            update_node("node-1", NodeUpdateBody(ticker="HKG: 9988"))

    # nodes.ticker cache holds the BARE ticker only, never "HKG:9988" — the
    # ~24 pre-existing read sites (search/list/typeahead) must keep showing
    # a plain ticker string.
    node_update_calls = [c for c in cur.execute.call_args_list if "UPDATE nodes SET" in c.args[0]]
    assert "9988" in node_update_calls[0].args[1]
    assert "HKG:9988" not in node_update_calls[0].args[1]

    insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO node_tickers" in c.args[0]]
    assert insert_calls[0].args[1] == ("node-1", "HKG", "9988")


def test_update_node_no_ticker_leaves_node_tickers_untouched():
    conn, cur = _mock_conn_for_update("dark_horse")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
            update_node("node-1", NodeUpdateBody(sector="Fintech"))
    insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO node_tickers" in c.args[0]]
    assert insert_calls == []


# ---------------------------------------------------------------------------
# GET/POST/DELETE /nodes/{id}/tickers
# ---------------------------------------------------------------------------


def test_list_node_tickers_404_when_node_missing():
    with patch.object(main, "query", return_value=[]):
        resp = list_node_tickers("missing")
    assert resp.status_code == 404


def test_list_node_tickers_returns_rows_primary_first():
    rows = [{"id": "t1", "exchange": "HKG", "ticker": "9988", "is_primary": True, "added_at": None}]
    with patch.object(main, "query", side_effect=[[{"id": "node-1"}], rows]):
        resp = list_node_tickers("node-1")
    assert resp == rows


def test_add_node_ticker_first_ticker_forced_primary():
    cur = MagicMock()
    # 1: node exists, 2: COUNT(*) == 0 (no existing tickers), 3: INSERT RETURNING id
    cur.fetchone.side_effect = [("node-1",), (0,), ("new-ticker-id",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = add_node_ticker("node-1", NodeTickerBody(ticker="AAPL", is_primary=False))
    assert resp["is_primary"] is True  # forced, even though caller said False
    conn.commit.assert_called_once()
    node_sync_calls = [c for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE nodes SET ticker")]
    assert len(node_sync_calls) == 1


def test_add_node_ticker_additional_not_primary_by_default():
    cur = MagicMock()
    cur.fetchone.side_effect = [("node-1",), (1,), ("new-ticker-id",)]  # 1 existing ticker already
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = add_node_ticker("node-1", NodeTickerBody(ticker="BABA", is_primary=False))
    assert resp["is_primary"] is False
    node_sync_calls = [c for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE nodes SET ticker")]
    assert node_sync_calls == []


def test_add_node_ticker_404_when_node_missing():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = add_node_ticker("missing", NodeTickerBody(ticker="AAPL"))
    assert resp.status_code == 404
    conn.rollback.assert_called_once()


def test_delete_node_ticker_promotes_next_when_primary_removed():
    cur = MagicMock()
    # 1: SELECT is_primary -> True, 2: SELECT next remaining ticker
    cur.fetchone.side_effect = [(True,), ("other-id", "BABA")]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = delete_node_ticker("node-1", "primary-id")
    assert resp == {"ok": True}
    promote_calls = [c for c in cur.execute.call_args_list if "is_primary = TRUE" in c.args[0]]
    assert len(promote_calls) == 1
    sync_calls = [c for c in cur.execute.call_args_list if c.args[0].startswith("UPDATE nodes SET ticker")]
    assert sync_calls[0].args[1] == ("BABA", "node-1")


def test_delete_node_ticker_clears_nodes_ticker_when_last_one_removed():
    cur = MagicMock()
    cur.fetchone.side_effect = [(True,), None]  # was primary, nothing left
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        delete_node_ticker("node-1", "primary-id")
    clear_calls = [c for c in cur.execute.call_args_list if "ticker = NULL" in c.args[0]]
    assert len(clear_calls) == 1


def test_delete_node_ticker_404_when_missing():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(main, "get_conn", return_value=conn):
        resp = delete_node_ticker("node-1", "missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /nodes/{id} — 404 + edge_summary shape
# ---------------------------------------------------------------------------


def test_get_node_detail_not_found():
    with patch.object(main, "query", return_value=[]):
        resp = get_node_detail("missing")
    assert resp.status_code == 404


def test_get_node_detail_includes_edge_summary():
    node_row = [
        {
            "id": "n1",
            "name": "Acme",
            "ticker": "ACME",
            "type": "public",
            "cik": None,
            "status": "active",
            "added_at": None,
            "added_by": "seed",
            "meta": {},
            "short_description": None,
            "sector": None,
            "is_public": True,
            "founded": None,
            "headquarters": None,
            "facts_source": None,
            "facts_fetched_at": None,
        }
    ]
    summary_row = [
        {
            "outgoing_count": 2,
            "incoming_count": 0,
            "outgoing_confirmed_usd": 500,
            "outgoing_estimated_usd": 0,
            "incoming_confirmed_usd": 0,
            "incoming_estimated_usd": 0,
        }
    ]

    calls = {"n": 0}

    def fake_query(sql, params=None):
        calls["n"] += 1
        return node_row if calls["n"] == 1 else summary_row

    with patch.object(main, "query", side_effect=fake_query):
        resp = get_node_detail("n1")
    assert resp["edge_summary"]["outgoing_count"] == 2
    assert resp["edge_summary"]["outgoing_total_usd"] == 500


def test_get_node_detail_edge_summary_splits_confirmed_and_estimated():
    """A node's incoming/outgoing total is confirmed+estimated,
    computed from the value_status-aware query, not a blind SUM(net_amount_usd)
    — the exact gap that let a node panel show a physically implausible total
    (confirmed live 2026-07-13 UAT: Anthropic "$987.8B received")."""
    node_row = [
        {
            "id": "n1",
            "name": "Acme",
            "ticker": "ACME",
            "type": "public",
            "cik": None,
            "status": "active",
            "added_at": None,
            "added_by": "seed",
            "meta": {},
            "short_description": None,
            "sector": None,
            "is_public": True,
            "founded": None,
            "headquarters": None,
            "facts_source": None,
            "facts_fetched_at": None,
        }
    ]
    summary_row = [
        {
            "outgoing_count": 0,
            "incoming_count": 3,
            "outgoing_confirmed_usd": 0,
            "outgoing_estimated_usd": 0,
            "incoming_confirmed_usd": 10_000_000_000,
            "incoming_estimated_usd": 45_000_000_000,
        }
    ]

    calls = {"n": 0}

    def fake_query(sql, params=None):
        calls["n"] += 1
        return node_row if calls["n"] == 1 else summary_row

    with patch.object(main, "query", side_effect=fake_query):
        resp = get_node_detail("n1")
    assert resp["edge_summary"]["incoming_confirmed_usd"] == 10_000_000_000
    assert resp["edge_summary"]["incoming_estimated_usd"] == 45_000_000_000
    assert resp["edge_summary"]["incoming_total_usd"] == 55_000_000_000


# ---------------------------------------------------------------------------
# _node_normalized_aliases — falls back to normalize(name) when no alias rows
# exist (the approve_candidate() path inserts into nodes with no self-alias)
# ---------------------------------------------------------------------------


def test_node_normalized_aliases_falls_back_to_name_when_no_aliases():
    def fake_query(sql, params=None):
        if "FROM nodes" in sql:
            return [{"name": "T. Rowe Price Group"}]
        return []  # no node_aliases rows

    with patch.object(pipeline_mod, "query", side_effect=fake_query):
        aliases = _node_normalized_aliases("n1")
    assert "t. rowe price group" in aliases or any("rowe" in a for a in aliases)


def test_node_normalized_aliases_missing_node_returns_empty():
    with patch.object(pipeline_mod, "query", return_value=[]):
        assert _node_normalized_aliases("missing") == []


# ---------------------------------------------------------------------------
# GET /news?node_id= — filter applied before LIMIT/OFFSET, both-direction match
# ---------------------------------------------------------------------------


def test_news_node_filter_uses_any_both_directions():
    with patch.object(pipeline_mod, "_node_normalized_aliases", return_value=["acme"]):
        with patch.object(pipeline_mod, "query", return_value=[]) as q:
            get_news(limit=10, offset=0, node_id="n1")
    sql, params = q.call_args[0]
    assert "normalized_investor = ANY(%s)" in sql
    assert "normalized_investee = ANY(%s)" in sql
    assert params[0] == ["acme"]
    assert params[1] == ["acme"]
    assert tuple(params[-2:]) == (10, 0)


def test_news_node_filter_short_circuits_when_no_aliases():
    with patch.object(pipeline_mod, "_node_normalized_aliases", return_value=[]):
        with patch.object(pipeline_mod, "query") as q:
            result = get_news(limit=10, offset=0, node_id="ghost")
    q.assert_not_called()
    assert result == []


def test_news_no_node_id_unfiltered_still_works():
    with patch.object(pipeline_mod, "query", return_value=[]) as q:
        get_news(limit=10, offset=0)
    sql, params = q.call_args[0]
    assert "normalized_investor = ANY" not in sql  # no node filter applied
    assert "pipeline_run_id = %s" not in sql
    assert params == (10, 0)


# ---------------------------------------------------------------------------
# GET /nodes/{id}/price — graceful no-ticker path (private/dark_horse nodes)
# ---------------------------------------------------------------------------


def test_price_no_ticker_returns_empty_points_not_error():
    with patch.object(main, "query", return_value=[{"ticker": None}]):
        resp = get_node_price("n1", range="1y")
    assert resp == {"ticker": None, "range": "1y", "points": [], "stale": False}


def test_price_missing_node_404():
    with patch.object(main, "query", return_value=[]):
        resp = get_node_price("missing", range="1y")
    assert resp.status_code == 404


def test_price_bad_range_400():
    with patch.object(main, "query", return_value=[{"ticker": "AAPL"}]):
        resp = get_node_price("n1", range="3q")
    assert resp.status_code == 400


def test_price_delegates_to_stockprice_module():
    # Get_node_price now makes a 2nd query for the node's primary
    # node_tickers row (exchange, ticker) before building the Yahoo symbol —
    # side_effect provides both calls in order. No node_tickers row (empty
    # 2nd result) falls back to the bare nodes.ticker with no suffix.
    with patch.object(main, "query", side_effect=[[{"ticker": "AAPL"}], []]):
        with patch.object(
            main,
            "get_price_history",
            return_value={"ticker": "AAPL", "range": "1y", "points": [1], "stale": False},
        ) as gp:
            resp = get_node_price("n1", range="1y")
    gp.assert_called_once_with("AAPL", "1y")
    assert resp["points"] == [1]


def test_price_uses_primary_node_ticker_exchange_for_yahoo_symbol():
    """A non-US-primary node (e.g. Alibaba's HKG listing '9988')
    needs its node_tickers exchange to build the Yahoo-expected symbol
    ('9988.HK') — the bare nodes.ticker cache alone ('9988') would fetch the
    wrong/no data from Yahoo. See stockprice.yahoo_symbol for the verified
    exchange->suffix mapping."""
    with patch.object(
        main,
        "query",
        side_effect=[
            [{"ticker": "9988"}],
            [{"exchange": "HKG", "ticker": "9988"}],
        ],
    ):
        with patch.object(
            main,
            "get_price_history",
            return_value={"ticker": "9988.HK", "range": "1y", "points": [1], "stale": False},
        ) as gp:
            resp = get_node_price("n1", range="1y")
    gp.assert_called_once_with("9988.HK", "1y")
    assert resp["points"] == [1]


# ---------------------------------------------------------------------------
# GET /nodes — extension: pagination, facts join, edge_count, sort.
# Kept backward-compatible with its typeahead (q/limit only, no offset).
# ---------------------------------------------------------------------------


def test_list_nodes_default_query_shape():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes()
    sql, params = q.call_args[0]
    assert "LEFT JOIN node_facts nf" in sql
    assert "edge_count" in sql
    assert "UNION ALL" in sql
    assert "WHERE" not in sql  # no q -> no filter clause
    assert params == (20, 0)  # limit, offset — old typeahead default preserved


def test_list_nodes_q_filters_name_and_ticker_only():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(q="acme")
    sql, params = q.call_args[0]
    assert "n.name ILIKE %s OR n.ticker ILIKE %s" in sql
    assert "sector ILIKE" not in sql  # q deliberately doesn't match sector
    assert params == ("%acme%", "%acme%", 20, 0)


def test_list_nodes_limit_capped_at_100():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(limit=500)
    _, params = q.call_args[0]
    assert params[-2] == 100


def test_list_nodes_limit_floor_at_1():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(limit=0)
    _, params = q.call_args[0]
    assert params[-2] == 1


def test_list_nodes_offset_passed_through_for_pagination():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(offset=40)
    _, params = q.call_args[0]
    assert params[-1] == 40


def test_list_nodes_negative_offset_clamped_to_zero():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(offset=-5)
    _, params = q.call_args[0]
    assert params[-1] == 0


def test_list_nodes_sort_edge_count_desc():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(sort="edge_count", order="desc")
    sql, _ = q.call_args[0]
    assert "ORDER BY edge_count DESC" in sql


def test_list_nodes_sort_defaults_to_name_asc():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes()
    sql, _ = q.call_args[0]
    assert "ORDER BY n.name ASC" in sql


def test_list_nodes_unknown_sort_column_falls_back_to_name():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(sort="'; DROP TABLE nodes; --")
    sql, _ = q.call_args[0]
    assert "ORDER BY n.name" in sql


def test_list_nodes_unknown_order_falls_back_to_asc():
    with patch.object(main, "query", return_value=[]) as q:
        list_nodes(sort="edge_count", order="sideways")
    sql, _ = q.call_args[0]
    assert "ORDER BY edge_count ASC" in sql


def test_list_nodes_returns_rows_unmodified():
    rows = [
        {
            "id": "n1",
            "name": "Acme",
            "ticker": "ACME",
            "type": "public",
            "sector": "Tech",
            "country": "US",
            "is_public": True,
            "edge_count": 3,
        }
    ]
    with patch.object(main, "query", return_value=rows):
        result = list_nodes()
    assert result == rows


# ---------------------------------------------------------------------------
# GET /graph/current carries `exchange` (primary node_tickers row's
# exchange, '' normalized to NULL) for the Graph-tab exchange filter.
# ---------------------------------------------------------------------------


def test_graph_current_nodes_query_joins_primary_node_ticker_exchange():
    with patch.object(graph_mod, "query", side_effect=[[], []]) as q:
        get_current_graph()
    nodes_sql = q.call_args_list[0].args[0]
    assert "node_tickers" in nodes_sql
    assert "nt.is_primary" in nodes_sql
    assert "NULLIF(nt.exchange, '') AS exchange" in nodes_sql


# ---------------------------------------------------------------------------
# Evidence-gated public -> private demotion (acquisition/delisting)
# ---------------------------------------------------------------------------

_DEMOTION_EVIDENCE = {
    "node_id": "node-1",
    "name": "Metsera, Inc.",
    "evidence_edge_id": "edge-1",
    "acquirer_node_id": "acquirer-1",
    "acquirer_name": "Pfizer Inc.",
    "amount_usd": 7_000_000_000,
}


def test_update_node_public_to_private_rejected_without_evidence():
    conn, cur = _mock_conn_for_update("public")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "check_acquisition_demotion_evidence", return_value=None):
            resp = update_node("node-1", NodeUpdateBody(type="private"))
    assert resp.status_code == 400
    assert "acquisition evidence" in resp.body.decode()
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_update_node_public_to_private_allowed_with_evidence():
    conn, cur = _mock_conn_for_update("public")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "check_acquisition_demotion_evidence", return_value=_DEMOTION_EVIDENCE):
            with patch.object(main, "_node_detail_row", return_value={"id": "node-1", "type": "private"}):
                resp = update_node("node-1", NodeUpdateBody(type="private"))
    conn.commit.assert_called_once()
    assert resp == {"id": "node-1", "type": "private"}

    # ticker cache cleared (no explicit new ticker given on this call).
    node_update_calls = [
        c for c in cur.execute.call_args_list if "UPDATE nodes SET" in c.args[0] and "meta = COALESCE" not in c.args[0]
    ]
    assert len(node_update_calls) == 1
    assert "ticker = NULL" in node_update_calls[0].args[0]

    # existing node_tickers rows marked historical, not deleted.
    deactivate_calls = [
        c for c in cur.execute.call_args_list if "UPDATE node_tickers" in c.args[0] and "active = FALSE" in c.args[0]
    ]
    assert len(deactivate_calls) == 1
    assert deactivate_calls[0].args[1] == ("node-1",)

    # Auditable meta marker written, mirroring its auto_promotion shape.
    meta_calls = [c for c in cur.execute.call_args_list if "SET meta = COALESCE" in c.args[0]]
    assert len(meta_calls) == 1
    written = meta_calls[0].args[1][0]
    # psycopg2.extras.Json wraps the dict — unwrap via its adapted attribute.
    payload = written.adapted if hasattr(written, "adapted") else written
    assert payload["acquisition_demotion"]["reason"] == "subsidiary_edge_incoming"
    assert payload["acquisition_demotion"]["evidence_edge_id"] == "edge-1"
    assert payload["acquisition_demotion"]["source"] == "endpoint_evidence_gated"
    assert payload["acquisition_demotion_candidate"] is None


def test_update_node_public_to_private_with_explicit_new_ticker_keeps_it():
    """A human demoting AND setting a new ticker in the same call (e.g. a
    relisting under a new symbol) should not have their explicit ticker
    clobbered by the NULL-clear path — but old node_tickers rows are still
    marked historical."""
    conn, cur = _mock_conn_for_update("public")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "check_acquisition_demotion_evidence", return_value=_DEMOTION_EVIDENCE):
            with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
                update_node("node-1", NodeUpdateBody(type="private", ticker="NEWCO"))

    node_update_calls = [
        c for c in cur.execute.call_args_list if "UPDATE nodes SET" in c.args[0] and "meta = COALESCE" not in c.args[0]
    ]
    assert "ticker = NULL" not in node_update_calls[0].args[0]
    assert "NEWCO" in node_update_calls[0].args[1]

    deactivate_calls = [
        c for c in cur.execute.call_args_list if "UPDATE node_tickers" in c.args[0] and "active = FALSE" in c.args[0]
    ]
    assert len(deactivate_calls) == 1


def test_update_node_private_to_public_does_not_check_demotion_evidence():
    """The evidence check is only relevant for public -> private — must not
    fire (and must not gate) any other transition."""
    conn, cur = _mock_conn_for_update("private")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "check_acquisition_demotion_evidence") as mock_check:
            with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
                update_node("node-1", NodeUpdateBody(type="public"))
    mock_check.assert_not_called()


def test_update_node_public_noop_type_does_not_check_demotion_evidence():
    """type == current_type is a no-op re-save, not a transition — must not
    trigger the evidence check."""
    conn, cur = _mock_conn_for_update("public")
    with patch.object(main, "get_conn", return_value=conn):
        with patch.object(main, "check_acquisition_demotion_evidence") as mock_check:
            with patch.object(main, "_node_detail_row", return_value={"id": "node-1"}):
                update_node("node-1", NodeUpdateBody(sector="Biotech"))
    mock_check.assert_not_called()
