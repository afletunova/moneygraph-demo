"""
Cross-source syndicate-round detection.

A separate, narrower detector already catches a syndicate-round overcount — N
investors all credited the FULL round amount because the source gave no
per-investor breakdown — but only WITHIN a single extraction result (one
filing/article processed once; see extraction/pipeline.py::_detect_syndicate_indices'
docstring, which explicitly scopes this out: "does NOT look across separate
ingestion runs or re-reports of the same round from a different source").

Confirmed live, an implausible "received ($987.8B)" total: the dominant
real-world case IS cross-source — each co-investor's participation gets
reported in a SEPARATE article/press release, picked up by SEPARATE extraction
runs, so the within-batch heuristic never fires and every one of those events
sits marked 'actual', fully counted.

This module generalises the same signal (same investee, same amount, no
per-investor breakdown) across the whole investment_events table instead of
one extraction batch. Dates aren't expected to match exactly — different
outlets report the same round's announcement over a real news cycle — so
clustering uses date PROXIMITY (greedy: start a new cluster when the gap
since the cluster's latest date exceeds date_tolerance_days), not an exact
match.

Reclassification only: flips already-'actual' rows in a detected cluster to
value_status='estimated', estimate_reason='syndicate_total'. Never deletes or
merges rows, never invents a per-investor split — a fabricated split
(round_total/N) was rejected as no more true than the original number, just
differently wrong.
"""

from __future__ import annotations

from datetime import date

from ..db import execute, query

MIN_COINVESTORS = 3
DATE_TOLERANCE_DAYS = 10


def _cluster_by_date(rows: list[dict], tolerance_days: int) -> list[list[dict]]:
    """Greedy proximity clustering on event_date within one (investee, amount)
    group. Rows are already sorted by event_date ascending by the caller.
    """
    clusters: list[list[dict]] = []
    current: list[dict] = []
    last_date: date | None = None
    for row in rows:
        d = row["event_date"]
        if current and last_date is not None and (d - last_date).days > tolerance_days:
            clusters.append(current)
            current = []
        current.append(row)
        last_date = d
    if current:
        clusters.append(current)
    return clusters


def detect_syndicate_clusters(
    min_coinvestors: int = MIN_COINVESTORS,
    date_tolerance_days: int = DATE_TOLERANCE_DAYS,
) -> list[dict]:
    """Read-only. Returns clusters of currently-'actual' events that look like
    a cross-source syndicate overcount: same investee + same delta_usd,
    >= min_coinvestors DISTINCT investors, event_dates within
    date_tolerance_days of each other (proximity-clustered, not exact match).

    Each cluster: {investee_id, investee_name, delta_usd, event_ids, investors}.
    """
    rows = query(
        """
        SELECT ie.id::text, ie.event_date, ie.delta_usd,
               e.to_node_id::text AS investee_id, tn.name AS investee_name,
               e.from_node_id::text AS investor_id, fn.name AS investor_name
        FROM investment_events ie
        JOIN edges e ON e.id = ie.edge_id
        JOIN nodes fn ON fn.id = e.from_node_id
        JOIN nodes tn ON tn.id = e.to_node_id
        WHERE ie.delta_usd > 0
          AND ie.value_status = 'actual'
          AND ie.corrects_event_id IS NULL
        ORDER BY e.to_node_id, ie.delta_usd, ie.event_date
        """
    )

    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["investee_id"], r["delta_usd"]), []).append(r)

    clusters: list[dict] = []
    for (investee_id, delta_usd), group_rows in groups.items():
        for cluster_rows in _cluster_by_date(group_rows, date_tolerance_days):
            distinct_investors = {r["investor_id"] for r in cluster_rows}
            if len(distinct_investors) < min_coinvestors:
                continue
            clusters.append(
                {
                    "investee_id": investee_id,
                    "investee_name": cluster_rows[0]["investee_name"],
                    "delta_usd": delta_usd,
                    "event_ids": [r["id"] for r in cluster_rows],
                    "investors": sorted({r["investor_name"] for r in cluster_rows}),
                }
            )
    return clusters


def flag_syndicate_clusters(
    min_coinvestors: int = MIN_COINVESTORS,
    date_tolerance_days: int = DATE_TOLERANCE_DAYS,
) -> dict:
    """Detect + reclassify. Flips every event in every detected cluster to
    value_status='estimated', estimate_reason='syndicate_total'. Returns a
    summary dict. Does NOT touch edges.net_amount_usd — that column stays the
    raw SUM(delta_usd) (used for the Graph tab's edge-thickness encoding);
    the confirmed/estimated split lives in the value_status-aware queries
    (GET /graph/current, GET /nodes/{id}) per the existing convention.
    """
    clusters = detect_syndicate_clusters(min_coinvestors, date_tolerance_days)
    flagged_events = 0
    for c in clusters:
        execute(
            """UPDATE investment_events
               SET value_status = 'estimated', estimate_reason = 'syndicate_total'
               WHERE id = ANY(%s::uuid[])""",
            (c["event_ids"],),
        )
        flagged_events += len(c["event_ids"])
    return {
        "clusters_found": len(clusters),
        "events_flagged": flagged_events,
        "clusters": clusters,
    }
