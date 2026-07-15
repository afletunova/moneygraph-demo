"""Background scheduler.

Four jobs. The first two are batch-lifecycle upkeep; the last two
are the free/zero-marginal-cost ingest sources running on their own
cadence, so the graph stays fresh without a human clicking "EDGAR run"/"RSS run".
Websearch is deliberately NOT scheduled here — real $ per call, stays
manual-trigger-only (POST /pipeline/websearch).

  1. harvest_job   — every 15 min: poll OpenAI batches awaiting harvest and write
                     results (reuses extraction.harvest_pending_batches, the same
                     code path as POST /pipeline/harvest).
  2. orphan_job    — hourly: any batch run still awaiting harvest >26h after submit
                     is marked status='failed' (never deleted — pipeline_runs rows
                     are append-only). The OpenAI cost is already
                     sunk; this just stops the run reading as perpetually 'running'.
  3. edgar_job     — cron, env PIPELINE_CRON (default '0 9 * * 1', weekly Monday
                     9am UTC). Runs the same
                     run_pipeline as POST /pipeline/run; its auto re-resolve
                     sweep fires at the end of it same as a manual run. Weekly
                     matches settings.snapshot_frequency_days=7 — EDGAR's own
                     submissions-cache freshness window, so running more often
                     than that re-reads the same cached filing list for no gain
                     (confirmed empirically: runs inside a 7-day window
                     found nothing new every time).
  4. rss_job       — interval, env RSS_INTERVAL_HOURS (default 6). RSS has no
                     cache-staleness ceiling like EDGAR (each poll hits the live
                     feed), so a shorter, plain interval fits — no cron needed.

Set env DISABLE_SCHEDULER=1 to skip startup (used to keep test/CLI processes clean).
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..db import execute
from ..ingest.extraction import harvest_pending_batches, run_rss_phase

logger = logging.getLogger("app")

# Runs still awaiting harvest older than this are treated as orphaned. OpenAI's
# batch completion window is 24h; 26h gives a 2h grace past the SLA.
ORPHAN_AGE_HOURS = 26

_DEFAULT_PIPELINE_CRON = "0 9 * * 1"
_DEFAULT_RSS_INTERVAL_HOURS = 6

_scheduler: BackgroundScheduler | None = None


def harvest_job() -> None:
    """Scheduled wrapper around harvest_pending_batches (never raises to the scheduler)."""
    try:
        stats = harvest_pending_batches()
        if stats.get("runs_checked"):
            logger.info("scheduled harvest: %s", stats)
    except Exception:
        logger.exception("scheduled harvest failed")


def edgar_job() -> None:
    """Scheduled wrapper around run_pipeline — same path as
    POST /pipeline/run, own pipeline_runs row, never raises to the scheduler
    (run_pipeline already catches its own exceptions and marks the run
    'failed'; this outer try/except is a second net against anything that
    escapes that, e.g. the initial INSERT itself failing).
    """
    from ..pipeline import run_pipeline

    try:
        run_id = execute(
            "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
            "VALUES ('running', 'realtime', 'edgar') RETURNING id::text",
        )[0][0]
        run_pipeline(run_id, mode="realtime")
        logger.info("scheduled edgar run %s done", run_id)
    except Exception:
        logger.exception("scheduled edgar run failed")


def rss_job() -> None:
    """Scheduled wrapper around run_rss_phase — same path as
    POST /pipeline/rss, own pipeline_runs row.
    """
    run_id = execute(
        "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
        "VALUES ('running', 'realtime', 'rss') RETURNING id::text",
    )[0][0]
    try:
        ev, cand, edges = run_rss_phase(run_id, feeds=None)
        execute(
            """UPDATE pipeline_runs
               SET status = 'completed', completed_at = NOW(),
                   events_logged = %s, candidates_found = %s, edges_created = %s
               WHERE id = %s""",
            (ev, cand, edges, run_id),
        )
        logger.info("scheduled rss run %s done: events=%d candidates=%d edges=%d", run_id, ev, cand, edges)
    except Exception as exc:
        logger.exception("scheduled rss run %s failed", run_id)
        execute(
            "UPDATE pipeline_runs SET status = 'failed', error_message = %s WHERE id = %s",
            (str(exc)[:500], run_id),
        )


def sweep_orphan_batches() -> int:
    """Mark batch runs awaiting harvest >ORPHAN_AGE_HOURS as failed. Returns count.

    Never deletes rows. Idempotent — already-failed runs no longer match the
    WHERE (status must still be 'running' with awaiting_harvest_since set).
    """
    rows = execute(
        """UPDATE pipeline_runs
           SET status = 'failed',
               completed_at = NOW(),
               error_message = %s
           WHERE status = 'running'
             AND awaiting_harvest_since IS NOT NULL
             AND awaiting_harvest_since < NOW() - (%s || ' hours')::interval
           RETURNING id::text""",
        (f"orphaned: batch not harvested within {ORPHAN_AGE_HOURS}h", ORPHAN_AGE_HOURS),
    )
    count = len(rows) if rows else 0
    if count:
        logger.warning(
            "orphan sweep: marked %d batch run(s) failed (>%dh unharvested)",
            count,
            ORPHAN_AGE_HOURS,
        )
    return count


def orphan_job() -> None:
    try:
        sweep_orphan_batches()
    except Exception:
        logger.exception("orphan sweep failed")


def start_scheduler() -> BackgroundScheduler | None:
    """Start the background scheduler. No-op (returns None) if DISABLE_SCHEDULER is set."""
    global _scheduler
    if os.environ.get("DISABLE_SCHEDULER"):
        logger.info("scheduler disabled via DISABLE_SCHEDULER")
        return None
    if _scheduler is not None:
        return _scheduler

    pipeline_cron = os.environ.get("PIPELINE_CRON", _DEFAULT_PIPELINE_CRON)
    rss_interval_hours = int(os.environ.get("RSS_INTERVAL_HOURS", _DEFAULT_RSS_INTERVAL_HOURS))

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(harvest_job, "interval", minutes=15, id="harvest", max_instances=1, coalesce=True)
    sched.add_job(orphan_job, "interval", hours=1, id="orphan_sweep", max_instances=1, coalesce=True)
    sched.add_job(
        edgar_job,
        CronTrigger.from_crontab(pipeline_cron),
        id="edgar_ingest",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        rss_job,
        "interval",
        hours=rss_interval_hours,
        id="rss_ingest",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler started — harvest every 15m, orphan sweep hourly, edgar cron '%s', rss every %dh",
        pipeline_cron,
        rss_interval_hours,
    )
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler shut down")
