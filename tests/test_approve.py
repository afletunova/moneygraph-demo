"""
Unit tests for approve_candidate's duplicate guard (queue-ops 2026-07-11).

No live DB: get_conn() is mocked with a scripted fake cursor/connection.
Covers the happy path (no collision -> unchanged INSERT behaviour), the
nodes.name collision (409, no write), the node_aliases collision (409, no
write), and the pre-existing not-found path.
"""

from unittest.mock import MagicMock, patch

import moneygraph.api.routers.candidates as main


class _FakeCursor:
    """Cursor whose fetchone()/fetchall() pop from scripted queues; records executes."""

    def __init__(self, fetchone_results=None, fetchall_results=None):
        self._fetchone = list(fetchone_results or [])
        self._fetchall = list(fetchall_results or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0)

    def fetchall(self):
        return self._fetchall.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _conn_with(fetchone_results=None, fetchall_results=None):
    cur = _FakeCursor(fetchone_results, fetchall_results)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _body(name="Broadcom Inc.", type_="public", ticker="AVGO"):
    return main.ApproveBody(name=name, type=type_, ticker=ticker)


# ---------------------------------------------------------------------------
# Not found — unchanged from before the guard
# ---------------------------------------------------------------------------


def test_approve_candidate_not_found_404():
    conn, _ = _conn_with(fetchone_results=[None])
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.approve_candidate("missing", _body())
    assert resp.status_code == 404
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# No collision — happy path, INSERT still happens, cascade still runs
# ---------------------------------------------------------------------------


def test_approve_no_collision_inserts_node():
    conn, cur = _conn_with(
        fetchone_results=[
            ("cand-1",),  # pending lookup
            None,  # node_aliases lookup -> no alias match
            ("node-new",),  # INSERT ... RETURNING id
        ],
        fetchall_results=[
            [("node-a", "Some Other Co"), ("node-b", "Unrelated Inc.")],  # nodes scan
            [],  # cascade UPDATE ... RETURNING
        ],
    )
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.approve_candidate("cand-1", _body(name="Broadcom Inc."))

    assert resp["node_id"] == "node-new"
    assert resp["cascade_count"] == 0
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO nodes" in sqls


# ---------------------------------------------------------------------------
# Collision on nodes.name (normalized) — 409, nothing written
# ---------------------------------------------------------------------------


def test_approve_duplicate_node_name_409():
    conn, cur = _conn_with(
        fetchone_results=[("cand-1",)],  # pending lookup
        fetchall_results=[
            [("node-existing", "Broadcom, Inc.")],  # nodes scan: normalizes to same as "Broadcom Inc."
        ],
    )
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.approve_candidate("cand-1", _body(name="Broadcom Inc."))

    assert resp.status_code == 409
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO nodes" not in sqls


# ---------------------------------------------------------------------------
# Collision via node_aliases — 409, nothing written
# ---------------------------------------------------------------------------


def test_approve_duplicate_alias_409():
    conn, cur = _conn_with(
        fetchone_results=[
            ("cand-1",),  # pending lookup
            ("node-owner",),  # node_aliases lookup -> match
            ("Owner Co",),  # owner name lookup
        ],
        fetchall_results=[
            [("node-a", "Totally Different Co")],  # nodes scan: no direct match
        ],
    )
    with patch.object(main, "get_conn", return_value=conn):
        resp = main.approve_candidate("cand-1", _body(name="Owner Co Alias"))

    assert resp.status_code == 409
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    sqls = " ".join(s for s, _ in cur.executed)
    assert "INSERT INTO nodes" not in sqls
