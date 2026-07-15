import re
from datetime import datetime, timezone
from typing import Literal

import psycopg2.extras
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from ...core.enrichment import check_acquisition_demotion_evidence
from ...core.stockprice import get_price_history, yahoo_symbol
from ...db import get_conn, query

router = APIRouter()


# Bare ticker (unchanged from pre-existing behaviour) OR an
# exchange-qualified ticker like "HKG: 9988" / "HKG:9988". Canonical stored
# form (what `NodeUpdateBody.ticker` / `NodeTickerBody.ticker` end up holding
# after validation) is "EXCHANGE:TICKER" with no space, or a bare ticker with
# no colon at all when no exchange was given.
_EXCHANGE_TICKER_RE = re.compile(r"^([A-Z]{1,10})\s*:\s*([A-Z0-9.\-]{1,10})$")
_BARE_TICKER_RE = re.compile(r"[A-Z0-9.\-]{1,10}")


def _validate_ticker_str(v: str) -> str:
    """Shared ticker-string validation for both the single-ticker
    NodeUpdateBody.ticker field and NodeTickerBody.ticker. Accepts:
      - a bare ticker ("AAPL", "BRK.B") — validated against the original
        regex, unchanged; backward compatible with every pre-        caller/test.
      - an exchange-qualified ticker ("HKG: 9988", "HKG:9988") — split on
        ':', exchange letters + ticker portion each validated, re-joined
        into a canonical "EXCHANGE:TICKER" (no space) form for storage.
    Raises ValueError with the original message shape on anything else.
    """
    v = v.strip().upper()
    m = _EXCHANGE_TICKER_RE.fullmatch(v)
    if m:
        exchange, ticker = m.group(1), m.group(2)
        return f"{exchange}:{ticker}"
    if not _BARE_TICKER_RE.fullmatch(v):
        raise ValueError(
            "ticker must be 1-10 chars: letters, digits, '.', '-' only "
            "(optionally prefixed with 'EXCHANGE:', e.g. 'HKG: 9988')"
        )
    return v


def split_ticker_field(v: str | None) -> tuple[str, str | None]:
    """Split a validated NodeUpdateBody/NodeTickerBody.ticker string into
    (exchange, ticker). Bare tickers (no colon) -> exchange=''  (this
    codebase's node_tickers sentinel for "no exchange qualifier" — see
    017_node_tickers.sql for why '' rather than NULL). Returns
    ("", None) for v is None (nothing to split).
    """
    if v is None:
        return "", None
    if ":" in v:
        exchange, ticker = v.split(":", 1)
        return exchange, ticker
    return "", v


class NodeUpdateBody(BaseModel):
    """Node-level edit (type/ticker/cik/sector). All fields optional —
    only provided fields are changed. Type transitions are restricted, see
    `_ALLOWED_TYPE_TRANSITIONS` below.

    `ticker` now also accepts an exchange-qualified value ("HKG:
    9988"). See `_validate_ticker_str` / `split_ticker_field` above — the
    endpoint upserts the parsed (exchange, ticker) into `node_tickers` as the
    node's primary and syncs the bare ticker portion back onto `nodes.ticker`
    (see update_node's docstring for why the cache stays bare, not
    "EXCHANGE:TICKER").
    """

    type: Literal["public", "private", "dark_horse"] | None = None
    ticker: str | None = None
    cik: str | None = None
    sector: str | None = None
    meta_patch: dict | None = None
    """Merged into nodes.meta (JSONB, via ||) when provided. Not
    exposed on the manual node-edit UI form — used internally by dark_horse
    auto-promotion to leave an auditable 'the pipeline did this, not a human'
    marker (see enrichment.check_dark_horse_promotion). Harmless if a human
    caller sets it too; no validation needed, it's opaque JSON."""

    @field_validator("ticker")
    @classmethod
    def _ticker_format(cls, v):
        if v is None or v.strip() == "":
            return None
        return _validate_ticker_str(v)

    @field_validator("cik")
    @classmethod
    def _cik_format(cls, v):
        if v is None or v.strip() == "":
            return None
        v = v.strip()
        if not re.fullmatch(r"\d{1,10}", v):
            raise ValueError("cik must be 1-10 digits")
        return v.zfill(10)


class NodeTickerBody(BaseModel):
    """One row of POST /nodes/{id}/tickers — adding an additional
    (non-primary, usually) ticker/exchange pair to a node that already has
    one, e.g. registering Alibaba's NYSE ADR "BABA" alongside its HKG primary
    listing "9988". `ticker` accepts the same bare-or-exchange-qualified
    shape as NodeUpdateBody.ticker (see _validate_ticker_str).
    """

    ticker: str
    is_primary: bool = False

    @field_validator("ticker")
    @classmethod
    def _ticker_format(cls, v):
        if v is None or v.strip() == "":
            raise ValueError("ticker is required")
        return _validate_ticker_str(v)


# Node.type transition rules:
#   dark_horse -> public | private | dark_horse   (anything — it's unresolved by definition)
#   private    -> public | private                (the one explicit ask)
#   public     -> public | private                (evidence-gated demotion only)
# Blocked and why:
#   - public -> dark_horse: no real-world case this models (regression from
#     "confirmed public-market history" to "unresolved rumour" isn't a thing
#     that happens). Not requested — left blocked.
#   - private -> dark_horse: would be a regression from "known, named private
#     company" to "rumoured/unconfirmed" status. Only the forward direction
#     (private -> public) is requested; reversing it isn't in scope and
#     doesn't have an obvious real-world trigger. Revisit if a concrete case
#     shows up (e.g. a mis-approved candidate that should never have been
#     resolved to a named node).
#
# public -> private: membership in this set is necessary but NOT
# sufficient — update_node additionally requires
# check_acquisition_demotion_evidence(node_id) to return real evidence (an
# existing subsidiary edge with this node as the investee) before allowing
# it through. This is intentionally NOT a blanket reopen of public -> private
# (that would defeat the original "stop accidental reversal of real IPO
# status" purpose the guard was built for) — see update_node's
# docstring and check_acquisition_demotion_evidence's docstring (enrichment.py)
# for the full reasoning (real recurring case: acquisitions, e.g. Pfizer ->
# Metsera, Inc.).
_ALLOWED_TYPE_TRANSITIONS = {
    "dark_horse": {"public", "private", "dark_horse"},
    "private": {"public", "private"},
    "public": {"public", "private"},
}


_NODE_SORT_COLUMNS = {
    "name": "n.name",
    "ticker": "n.ticker",
    "type": "n.type::text",
    "sector": "nf.sector",
    "country": "nf.country",
    "edge_count": "edge_count",
}


@router.get("/nodes")
def list_nodes(
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "name",
    order: str = "asc",
):
    """Node list.

    Originally a lightweight typeahead (q/limit only, id/name/ticker/
    type). Extended in place for the full browsable "Nodes" tab
    rather than adding a parallel endpoint: the new params (offset/sort/
    order) all default to the old behaviour (offset=0, sort=name asc), and
    the new response fields are additive, so existing callers (CandidateRow's
    link-typeahead) are unaffected — verified it only reads id/name/ticker/
    type off each row.

    Adds: `offset` for pagination past the old 100-row cap (limit is still
    capped at 100 per page — use offset to page further, same convention as
    GET /news); sector/country/is_public via a node_facts LEFT JOIN; and an
    aggregated edge_count per node via a single UNION ALL + GROUP BY subquery
    (not row-by-row — same data _node_detail_row's edge_summary computes per
    node, but batched here for the whole list in one query).

    Follow-up: `acquisition_flagged` is a cheap boolean read of
    `nodes.meta->'acquisition_demotion_candidate'` (no new logic — that field
    is written elsewhere by enrichment.flag_acquisition_demotion_candidate /
    extraction/pipeline.py) so the Nodes tab can show a small indicator
    without a per-row detail fetch.

    `q` matches name/ticker only (not sector), same as before. Kept
    that way on purpose: sector strings are sparse free text, and folding
    them into the identity search would blur "find this company" with
    "browse this category" — a separate sector filter (not built here, out
    of scope) would be the cleaner way to filter by sector.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    sort_col = _NODE_SORT_COLUMNS.get(sort, "n.name")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"

    where_sql = ""
    params: list = []
    if q:
        where_sql = "WHERE n.name ILIKE %s OR n.ticker ILIKE %s"
        params.extend([f"%{q}%", f"%{q}%"])

    sql = f"""
        SELECT n.id::text, n.name, n.ticker, n.type::text AS type,
               nf.sector, nf.country, nf.is_public,
               COALESCE(ec.edge_count, 0) AS edge_count,
               jsonb_typeof(n.meta -> 'acquisition_demotion_candidate') = 'object' AS acquisition_flagged
        FROM nodes n
        LEFT JOIN node_facts nf ON nf.node_id = n.id
        LEFT JOIN (
            SELECT node_id, COUNT(*) AS edge_count FROM (
                SELECT from_node_id AS node_id FROM edges
                UNION ALL
                SELECT to_node_id AS node_id FROM edges
            ) e GROUP BY node_id
        ) ec ON ec.node_id = n.id
        {where_sql}
        ORDER BY {sort_col} {order_sql} NULLS LAST, n.name ASC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    return query(sql, tuple(params))


def _node_detail_row(node_id: str) -> dict | None:
    rows = query(
        """
        SELECT n.id::text, n.name, n.ticker, n.type::text AS type, n.cik,
               n.status::text AS status, n.added_at, n.added_by, n.meta,
               nf.short_description, nf.sector, nf.is_public, nf.founded,
               nf.headquarters, nf.country, nf.source AS facts_source, nf.fetched_at AS facts_fetched_at
        FROM nodes n
        LEFT JOIN node_facts nf ON nf.node_id = n.id
        WHERE n.id = %s
        """,
        (node_id,),
    )
    if not rows:
        return None
    node = rows[0]
    if node.get("added_at") is not None:
        node["added_at"] = node["added_at"].isoformat()
    if node.get("facts_fetched_at") is not None:
        node["facts_fetched_at"] = node["facts_fetched_at"].isoformat()

    # Incoming/outgoing totals split into confirmed ('actual' events)
    # vs estimated ('estimated' events, e.g. a syndicate-round total
    # misattributed to a co-investor with no per-investor breakdown) — same
    # per_event/effective_status pattern as GET /graph/current's
    # confirmed_amount_usd/estimated_amount_usd, never collapsed into one
    # blind SUM(net_amount_usd). A blind sum is exactly what let a node's
    # "total received" read as a physically implausible number (confirmed
    # live: Anthropic showing "$987.8B received").
    summary = query(
        """
        WITH per_event AS (
            SELECT ev.id, ev.edge_id, ev.delta_usd,
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
                   COALESCE(SUM(delta_usd) FILTER (WHERE effective_status = 'actual'), 0) AS confirmed_usd,
                   COALESCE(SUM(delta_usd) FILTER (WHERE effective_status = 'estimated'), 0) AS estimated_usd
            FROM per_event
            GROUP BY edge_id
        )
        SELECT
            (SELECT COUNT(*) FROM edges WHERE from_node_id = %s) AS outgoing_count,
            (SELECT COUNT(*) FROM edges WHERE to_node_id = %s) AS incoming_count,
            (SELECT COALESCE(SUM(t.confirmed_usd), 0) FROM edges e LEFT JOIN totals t ON t.edge_id = e.id
             WHERE e.from_node_id = %s) AS outgoing_confirmed_usd,
            (SELECT COALESCE(SUM(t.estimated_usd), 0) FROM edges e LEFT JOIN totals t ON t.edge_id = e.id
             WHERE e.from_node_id = %s) AS outgoing_estimated_usd,
            (SELECT COALESCE(SUM(t.confirmed_usd), 0) FROM edges e LEFT JOIN totals t ON t.edge_id = e.id
             WHERE e.to_node_id = %s) AS incoming_confirmed_usd,
            (SELECT COALESCE(SUM(t.estimated_usd), 0) FROM edges e LEFT JOIN totals t ON t.edge_id = e.id
             WHERE e.to_node_id = %s) AS incoming_estimated_usd
        """,
        (node_id, node_id, node_id, node_id, node_id, node_id),
    )[0]
    summary["outgoing_total_usd"] = summary["outgoing_confirmed_usd"] + summary["outgoing_estimated_usd"]
    summary["incoming_total_usd"] = summary["incoming_confirmed_usd"] + summary["incoming_estimated_usd"]
    node["edge_summary"] = summary
    return node


@router.get("/nodes/{node_id}")
def get_node_detail(node_id: str):
    """
    Node-click side panel data source.

    Design call: the frontend's `/graph/current` payload already carries
    facts (short_description/sector/is_public/founded/headquarters) for
    every node in memory, so the panel could in principle read straight off
    graph state with zero extra requests. This endpoint exists anyway for two
    reasons: (1) after an edit (POST /nodes/{id}/update) the panel needs a
    cheap way to refresh just that node without re-fetching the whole graph,
    and (2) it adds an edge_summary (in/out edge counts + $ totals) that
    isn't in /graph/current and isn't worth widening that endpoint for.
    """
    node = _node_detail_row(node_id)
    if node is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return node


@router.post("/nodes/{node_id}/update")
def update_node(node_id: str, body: NodeUpdateBody):
    """Edit node.type/ticker/cik and node_facts.sector (category).

    Real mutation on non-append-only tables (nodes/node_facts aren't append-only —
    investment_events/node_aliases/sources are). Type transitions are
    restricted — see _ALLOWED_TYPE_TRANSITIONS.

    `ticker` accepts an exchange-qualified value ("HKG: 9988") in
    addition to the original bare form. It is parsed (split_ticker_field)
    into (exchange, ticker) and upserted into `node_tickers` as THIS node's
    primary row (any previously-primary row for the node is un-primaried
    first — one field, one edit, one primary). `nodes.ticker` — the cache the
    ~24 pre-existing read sites (search/typeahead/node-list/node-detail) all
    read directly — is synced to the BARE ticker portion only, never
    "EXCHANGE:TICKER", so none of those call sites need to change: a node
    with primary "HKG:9988" still shows "9988" in the node list exactly like
    a plain US ticker would. The exchange qualifier is only surfaced (a) in
    the node detail panel's ticker list (GET /nodes/{id}/tickers) and (b) via
    node_tickers for the Yahoo price lookup (see get_node_price) and the
    Graph-tab exchange filter (see get_current_graph).

    `public -> private` is allowed ONLY when
    check_acquisition_demotion_evidence(node_id) finds a real subsidiary edge
    with this node as the investee (see that function's docstring in
    enrichment.py for why this is evidence-gated rather than a blanket
    reopen). When it fires:
      - `nodes.ticker` is cleared (set NULL) unless this same call also
        supplies a new `ticker` explicitly (rare — a human editing both at
        once wins).
      - every currently-active `node_tickers` row for the node is marked
        `active = FALSE, delisted_at = NOW()` — not deleted (this project
        prefers non-destructive history, e.g. node_aliases; a delisted
        ticker is a fact worth keeping, not noise).
      - an `acquisition_demotion` marker is merged into `nodes.meta`
        (auditable, mirrors its `auto_promotion` marker) and any
        pending `acquisition_demotion_candidate` flag (see
        enrichment.flag_acquisition_demotion_candidate) is cleared since
        it's now resolved.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT type::text AS type FROM nodes WHERE id = %s", (node_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": "not found"})
            current_type = row[0]

            demotion_evidence = None
            if body.type is not None and body.type != current_type:
                allowed = _ALLOWED_TYPE_TRANSITIONS.get(current_type, set())
                if body.type not in allowed:
                    conn.rollback()
                    return JSONResponse(
                        status_code=400,
                        content={"error": f"cannot transition node type '{current_type}' -> '{body.type}'"},
                    )
                if current_type == "public" and body.type == "private":
                    # Set membership above is necessary but not
                    # sufficient — this direction additionally requires
                    # real evidence (see docstring).
                    demotion_evidence = check_acquisition_demotion_evidence(node_id)
                    if demotion_evidence is None:
                        conn.rollback()
                        return JSONResponse(
                            status_code=400,
                            content={
                                "error": (
                                    "cannot transition node type 'public' -> 'private': "
                                    "no acquisition evidence found (needs an existing "
                                    "subsidiary edge with this node as the investee)"
                                )
                            },
                        )

            ticker_exchange, ticker_bare = split_ticker_field(body.ticker)

            node_sets, node_params = [], []
            if body.type is not None:
                node_sets.append("type = %s::node_type")
                node_params.append(body.type)
            if body.ticker is not None:
                node_sets.append("ticker = %s")
                node_params.append(ticker_bare)
            elif demotion_evidence is not None:
                # No explicit new ticker given on a demotion — clear
                # the now-defunct one from the cache column read by the ~24
                # existing read sites. node_tickers keeps the historical row
                # (marked inactive below), so nothing is actually lost.
                node_sets.append("ticker = NULL")
            if body.cik is not None:
                node_sets.append("cik = %s")
                node_params.append(body.cik)

            if node_sets:
                node_params.append(node_id)
                cur.execute(f"UPDATE nodes SET {', '.join(node_sets)} WHERE id = %s", node_params)

            if body.ticker is not None:
                # This edit's ticker becomes the primary — un-primary any
                # existing primary row for this node first (partial unique
                # index idx_node_tickers_one_primary allows only one).
                cur.execute(
                    "UPDATE node_tickers SET is_primary = FALSE WHERE node_id = %s AND is_primary",
                    (node_id,),
                )
                cur.execute(
                    """
                    INSERT INTO node_tickers (node_id, exchange, ticker, is_primary)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (node_id, exchange, ticker) DO UPDATE SET is_primary = TRUE
                    """,
                    (node_id, ticker_exchange, ticker_bare),
                )

            if demotion_evidence is not None:
                # Mark every currently-active node_tickers row for
                # this node historical (non-destructive — see docstring).
                # Runs regardless of whether body.ticker was also set, so a
                # simultaneous "set new ticker + demote" call doesn't leave
                # a stale active old-ticker row behind.
                cur.execute(
                    """
                    UPDATE node_tickers
                    SET active = FALSE, delisted_at = NOW()
                    WHERE node_id = %s AND active
                    """,
                    (node_id,),
                )

            if body.sector is not None:
                cur.execute(
                    """
                    INSERT INTO node_facts (node_id, sector, fetched_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (node_id) DO UPDATE SET sector = EXCLUDED.sector
                    """,
                    (node_id, body.sector),
                )

            effective_meta_patch = dict(body.meta_patch) if body.meta_patch else {}
            if demotion_evidence is not None:
                effective_meta_patch["acquisition_demotion"] = {
                    "from": "public",
                    "to": "private",
                    "reason": "subsidiary_edge_incoming",
                    "evidence_edge_id": demotion_evidence["evidence_edge_id"],
                    "acquirer_node_id": demotion_evidence["acquirer_node_id"],
                    "acquirer_name": demotion_evidence["acquirer_name"],
                    "at": datetime.now(timezone.utc).isoformat(),
                    "source": "endpoint_evidence_gated",
                }
                # Resolved now — clear the pending review flag (if any) so
                # the node doesn't keep showing as an unactioned candidate.
                effective_meta_patch["acquisition_demotion_candidate"] = None

            if effective_meta_patch:
                # Merge (not overwrite) into the existing meta
                # JSONB — see NodeUpdateBody.meta_patch docstring.
                cur.execute(
                    "UPDATE nodes SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                    (psycopg2.extras.Json(effective_meta_patch), node_id),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _node_detail_row(node_id)


@router.get("/nodes/{node_id}/price")
def get_node_price(node_id: str, range: str = Query("1y", description="1d | 1m | 1y | 5y | max")):
    """Stock price widget data. See core/stockprice.py for source
    choice (Yahoo Finance chart endpoint). Nodes with no ticker (private/dark
    horse) simply have no price data — this returns an empty points list, not
    an error, so the UI can render that state gracefully.

    the Yahoo symbol is built from the node's PRIMARY node_tickers row
    (exchange, ticker), not the bare nodes.ticker cache directly — a
    cross-listed node's primary might be a non-US exchange (e.g. Alibaba's
    HKG "9988"), which Yahoo requires as "9988.HK", not "9988". See
    stockprice.yahoo_symbol() for the verified exchange->suffix mapping.
    Falls back to the bare nodes.ticker with no suffix if no node_tickers row
    exists yet (pre-backfill safety net — shouldn't happen post-migration).
    """
    if range not in ("1d", "1m", "1y", "5y", "max"):
        return JSONResponse(status_code=400, content={"error": f"unknown range: {range}"})

    rows = query("SELECT ticker FROM nodes WHERE id = %s", (node_id,))
    if not rows:
        return JSONResponse(status_code=404, content={"error": "not found"})
    ticker = rows[0]["ticker"]
    if not ticker:
        return {"ticker": None, "range": range, "points": [], "stale": False}

    primary = query(
        "SELECT exchange, ticker FROM node_tickers WHERE node_id = %s AND is_primary",
        (node_id,),
    )
    if primary:
        exchange, primary_ticker = primary[0]["exchange"], primary[0]["ticker"]
    else:
        exchange, primary_ticker = "", ticker

    yahoo_ticker = yahoo_symbol(primary_ticker, exchange)
    return get_price_history(yahoo_ticker, range)


@router.get("/nodes/{node_id}/tickers")
def list_node_tickers(node_id: str):
    """All known ticker/exchange pairs for a node (node detail panel's
    multi-ticker list), primary first.
    """
    exists = query("SELECT id FROM nodes WHERE id = %s", (node_id,))
    if not exists:
        return JSONResponse(status_code=404, content={"error": "not found"})
    rows = query(
        """
        SELECT id::text, exchange, ticker, is_primary, added_at
        FROM node_tickers
        WHERE node_id = %s
        ORDER BY is_primary DESC, added_at ASC
        """,
        (node_id,),
    )
    for r in rows:
        if r.get("added_at") is not None:
            r["added_at"] = r["added_at"].isoformat()
    return rows


@router.post("/nodes/{node_id}/tickers", status_code=201)
def add_node_ticker(node_id: str, body: NodeTickerBody):
    """Register an additional ticker/exchange pair on a node that
    already has one (e.g. Alibaba's NYSE ADR "BABA" alongside its HKG primary
    "9988"). A node's FIRST ticker is always primary regardless of
    `is_primary` (there must be exactly one); after that, `is_primary=true`
    explicitly promotes this one (demoting whichever was primary before) —
    same "one field write, one primary" rule as POST /nodes/{id}/update's
    ticker field.
    """
    exchange, ticker = split_ticker_field(body.ticker)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM nodes WHERE id = %s", (node_id,))
            if not cur.fetchone():
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": "not found"})

            cur.execute("SELECT COUNT(*) FROM node_tickers WHERE node_id = %s", (node_id,))
            has_existing = cur.fetchone()[0] > 0
            make_primary = body.is_primary or not has_existing

            if make_primary:
                cur.execute(
                    "UPDATE node_tickers SET is_primary = FALSE WHERE node_id = %s AND is_primary",
                    (node_id,),
                )

            cur.execute(
                """
                INSERT INTO node_tickers (node_id, exchange, ticker, is_primary)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (node_id, exchange, ticker) DO UPDATE SET is_primary = EXCLUDED.is_primary
                RETURNING id::text
                """,
                (node_id, exchange, ticker, make_primary),
            )
            new_id = cur.fetchone()[0]

            if make_primary:
                cur.execute("UPDATE nodes SET ticker = %s WHERE id = %s", (ticker, node_id))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "id": new_id,
        "node_id": node_id,
        "exchange": exchange,
        "ticker": ticker,
        "is_primary": make_primary,
    }


@router.delete("/nodes/{node_id}/tickers/{ticker_id}")
def delete_node_ticker(node_id: str, ticker_id: str):
    """Remove one ticker/exchange row. If it was the primary and other
    tickers remain for the node, the earliest-added remaining row is promoted
    to primary and `nodes.ticker` is re-synced to it (a node should never be
    left with tickers but no primary). If it was the last ticker, `nodes.ticker`
    is cleared to NULL — same "no ticker -> no price data, not an error"
    contract as before.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_primary FROM node_tickers WHERE id = %s AND node_id = %s",
                (ticker_id, node_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": "not found"})
            was_primary = row[0]

            cur.execute("DELETE FROM node_tickers WHERE id = %s", (ticker_id,))

            if was_primary:
                cur.execute(
                    """
                    SELECT id::text, ticker FROM node_tickers
                    WHERE node_id = %s ORDER BY added_at ASC LIMIT 1
                    """,
                    (node_id,),
                )
                next_row = cur.fetchone()
                if next_row:
                    cur.execute(
                        "UPDATE node_tickers SET is_primary = TRUE WHERE id = %s",
                        (next_row[0],),
                    )
                    cur.execute("UPDATE nodes SET ticker = %s WHERE id = %s", (next_row[1], node_id))
                else:
                    cur.execute("UPDATE nodes SET ticker = NULL WHERE id = %s", (node_id,))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"ok": True}
