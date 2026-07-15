"""
Entity enrichment — minimal company facts.

Fetches a handful of facts per company (public/private, founding year,
sector, headquarters, one-line description) from Wikidata and, when a CIK is
known, EDGAR. Cached on `node_facts` (resolved nodes) or `candidates.facts`
(pending review rows).

Wikidata disambiguation guard: a search hit is only accepted once its P31
(instance of) claims match a small allowlist of business-shaped entity
types. This exists because short company names ("Arm") collide with
unrelated Wikidata entries; guessing wrong is worse than not enriching.

Design doc risk callout: "match on QID via alias + verify instance of:
business/enterprise; fall back to no-enrichment rather than guess wrong."
"""

import logging
import time
from datetime import datetime, timezone

import requests

from ..db import execute, query
from .resolve import _SUFFIX_RE

logger = logging.getLogger(__name__)

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_USER_AGENT = "MoneyGraph/1.0 (personal project)"
_TIMEOUT = 10

# P31 (instance of) values accepted as "this is a business", not some other
# kind of Wikidata entity (person, place, band, etc. sharing the same name).
_BUSINESS_QIDS = frozenset(
    {
        "Q4830453",  # business
        "Q6881511",  # enterprise
        "Q783794",  # company
        "Q891723",  # public company
        "Q5225895",  # privately held company
        "Q18388277",  # tech company
        "Q1058914",  # software company
        "Q43229",  # organization
    }
)

_ENRICH_THROTTLE_SECS = 1.0


# Pause between consecutive Wikidata calls within one entity (search + claims
# + label lookups) — the 1 req/sec entity throttle alone still trips the
# anonymous rate limit on these bursts (observed 429s, 2026-07-08).
_INTER_CALL_SECS = 0.5
_MAX_RETRIES = 3


def _wikidata_get(params: dict) -> dict | None:
    for attempt in range(_MAX_RETRIES):
        try:
            time.sleep(_INTER_CALL_SECS)
            resp = requests.get(
                _WIKIDATA_API,
                params={**params, "format": "json"},
                headers={"User-Agent": _USER_AGENT},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    wait = float(resp.headers.get("Retry-After", 5 * (attempt + 1)))
                    logger.warning("wikidata 429, retrying in %.0fs  params=%s", wait, params)
                    time.sleep(wait)
                    continue
                # Observability fix: retries exhausted under sustained
                # rate-limiting. Logged distinctly (not via the generic
                # "request failed" exception path below) so this is
                # grep-able and distinguishable after the fact from a
                # genuinely empty Wikidata response — both currently still
                # surface to the caller as None/no-match, but a rate-limited
                # "None" is a transient failure worth re-running later, while
                # a confirmed empty search is a real miss. Full auto-retry/
                # resume of just the rate-limited subset is deferred (not
                # observed to actually occur in the backfill run that
                # prompted this).
                logger.warning(
                    "wikidata: exhausted %d retries under sustained 429s — "
                    "treating as unavailable (NOT a confirmed empty result)  params=%s",
                    _MAX_RETRIES,
                    params,
                )
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.exception("wikidata request failed  params=%s", params)
            return None
    return None


def _strip_legal_suffix(name: str) -> str:
    """Iteratively strip a trailing corporate-entity suffix (Inc./Corp./LLC/
    Holdings/...), preserving case. Reuses resolve.py's `_SUFFIX_RE` (same
    suffix vocabulary as the dedup/shell-folding normalizer) rather than
    duplicating the pattern — but does NOT lowercase, unlike
    `resolve.normalize()`, since this feeds a Wikidata search term, not a
    dedup key.
    """
    s = name.strip()
    while True:
        stripped = _SUFFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return s


def _wbsearchentities(term: str) -> list[str]:
    data = _wikidata_get(
        {
            "action": "wbsearchentities",
            "search": term,
            "language": "en",
            "type": "item",
            "limit": 5,
        }
    )
    if not data:
        return []
    return [hit["id"] for hit in data.get("search", [])]


def _search_candidates(name: str) -> list[str]:
    """Return up to 5 candidate QIDs for a name via wbsearchentities.

    bug fix: `wbsearchentities` does near-exact label matching, not
    fuzzy matching — a name carrying a legal-entity suffix ("Klarna Inc.",
    "Unity Technologies, Inc.") reliably returns ZERO results even though
    Wikidata's own label omits the suffix ("Klarna", "Unity Technologies").
    Confirmed live against the API while investigating a 0/104 candidate
    backfill run (2026-07-10): raw suffixed names returned 0 hits for the
    large majority of that run's misses; the identical names with the legal
    suffix stripped matched immediately (Klarna, Unity Technologies, Apollo
    Global Management, Globalstar, Guidewire Software, Skydio all confirmed).
    Falls back to a suffix-stripped retry only when the raw search comes back
    empty. The existing business-entity P31 guard downstream still has to
    pass before any match is accepted from either search, which bounds (but
    doesn't eliminate) the risk of an over-stripped, too-generic remainder
    (e.g. "Thrive Holdings, LLC" -> "Thrive") latching onto an unrelated QID.
    """
    qids = _wbsearchentities(name)
    if qids:
        return qids

    stripped = _strip_legal_suffix(name)
    if stripped and stripped.lower() != name.strip().lower():
        qids = _wbsearchentities(stripped)
        if qids:
            logger.info("wikidata: suffix-stripped search matched  %r -> %r", name, stripped)
        return qids

    return []


def _get_entities(qids: list[str], props: str) -> dict:
    """Batch wbgetentities call. Returns {qid: entity_dict}, {} on failure."""
    if not qids:
        return {}
    data = _wikidata_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": props,
        }
    )
    if not data:
        return {}
    return data.get("entities", {})


def _first_claim_value(entity: dict, prop: str) -> dict | None:
    claims = entity.get("claims", {}).get(prop)
    if not claims:
        return None
    try:
        return claims[0]["mainsnak"]["datavalue"]["value"]
    except (KeyError, IndexError):
        return None


def _is_business(entity: dict) -> bool:
    p31_claims = entity.get("claims", {}).get("P31", [])
    for claim in p31_claims:
        try:
            qid = claim["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError):
            continue
        if qid in _BUSINESS_QIDS:
            return True
    return False


def fetch_wikidata_facts(name: str) -> dict | None:
    """
    Look up a company on Wikidata. Returns None if no candidate QID passes
    the business-entity disambiguation guard, or on any HTTP failure.
    """
    qids = _search_candidates(name)
    if not qids:
        return None

    entities = _get_entities(qids, props="claims|descriptions")
    if not entities:
        return None

    chosen_qid = None
    chosen_entity = None
    for qid in qids:
        entity = entities.get(qid)
        if entity and _is_business(entity):
            chosen_qid = qid
            chosen_entity = entity
            break

    if chosen_entity is None:
        logger.info("wikidata: no business-entity QID matched for %r", name)
        return None

    founded = None
    inception = _first_claim_value(chosen_entity, "P571")
    if inception and isinstance(inception, dict):
        time_str = inception.get("time")  # e.g. "+1998-09-04T00:00:00Z"
        if time_str:
            try:
                founded = int(time_str.lstrip("+").split("-")[0])
            except (ValueError, IndexError):
                founded = None

    # True if a stock-exchange claim (P414) exists, else None — absence isn't proof of private.
    has_listing = _first_claim_value(chosen_entity, "P414") is not None
    is_public = True if has_listing else None

    short_description = chosen_entity.get("descriptions", {}).get("en", {}).get("value")

    headquarters = None
    hq_country_qid = None
    hq_value = _first_claim_value(chosen_entity, "P159")
    if hq_value and isinstance(hq_value, dict) and hq_value.get("entity-type") == "item":
        # Fetch claims alongside labels here (not a separate call) so
        # we can also read the HQ entity's own P17 (country) — e.g. HQ =
        # "Mountain View" -> P17 -> "United States". Preferred over the org's
        # own P17 below since a company's own country claim is sometimes
        # stale/generic; the HQ location's country is usually more specific.
        hq_entities = _get_entities([hq_value["id"]], props="labels|claims")
        hq_entity = hq_entities.get(hq_value["id"], {})
        headquarters = hq_entity.get("labels", {}).get("en", {}).get("value")
        hq_country_value = _first_claim_value(hq_entity, "P17")
        if hq_country_value and isinstance(hq_country_value, dict) and hq_country_value.get("entity-type") == "item":
            hq_country_qid = hq_country_value["id"]

    # Country resolution, in priority order — (a) the HQ entity's own
    # P17, (b) fall back to the org entity's own P17 directly. Never guess:
    # stays None if neither resolves.
    country_qid = hq_country_qid
    if country_qid is None:
        org_country_value = _first_claim_value(chosen_entity, "P17")
        if org_country_value and isinstance(org_country_value, dict) and org_country_value.get("entity-type") == "item":
            country_qid = org_country_value["id"]

    country = None
    if country_qid:
        country_entities = _get_entities([country_qid], props="labels")
        country_entity = country_entities.get(country_qid, {})
        country = country_entity.get("labels", {}).get("en", {}).get("value")

    sector = None
    industry_value = _first_claim_value(chosen_entity, "P452")
    if industry_value and isinstance(industry_value, dict) and industry_value.get("entity-type") == "item":
        industry_entities = _get_entities([industry_value["id"]], props="labels")
        industry_entity = industry_entities.get(industry_value["id"], {})
        sector = industry_entity.get("labels", {}).get("en", {}).get("value")

    return {
        "is_public": is_public,
        "founded": founded,
        "sector": sector,
        "headquarters": headquarters,
        "country": country,
        "short_description": short_description,
        "wikidata_qid": chosen_qid,
        "source": "wikidata",
    }


def fetch_edgar_facts(cik: str) -> dict | None:
    """
    Look up sector/is_public from the EDGAR submissions API (reuses
    edgar.py's cached fetch_submissions when available).

    every node with a CIK is an SEC filer, i.e. US-domiciled for
    practical purposes at this project's scale — cheaper than a Wikidata
    round-trip and very likely correct. Sanity-checked against a live cached
    submissions.json (Apple, CIK 320193): `addresses.business.isForeignLocation`
    is the field that would flag a foreign private issuer/ADR; it's 0/None for
    a normal domestic filer. Guarded on that flag rather than assumed blindly,
    so a genuine foreign filer doesn't get a wrong "United States" label.
    """
    try:
        from ..ingest import edgar

        data = edgar.fetch_submissions(cik)
    except Exception:
        logger.exception("edgar facts fetch failed  CIK %s", cik)
        return None

    sector = data.get("sicDescription")
    if not sector:
        return None

    is_foreign = data.get("addresses", {}).get("business", {}).get("isForeignLocation")
    country = None if is_foreign else "United States"

    return {
        "is_public": True,
        "founded": None,
        "sector": sector,
        "headquarters": None,
        "country": country,
        "short_description": None,
        "wikidata_qid": None,
        "source": "edgar",
    }


def _add_ticker(facts: dict, name: str) -> dict:
    """For a facts dict already resolved as public, attach a ticker
    looked up from SEC's company_tickers.json (see ticker_lookup.py) — never
    guessed for private/unknown companies. Stored alongside the rest of
    `facts` (candidates.facts JSONB); node_facts is a relational table with
    no ticker column, so this extra key is harmlessly ignored by
    upsert_node_facts() for the node-enrichment path and only actually used
    by the candidate-approve pre-fill.
    """
    if facts.get("is_public") is not True:
        return facts
    try:
        from .ticker_lookup import lookup_ticker

        ticker = lookup_ticker(name)
        if ticker:
            facts["ticker"] = ticker
    except Exception:
        logger.exception("ticker lookup failed  %s", name)
    return facts


def enrich(name: str, cik: str | None = None) -> dict | None:
    """
    Merge Wikidata + EDGAR facts. EDGAR wins for is_public/sector when a CIK
    is present (authoritative SEC identity); Wikidata fills the rest.
    EDGAR's country (CIK shortcut) also wins over Wikidata's when
    both resolve, same precedent — falls back to Wikidata's country if EDGAR
    didn't resolve one (e.g. flagged foreign filer).
    when the resolved facts say is_public, also attaches a `ticker`
    key (SEC bulk-file lookup, see _add_ticker/ticker_lookup.py).
    Returns None if both sources fail (or return nothing usable).
    """
    edgar_facts = fetch_edgar_facts(cik) if cik else None
    wikidata_facts = fetch_wikidata_facts(name)

    if edgar_facts is None and wikidata_facts is None:
        return None

    if edgar_facts is None:
        return _add_ticker(wikidata_facts, name)

    if wikidata_facts is None:
        return _add_ticker(edgar_facts, name)

    merged = dict(wikidata_facts)
    merged["is_public"] = edgar_facts["is_public"]
    merged["sector"] = edgar_facts["sector"]
    merged["country"] = edgar_facts["country"] or wikidata_facts.get("country")
    merged["source"] = "both"
    return _add_ticker(merged, name)


def upsert_node_facts(node_id: str, facts: dict) -> None:
    execute(
        """
        INSERT INTO node_facts
            (node_id, is_public, founded, sector, headquarters, country,
             short_description, wikidata_qid, source, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (node_id) DO UPDATE SET
            is_public          = EXCLUDED.is_public,
            founded            = EXCLUDED.founded,
            sector             = EXCLUDED.sector,
            headquarters       = EXCLUDED.headquarters,
            country            = EXCLUDED.country,
            short_description  = EXCLUDED.short_description,
            wikidata_qid       = EXCLUDED.wikidata_qid,
            source             = EXCLUDED.source,
            fetched_at         = NOW()
        """,
        (
            node_id,
            facts.get("is_public"),
            facts.get("founded"),
            facts.get("sector"),
            facts.get("headquarters"),
            facts.get("country"),
            facts.get("short_description"),
            facts.get("wikidata_qid"),
            facts.get("source"),
        ),
    )


def _dark_horse_promotion_signal(name: str, facts: dict | None) -> dict | None:
    """
    decide whether `name`'s enrichment result carries unambiguous
    confirmed-public evidence, conservatively — same "never guess" bar as
    the rest of this module (see module docstring / Wikidata disambiguation
    guard, ticker_lookup.py's exact/fuzzy<=1 bar).

    Three independent signals, any one of which is sufficient:
      1. facts["is_public"] is True (Wikidata P414 listing or EDGAR CIK
         presence — enrich() already only sets this on positive evidence,
         never guessed; see enrich()/fetch_wikidata_facts() docstrings).
      2. A real CIK found via ticker_lookup.py's exact/fuzzy<=1 match
         against SEC's company_tickers.json.
      3. A real ticker found via the same lookup (weaker than a CIK alone
         only in that it's the same underlying signal; kept as its own
         branch so a ticker-only hit still counts, e.g. a fuzzy match that
         happens to carry a ticker but an unresolved cik_str).
    Ticker/CIK are checked independently of `facts` (even a `None` facts
    result, or one where Wikidata/EDGAR found nothing) — a thin/absent
    Wikidata page doesn't preclude the entity already being an SEC-registered
    filer today.

    Returns None if no evidence clears the bar. Otherwise a dict describing
    what was found: {"reason": ..., "ticker": str|None, "cik": str|None}.
    """
    ticker = None
    cik = None
    reason = None

    if facts and facts.get("is_public") is True:
        reason = "is_public"
        ticker = facts.get("ticker")

    try:
        from .ticker_lookup import lookup_ticker_and_cik

        looked_up_ticker, looked_up_cik = lookup_ticker_and_cik(name)
    except Exception:
        logger.exception("dark_horse promotion: ticker/cik lookup failed  %s", name)
        looked_up_ticker, looked_up_cik = None, None

    if looked_up_cik:
        cik = looked_up_cik
        ticker = ticker or looked_up_ticker
        reason = reason or "cik_confirmed"
    elif looked_up_ticker and reason is None:
        ticker = looked_up_ticker
        reason = "ticker_confirmed"

    if reason is None:
        return None
    return {"reason": reason, "ticker": ticker, "cik": cik}


def check_dark_horse_promotion(node_id: str, name: str, facts: dict | None) -> dict | None:
    """
    Auto-promote a `dark_horse` node to `public` when enrichment
    (this call or a past one) turns up unambiguous confirmed evidence.

    Asymmetric by design — dark_horse -> public only, never dark_horse ->
    private: a CIK/ticker/is_public=true hit is unambiguous machine-checkable
    evidence ("this is now a real, SEC-registered or exchange-listed
    company"). There is no comparably strong automatic signal for "this is
    now confirmed as a real *private* company" — that's inherently a softer
    judgment call (no listing to NOT find is proof of anything; see
    enrich()/is_public's "absence isn't proof of private" comment). Per the
    project's framing ("rumour -> SEC confirmation"), dark_horse ->
    private stays a manual call via the existing node-panel edit
    (POST /nodes/{id}/update) — this function never writes it.

    Hooked into enrich_all_nodes() (the only call site of
    upsert_node_facts(), see call site below) rather than inside
    upsert_node_facts() itself, so a hypothetical future single-node
    re-enrich path that reuses upsert_node_facts() doesn't inherit a
    promotion side-effect it didn't ask for.

    Returns None if the node isn't currently `dark_horse` or no evidence
    clears the bar (the common no-op case for all 7 nodes as of 2026-07-11 —
    none have any CIK/ticker/is_public evidence yet). Otherwise a dict:
      {"promoted": True,  "reason": ..., "ticker": ..., "cik": ...}
      {"promoted": False, "reason": "collision", "collides_with_node_id": ..., "collides_with_name": ...}
      {"promoted": False, "reason": "update_rejected"}
    """
    rows = query("SELECT type::text AS type FROM nodes WHERE id = %s", (node_id,))
    if not rows or rows[0]["type"] != "dark_horse":
        return None

    signal = _dark_horse_promotion_signal(name, facts)
    if signal is None:
        return None

    ticker, cik = signal["ticker"], signal["cik"]

    # Dedup guard (mirrors the spirit of approve_candidate's live-node dedup
    # guard, queue-ops 2026-07-11 — see main.py): a promotion writes
    # ticker/cik onto an *existing* node rather than minting a new one, so
    # the collision that matters here isn't a name match, it's another node
    # already holding this same ticker/CIK. That would mean the dark_horse
    # row is actually an unresolved duplicate of an already-known public
    # node (should have been merged, not promoted) — block and log instead
    # of silently giving two nodes the same real-world identity.
    # cik comparison is zero-pad-tolerant: NodeUpdateBody's own cik validator
    # zero-pads to 10 digits on write (existing behaviour — see
    # test_nodes.py::test_cik_zero_padded), but pre-existing `nodes.cik`
    # values were stored unpadded (e.g. "1045810"). LPAD both sides so a
    # collision isn't missed purely because of that formatting mismatch.
    collision = None
    if ticker or cik:
        collision_rows = query(
            """
            SELECT id::text, name FROM nodes
            WHERE id != %s
              AND ((ticker IS NOT NULL AND ticker = %s)
                OR (cik IS NOT NULL AND LPAD(cik, 10, '0') = LPAD(%s, 10, '0')))
            LIMIT 1
            """,
            (node_id, ticker, cik),
        )
        if collision_rows:
            collision = collision_rows[0]

    if collision:
        logger.warning(
            "dark_horse auto-promotion BLOCKED (ticker/cik collision)  node=%s name=%r "
            "would collide with existing node=%s (%r)  ticker=%s cik=%s",
            node_id,
            name,
            collision["id"],
            collision["name"],
            ticker,
            cik,
        )
        return {
            "promoted": False,
            "reason": "collision",
            "collides_with_node_id": collision["id"],
            "collides_with_name": collision["name"],
        }

    from fastapi.responses import JSONResponse

    from ..api.routers.nodes import NodeUpdateBody, update_node

    body = NodeUpdateBody(
        type="public",
        ticker=ticker,
        cik=cik,
        meta_patch={
            "auto_promotion": {
                "from": "dark_horse",
                "to": "public",
                "reason": signal["reason"],
                "ticker": ticker,
                "cik": cik,
                "at": datetime.now(timezone.utc).isoformat(),
                "source": "pipeline_auto",
            }
        },
    )
    resp = update_node(node_id, body)
    if isinstance(resp, JSONResponse):
        logger.warning(
            "dark_horse auto-promotion rejected by update_node  node=%s name=%r  status=%s",
            node_id,
            name,
            resp.status_code,
        )
        return {"promoted": False, "reason": "update_rejected"}

    logger.info(
        "dark_horse auto-promoted -> public  node=%s name=%r reason=%s ticker=%s cik=%s",
        node_id,
        name,
        signal["reason"],
        ticker,
        cik,
    )
    return {"promoted": True, "reason": signal["reason"], "ticker": ticker, "cik": cik}


# ---------------------------------------------------------------------------
# Acquisition/delisting demotion candidates (public -> private)
# ---------------------------------------------------------------------------


def check_acquisition_demotion_evidence(node_id: str) -> dict | None:
    """
    Is this `public` node the investee (to_node_id) of a
    `subsidiary` edge? That's a strong-but-imperfect signal it's been
    acquired/absorbed and is no longer independently public (real case:
    Pfizer -> Metsera, Inc.). Mirrors check_dark_horse_promotion's shape but for the
    opposite direction and a weaker evidence type.

    Deliberately not as strong an evidence bar as the promotion signal, which
    fires on a CIK/ticker match against a SEC bulk file — near-certain,
    government-published, machine-checkable. This signal depends on the
    extraction pipeline's own `edge_type` classification being correct, and
    that classification is known to be unverified ("edge_type may bias toward
    ownership even when filings describe JVs or subsidiaries") — i.e.
    `edge_type='subsidiary'` is model output, not ground truth, and could in
    principle also mislabel a JV or a minority-stake deal as `subsidiary`.

    Given that acknowledged weakness, this function is READ-ONLY — it never
    writes `nodes.type`. It mirrors the precedent already set by
    check_dark_horse_promotion's own docstring: that function explicitly
    refuses to auto-write dark_horse -> private because "there is no
    comparably strong automatic signal for private" and leaves it to the
    manual node-panel edit. The same reasoning applies here, one notch
    stronger (the evidence exists, it's just not certified-reliable), so the
    design lands on the middle path: detect + flag for review
    (flag_acquisition_demotion_candidate), never silently auto-apply. Actual
    type changes only ever happen through POST /nodes/{id}/update's
    evidence-gated public -> private transition (see main.py), which a human
    (or a future explicitly-opted-in script) triggers deliberately — never
    this function.

    Returns None if the node isn't currently `public` or has no incoming
    `subsidiary` edge. Otherwise:
      {"node_id", "name", "evidence_edge_id", "acquirer_node_id",
       "acquirer_name", "amount_usd"}
    (picks the most recently confirmed subsidiary edge if more than one).
    """
    rows = query("SELECT type::text AS type, name FROM nodes WHERE id = %s", (node_id,))
    if not rows or rows[0]["type"] != "public":
        return None

    edge_rows = query(
        """
        SELECT e.id::text AS edge_id, e.net_amount_usd AS amount_usd,
               a.id::text AS acquirer_node_id, a.name AS acquirer_name
        FROM edges e
        JOIN nodes a ON a.id = e.from_node_id
        WHERE e.to_node_id = %s AND e.edge_type = 'subsidiary'
        ORDER BY e.last_confirmed DESC
        LIMIT 1
        """,
        (node_id,),
    )
    if not edge_rows:
        return None

    ev = edge_rows[0]
    return {
        "node_id": node_id,
        "name": rows[0]["name"],
        "evidence_edge_id": ev["edge_id"],
        "acquirer_node_id": ev["acquirer_node_id"],
        "acquirer_name": ev["acquirer_name"],
        "amount_usd": ev["amount_usd"],
    }


def flag_acquisition_demotion_candidate(node_id: str) -> dict | None:
    """
    write a review-queue-style marker onto `nodes.meta` when
    check_acquisition_demotion_evidence finds evidence — never changes
    `nodes.type` (see that function's docstring for why: the signal isn't
    certified-reliable enough to auto-apply, mirroring
    check_dark_horse_promotion's own dark_horse -> private refusal).

    Idempotent: if the node is already flagged for this exact evidence edge,
    this is a no-op (avoids spamming `meta` writes / logs on repeated calls
    from a periodic sweep or a re-ingest of the same filing).

    Returns None if there's no evidence (nothing to flag). Otherwise:
      {"flagged": True, ...evidence...}   — new flag written
      {"flagged": False, "reason": "already_flagged", ...evidence...}
    """
    evidence = check_acquisition_demotion_evidence(node_id)
    if evidence is None:
        return None

    rows = query("SELECT meta FROM nodes WHERE id = %s", (node_id,))
    existing_meta = (rows[0]["meta"] if rows else None) or {}
    existing_flag = existing_meta.get("acquisition_demotion_candidate")
    if existing_flag and existing_flag.get("evidence_edge_id") == evidence["evidence_edge_id"]:
        return {"flagged": False, "reason": "already_flagged", **evidence}

    marker = {
        "acquisition_demotion_candidate": {
            "reason": "subsidiary_edge_incoming",
            "evidence_edge_id": evidence["evidence_edge_id"],
            "acquirer_node_id": evidence["acquirer_node_id"],
            "acquirer_name": evidence["acquirer_name"],
            "at": datetime.now(timezone.utc).isoformat(),
            "source": "pipeline_auto",
        }
    }
    import psycopg2.extras

    execute(
        "UPDATE nodes SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb WHERE id = %s",
        (psycopg2.extras.Json(marker), node_id),
    )
    logger.info(
        "acquisition demotion candidate flagged  node=%s name=%r evidence_edge=%s acquirer=%r",
        node_id,
        evidence["name"],
        evidence["evidence_edge_id"],
        evidence["acquirer_name"],
    )
    return {"flagged": True, **evidence}


def sweep_acquisition_demotion_candidates() -> dict:
    """
    standalone whole-graph sweep, callable on demand (same spirit as
    the re-resolve sweep script) — catches `subsidiary` edges that
    already existed before this feature's edge-creation hook
    (extraction/pipeline.py::_process_event) was added, or that were written
    by a run where the hook itself failed/was skipped. Idempotent (safe to
    re-run): flag_acquisition_demotion_candidate no-ops on already-flagged
    nodes and check_acquisition_demotion_evidence no-ops on non-public nodes.

    Returns {"checked", "flagged", "already_flagged", "skipped"}.
    """
    rows = query(
        """
        SELECT DISTINCT n.id::text AS id
        FROM nodes n
        JOIN edges e ON e.to_node_id = n.id AND e.edge_type = 'subsidiary'
        WHERE n.type = 'public'
        """
    )
    counts = {"checked": 0, "flagged": 0, "already_flagged": 0, "skipped": 0}
    for row in rows:
        counts["checked"] += 1
        result = flag_acquisition_demotion_candidate(row["id"])
        if result is None:
            counts["skipped"] += 1
        elif result.get("flagged"):
            counts["flagged"] += 1
        else:
            counts["already_flagged"] += 1
    return counts


def enrich_all_nodes(mode: str = "missing") -> dict:
    """
    Iterate nodes, fetch + upsert facts, throttled to ~1 req/sec.

    mode:
      - "missing" (default): nodes with no node_facts row at all — the
        original behaviour.
      - "missing_country": nodes that already have a node_facts row
        but country IS NULL — backfills the 35 existing enriched nodes now
        that country is a resolvable field. Chose to re-run the *full*
        enrich() and let the upsert overwrite every field with fresh data,
        rather than build a country-only partial-fetch path: simpler, and
        not a correctness risk since enrich() output is idempotent for
        unchanged upstream data and never worse than what's already stored.
      - "all": re-enrich every node regardless of existing facts.

    Returns {enriched, skipped, failed}.
    """
    if mode not in ("missing", "missing_country", "all"):
        raise ValueError(f"unknown enrich_all_nodes mode: {mode!r}")

    sql = """
        SELECT n.id::text, n.name, n.cik
        FROM nodes n
        LEFT JOIN node_facts nf ON nf.node_id = n.id
    """
    if mode == "missing":
        sql += " WHERE nf.node_id IS NULL"
    elif mode == "missing_country":
        sql += " WHERE nf.node_id IS NOT NULL AND nf.country IS NULL"
    nodes = query(sql)

    counts = {"enriched": 0, "skipped": 0, "failed": 0}

    for node in nodes:
        try:
            facts = enrich(node["name"], node.get("cik"))
            if facts is None:
                counts["skipped"] += 1
                logger.info("enrich skipped (no facts)  %s", node["name"])
            else:
                upsert_node_facts(node["id"], facts)
                counts["enriched"] += 1
                logger.info("enrich ok  %s  source=%s", node["name"], facts.get("source"))

            # Dark_horse auto-promotion check. Runs regardless of
            # whether `facts` resolved (a confident SEC ticker/CIK match can
            # exist even when Wikidata/EDGAR found nothing usable — see
            # check_dark_horse_promotion). check_dark_horse_promotion()
            # itself no-ops immediately (one cheap type check) for any node
            # that isn't currently `dark_horse`, so this is safe to run
            # unconditionally across every enrich_all_nodes mode.
            promo = check_dark_horse_promotion(node["id"], node["name"], facts)
            if promo is not None:
                if promo.get("promoted"):
                    counts["dark_horse_promoted"] = counts.get("dark_horse_promoted", 0) + 1
                else:
                    counts["dark_horse_blocked"] = counts.get("dark_horse_blocked", 0) + 1
        except Exception:
            counts["failed"] += 1
            logger.exception("enrich failed  %s", node["name"])
        time.sleep(_ENRICH_THROTTLE_SECS)

    return counts
