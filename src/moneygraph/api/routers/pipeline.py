import logging
import os
import time

import psycopg2.extras
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse

from ...core.enrichment import _ENRICH_THROTTLE_SECS, enrich, enrich_all_nodes
from ...core.reresolve import run_reresolve_sweep
from ...core.resolve import normalize
from ...db import execute, query
from ...ingest.extraction import harvest_pending_batches, run_rss_phase, run_websearch_phase
from ...ingest.extraction.rss import _FEEDS
from ...pipeline import run_pipeline
from ..common import _display_status, _duration_seconds, _estimate_websearch_cost, _run_progress

router = APIRouter()
logger = logging.getLogger("app")


@router.get("/pipeline/latest")
def get_latest_pipeline_run():
    rows = query("""
        SELECT id::text, started_at, completed_at, status::text, run_type,
               awaiting_harvest_since,
               nodes_processed, edges_created, candidates_found,
               events_logged, search_calls_made, error_message,
               total_units, units_processed
        FROM pipeline_runs
        ORDER BY started_at DESC
        LIMIT 1
    """)
    if not rows:
        return JSONResponse(status_code=404, content={"error": "no runs"})
    row = rows[0]
    row["display_status"] = _display_status(row["status"], row["awaiting_harvest_since"])
    row["est_cost_usd"] = _estimate_websearch_cost(row.get("search_calls_made"))
    row.update(_run_progress(row["status"], row["started_at"], row.get("total_units"), row.get("units_processed")))
    for k in ("started_at", "completed_at", "awaiting_harvest_since"):
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return row


@router.get("/pipeline/runs")
def list_pipeline_runs(limit: int = 25, offset: int = 0):
    """Run history for the manual-run UI.

    Adds `run_type` (edgar|rss|websearch|legacy), a UI-facing `display_status`
    (batch runs awaiting harvest are 'awaiting_harvest', not 'failed'),
    `failed_rows` (batch rows that errored) — only meaningful once a batch run has
    completed harvest, so it is reported 0 while a run is still awaiting harvest —
    and `duration_seconds` (completed_at - started_at, or elapsed-so-far for a
    run that is still going).
    """
    limit = max(1, min(limit, 200))
    rows = query(
        """SELECT pr.id::text, pr.started_at, pr.completed_at, pr.status::text,
                  pr.run_type, pr.extraction_mode, pr.awaiting_harvest_since,
                  pr.nodes_processed, pr.edges_created, pr.candidates_found,
                  pr.events_logged, pr.search_calls_made, pr.error_message,
                  pr.total_units, pr.units_processed,
                  COALESCE(pb.unharvested, 0) AS unharvested
           FROM pipeline_runs pr
           LEFT JOIN (
               SELECT run_id, COUNT(*) AS unharvested
               FROM processing_batches
               WHERE harvested_at IS NULL
               GROUP BY run_id
           ) pb ON pb.run_id = pr.id
           ORDER BY pr.started_at DESC
           LIMIT %s OFFSET %s""",
        (limit, offset),
    )
    out = []
    for row in rows:
        display = _display_status(row["status"], row["awaiting_harvest_since"])
        # Unharvested rows are only "failures" once harvest has run (completed);
        # while awaiting_harvest they are simply not-yet-processed.
        row["failed_rows"] = row.pop("unharvested") if display == "completed" else 0
        row["display_status"] = display
        row["est_cost_usd"] = _estimate_websearch_cost(row.get("search_calls_made"))
        # Duration/progress from datetime objects, before they are stringified below.
        row["duration_seconds"] = _duration_seconds(row["started_at"], row["completed_at"])
        row.update(_run_progress(row["status"], row["started_at"], row.get("total_units"), row.get("units_processed")))
        for k in ("started_at", "completed_at", "awaiting_harvest_since"):
            if row.get(k) is not None:
                row[k] = row[k].isoformat()
        out.append(row)
    return {"runs": out, "limit": limit, "offset": offset}


@router.post("/pipeline/run", status_code=202)
def start_pipeline_run(
    background_tasks: BackgroundTasks,
    mode: str | None = Query(None, description="realtime | batch (overrides EXTRACTION_MODE env var)"),
    filing_path: str | None = Query(None, description="Dev only: extract a single filing, skip fetch phase"),
):
    extraction_mode = mode or os.environ.get("EXTRACTION_MODE", "realtime")
    if extraction_mode not in ("realtime", "batch"):
        return JSONResponse(status_code=400, content={"error": f"unknown mode: {extraction_mode}"})

    rows = execute(
        "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
        "VALUES ('running', %s, 'edgar') RETURNING id::text",
        (extraction_mode,),
    )
    run_id = rows[0][0]
    background_tasks.add_task(run_pipeline, run_id, mode=extraction_mode, filing_path=filing_path)
    return {"run_id": run_id, "extraction_mode": extraction_mode}


@router.post("/pipeline/websearch", status_code=202)
def start_websearch(
    background_tasks: BackgroundTasks,
    node_name: str | None = Query(None, description="Run for one node only (by name). Omit for all private nodes."),
):
    """
    Run web search for private seed nodes (cik IS NULL).
    Specify ?node_name=OpenAI to test a single node.
    """
    rows = execute(
        "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
        "VALUES ('running', 'realtime', 'websearch') RETURNING id::text",
    )
    run_id = rows[0][0]

    if node_name:
        node_rows = query("SELECT id::text, name FROM nodes WHERE name = %s AND cik IS NULL", (node_name,))
        if not node_rows:
            execute(
                "UPDATE pipeline_runs SET status = 'failed', error_message = %s WHERE id = %s",
                (f"node not found or not private: {node_name}", run_id),
            )
            return JSONResponse(status_code=404, content={"error": f"private node not found: {node_name}"})
        nodes = node_rows
    else:
        nodes = None  # run_websearch_phase queries all private nodes

    def _run():
        try:
            ev, cand, edges = run_websearch_phase(run_id, nodes=nodes)
            execute(
                """UPDATE pipeline_runs
                   SET status = 'completed', completed_at = NOW(),
                       events_logged = %s, candidates_found = %s, edges_created = %s
                   WHERE id = %s""",
                (ev, cand, edges, run_id),
            )
        except Exception as exc:
            logger.exception("websearch phase failed  run=%s", run_id)
            execute(
                "UPDATE pipeline_runs SET status = 'failed', error_message = %s WHERE id = %s",
                (str(exc)[:500], run_id),
            )

    background_tasks.add_task(_run)
    return {"run_id": run_id, "node_name": node_name or "all_private"}


@router.post("/pipeline/rss", status_code=202)
def start_rss(
    background_tasks: BackgroundTasks,
    feed: str | None = Query(None, description="Run one feed only (by name, e.g. 'TechCrunch'). Omit for all feeds."),
):
    """
    Poll RSS press-wire / tech feeds, entity-filter against seed nodes, then run
    matched articles through the web gate/write path.
    Specify ?feed=TechCrunch to test a single feed.
    """
    if feed:
        selected = [f for f in _FEEDS if f["name"] == feed]
        if not selected:
            names = ", ".join(f["name"] for f in _FEEDS)
            return JSONResponse(status_code=404, content={"error": f"feed not found: {feed}", "available": names})
        feeds = selected
    else:
        feeds = None  # run_rss_phase defaults to all feeds

    rows = execute(
        "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
        "VALUES ('running', 'realtime', 'rss') RETURNING id::text",
    )
    run_id = rows[0][0]

    def _run():
        try:
            ev, cand, edges = run_rss_phase(run_id, feeds=feeds)
            execute(
                """UPDATE pipeline_runs
                   SET status = 'completed', completed_at = NOW(),
                       events_logged = %s, candidates_found = %s, edges_created = %s
                   WHERE id = %s""",
                (ev, cand, edges, run_id),
            )
        except Exception as exc:
            logger.exception("rss phase failed  run=%s", run_id)
            execute(
                "UPDATE pipeline_runs SET status = 'failed', error_message = %s WHERE id = %s",
                (str(exc)[:500], run_id),
            )

    background_tasks.add_task(_run)
    return {"run_id": run_id, "feed": feed or "all"}


@router.post("/pipeline/harvest")
def trigger_harvest():
    """
    Poll all pipeline_runs awaiting batch harvest; harvest any that are ready.
    Manual trigger — also wired into the background scheduler.
    """
    stats = harvest_pending_batches()
    return stats


@router.post("/pipeline/reresolve", status_code=202)
def trigger_reresolve(background_tasks: BackgroundTasks):
    """
    manual trigger: run the re-resolve sweep on demand (zero tokens).

    Normally auto-triggered after every realtime EDGAR run (pipeline.py), but
    useful standalone right after a batch of candidate approvals/links — no
    need to wait for the next EDGAR run to pick up newly-resolvable news_feed
    rows. run_reresolve_sweep() creates and completes its own pipeline_runs
    row (run_type='reresolve'), so this endpoint just fires it in the
    background — same fire-and-forget shape as /enrichment/backfill.
    """

    def _run():
        try:
            run_reresolve_sweep(apply=True)
        except Exception:
            logger.exception("manual re-resolve sweep failed")

    background_tasks.add_task(_run)
    return {"status": "started"}


def _backfill_candidate_facts() -> dict:
    """Phase 2 of the enrichment backfill: fill facts for pending candidates missing them."""
    rows = query("SELECT id::text, name FROM candidates WHERE status = 'pending' AND facts IS NULL")
    counts = {"enriched": 0, "skipped": 0, "failed": 0}
    for row in rows:
        try:
            facts = enrich(row["name"])
            if facts is None:
                counts["skipped"] += 1
                logger.info("candidate backfill skipped (no facts)  %s", row["name"])
            else:
                execute(
                    "UPDATE candidates SET facts = %s WHERE id = %s",
                    (psycopg2.extras.Json(facts), row["id"]),
                )
                counts["enriched"] += 1
                logger.info("candidate backfill ok  %s  source=%s", row["name"], facts.get("source"))
        except Exception:
            counts["failed"] += 1
            logger.exception("candidate backfill failed  %s", row["name"])
        time.sleep(_ENRICH_THROTTLE_SECS)
    return counts


@router.post("/enrichment/backfill", status_code=202)
def start_enrichment_backfill(background_tasks: BackgroundTasks):
    """
    Backfill entity facts: phase 1 over resolved nodes (LEFT JOIN node_facts,
    skips rows already enriched), phase 1.5 re-enriches existing
    node_facts rows still missing country, phase 2 over pending candidates
    missing facts. Runs in the background; throttled ~1 req/sec against
    Wikidata/EDGAR.
    """

    def _run():
        node_counts = enrich_all_nodes(mode="missing")
        logger.info("enrichment backfill (nodes) done: %s", node_counts)
        country_counts = enrich_all_nodes(mode="missing_country")
        logger.info("enrichment backfill (country) done: %s", country_counts)
        candidate_counts = _backfill_candidate_facts()
        logger.info("enrichment backfill (candidates) done: %s", candidate_counts)

    background_tasks.add_task(_run)
    return {"status": "started"}


_NEWS_CANONICAL_CTE = """
    WITH limited AS (
        SELECT id, headline, url, source_tier, source_name, published_at,
               extracted_investor, extracted_investee,
               normalized_investor, normalized_investee,
               amount_usd, confirmed_by_sec, sec_source_id, pipeline_run_id
        FROM news_feed
        {where}
        ORDER BY published_at DESC
        LIMIT %s OFFSET %s
    )
    SELECT
        l.id::text, l.headline, l.url, l.source_tier, l.source_name,
        l.published_at, l.extracted_investor, l.extracted_investee,
        l.amount_usd, l.confirmed_by_sec, l.sec_source_id,
        l.pipeline_run_id::text,
        COALESCE(
            (SELECT n.name FROM nodes n
             JOIN node_aliases na ON na.node_id = n.id
             WHERE na.normalized_alias = l.normalized_investor LIMIT 1),
            (SELECT c.name FROM candidates c
             WHERE c.normalized_name = l.normalized_investor
               AND c.status IN ('approved', 'pending')
             ORDER BY CASE c.status WHEN 'approved' THEN 0 ELSE 1 END,
                      c.discovered_at DESC LIMIT 1),
            l.extracted_investor
        ) AS canonical_investor,
        COALESCE(
            (SELECT n.name FROM nodes n
             JOIN node_aliases na ON na.node_id = n.id
             WHERE na.normalized_alias = l.normalized_investee LIMIT 1),
            (SELECT c.name FROM candidates c
             WHERE c.normalized_name = l.normalized_investee
               AND c.status IN ('approved', 'pending')
             ORDER BY CASE c.status WHEN 'approved' THEN 0 ELSE 1 END,
                      c.discovered_at DESC LIMIT 1),
            l.extracted_investee
        ) AS canonical_investee
    FROM limited l
"""


def _node_normalized_aliases(node_id: str) -> list[str]:
    """All normalized_alias strings for a node, PLUS normalize(nodes.name) as
    a fallback. Needed because approve_candidate() (the common node-creation
    path) inserts straight into `nodes` with no node_aliases row — resolve.py
    Pass 1 matches nodes.name directly, so a node can have zero aliases yet
    still be the correct match for news mentioning it by its exact name.
    """
    rows = query("SELECT name FROM nodes WHERE id = %s", (node_id,))
    if not rows:
        return []
    names = {normalize(rows[0]["name"])}
    alias_rows = query("SELECT normalized_alias FROM node_aliases WHERE node_id = %s", (node_id,))
    names.update(r["normalized_alias"] for r in alias_rows)
    return list(names)


@router.get("/news")
def get_news(limit: int = 50, offset: int = 0, run_id: str | None = None, node_id: str | None = None):
    """
    `node_id` filters to news_feed rows whose canonical investor OR
    investee resolves to that node — the "filtered news" tab on the node
    detail panel. Matches on normalized_investor/investee against the node's
    normalized aliases (see _node_normalized_aliases) rather than a join to
    canonical_investor/investee (those are computed post-limit in the CTE
    below), so the filter applies BEFORE limit/offset, not after.
    """
    node_aliases = _node_normalized_aliases(node_id) if node_id is not None else None
    if node_id is not None and not node_aliases:
        return []  # node not found, or somehow has no name — nothing can match

    if run_id is not None and node_id is not None:
        sql = _NEWS_CANONICAL_CTE.format(
            where="""WHERE pipeline_run_id = %s
                     AND (normalized_investor = ANY(%s) OR normalized_investee = ANY(%s))"""
        )
        rows = query(sql, (run_id, node_aliases, node_aliases, limit, offset))
    elif node_id is not None:
        sql = _NEWS_CANONICAL_CTE.format(where="WHERE normalized_investor = ANY(%s) OR normalized_investee = ANY(%s)")
        rows = query(sql, (node_aliases, node_aliases, limit, offset))
    elif run_id is not None:
        sql = _NEWS_CANONICAL_CTE.format(where="WHERE pipeline_run_id = %s")
        rows = query(sql, (run_id, limit, offset))
    else:
        sql = _NEWS_CANONICAL_CTE.format(where="")
        rows = query(sql, (limit, offset))
    for r in rows:
        if r.get("published_at") is not None:
            r["published_at"] = r["published_at"].isoformat()
    return rows


@router.get("/pipeline/status/{run_id}")
def get_pipeline_status(run_id: str):
    rows = query(
        """SELECT id::text, started_at, completed_at, status::text, run_type,
                  awaiting_harvest_since,
                  nodes_processed, edges_created, candidates_found,
                  events_logged, search_calls_made, error_message,
                  total_units, units_processed
           FROM pipeline_runs
           WHERE id = %s""",
        (run_id,),
    )
    if not rows:
        return JSONResponse(status_code=404, content={"error": "not found"})
    row = rows[0]
    row["display_status"] = _display_status(row["status"], row["awaiting_harvest_since"])
    row["est_cost_usd"] = _estimate_websearch_cost(row.get("search_calls_made"))
    row.update(_run_progress(row["status"], row["started_at"], row.get("total_units"), row.get("units_processed")))
    for k in ("started_at", "completed_at", "awaiting_harvest_since"):
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return row
