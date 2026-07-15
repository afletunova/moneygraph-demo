"""
re-resolve sweep.

Recovers already-extracted news_feed events whose investor/investee never became a
graph edge because a name didn't resolve to a node at ingest time. Now that more
nodes/aliases exist (seed growth, links, fuzzy auto-registration, or a batch
of candidate approvals), some of those names resolve — so the edge is recoverable
WITHOUT re-extracting anything.

ZERO OpenAI tokens. Pure local resolution over stored data: the only network is the
Postgres connection.

READ-ONLY resolution. `resolve.resolve()` itself mutates (Pass 4 auto-registers an
alias, Pass 5 creates a candidate), so we do NOT call it. Instead ReadOnlyResolver
mirrors its 5-pass lookup read-only, reusing the SAME `normalize()` and the SAME
Levenshtein<=1 fuzzy rule so the measurement matches real ingest behaviour.

Entry point: run_reresolve_sweep(apply=True) -> dict. Used by:
  - scripts/reresolve_edges.py (manual CLI, dry-run by default, prints a report)
  - pipeline.py's run_pipeline (auto-triggered after every realtime
    EDGAR run, apply=True, writes its own pipeline_runs row so the sweep shows
    on the Runs tab)

No pre-apply DB backup here (unlike the CLI script's own backup.sh call) — this
runs inside the api container as a FastAPI background task, and the container
image only ever COPYs app/, not scripts/, so there is no docker CLI or backup.sh
available to shell out to from in here (same constraint documented for other
in-container scripts). Acceptable because this sweep is
additive-only: it materialises recoverable edges/events via the same
_process_event() ingest path as any live pipeline run, never UPDATEs or DELETEs
existing rows. The CLI script keeps its own backup requirement for ad-hoc manual
use, where the extra safety margin costs nothing.
"""

from __future__ import annotations

from ..db import execute, query
from .resolve import normalize

try:
    import Levenshtein

    def _dist(a: str, b: str) -> int:
        return Levenshtein.distance(a, b)
except Exception:  # pragma: no cover - container always has it

    def _dist(a: str, b: str) -> int:
        if a == b:
            return 0
        if abs(len(a) - len(b)) > 1:
            return 2
        if len(a) == len(b):
            return 1 if sum(x != y for x, y in zip(a, b)) == 1 else 2
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        for i in range(len(longer)):
            if shorter == longer[:i] + longer[i + 1 :]:
                return 1
        return 2


# ---------------------------------------------------------------------------
# Read-only 5-pass resolver (no writes)
# ---------------------------------------------------------------------------


class ReadOnlyResolver:
    """Mirrors resolve.resolve() Passes 1-4 as pure lookups; never writes."""

    def __init__(self, nodes_by_name, alias_by_raw, alias_by_norm, norm_alias_rows):
        self._nodes_by_name = nodes_by_name  # name -> (id, name)
        self._alias_by_raw = alias_by_raw  # alias -> (id, name)
        self._alias_by_norm = alias_by_norm  # normalized_alias -> (id, name)
        self._norm_alias_rows = norm_alias_rows  # list[(normalized_alias, id, name)]

    def resolve(self, name: str):
        """Return (node_id | None, node_name | None, via).

        via in {'exact','alias','norm','fuzzy',None}. 'exact/alias/norm' = the
        node already resolves today (matches the /news display CTE). 'fuzzy' = it
        only resolves once the Levenshtein<=1 pass is applied (recoverable extra).
        """
        if not name:
            return None, None, None
        if name in self._nodes_by_name:  # Pass 1
            nid, nm = self._nodes_by_name[name]
            return nid, nm, "exact"
        if name in self._alias_by_raw:  # Pass 2
            nid, nm = self._alias_by_raw[name]
            return nid, nm, "alias"
        norm = normalize(name)
        if norm in self._alias_by_norm:  # Pass 3
            nid, nm = self._alias_by_norm[norm]
            return nid, nm, "norm"
        best_d, best = None, None  # Pass 4 (fuzzy)
        for na, nid, nm in self._norm_alias_rows:
            d = _dist(norm, na)
            if best_d is None or d < best_d:
                best_d, best = d, (nid, nm)
                if best_d == 0:
                    break
        if best_d is not None and best_d <= 1:
            return best[0], best[1], "fuzzy"
        return None, None, None


_FUZZY_VIAS = {"fuzzy"}


# ---------------------------------------------------------------------------
# Pure classification (unit-tested)
# ---------------------------------------------------------------------------


def classify(rows, resolver, edge_exists):
    """Classify news_feed rows into both-resolve / existing / new / unresolved.

    The recovery signal is: BOTH sides resolve to distinct nodes now, but no
    directed edge exists between them → an extracted event that never became a
    graph edge. This keys on EDGE EXISTENCE, not which resolution pass matched,
    so it also catches rows made resolvable by alias links (which resolve
    a side but deliberately create no edge).

    rows: iterable of dicts {inv, vee, amount}.
    resolver: obj with .resolve(name)->(id,name,via).
    edge_exists: fn(from_id,to_id)->bool.
    Returns a summary dict; `new_edges` is the headline recoverable count.
    """
    both_resolve = 0
    existing = new = 0
    unresolved_sides = 0
    total = 0
    samples = []  # (amount, inv, vee, inv_node, vee_node, via_note) for display
    recoverable = []  # full dicts (carry the row) for --apply

    for r in rows:
        total += 1
        inv_id, inv_nm, inv_via = resolver.resolve((r.get("inv") or "").strip())
        vee_id, vee_nm, vee_via = resolver.resolve((r.get("vee") or "").strip())

        for nid in (inv_id, vee_id):
            if nid is None:
                unresolved_sides += 1

        if inv_id is not None and vee_id is not None and inv_id != vee_id:
            both_resolve += 1
            if edge_exists(inv_id, vee_id):
                existing += 1
            else:
                new += 1
                via_note = "fuzzy" if ({inv_via, vee_via} & _FUZZY_VIAS) else "exact/alias"
                amount = r.get("amount") or 0
                samples.append((amount, r.get("inv"), r.get("vee"), inv_nm, vee_nm, via_note))
                recoverable.append(
                    {
                        "amount": amount,
                        "inv": r.get("inv"),
                        "vee": r.get("vee"),
                        "inv_id": inv_id,
                        "inv_name": inv_nm,
                        "vee_id": vee_id,
                        "vee_name": vee_nm,
                        "via": via_note,
                        "row": r,
                    }
                )

    recoverable.sort(key=lambda x: x["amount"], reverse=True)
    return {
        "total_rows": total,
        "both_resolve_rows": both_resolve,
        "existing_edges": existing,  # both resolve AND edge already in graph
        "new_edges": new,  # both resolve, NO edge → recoverable
        "unresolved_sides": unresolved_sides,
        "samples": sorted(samples, key=lambda x: x[0], reverse=True),
        "recoverable": recoverable,
    }


# ---------------------------------------------------------------------------
# DB load
# ---------------------------------------------------------------------------


def _load_resolver() -> ReadOnlyResolver:
    nodes = query("SELECT id::text, name FROM nodes")
    nodes_by_name = {n["name"]: (n["id"], n["name"]) for n in nodes}
    node_name = {n["id"]: n["name"] for n in nodes}
    aliases = query("SELECT node_id::text, alias, normalized_alias FROM node_aliases")
    alias_by_raw, alias_by_norm, norm_rows = {}, {}, []
    for a in aliases:
        nm = node_name.get(a["node_id"], a["node_id"])
        alias_by_raw.setdefault(a["alias"], (a["node_id"], nm))
        alias_by_norm.setdefault(a["normalized_alias"], (a["node_id"], nm))
        norm_rows.append((a["normalized_alias"], a["node_id"], nm))
    return ReadOnlyResolver(nodes_by_name, alias_by_raw, alias_by_norm, norm_rows)


def _load_existing_edges():
    rows = query("SELECT from_node_id::text AS f, to_node_id::text AS t FROM edges")
    return {(r["f"], r["t"]) for r in rows}


# ---------------------------------------------------------------------------
# Exclusions (minimal + conservative — when unsure INCLUDE and log)
# ---------------------------------------------------------------------------

# Flagged mis-extraction from Stage-1 review: SpaceX did not invest $60B in
# Anysphere (Cursor). Excluded by resolved node-name pair.
_EXCLUDE_NODE_PAIRS = {("SpaceX", "Anysphere, Inc.")}


def exclusion_reason(rec) -> str | None:
    """Return a reason to skip materializing this recoverable row, else None."""
    if rec["inv_id"] == rec["vee_id"]:
        return "self-loop (both sides resolve to the same node)"
    if (rec["inv_name"], rec["vee_name"]) in _EXCLUDE_NODE_PAIRS:
        return "flagged mis-extraction (SpaceX -> Anysphere $60B)"
    return None


def split_recoverable(recoverable):
    """Return (to_materialize, excluded[(rec, reason)])."""
    keep, excluded = [], []
    for rec in recoverable:
        reason = exclusion_reason(rec)
        (excluded.append((rec, reason)) if reason else keep.append(rec))
    return keep, excluded


# ---------------------------------------------------------------------------
# Apply — materialize each recoverable edge via the ingest path
# ---------------------------------------------------------------------------


def _materialize(rec, run_id) -> tuple:
    """Reuse _process_event to write edge+event+source from stored news_feed
    fields. Idempotent (canonical-key ON CONFLICT); never UPDATEs edge amount
    directly (the ingest path recomputes via SUM). Returns _process_event's
    (event_logged, candidate_created, edge_created).
    """
    from ..ingest.extraction.pipeline import _process_event

    row = rec["row"]
    published = row.get("published_at")
    event = {
        "investor": rec["inv"],
        "investee": rec["vee"],
        "amount_usd": rec["amount"] or None,
        "excerpt": row.get("headline") or "",
        "date": published.date().isoformat() if published else None,
    }
    filing_meta = {
        "form_type": "WEB",
        "url": row.get("url") or "",
        "date": published.isoformat() if published else None,
        "discovery_source": "web",
        "source_tier": row.get("source_tier") or 3,
        "source_name": row.get("source_name") or "news",
    }
    # write_news_feed=False: the sweep RE-processes existing news_feed rows; it
    # must create edges/events/sources but NOT mint new news_feed rows.
    return _process_event(event, filing_meta, run_id, write_news_feed=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_reresolve_sweep(apply: bool = True, run_type: str = "reresolve") -> dict:
    """Run the full sweep. If apply, writes a pipeline_runs row (run_type,
    default 'reresolve') and materializes recoverable edges/events; returns a
    summary dict including run_id. If not apply, measures only — no DB writes,
    no pipeline_runs row (matches the CLI script's original dry-run contract).
    """
    resolver = _load_resolver()
    edges = _load_existing_edges()
    rows = query(
        """SELECT extracted_investor AS inv, extracted_investee AS vee,
                  amount_usd AS amount, source_tier, source_name, url, published_at
           FROM news_feed"""
    )
    summary = classify(rows, resolver, lambda f, t: (f, t) in edges)
    keep, excluded = split_recoverable(summary["recoverable"])

    result = {
        "total_rows": summary["total_rows"],
        "both_resolve_rows": summary["both_resolve_rows"],
        "existing_edges": summary["existing_edges"],
        "recoverable": len(keep),
        "excluded": len(excluded),
        "excluded_detail": excluded,
    }

    if not apply:
        result["would_materialize"] = keep
        return result

    run_id = execute(
        "INSERT INTO pipeline_runs (status, extraction_mode, run_type) "
        "VALUES ('running', 'realtime', %s) RETURNING id::text",
        (run_type,),
    )[0][0]

    materialized = new_edges = skipped_unresolved = 0
    materialized_detail = []
    for rec in keep:
        try:
            event_logged, candidate_created, edge_created = _materialize(rec, run_id)
        except Exception as exc:  # never let one bad row abort the batch
            materialized_detail.append({"rec": rec, "error": str(exc)})
            continue
        if event_logged:
            materialized += 1
            new_edges += 1 if edge_created else 0
            materialized_detail.append({"rec": rec, "edge_created": edge_created})
        else:
            skipped_unresolved += 1

    execute(
        "UPDATE pipeline_runs SET status='completed', completed_at=NOW(), "
        "edges_created=%s, events_logged=%s WHERE id=%s",
        (new_edges, materialized, run_id),
    )

    result.update(
        {
            "run_id": run_id,
            "events_written": materialized,
            "new_edges": new_edges,
            "resolve_miss": skipped_unresolved,
            "materialized_detail": materialized_detail,
        }
    )
    return result
