"""
Pipeline orchestration — fetch phase + extract phase in one run.

run_pipeline(run_id, mode, filing_path) is called as a FastAPI BackgroundTask
from POST /pipeline/run. It updates the pipeline_runs record on completion or failure.

Batch mode: status stays 'running' after this function returns; awaiting_harvest_since
is set. harvest_pending_batches() (POST /pipeline/harvest) completes the lifecycle.
"""

import logging
import os

from .db import bump_run_counters, execute, query
from .ingest.edgar import download_filing, recent_filings
from .ingest.extraction import run_extract_phase

logger = logging.getLogger(__name__)


def _fetch_all_nodes(
    nodes: list[dict],
    lookback_days: int,
    snapshot_days: int,
    run_id: str | None = None,
) -> tuple[int, list[str]]:
    """Fetch recent filings for every node. When run_id is given,
    bumps pipeline_runs.nodes_processed after each node so the Runs-tab 5s
    poll shows live progress during a long-running fetch phase — the final
    absolute UPDATE in run_pipeline() still runs afterwards as reconciliation.
    """
    nodes_processed = 0
    errors: list[str] = []

    for node in nodes:
        try:
            filings = recent_filings(
                node["cik"],
                lookback_days,
                submissions_max_age_days=snapshot_days,
            )
            for f in filings:
                if f["accession_number"] and f["primary_document"]:
                    download_filing(
                        node["cik"],
                        f["form"],
                        f["accession_number"],
                        f["primary_document"],
                    )
            nodes_processed += 1
            if run_id is not None:
                bump_run_counters(run_id, nodes_processed=1)
            logger.info(
                "fetched %-30s CIK %-10s  %d filing(s)",
                node["name"],
                node["cik"],
                len(filings),
            )
        except Exception as exc:
            msg = f"{node['name']} (CIK {node['cik']}): {exc}"
            errors.append(msg)
            logger.warning("fetch failed — %s", msg)

    return nodes_processed, errors


def run_pipeline(
    run_id: str,
    mode: str | None = None,
    filing_path: str | None = None,
) -> None:
    try:
        extraction_mode = mode or os.environ.get("EXTRACTION_MODE", "realtime")

        settings = {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}
        lookback_days = int(settings.get("news_feed_lookback_days", "30"))
        snapshot_days = int(settings.get("snapshot_frequency_days", "7"))

        nodes = query("SELECT id::text, name, cik FROM nodes WHERE cik IS NOT NULL")

        if filing_path:
            # Single-filing dev mode: skip the fetch phase entirely.
            nodes_processed = 0
            fetch_errors: list[str] = []
        else:
            nodes_processed, fetch_errors = _fetch_all_nodes(nodes, lookback_days, snapshot_days, run_id=run_id)

        events_logged, candidates_found, edges_created = run_extract_phase(
            run_id, extraction_mode, filing_path=filing_path
        )

        if extraction_mode == "batch":
            # Batch submitted: status stays 'running', batch_id + awaiting_harvest_since
            # are already set by run_extract_phase → _store_batch_submission.
            # Only update the fetch counts here.
            execute(
                """UPDATE pipeline_runs
                   SET nodes_processed = %s,
                       error_message   = %s
                   WHERE id = %s""",
                (nodes_processed, "\n".join(fetch_errors) or None, run_id),
            )
        else:
            execute(
                """UPDATE pipeline_runs
                   SET status           = 'completed',
                       completed_at     = NOW(),
                       nodes_processed  = %s,
                       events_logged    = %s,
                       candidates_found = %s,
                       edges_created    = %s,
                       error_message    = %s
                   WHERE id = %s""",
                (
                    nodes_processed,
                    events_logged,
                    candidates_found,
                    edges_created,
                    "\n".join(fetch_errors) or None,
                    run_id,
                ),
            )

            # An EDGAR run mints/updates nodes+candidates whose
            # matching news_feed rows may already exist but never resolved to
            # an edge at ingest time (e.g. the OTHER side just got approved).
            # Sweep for those now, zero tokens, own pipeline_runs row on the
            # Runs tab — never let a sweep failure fail the EDGAR run itself.
            try:
                from .core.reresolve import run_reresolve_sweep

                run_reresolve_sweep(apply=True)
            except Exception:
                logger.exception("post-edgar re-resolve sweep failed (edgar run %s unaffected)", run_id)

    except Exception as exc:
        logger.exception("pipeline run %s failed: %s", run_id, exc)
        execute(
            """UPDATE pipeline_runs
               SET status = 'failed', completed_at = NOW(), error_message = %s
               WHERE id = %s""",
            (str(exc), run_id),
        )
