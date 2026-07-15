import os

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def query(sql: str, params=None) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql: str, params=None) -> list[tuple]:
    """Run a DML statement and return any RETURNING rows."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns bump_run_counters is allowed to touch — an explicit allowlist so a
# typo'd kwarg fails loudly instead of silently building an f-string with an
# arbitrary column/SQL fragment.
_RUN_COUNTER_COLUMNS = {
    "nodes_processed",
    "edges_created",
    "candidates_found",
    "events_logged",
    "search_calls_made",
    "units_processed",
}


def bump_run_counters(run_id: str, **deltas: int) -> None:
    """Atomically increment integer counter columns on one pipeline_runs row.

    phases call this after each unit of work (node / filing / article)
    so the Runs-tab 5s poll can show live counts climbing while a run is still
    `status='running'`, instead of only a final total written at completion.
    Increment-based (col = col + %s), not an absolute SET, so it composes with
    concurrent-looking call sites without needing to track a running total by
    hand, and a later absolute reconciliation UPDATE (existing end-of-phase
    writes, left in place) is idempotent on top of it.
    """
    if not deltas:
        return
    bad = set(deltas) - _RUN_COUNTER_COLUMNS
    if bad:
        raise ValueError(f"bump_run_counters: unknown column(s) {bad}")
    set_clause = ", ".join(f"{col} = {col} + %s" for col in deltas)
    execute(
        f"UPDATE pipeline_runs SET {set_clause} WHERE id = %s",
        (*deltas.values(), run_id),
    )


def set_run_total_units(run_id: str, total: int) -> None:
    """Set pipeline_runs.total_units ONCE at the start of a phase.

    An absolute SET, not an increment — total_units is computed once (the
    real amount of work about to be attempted, post idempotency-skip) and
    never revised mid-run. Distinct from bump_run_counters, which only ever
    increments.
    """
    execute("UPDATE pipeline_runs SET total_units = %s WHERE id = %s", (total, run_id))
