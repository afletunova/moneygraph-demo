from fastapi import APIRouter

from ...db import query

router = APIRouter()


@router.get("/graph/current")
def get_current_graph():
    # `exchange` (a node's primary ticker's exchange) feeds the
    # Graph-tab exchange filter (FilterBar/applyFilters, same highlight-and-dim
    # pattern as its category/country/role dimensions). '' (this
    # codebase's node_tickers "no exchange qualifier" sentinel) is normalised
    # to NULL here so the frontend's existing `d.exchange && ...` truthiness
    # checks treat "no exchange" exactly like "no sector"/"no country" today —
    # never matches any filter value, doesn't show up as a bogus '' option.
    nodes = query("""
        SELECT n.id::text, n.name, n.ticker, n.type::text AS type, n.cik,
               n.status::text AS status, n.added_at, n.added_by, n.meta,
               nf.short_description, nf.sector, nf.is_public, nf.founded, nf.headquarters, nf.country,
               NULLIF(nt.exchange, '') AS exchange
        FROM nodes n
        LEFT JOIN node_facts nf ON nf.node_id = n.id
        LEFT JOIN node_tickers nt ON nt.node_id = n.id AND nt.is_primary
        ORDER BY n.name
    """)
    for n in nodes:
        if n.get("added_at") is not None:
            n["added_at"] = n["added_at"].isoformat()

    # confirmed_amount_usd (sum of 'actual' events) and
    # estimated_amount_usd (sum of 'estimated' events, INCLUDING any 'actual'
    # event superseded by a correction row) are reported alongside
    # net_amount_usd — never collapsed into one figure. Confirmed + estimated always ==
    # net_amount_usd: correction rows (delta_usd=0) are excluded from the sum
    # and every other event counts exactly once as either actual or estimated.
    edges = query("""
        WITH per_event AS (
            SELECT
                ev.id,
                ev.edge_id,
                ev.delta_usd,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM investment_events c WHERE c.corrects_event_id = ev.id
                    ) THEN 'estimated'
                    ELSE ev.value_status
                END AS effective_status
            FROM investment_events ev
            WHERE ev.corrects_event_id IS NULL
        ),
        totals AS (
            SELECT edge_id,
                   COALESCE(SUM(delta_usd) FILTER (WHERE effective_status = 'actual'), 0)
                       AS confirmed_amount_usd,
                   COALESCE(SUM(delta_usd) FILTER (WHERE effective_status = 'estimated'), 0)
                       AS estimated_amount_usd
            FROM per_event
            GROUP BY edge_id
        )
        SELECT e.id::text,
               e.from_node_id::text,
               e.to_node_id::text,
               fn.name AS from_name,
               tn.name AS to_name,
               e.net_amount_usd,
               COALESCE(t.confirmed_amount_usd, 0) AS confirmed_amount_usd,
               COALESCE(t.estimated_amount_usd, 0) AS estimated_amount_usd,
               e.status::text AS status,
               e.source_count,
               e.is_confirmed,
               e.last_confirmed
        FROM edges e
        JOIN nodes fn ON fn.id = e.from_node_id
        JOIN nodes tn ON tn.id = e.to_node_id
        LEFT JOIN totals t ON t.edge_id = e.id
        ORDER BY e.net_amount_usd DESC
    """)
    for e in edges:
        if e.get("last_confirmed") is not None:
            e["last_confirmed"] = e["last_confirmed"].isoformat()

    return {"nodes": nodes, "edges": edges}


@router.get("/edges/{edge_id}/events")
def get_edge_events(edge_id: str):
    # Value_status/estimate_reason/corrects_event_id expose the
    # syndicate-round classification per event so the UI can show WHY a given
    # row counts toward "estimated" rather than "confirmed" (see /graph/current
    # confirmed_amount_usd / estimated_amount_usd).
    rows = query(
        """
        SELECT id::text, edge_id::text, delta_usd, event_type::text,
               event_date, source_url, source_tier, confidence::text,
               raw_excerpt, created_at, value_status, estimate_reason,
               corrects_event_id::text
        FROM investment_events
        WHERE edge_id = %s
        ORDER BY event_date DESC
    """,
        (edge_id,),
    )
    for r in rows:
        if r.get("event_date") is not None:
            r["event_date"] = r["event_date"].isoformat()
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].isoformat()
    return rows
