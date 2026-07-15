"""
Extraction pipeline: scans cached EDGAR filings, dispatches to a backend,
writes events to DB.

Public:
  run_extract_phase(run_id, mode, filing_path=None) → (events_logged, candidates_found, edges_created)
  harvest_pending_batches() → dict
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from ...core.resolve import is_generic_entity, resolve
from ...core.resolve import normalize as _normalize_name
from ...db import bump_run_counters, execute, get_conn, query, set_run_total_units
from .backend import ExtractionJob, ExtractionRequest, ExtractionResult, get_backend
from .prompt import PROMPT_VERSION

logger = logging.getLogger(__name__)

_DATA_DIR = Path("/app/data")

_SOURCE_TIER: dict[str, int] = {"8-K": 1, "SC 13D": 1, "SC 13G": 1}

# Maximum confidence level allowed per source tier.
# Model output is capped down to this; never upgraded.
_TIER_CONFIDENCE_CAP: dict[int, str] = {
    1: "high",
    2: "high",
    3: "medium",
    4: "medium",
    5: "low",
}
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

_VALID_EVENT_TYPES = {"investment", "partial_exit", "full_exit", "cancelled", "correction"}
_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_EDGE_TYPES = {
    "ownership",
    "subsidiary",
    "joint_venture",
    "creditor_debtor",
    "supplier_customer",
}

RELEVANT_ITEMS = {"1.01", "2.01", "3.02", "5.01", "8.01"}

# Syndicate-round detection threshold. A press release naming this many
# (or more) DISTINCT co-investors for the same investee/date/amount, with no
# per-investor breakdown, is treated as one shared round total misattributed to
# each investor individually (not N separate rounds). Tuned to the confirmed
# live examples (Waymo: 13 co-investors, xAI: 5) with margin above the smallest
# legitimate real syndicate size we've seen reported with a genuine per-investor
# breakdown (2, e.g. "led by X, joined by Y") — 3 is the smallest N where "no
# breakdown given" starts being the more likely explanation than "there simply
# were only 2 investors, one of which we've mis-extracted."
SYNDICATE_MIN_COINVESTORS = 3


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(content: str) -> str:
    parser = _StripHTML()
    try:
        parser.feed(content)
        return re.sub(r"\s+", " ", parser.get_text()).strip()
    except Exception:
        return content


# ---------------------------------------------------------------------------
# Item-section extraction
# ---------------------------------------------------------------------------

_ITEM_HEADER_RE = re.compile(r"\bitem\s+(\d+\.\d+)", re.IGNORECASE)


def _extract_relevant_sections(text: str, items: set[str]) -> str:
    """
    Return only the requested Item sections from a stripped filing text.
    Section boundary = any 'Item X.XX' header. Returns "" when no headers found.
    """
    if not items:
        return ""
    matches = list(_ITEM_HEADER_RE.finditer(text))
    if not matches:
        return ""
    sections: list[str] = []
    for i, match in enumerate(matches):
        if match.group(1) not in items:
            continue
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(text[start:end].strip())
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Submission metadata from cached submissions.json
# ---------------------------------------------------------------------------


def _get_filing_meta_map(cik: str) -> dict[str, dict]:
    """Returns {accession_no_dashes: {date, form, primary_doc, items}}."""
    sub_file = _DATA_DIR / "cache" / cik / "submissions.json"
    if not sub_file.exists():
        return {}
    data = json.loads(sub_file.read_text(encoding="utf-8"))
    recent = data.get("filings", {}).get("recent", {})
    items_list = recent.get("items", [])
    result: dict[str, dict] = {}
    for acc, filing_date, form, doc, items_str in zip(
        recent.get("accessionNumber", []),
        recent.get("filingDate", []),
        recent.get("form", []),
        recent.get("primaryDocument", []),
        items_list if items_list else [""] * len(recent.get("accessionNumber", [])),
    ):
        result[acc.replace("-", "")] = {
            "date": filing_date,
            "form": form,
            "primary_doc": doc,
            "items": items_str or "",
        }
    return result


# ---------------------------------------------------------------------------
# Processed-filings idempotency helpers
# ---------------------------------------------------------------------------


def _get_processed_filing(cik: str, accession: str) -> dict | None:
    rows = query(
        "SELECT content_hash FROM processed_filings WHERE cik = %s AND accession = %s",
        (cik, accession),
    )
    return rows[0] if rows else None


def _upsert_processed_filing(
    cik: str,
    accession: str,
    content_hash: str,
    run_id: str | None,
    events_count: int,
) -> None:
    execute(
        """INSERT INTO processed_filings
               (cik, accession, content_hash, run_id, events_count)
           VALUES (%s, %s, %s, %s::uuid, %s)
           ON CONFLICT (cik, accession) DO UPDATE
             SET content_hash = EXCLUDED.content_hash,
                 run_id       = EXCLUDED.run_id,
                 events_count = EXCLUDED.events_count,
                 processed_at = NOW()""",
        (cik, accession, content_hash, run_id, events_count),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_headline(investor: str, investee: str, amount_usd: int | None) -> str:
    if amount_usd and amount_usd > 0:
        if amount_usd >= 1_000_000_000:
            s = f"${amount_usd / 1e9:.1f}B"
        elif amount_usd >= 1_000_000:
            s = f"${amount_usd / 1e6:.0f}M"
        else:
            s = f"${amount_usd:,}"
        return f"{investor} invests {s} in {investee}"
    return f"{investor} invests in {investee}"


def _build_url(cik: str, acc: str, primary_doc: str | None, fallback_name: str) -> str:
    try:
        cik_int = int(cik)
    except ValueError:
        cik_int = 0
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{primary_doc or fallback_name}"


# ---------------------------------------------------------------------------
# Syndicate-round detection (pure — no DB, unit-tested)
# ---------------------------------------------------------------------------


def _detect_syndicate_indices(
    events: list[dict],
    min_coinvestors: int = SYNDICATE_MIN_COINVESTORS,
) -> set[int]:
    """Return the indices into `events` that look like a syndicate-round
    overcount: same investee + same event date + same nonzero amount, named
    by >= min_coinvestors DISTINCT investors, all WITHIN one extraction result
    (one filing / one press release / one web page — the caller always passes
    the events produced by a single source).

    This is a within-result heuristic only. It does NOT look across separate
    ingestion runs or re-reports of the same round from a different source —
    that's a cross-source double-count problem, a different root cause,
    handled separately (see the cross-source syndicate-round detector),
    explicitly out of scope here.

    Amount key: delta_usd if present, else amount_usd (mirrors _process_event's
    own fallback). Events with no positive amount are never grouped — grouping
    on "0" or "None" would flag unrelated no-amount events as a fake syndicate.
    """
    groups: dict[tuple, list[int]] = {}
    for idx, ev in enumerate(events):
        investee = _normalize_name((ev.get("investee") or "").strip())
        investor = _normalize_name((ev.get("investor") or "").strip())
        if not investee or not investor:
            continue

        raw = ev.get("delta_usd")
        if not (isinstance(raw, (int, float)) and raw):
            raw = ev.get("amount_usd")
        try:
            amount = int(raw) if isinstance(raw, (int, float)) and raw else 0
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            continue

        key = (investee, ev.get("date") or "", amount)
        groups.setdefault(key, []).append(idx)

    syndicate_idxs: set[int] = set()
    for idxs in groups.values():
        investors = {_normalize_name((events[i].get("investor") or "").strip()) for i in idxs}
        if len(investors) >= min_coinvestors:
            syndicate_idxs.update(idxs)
    return syndicate_idxs


# ---------------------------------------------------------------------------
# DB write: single event
# ---------------------------------------------------------------------------


def _process_event(
    event: dict,
    filing_meta: dict,
    run_id: str,
    write_news_feed: bool = True,
    force_estimate_reason: str | None = None,
) -> tuple[bool, bool, bool]:
    """Write one extracted event to DB. Returns (event_logged, candidate_created, edge_created).

    write_news_feed=False skips the news_feed INSERT — used by the
    re-resolve sweep, which RE-processes existing news_feed rows and must not
    mint new ones (edge/event/source writes still happen).

    force_estimate_reason: when set (currently only 'syndicate_total'),
    overrides the normal value_status inference so the row is written as
    value_status='estimated' with this estimate_reason, even though the model
    DID return a numeric amount. Callers detect the syndicate pattern across
    the full set of events from one source (see _detect_syndicate_indices)
    before calling this function per-event.
    """
    investor_name = (event.get("investor") or "").strip()
    investee_name = (event.get("investee") or "").strip()
    if not investor_name or not investee_name:
        return False, False, False

    # Collective nouns ("Various Underwriters", "Public Market
    # Investors") can't anchor a real edge — drop the event before any
    # candidate is created for either party.
    for role, party in (("investor", investor_name), ("investee", investee_name)):
        if is_generic_entity(party):
            logger.info("dropped event: generic %s entity %r", role, party)
            return False, False, False

    raw_amount = event.get("amount_usd")
    amount_usd = int(raw_amount) if isinstance(raw_amount, (int, float)) and raw_amount else None
    raw_delta = event.get("delta_usd")
    delta_usd = int(raw_delta) if isinstance(raw_delta, (int, float)) and raw_delta else (amount_usd or 0)

    event_type = event.get("event_type", "investment")
    if event_type not in _VALID_EVENT_TYPES:
        event_type = "investment"

    confidence = event.get("confidence", "medium")
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"

    edge_type = event.get("edge_type", "ownership")
    if edge_type not in _VALID_EDGE_TYPES:
        edge_type = "ownership"

    excerpt = (event.get("excerpt") or "")[:500]

    filing_date_str = filing_meta.get("date")
    try:
        event_date = date.fromisoformat(event.get("date") or "")
    except (ValueError, TypeError):
        try:
            event_date = date.fromisoformat(filing_date_str) if filing_date_str else date.today()
        except ValueError:
            event_date = date.today()

    if filing_date_str:
        try:
            published_at = datetime.fromisoformat(filing_date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            published_at = datetime.now(timezone.utc)
    else:
        published_at = datetime.now(timezone.utc)

    form_type = filing_meta.get("form_type", "")
    # Web hits pass source_tier explicitly; EDGAR derives it from form type.
    # max(x, 3) enforces the hard floor: web is never tier 1/2.
    if "source_tier" in filing_meta:
        source_tier = max(int(filing_meta["source_tier"]), 3)
    else:
        source_tier = _SOURCE_TIER.get(form_type, 3)
    discovery_source = filing_meta.get("discovery_source", "edgar")
    source_name = filing_meta.get("source_name", "EDGAR/SEC")
    source_url = filing_meta.get("url", "")

    # Cap confidence by source tier — model output cannot exceed tier's ceiling.
    cap = _TIER_CONFIDENCE_CAP.get(source_tier, "low")
    if _CONFIDENCE_RANK.get(confidence, 1) > _CONFIDENCE_RANK[cap]:
        confidence = cap

    # value_status: 'actual' when model returned a numeric amount; 'estimated' otherwise.
    if amount_usd is not None and amount_usd != 0:
        value_status = "actual"
        estimate_reason = None
    else:
        value_status = "estimated"
        estimate_reason = "no_amount"

    # Syndicate-round override — amount IS known, but it's the FULL
    # round total the source attributed to every co-investor with no
    # per-investor breakdown. Downgrade to 'estimated' with a distinct reason
    # so this is never confused with the "model gave no number" case above.
    if force_estimate_reason:
        value_status = "estimated"
        estimate_reason = force_estimate_reason

    norm_investor = _normalize_name(investor_name)
    norm_investee = _normalize_name(investee_name)

    investor_res = resolve(investor_name, source_url=source_url)
    investee_res = resolve(investee_name, investor_res.node_id, source_url=source_url)

    event_logged = False
    candidate_created = False
    edge_created = False
    source_id: str | None = None

    if investor_res.resolved and investee_res.resolved:
        from_id = investor_res.node_id
        to_id = investee_res.node_id

        # All four writes share one connection so the deferred edges_require_source
        # trigger fires after the sources INSERT, not after the edge INSERT alone.
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # edge_type excluded from DO UPDATE SET — first-seen classification wins.
                cur.execute(
                    """INSERT INTO edges
                       (from_node_id, to_node_id, net_amount_usd, first_seen, last_confirmed,
                        source_count, is_confirmed, edge_type)
                       VALUES (%s::uuid, %s::uuid, 0, NOW(), NOW(), 0, %s, %s)
                       ON CONFLICT (from_node_id, to_node_id) DO UPDATE
                         SET last_confirmed = NOW(),
                             is_confirmed   = EXCLUDED.is_confirmed OR edges.is_confirmed
                       RETURNING id::text, (xmax = 0) AS is_new""",
                    (from_id, to_id, confidence == "high", edge_type),
                )
                row = cur.fetchone()
                edge_id: str = row[0]
                edge_created = bool(row[1])

                # ON CONFLICT DO UPDATE returns existing event_id on canonical match.
                # NOTE: value_status/estimate_reason are NOT in the DO UPDATE SET —
                # append-only rule: a re-ingest of the same canonical event never
                # changes an existing row's classification.
                #
                # Canonical key is (edge_id, event_type, source_url,
                # delta_usd), NOT event_date — event_date is an LLM extraction
                # output, not a stable fact, and keying on it let a re-processed
                # source with a slightly different extracted date slip past as
                # a "new" event, silently inflating the edge's summed total
                # (confirmed live: 40 edges, ~$352.1B phantom, see migration
                # 020_canonical_key_source_url.sql). source_url is the actually
                # stable identifier of "this is the same real-world fact."
                cur.execute(
                    """INSERT INTO investment_events
                       (edge_id, delta_usd, event_type, event_date, source_url,
                        source_tier, filing_type, confidence, raw_excerpt, value_status,
                        estimate_reason, discovery_source)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (edge_id, event_type, source_url, delta_usd)
                         DO UPDATE SET created_at = investment_events.created_at
                       RETURNING id::text""",
                    (
                        edge_id,
                        delta_usd,
                        event_type,
                        event_date,
                        source_url,
                        source_tier,
                        form_type,
                        confidence,
                        excerpt,
                        value_status,
                        estimate_reason,
                        discovery_source,
                    ),
                )
                ev_row = cur.fetchone()
                event_id = ev_row[0] if ev_row else None

                # Always SUM from events — never set net_amount_usd directly.
                cur.execute(
                    """UPDATE edges
                       SET net_amount_usd = (
                           SELECT COALESCE(SUM(delta_usd), 0)
                           FROM investment_events WHERE edge_id = %s::uuid
                       )
                       WHERE id = %s::uuid""",
                    (edge_id, edge_id),
                )

                # Source row inserted last so the deferred trigger sees it at commit.
                cur.execute(
                    """INSERT INTO sources
                       (edge_id, url, filing_type, source_tier, published_at, raw_excerpt, event_id,
                        discovery_source)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::uuid, %s)
                       RETURNING id::text""",
                    (
                        edge_id,
                        source_url,
                        form_type,
                        source_tier,
                        published_at,
                        excerpt,
                        event_id,
                        discovery_source,
                    ),
                )
                src_row = cur.fetchone()
                source_id = src_row[0] if src_row else None

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        event_logged = True

        if edge_type == "subsidiary":
            # A subsidiary edge landing on the investee is the
            # natural trigger point for the acquisition-demotion signal —
            # it's about the edge just written, not about node facts, so it
            # runs here rather than in enrich_all_nodes (its hook
            # point). Flag-only (see check_acquisition_demotion_evidence /
            # flag_acquisition_demotion_candidate docstrings for why this
            # never auto-writes nodes.type). Best-effort: a failure here
            # must never break event/edge ingestion, which already
            # committed above.
            try:
                from ...core.enrichment import flag_acquisition_demotion_candidate

                flag_acquisition_demotion_candidate(to_id)
            except Exception:
                logger.exception("acquisition-demotion flag check failed  node=%s", to_id)

    if not investor_res.resolved or not investee_res.resolved:
        candidate_created = True

    if not write_news_feed:
        return event_logged, candidate_created, edge_created

    headline = _make_headline(investor_name, investee_name, amount_usd)
    try:
        execute(
            """INSERT INTO news_feed
               (headline, url, source_tier, source_name, published_at,
                extracted_investor, extracted_investee, amount_usd,
                normalized_investor, normalized_investee,
                confirmed_by_sec, sec_source_id, pipeline_run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s::uuid)
               ON CONFLICT (normalized_investor, normalized_investee, utc_date(published_at), amount_usd)
                 DO NOTHING""",
            (
                headline,
                source_url,
                source_tier,
                source_name,
                published_at,
                investor_name,
                investee_name,
                amount_usd,
                norm_investor,
                norm_investee,
                source_tier == 1,
                source_id,
                run_id,
            ),
        )
    except Exception as exc:
        logger.warning("news_feed insert skipped: %s", exc)

    return event_logged, candidate_created, edge_created


# ---------------------------------------------------------------------------
# File scanning — build ExtractionRequest list
# ---------------------------------------------------------------------------


def _build_request(
    run_id: str,
    cik: str,
    form_type: str,
    acc: str,
    filing_file: Path,
    node_name: str,
    file_meta: dict | None = None,
    preread_stripped_text: str | None = None,
) -> tuple[ExtractionRequest | None, dict]:
    """Read one filing and build an ExtractionRequest. Returns (None, {}) to skip.

    Pass preread_stripped_text to reuse already-read+stripped content and avoid a
    second file read when the caller already computed it for the idempotency check.
    """
    if file_meta is None:
        file_meta = {}

    actual_form = file_meta.get("form") or form_type
    filing_date = file_meta.get("date")
    primary_doc = file_meta.get("primary_doc") or filing_file.name
    url = _build_url(cik, acc, primary_doc, filing_file.name)

    try:
        if preread_stripped_text is not None:
            text = preread_stripped_text
        else:
            content = filing_file.read_text(encoding="utf-8", errors="replace")
            text = _strip_html(content)

        if len(text) < 100:
            return None, {}

        effective_text = text
        if actual_form == "8-K":
            raw_items = file_meta.get("items", "")
            filed_items = {i.strip() for i in raw_items.split(",") if i.strip()}
            target_items = (filed_items & RELEVANT_ITEMS) if filed_items else RELEVANT_ITEMS
            sliced = _extract_relevant_sections(text, target_items)
            if sliced:
                effective_text = sliced
            else:
                logger.info(
                    "item-extraction fallback  cik=%s  accession=%s  items=%s",
                    cik,
                    acc,
                    sorted(filed_items),
                )

    except Exception as exc:
        logger.warning("read failed — %s CIK %s %s: %s", node_name, cik, filing_file.name, exc)
        return None, {}

    filing_meta = {
        "url": url,
        "form_type": actual_form,
        "date": filing_date,
        "node_name": node_name,
    }
    req = ExtractionRequest(
        custom_id=f"{run_id}:{cik}:{acc}",
        cik=cik,
        accession=acc,
        form_type=actual_form,
        node_name=node_name,
        text=effective_text,
        filing_date=filing_date,
        source_url=url,
    )
    return req, filing_meta


def _scan_cache(
    run_id: str,
    single_path: Path | None = None,
) -> tuple[list[ExtractionRequest], dict[str, dict]]:
    """
    Walk cache directory (or one file in single-filing dev mode).
    Returns (requests, meta_lookup) where meta_lookup is keyed by custom_id.
    Filings with a .processed sidecar or a matching processed_filings DB row are skipped.
    """
    nodes = query("SELECT id::text, name, cik FROM nodes WHERE cik IS NOT NULL")
    node_by_cik: dict[str, dict] = {n["cik"]: n for n in nodes}

    requests: list[ExtractionRequest] = []
    meta_lookup: dict[str, dict] = {}

    if single_path is not None:
        if single_path.suffix == ".processed":
            return [], {}
        parts = single_path.parts
        if len(parts) < 3:
            logger.warning("filing_path too short to parse: %s", single_path)
            return [], {}
        cik = parts[-3]
        form_type = parts[-2].replace("_", " ")
        acc = single_path.stem
        node = node_by_cik.get(cik)
        node_name = node["name"] if node else cik

        sidecar = single_path.with_suffix(".processed")
        if sidecar.exists():
            logger.info("skipped (sidecar)  %s/%s", cik, acc)
            return [], {}

        try:
            stripped_text = _strip_html(single_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("read failed %s: %s", single_path, exc)
            return [], {}

        content_hash = hashlib.sha256(stripped_text.encode()).hexdigest()
        existing = _get_processed_filing(cik, acc)
        if existing:
            if existing["content_hash"] == content_hash:
                sidecar.touch()
                logger.info("skipped (db)  %s/%s — sidecar restored", cik, acc)
                return [], {}
            logger.info("re-processing (amended)  %s/%s", cik, acc)

        req, meta = _build_request(
            run_id,
            cik,
            form_type,
            acc,
            single_path,
            node_name,
            preread_stripped_text=stripped_text,
        )
        if req:
            meta.update(
                {
                    "content_hash": content_hash,
                    "sidecar_path": sidecar,
                    "cik": cik,
                    "accession": acc,
                }
            )
            requests.append(req)
            meta_lookup[req.custom_id] = meta
        return requests, meta_lookup

    cache_root = _DATA_DIR / "cache"
    if not cache_root.exists():
        logger.warning("cache directory %s not found — skipping extraction", cache_root)
        return [], {}

    for cik_dir in sorted(cache_root.iterdir()):
        if not cik_dir.is_dir():
            continue
        cik = cik_dir.name
        node = node_by_cik.get(cik)
        if not node:
            continue

        meta_map = _get_filing_meta_map(cik)
        node_name = node["name"]

        for form_dir in sorted(cik_dir.iterdir()):
            if not form_dir.is_dir():
                continue
            form_type = form_dir.name.replace("_", " ")

            for filing_file in sorted(form_dir.iterdir()):
                if not filing_file.is_file():
                    continue
                if filing_file.suffix == ".processed":
                    continue

                acc = filing_file.stem
                file_meta = meta_map.get(acc, {})
                actual_form = file_meta.get("form") or form_type

                # 8-K item filter: skip filings whose items are all irrelevant.
                if actual_form == "8-K":
                    raw_items = file_meta.get("items", "")
                    filed_items = {i.strip() for i in raw_items.split(",") if i.strip()}
                    if filed_items and not filed_items.intersection(RELEVANT_ITEMS):
                        logger.info("skipped  %-30s 8-K  items=%s", node_name, sorted(filed_items))
                        continue

                # Fast path: sidecar exists → skip without any file I/O.
                sidecar = filing_file.with_suffix(".processed")
                if sidecar.exists():
                    logger.info("skipped (sidecar)  %-30s %s", node_name, acc)
                    continue

                # Read file and compute hash for the DB fallback check.
                try:
                    stripped_text = _strip_html(filing_file.read_text(encoding="utf-8", errors="replace"))
                    if len(stripped_text) < 100:
                        continue
                except Exception as exc:
                    logger.warning("read failed — %s CIK %s %s: %s", node_name, cik, filing_file.name, exc)
                    continue

                content_hash = hashlib.sha256(stripped_text.encode()).hexdigest()

                # DB fallback: same hash → restore sidecar and skip.
                # Different hash → filing was amended; fall through to process.
                existing = _get_processed_filing(cik, acc)
                if existing:
                    if existing["content_hash"] == content_hash:
                        sidecar.touch()
                        logger.info("skipped (db)  %-30s %s — sidecar restored", node_name, acc)
                        continue
                    logger.info("re-processing (amended)  %-30s %s", node_name, acc)

                req, meta = _build_request(
                    run_id,
                    cik,
                    form_type,
                    acc,
                    filing_file,
                    node_name,
                    file_meta,
                    preread_stripped_text=stripped_text,
                )
                if req:
                    meta.update(
                        {
                            "content_hash": content_hash,
                            "sidecar_path": sidecar,
                            "cik": cik,
                            "accession": acc,
                        }
                    )
                    requests.append(req)
                    meta_lookup[req.custom_id] = meta

    return requests, meta_lookup


# ---------------------------------------------------------------------------
# Result processing
# ---------------------------------------------------------------------------


def _process_results(
    results: list[ExtractionResult],
    meta_lookup: dict[str, dict],
    run_id: str,
) -> tuple[int, int, int]:
    events_logged = 0
    candidates_found = 0
    edges_created = 0

    for result in results:
        filing_meta = meta_lookup.get(result.custom_id)
        if not filing_meta:
            logger.warning("no filing_meta for custom_id %s — skipping", result.custom_id)
            continue
        if result.error:
            logger.warning("extraction failed for %s: %s", result.custom_id, result.error)
            continue
        logger.info(
            "%-30s %-10s → %d event(s)",
            filing_meta.get("node_name", result.custom_id),
            filing_meta.get("form_type", ""),
            len(result.events),
        )

        # Detect syndicate-round events within this one filing/result
        # BEFORE writing any of them, so all co-investor rows in the group are
        # tagged consistently.
        syndicate_idxs = _detect_syndicate_indices(result.events)

        filing_events = 0
        filing_candidates = 0
        filing_edges = 0
        for idx, event in enumerate(result.events):
            force_reason = "syndicate_total" if idx in syndicate_idxs else None
            logged, candidate, new_edge = _process_event(event, filing_meta, run_id, force_estimate_reason=force_reason)
            if logged:
                events_logged += 1
                filing_events += 1
            if candidate:
                candidates_found += 1
                filing_candidates += 1
            if new_edge:
                edges_created += 1
                filing_edges += 1

        # Live-progress bump — once per filing (not per event,
        # to keep write volume sane), so a run in flight shows real counts
        # climbing via the existing 5s Runs-tab poll instead of only updating
        # at the end. units_processed is always included (this filing was
        # attempted regardless of yield — the percent-complete numerator must
        # move even for a zero-event filing); events/candidates/edges are only
        # included when nonzero, same as before.
        deltas = {"units_processed": 1}
        if filing_events:
            deltas["events_logged"] = filing_events
        if filing_candidates:
            deltas["candidates_found"] = filing_candidates
        if filing_edges:
            deltas["edges_created"] = filing_edges
        bump_run_counters(run_id, **deltas)

        # Mark filing processed — even if filing_events == 0 (legitimate no-content filings
        # should not be re-extracted on the next run).
        content_hash = filing_meta.get("content_hash")
        sidecar_path: Path | None = filing_meta.get("sidecar_path")
        cik = filing_meta.get("cik")
        accession = filing_meta.get("accession")
        if not (cik and accession):
            parts = result.custom_id.split(":", 2)
            if len(parts) == 3:
                _, cik, accession = parts

        if content_hash and cik and accession:
            try:
                _upsert_processed_filing(cik, accession, content_hash, run_id, filing_events)
            except Exception as exc:
                logger.warning("processed_filings upsert failed %s/%s: %s", cik, accession, exc)

        if sidecar_path is not None:
            try:
                sidecar_path.touch()
            except Exception as exc:
                logger.warning("sidecar write failed %s: %s", sidecar_path, exc)

    return events_logged, candidates_found, edges_created


# ---------------------------------------------------------------------------
# Forward safety net: OpenAI response id logging
# ---------------------------------------------------------------------------


def _log_openai_response(
    endpoint: str,
    model: str,
    response_id: str | None,
    context: dict,
) -> None:
    """Persist an OpenAI response/completion id so it can be recovered later.

    Best-effort and never raises — a failure here must not break extraction.
    Covers the two call sites that had no durable id (realtime.py chat
    completions; websearch.py search + extract). batch.py's batch.id already
    has a durable home in processing_batches and is NOT logged here (would be
    a duplicate, not a gap).
    """
    if not response_id:
        logger.warning(
            "no response id to log — endpoint=%s model=%s context=%s",
            endpoint,
            model,
            context,
        )
        return
    try:
        execute(
            """INSERT INTO openai_response_log (endpoint, model, response_id, context)
               VALUES (%s, %s, %s, %s::jsonb)""",
            (endpoint, model, response_id, json.dumps(context)),
        )
    except Exception as exc:
        logger.warning(
            "openai_response_log insert failed  endpoint=%s response_id=%s: %s",
            endpoint,
            response_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Batch DB bookkeeping
# ---------------------------------------------------------------------------


def _store_batch_submission(
    run_id: str,
    batch_id: str,
    requests: list[ExtractionRequest],
) -> None:
    """Persist batch metadata to DB so harvest can route results without re-reading disk."""
    for req in requests:
        execute(
            """INSERT INTO processing_batches
               (run_id, batch_id, custom_id, cik, accession, form_type,
                filing_date, source_url, prompt_version)
               VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (batch_id, custom_id) DO NOTHING""",
            (
                run_id,
                batch_id,
                req.custom_id,
                req.cik,
                req.accession,
                req.form_type,
                req.filing_date,
                req.source_url,
                PROMPT_VERSION,
            ),
        )
    execute(
        "UPDATE pipeline_runs SET batch_id = %s, awaiting_harvest_since = NOW() WHERE id = %s",
        (batch_id, run_id),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_extract_phase(
    run_id: str,
    mode: str,
    filing_path: str | None = None,
) -> tuple[int, int, int]:
    """
    Scan cached filings and extract events via the chosen backend.

    realtime: runs synchronously, returns actual (events, candidates, edges) counts.
    batch: submits to OpenAI Batch API, stores metadata, returns (0, 0, 0).
           Counts are recorded when harvest_pending_batches() runs.
    """
    single_path = Path(filing_path) if filing_path else None
    requests, meta_lookup = _scan_cache(run_id, single_path)

    # Total_units = filings that will actually be extracted, i.e.
    # AFTER the existing idempotency skip inside _scan_cache (sidecar/hash
    # match) — a skipped filing is an instant no-op, not billed/timed work,
    # so counting it would make the ETA dishonest. Set once here regardless
    # of mode: batch-mode's real extraction happens later in
    # harvest_pending_batches(), but the total (what WILL be extracted) is
    # already fully known at submit time.
    set_run_total_units(run_id, len(requests))

    if not requests:
        logger.info("no filings to extract for run %s", run_id)
        return 0, 0, 0

    backend = get_backend(mode)
    job = backend.submit(requests)

    if mode == "batch":
        _store_batch_submission(run_id, job.batch_id, requests)
        logger.info(
            "batch %s submitted — %d requests queued, harvest pending",
            job.batch_id,
            len(requests),
        )
        return 0, 0, 0

    results = backend.harvest(job)
    return _process_results(results, meta_lookup, run_id)


def harvest_pending_batches() -> dict:
    """
    Walk pipeline_runs awaiting harvest, poll each batch, harvest if ready.

    Manual trigger, also wired into the background scheduler.
    Returns {"runs_checked": N, "runs_harvested": N, "runs_pending": N}.
    """
    from .batch import BatchBackend

    runs = query(
        """SELECT id::text, batch_id
           FROM pipeline_runs
           WHERE awaiting_harvest_since IS NOT NULL AND status = 'running'
           ORDER BY awaiting_harvest_since"""
    )

    checked = 0
    harvested = 0
    pending = 0
    backend = BatchBackend()

    for run in runs:
        checked += 1
        run_id = run["id"]
        job = ExtractionJob(mode="batch", batch_id=run["batch_id"])

        if not backend.is_ready(job):
            pending += 1
            logger.info("batch %s not ready yet (run %s)", run["batch_id"], run_id)
            continue

        batch_rows = query(
            """SELECT custom_id, cik, accession, form_type,
                      filing_date, source_url, prompt_version
               FROM processing_batches
               WHERE run_id = %s AND harvested_at IS NULL""",
            (run_id,),
        )

        # Build meta_lookup with idempotency fields for _process_results.
        meta_lookup: dict[str, dict] = {}
        for r in batch_rows:
            cik = r["cik"]
            accession = r["accession"]
            form_type = r["form_type"]
            form_dir_name = form_type.replace(" ", "_")
            filing_dir = _DATA_DIR / "cache" / cik / form_dir_name

            sidecar_path: Path | None = None
            content_hash: str | None = None
            if filing_dir.exists():
                sidecar_path = filing_dir / f"{accession}.processed"
                filing_files = [
                    f for f in filing_dir.iterdir() if f.stem == accession and f.suffix != ".processed" and f.is_file()
                ]
                if filing_files:
                    try:
                        text = _strip_html(filing_files[0].read_text(encoding="utf-8", errors="replace"))
                        content_hash = hashlib.sha256(text.encode()).hexdigest()
                    except Exception as exc:
                        logger.warning("hash computation failed %s/%s: %s", cik, accession, exc)

            meta_lookup[r["custom_id"]] = {
                "url": r["source_url"] or "",
                "form_type": form_type,
                "date": r["filing_date"],
                "node_name": cik,
                "cik": cik,
                "accession": accession,
                "content_hash": content_hash,
                "sidecar_path": sidecar_path,
            }

        stale = [r for r in batch_rows if r["prompt_version"] != PROMPT_VERSION]
        if stale:
            logger.warning(
                "run %s: %d/%d requests used a different prompt_version — results may not match current schema",
                run_id,
                len(stale),
                len(batch_rows),
            )

        results = backend.harvest(job)
        events_logged, candidates_found, edges_created = _process_results(results, meta_lookup, run_id)

        for result in results:
            if not result.error:
                execute(
                    """UPDATE processing_batches
                       SET harvested_at = NOW()
                       WHERE run_id = %s AND custom_id = %s""",
                    (run_id, result.custom_id),
                )

        execute(
            """UPDATE pipeline_runs
               SET status                = 'completed',
                   completed_at          = NOW(),
                   events_logged         = %s,
                   candidates_found      = %s,
                   edges_created         = %s,
                   awaiting_harvest_since = NULL
               WHERE id = %s""",
            (events_logged, candidates_found, edges_created, run_id),
        )
        harvested += 1
        logger.info(
            "harvested run %s — events=%d candidates=%d edges=%d",
            run_id,
            events_logged,
            candidates_found,
            edges_created,
        )

    return {"runs_checked": checked, "runs_harvested": harvested, "runs_pending": pending}
