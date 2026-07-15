from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.resolve import normalize
from ...db import execute, get_conn, query

router = APIRouter()


class ApproveBody(BaseModel):
    name: str
    type: Literal["public", "private", "dark_horse"]
    ticker: str | None = None


class RejectBody(BaseModel):
    notes: str | None = None


class LinkBody(BaseModel):
    node_id: str


@router.get("/candidates")
def get_candidates(limit: int = 50, offset: int = 0):
    rows = query(
        """
        SELECT c.id::text, c.name, c.discovered_via, c.amount_usd,
               c.discovered_at, c.discovery_count, c.facts,
               c.discovered_urls[1:3] AS discovered_urls,
               COALESCE(array_length(c.discovered_by_nodes, 1), 0) AS discovered_by_nodes_count,
               ARRAY(
                   SELECT n2.name FROM nodes n2
                   WHERE n2.id = ANY(c.discovered_by_nodes[1:3])
                   LIMIT 3
               ) AS discovered_by_nodes_names,
               n.name AS suggested_investor_name
        FROM candidates c
        LEFT JOIN nodes n ON n.id = c.suggested_investor
        WHERE c.status = 'pending'
        ORDER BY c.discovered_at DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    )
    for r in rows:
        if r.get("discovered_at") is not None:
            r["discovered_at"] = r["discovered_at"].isoformat()
    return rows


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, body: ApproveBody):
    """Approve a pending candidate, minting a new node.

    Duplicate guard (queue-ops 2026-07-11): a candidate's name can already
    correspond to a live node (missed by an earlier triage/reject pass, or a
    node created after the candidate was queued) or to a registered alias.
    Approving would silently mint a second node for an entity that already
    has one — the exact latent-duplicate risk reject_triage/link_dupes exist
    to catch. So: check both nodes.name and node_aliases.normalized_alias
    (via resolve.py's normalize(), the one canonical normalizer — not
    reimplemented here) before inserting; on a match, 409 and write nothing.
    No collision (the overwhelming common case for a legitimate approval)
    leaves the pre-existing INSERT path byte-for-byte unchanged.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM candidates WHERE id = %s AND status = 'pending'",
                (candidate_id,),
            )
            if not cur.fetchone():
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": "not found"})

            norm_name = normalize(body.name)

            cur.execute("SELECT id::text, name FROM nodes")
            for existing_id, existing_name in cur.fetchall():
                if normalize(existing_name) == norm_name:
                    conn.rollback()
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "duplicate of existing node",
                            "node_id": existing_id,
                            "node_name": existing_name,
                        },
                    )

            cur.execute(
                "SELECT node_id::text FROM node_aliases WHERE normalized_alias = %s",
                (norm_name,),
            )
            alias_row = cur.fetchone()
            if alias_row:
                alias_node_id = alias_row[0]
                cur.execute("SELECT name FROM nodes WHERE id = %s", (alias_node_id,))
                owner = cur.fetchone()
                conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "duplicate of existing node (alias match)",
                        "node_id": alias_node_id,
                        "node_name": owner[0] if owner else None,
                    },
                )

            cur.execute(
                """
                INSERT INTO nodes (name, ticker, type, added_by)
                VALUES (%s, %s, %s::node_type, 'review_queue')
                RETURNING id::text
                """,
                (body.name, body.ticker, body.type),
            )
            node_id = cur.fetchone()[0]

            cur.execute(
                "UPDATE candidates SET status = 'approved', reviewed_at = NOW() WHERE id = %s",
                (candidate_id,),
            )

            cur.execute(
                """
                UPDATE candidates
                SET status      = 'approved',
                    reviewed_at = NOW(),
                    notes       = COALESCE(notes || E'\\n', '') || 'auto-collapsed dupe of ' || %s
                WHERE normalized_name = (SELECT normalized_name FROM candidates WHERE id = %s)
                  AND status = 'pending'
                  AND id != %s
                RETURNING id::text
                """,
                (candidate_id, candidate_id, candidate_id),
            )
            cascade_count = len(cur.fetchall())

        conn.commit()
        return {"node_id": node_id, "cascade_count": cascade_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/candidates/{candidate_id}/link")
def link_candidate(candidate_id: str, body: LinkBody):
    """Link a pending candidate to an EXISTING node.

    Registers the candidate's name variants as node_aliases on the target node so
    resolve.py auto-attributes future mentions and the /news CTE re-attributes
    existing rows — no new node, no edge/event logic. The candidate is resolved
    (rejected) with a note, never deleted.

    node_aliases has UNIQUE(normalized_alias): an alias already mapping to THIS
    node is an idempotent no-op; one mapping to a DIFFERENT node returns 409 and
    nothing is written.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, normalized_name FROM candidates WHERE id = %s AND status = 'pending'",
                (candidate_id,),
            )
            cand = cur.fetchone()
            if not cand:
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": "candidate not found or not pending"})
            cand_name, cand_norm = cand[0], cand[1]

            cur.execute("SELECT name FROM nodes WHERE id = %s", (body.node_id,))
            node_row = cur.fetchone()
            if not node_row:
                conn.rollback()
                return JSONResponse(status_code=404, content={"error": f"node not found: {body.node_id}"})
            node_name = node_row[0]

            # Build the distinct (alias, normalized_alias) pairs to register.
            pairs: dict[str, str] = {}  # normalized_alias -> alias
            for alias in (cand_name, cand_norm):
                if not alias:
                    continue
                na = normalize(alias)
                if na:
                    pairs.setdefault(na, alias)

            aliases_added = 0
            aliases_existing = 0
            for norm_alias, alias in pairs.items():
                cur.execute(
                    "SELECT node_id::text FROM node_aliases WHERE normalized_alias = %s",
                    (norm_alias,),
                )
                owner = cur.fetchone()
                if owner:
                    if owner[0] == body.node_id:
                        aliases_existing += 1  # idempotent no-op
                        continue
                    # Maps to a different node — refuse, write nothing.
                    cur.execute("SELECT name FROM nodes WHERE id = %s", (owner[0],))
                    other = cur.fetchone()
                    other_name = other[0] if other else owner[0]
                    conn.rollback()
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": f"alias '{norm_alias}' already maps to node '{other_name}' ({owner[0]})",
                            "owning_node_id": owner[0],
                            "owning_node_name": other_name,
                        },
                    )
                cur.execute(
                    """INSERT INTO node_aliases (node_id, alias, normalized_alias, source)
                       VALUES (%s, %s, %s, 'user_approved')""",
                    (body.node_id, alias, norm_alias),
                )
                aliases_added += 1

            cur.execute(
                """UPDATE candidates
                   SET status      = 'rejected',
                       reviewed_at = NOW(),
                       notes       = COALESCE(notes || E'\\n', '')
                                     || 'linked to node ' || %s || ''
                   WHERE id = %s""",
                (node_name, candidate_id),
            )

        conn.commit()
        return {
            "candidate_id": candidate_id,
            "node_id": body.node_id,
            "node_name": node_name,
            "aliases_added": aliases_added,
            "aliases_existing": aliases_existing,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: str,
    body: RejectBody,
    cascade: bool = Query(False, description="Reject all pending dupes with the same normalized_name"),
):
    exists = query(
        "SELECT id FROM candidates WHERE id = %s AND status = 'pending'",
        (candidate_id,),
    )
    if not exists:
        return JSONResponse(status_code=404, content={"error": "not found"})

    execute(
        """
        UPDATE candidates
        SET status = 'rejected', reviewed_at = NOW(), notes = COALESCE(%s, notes)
        WHERE id = %s
        """,
        (body.notes, candidate_id),
    )

    cascade_count = 0
    if cascade:
        rows = execute(
            """
            UPDATE candidates
            SET status      = 'rejected',
                reviewed_at = NOW(),
                notes       = COALESCE(notes || E'\\n', '') || 'cascade-rejected with ' || %s
            WHERE normalized_name = (SELECT normalized_name FROM candidates WHERE id = %s)
              AND status = 'pending'
              AND id != %s
            RETURNING id::text
            """,
            (candidate_id, candidate_id, candidate_id),
        )
        cascade_count = len(rows)

    return {"ok": True, "cascade_count": cascade_count}
