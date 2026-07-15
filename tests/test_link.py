"""
Unit tests for linking a candidate to an existing node.

No live DB: get_conn() is mocked with a scripted fake cursor. Covers the link
endpoint's alias registration, idempotent no-op, 409-on-different-node, and
candidate/node not-found paths.
"""

from unittest.mock import MagicMock, patch

import moneygraph.api.routers.candidates as main


class _FakeCursor:
    """Cursor whose fetchone() pops from a scripted list; records executes."""

    def __init__(self, fetch_results):
        self._fetch = list(fetch_results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _conn_with(fetch_results):
    cur = _FakeCursor(fetch_results)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _body(node_id):
    return main.LinkBody(node_id=node_id)


# ---------------------------------------------------------------------------
# Happy path — alias inserted, candidate resolved, committed
# ---------------------------------------------------------------------------


def test_link_success_inserts_alias_and_resolves():
    # candidate lookup, node lookup, alias-owner lookup (None → insert)
    conn, cur = _conn_with([("Waymo", "waymo"), ("Waymo LLC",), None])
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.link_candidate("cand-1", _body("node-1"))

    assert resp["node_id"] == "node-1"
    assert resp["node_name"] == "Waymo LLC"
    assert resp["aliases_added"] == 1
    conn.commit.assert_called_once()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO node_aliases" in sqls
    assert "'user_approved'" in sqls
    assert "UPDATE candidates" in sqls
    # never a DELETE
    assert "DELETE" not in sqls.upper()


# ---------------------------------------------------------------------------
# Idempotent — alias already maps to THIS node → no insert, still resolves
# ---------------------------------------------------------------------------


def test_link_idempotent_same_node():
    conn, cur = _conn_with([("Waymo", "waymo"), ("Waymo LLC",), ("node-1",)])
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.link_candidate("cand-1", _body("node-1"))

    assert resp["aliases_added"] == 0
    assert resp["aliases_existing"] == 1
    conn.commit.assert_called_once()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO node_aliases" not in sqls  # no duplicate insert
    assert "UPDATE candidates" in sqls  # candidate still resolved


# ---------------------------------------------------------------------------
# Conflict — alias maps to a DIFFERENT node → 409, nothing written
# ---------------------------------------------------------------------------


def test_link_conflict_different_node_409():
    conn, cur = _conn_with(
        [
            ("Waymo", "waymo"),  # candidate
            ("Waymo LLC",),  # target node name
            ("other-node",),  # alias owner (different)
            ("Other Co",),  # owner's name
        ]
    )
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.link_candidate("cand-1", _body("node-1"))

    assert resp.status_code == 409
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO node_aliases" not in sqls  # no write on conflict
    assert "UPDATE candidates" not in sqls


# ---------------------------------------------------------------------------
# Not-found paths
# ---------------------------------------------------------------------------


def test_link_candidate_not_found_404():
    conn, _ = _conn_with([None])
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.link_candidate("missing", _body("node-1"))
    assert resp.status_code == 404
    conn.commit.assert_not_called()


def test_link_node_not_found_404():
    conn, _ = _conn_with([("Waymo", "waymo"), None])
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.link_candidate("cand-1", _body("nope"))
    assert resp.status_code == 404
    conn.commit.assert_not_called()
