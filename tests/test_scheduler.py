"""
Unit tests for the batch-lifecycle scheduler + display-status logic.

No live DB, no network, no real background threads (BackgroundScheduler is
mocked). Covers:
  - _display_status: the 4-state mapping (batch awaiting harvest != failure).
  - sweep_orphan_batches: SQL params + returned count, marks failed never deletes.
  - start_scheduler: DISABLE_SCHEDULER guard + job registration (15m / 1h).
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import moneygraph.api.scheduler as scheduler
from moneygraph.api.common import _display_status, _duration_seconds
from moneygraph.api.routers import pipeline as main
from moneygraph.api.routers.pipeline import list_pipeline_runs

# ---------------------------------------------------------------------------
# _display_status — batch awaiting harvest must NOT read as failure
# ---------------------------------------------------------------------------


def test_display_status_realtime_running():
    assert _display_status("running", None) == "running"


def test_display_status_batch_awaiting_harvest():
    # status still 'running' but awaiting_harvest_since set → distinct state
    assert _display_status("running", "2026-07-09T00:00:00Z") == "awaiting_harvest"


def test_display_status_completed():
    assert _display_status("completed", None) == "completed"


def test_display_status_failed():
    assert _display_status("failed", None) == "failed"


def test_display_status_completed_ignores_harvest_ts():
    # completed run whose awaiting_harvest_since was cleared on harvest
    assert _display_status("completed", None) == "completed"


# ---------------------------------------------------------------------------
# _duration_seconds — completed vs still-running
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_duration_completed_row():
    completed = _T0 + timedelta(minutes=2, seconds=14)
    assert _duration_seconds(_T0, completed) == 134


def test_duration_running_row_uses_now():
    now = _T0 + timedelta(seconds=45)
    assert _duration_seconds(_T0, None, now=now) == 45


def test_duration_none_start():
    assert _duration_seconds(None, None, now=_T0) is None


def test_duration_never_negative():
    # clock skew: completed slightly before started → clamp to 0, not negative
    assert _duration_seconds(_T0, _T0 - timedelta(seconds=3)) == 0


# ---------------------------------------------------------------------------
# GET /pipeline/runs — duration surfaced for completed + running rows
# ---------------------------------------------------------------------------


def test_runs_endpoint_duration_completed_and_running():
    started = _T0
    completed = _T0 + timedelta(seconds=90)
    db_rows = [
        {  # completed row
            "id": "r-done",
            "started_at": started,
            "completed_at": completed,
            "status": "completed",
            "run_type": "edgar",
            "extraction_mode": "realtime",
            "awaiting_harvest_since": None,
            "nodes_processed": 1,
            "edges_created": 0,
            "candidates_found": 0,
            "events_logged": 0,
            "search_calls_made": 0,
            "error_message": None,
            "unharvested": 0,
        },
        {  # still-running row (no completed_at)
            "id": "r-run",
            "started_at": started,
            "completed_at": None,
            "status": "running",
            "run_type": "rss",
            "extraction_mode": "realtime",
            "awaiting_harvest_since": None,
            "nodes_processed": 0,
            "edges_created": 0,
            "candidates_found": 0,
            "events_logged": 0,
            "search_calls_made": 0,
            "error_message": None,
            "unharvested": 0,
        },
    ]
    with patch.object(main, "query", return_value=db_rows):
        resp = list_pipeline_runs(limit=25, offset=0)

    done, running = resp["runs"]
    assert done["duration_seconds"] == 90
    assert done["completed_at"] is not None
    # running row: elapsed computed against real now → a non-negative int, no completed_at
    assert isinstance(running["duration_seconds"], int)
    assert running["duration_seconds"] >= 0
    assert running["completed_at"] is None
    assert running["display_status"] == "running"
    assert done["est_cost_usd"] == 0.0
    assert running["est_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# sweep_orphan_batches — mark failed, never delete; correct params
# ---------------------------------------------------------------------------


def test_sweep_orphan_marks_failed():
    with patch.object(scheduler, "execute") as ex:
        ex.return_value = [("run-1",), ("run-2",)]
        count = scheduler.sweep_orphan_batches()
    assert count == 2
    sql, params = ex.call_args[0]
    assert "UPDATE pipeline_runs" in sql
    assert "SET status = 'failed'" in sql
    assert "DELETE" not in sql.upper()
    # 26h threshold passed as a param, not deletion
    assert scheduler.ORPHAN_AGE_HOURS in params
    assert "orphaned" in params[0]


def test_sweep_orphan_none():
    with patch.object(scheduler, "execute") as ex:
        ex.return_value = []
        assert scheduler.sweep_orphan_batches() == 0


# ---------------------------------------------------------------------------
# start_scheduler — guard + job registration
# ---------------------------------------------------------------------------


def test_scheduler_disabled_via_env():
    scheduler._scheduler = None
    with patch.dict(os.environ, {"DISABLE_SCHEDULER": "1"}):
        assert scheduler.start_scheduler() is None
    assert scheduler._scheduler is None


def test_scheduler_registers_four_jobs():
    # Edgar_ingest (cron) + rss_ingest (interval) join the pre-existing
    # harvest/orphan_sweep jobs. Websearch is never registered — real $ per
    # call, stays manual-trigger-only.
    scheduler._scheduler = None
    fake = MagicMock()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DISABLE_SCHEDULER", None)
        with patch.object(scheduler, "BackgroundScheduler", return_value=fake):
            scheduler.start_scheduler()
    ids = {c.kwargs.get("id") for c in fake.add_job.call_args_list}
    assert ids == {"harvest", "orphan_sweep", "edgar_ingest", "rss_ingest"}
    assert fake.add_job.call_count == 4
    fake.start.assert_called_once()
    scheduler._scheduler = None  # reset global for other tests


def test_scheduler_edgar_uses_pipeline_cron_env():
    scheduler._scheduler = None
    fake = MagicMock()
    with patch.dict(os.environ, {"PIPELINE_CRON": "30 4 * * 3"}, clear=False):
        os.environ.pop("DISABLE_SCHEDULER", None)
        with (
            patch.object(scheduler, "BackgroundScheduler", return_value=fake),
            patch.object(scheduler.CronTrigger, "from_crontab", wraps=scheduler.CronTrigger.from_crontab) as fc,
        ):
            scheduler.start_scheduler()
    fc.assert_called_once_with("30 4 * * 3")
    scheduler._scheduler = None


def test_scheduler_rss_uses_interval_env():
    scheduler._scheduler = None
    fake = MagicMock()
    with patch.dict(os.environ, {"RSS_INTERVAL_HOURS": "2"}, clear=False):
        os.environ.pop("DISABLE_SCHEDULER", None)
        with patch.object(scheduler, "BackgroundScheduler", return_value=fake):
            scheduler.start_scheduler()
    rss_call = next(c for c in fake.add_job.call_args_list if c.kwargs.get("id") == "rss_ingest")
    assert rss_call.args[1] == "interval"
    assert rss_call.kwargs.get("hours") == 2
    scheduler._scheduler = None


# ---------------------------------------------------------------------------
# Edgar_job / rss_job — scheduled wrappers
# ---------------------------------------------------------------------------


def test_edgar_job_inserts_run_and_calls_pipeline():
    with (
        patch.object(scheduler, "execute", return_value=[("run-edgar-1",)]) as ex,
        patch("moneygraph.pipeline.run_pipeline") as rp,
    ):
        scheduler.edgar_job()
    (sql,) = ex.call_args[0]
    assert "'edgar'" in sql
    rp.assert_called_once_with("run-edgar-1", mode="realtime")


def test_edgar_job_never_raises():
    with patch.object(scheduler, "execute", side_effect=RuntimeError("db down")):
        scheduler.edgar_job()  # must not raise


def test_rss_job_inserts_run_completes_on_success():
    with (
        patch.object(scheduler, "execute", return_value=[("run-rss-1",)]) as ex,
        patch.object(scheduler, "run_rss_phase", return_value=(3, 1, 2)) as rr,
    ):
        scheduler.rss_job()
    rr.assert_called_once_with("run-rss-1", feeds=None)
    update_sql, update_params = ex.call_args_list[-1][0]
    assert "status = 'completed'" in update_sql
    assert update_params == (3, 1, 2, "run-rss-1")


def test_rss_job_marks_failed_on_error():
    with (
        patch.object(scheduler, "execute", return_value=[("run-rss-2",)]) as ex,
        patch.object(scheduler, "run_rss_phase", side_effect=RuntimeError("feed timeout")),
    ):
        scheduler.rss_job()  # must not raise
    update_sql, update_params = ex.call_args_list[-1][0]
    assert "status = 'failed'" in update_sql
    assert "feed timeout" in update_params[0]
