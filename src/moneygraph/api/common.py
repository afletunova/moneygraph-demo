"""Shared helpers used by more than one router (pipeline runs + status display)."""

from datetime import datetime, timezone


def _display_status(status: str, awaiting_harvest_since) -> str:
    """Map a pipeline_runs row to a UI-facing status.

    Batch runs sit at status='running' with awaiting_harvest_since set until the
    harvest callback fires (they report 0/0/0 counts meanwhile). That is NOT a
    failure — surface it as its own 'awaiting_harvest' state so dashboards/alerts
    don't false-positive on it.
    """
    if status == "running" and awaiting_harvest_since is not None:
        return "awaiting_harvest"
    return status


def _duration_seconds(started_at, completed_at, now=None) -> int | None:
    """Whole seconds a run took: completed_at - started_at, or elapsed so far
    (NOW() - started_at) for a run that hasn't completed. None if no start.
    """
    if started_at is None:
        return None
    end = completed_at if completed_at is not None else (now or datetime.now(timezone.utc))
    return max(0, int((end - started_at).total_seconds()))


# Websearch cost estimate = search calls x the per-call fee.
# Token cost (the LLM extraction call) is explicitly out of scope per the
# original spec. Search discovery runs through
# Brave Search's free-tier API (search_provider.py), not OpenAI's paid
# web_search tool — so the per-call fee is $0, not the old $0.025 OpenAI rate.
# Computed here (not stored) so the $/call rate can be corrected without a
# migration or a frontend redeploy — pipeline_runs.search_calls_made (the raw
# count) is the durable fact; this is a derived display value.
_WEBSEARCH_COST_PER_CALL_USD = 0.0


def _estimate_websearch_cost(search_calls_made: int | None) -> float:
    """Round to the nearest tenth of a cent — plenty for a cost *estimate*."""
    return round((search_calls_made or 0) * _WEBSEARCH_COST_PER_CALL_USD, 4)


# Percent-complete + ETA, computed on read (never stored) from
# started_at + units_processed/total_units — same "derive on poll, don't
# persist a value that can go stale" pattern as _estimate_websearch_cost.
# total_units is set ONCE at phase start (see set_run_total_units call sites
# in websearch.py/rss.py/extraction/pipeline.py); units_processed climbs via
# the existing bump_run_counters live-progress mechanism.
def _run_progress(
    status: str,
    started_at,
    total_units: int | None,
    units_processed: int | None,
    now=None,
) -> dict:
    """Returns {"percent_complete": float|None, "eta_seconds": int|None}.

    None/None (not 0/0) for anything that would be a divide-by-zero, a
    not-yet-computed total, or a run that isn't currently in progress — the
    frontend's job is to show nothing rather than a fabricated 0% or a
    nonsense ETA. A just-started phase (few samples) legitimately produces a
    noisy ETA; no smoothing is applied on purpose (over-engineering for a
    display estimate, not a billing figure).
    """
    if status != "running" or not total_units:
        return {"percent_complete": None, "eta_seconds": None}

    processed = units_processed or 0
    percent = round(100 * min(processed, total_units) / total_units, 1)

    elapsed = _duration_seconds(started_at, None, now=now)
    if elapsed is None or processed <= 0:
        # Nothing processed yet (or no start time) — can't project a rate.
        return {"percent_complete": percent, "eta_seconds": None}

    remaining_units = max(total_units - processed, 0)
    seconds_per_unit = elapsed / processed
    eta_seconds = int(round(seconds_per_unit * remaining_units))
    return {"percent_complete": percent, "eta_seconds": eta_seconds}
