"""
Entity resolution — 5-pass alias lookup, plus a
parent/subsidiary shell-fold pass.

Pass 1: exact match on nodes.name
Pass 2: exact match on node_aliases.alias
Pass 3: normalised match on node_aliases.normalized_alias
Pass 3.5: legal-shell / subsidiary-wrapper fold — strips SEC-filing
          vehicle qualifiers (e.g. "NV Investment Holdings", "Mobility II")
          and, on an exact match to an existing node's alias, auto-registers
          the raw name as an alias of that parent (source='pipeline_auto').
          See find_shell_parent().
Pass 4: Levenshtein fuzzy match
         ≤ 1 → auto-register alias (source='pipeline_auto') and link
               if ON CONFLICT (normalized_alias) → fall through to candidate
         2–5 → create candidate flagged for review with suggested match
Pass 5: no match → create candidate
"""

import logging
import re
from dataclasses import dataclass, field

import psycopg2.extras
from rapidfuzz.distance import Levenshtein

from ..db import execute, query

logger = logging.getLogger(__name__)

# Stripped iteratively in normalize(); longer/dotted variants listed first
# so the regex alternation is unambiguous.
_SUFFIX_RE = re.compile(
    r"(?:[,\s]+)"
    r"(?:n\.v\.?|incorporated|corporation|international|holdings|limited"
    r"|inc\.?|corp\.?|co\.?|ltd\.?|llc|llp|plc|pbc|se|ag|group)"
    r"\.?\s*$",
    re.IGNORECASE,
)


def normalize(name: str) -> str:
    """Canonical form used for Pass 3 and fuzzy matching."""
    s = name.lower().strip()
    while True:
        stripped = _SUFFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(".,;:")


# Collective-noun gate. A name is rejected as a non-entity when its
# head token is a generic role noun AND every token is generic vocabulary —
# "Various Underwriters" fails, "Fidelity Investments" passes on "fidelity".
_GENERIC_HEAD_NOUNS = frozenset(
    {
        "investor",
        "investors",
        "underwriter",
        "underwriters",
        "shareholder",
        "shareholders",
        "stockholder",
        "stockholders",
        "lender",
        "lenders",
        "bank",
        "banks",
        "holder",
        "holders",
        "purchaser",
        "purchasers",
        "buyer",
        "buyers",
        "public",
        "syndicate",
        "offering",
    }
)
_GENERIC_QUALIFIERS = frozenset(
    {
        "various",
        "certain",
        "several",
        "multiple",
        "the",
        "a",
        "an",
        "of",
        "and",
        "its",
        "through",
        "public",
        "market",
        "institutional",
        "retail",
        "existing",
        "new",
        "unnamed",
        "undisclosed",
        "other",
        "affiliated",
        "accredited",
        "consortium",
        "offering",
        "underwriting",
        "initial",
        "private",
        "strategic",
        # Nationality/scope adjectives — "unnamed Chinese investors" leaked
        # through the gate live (RSS run, 2026-07-08).
        "foreign",
        "domestic",
        "overseas",
        "global",
        "local",
        "sovereign",
        "american",
        "european",
        "asian",
        "chinese",
        "japanese",
        "korean",
        "indian",
        "saudi",
        "emirati",
        "qatari",
        "singaporean",
        "russian",
        "german",
        "french",
        "british",
        "canadian",
        "israeli",
    }
)
# Placeholder strings the model emits when it has no real name.
_PLACEHOLDER_NAMES = frozenset(
    {
        "n/a",
        "na",
        "none",
        "unknown",
        "not specified",
        "not disclosed",
        "undisclosed",
        "various",
        "tbd",
    }
)

# Affiliate / fund-pool fragment patterns. These reference another
# entity or an unnamed pool rather than naming a distinct organization:
#   "affiliate of Silver Lake", "an affiliate of Silver Lake",
#   "Silver Lake affiliate", "Apollo-managed funds and affiliates",
#   "... funds and affiliates". The real entity (Silver Lake, Apollo) is a
#   separate, valid node — only the fragment is rejected.
_FRAGMENT_RE = re.compile(
    r"\baffiliates?\s+of\b"  # affiliate(s) of X
    r"|\baffiliates?$"  # ...trailing "affiliate(s)"  (X affiliate)
    r"|\band\s+affiliates?\b"  # X and affiliates / funds and affiliates
    r"|\baffiliates?\s+and\b"  # X affiliates and funds (reverse order)
    r"|\bmanaged\s+funds?\b",  # X-managed fund(s)
)


def is_generic_entity(name: str) -> bool:
    """True when the name is a collective noun, placeholder, or affiliate/fund
    fragment — not a specific organization."""
    norm = normalize(name)
    if not norm or norm in _PLACEHOLDER_NAMES:
        return True
    if _FRAGMENT_RE.search(norm):
        return True
    tokens = norm.split()
    return tokens[-1] in _GENERIC_HEAD_NOUNS and all(
        t in _GENERIC_HEAD_NOUNS or t in _GENERIC_QUALIFIERS for t in tokens
    )


# Legal-shell / SEC-filing subsidiary-wrapper qualifier phrases.
# These are financing/filing vehicles that carry no independent business
# identity — e.g. "Amazon.com NV Investment Holdings LLC" is the SEC-filing
# entity that legally holds Amazon's stake in OpenAI, not a distinct investor;
# "AT&T Mobility II LLC" is AT&T's debt-issuing subsidiary. Stripped
# iteratively (like _SUFFIX_RE) so multi-qualifier names reduce fully.
# Deliberately narrow and checked against a real existing alias afterward
# (see find_shell_parent) — this must NOT fold real operating subsidiaries
# that carry their own independent investment activity (Waymo LLC, Altera
# Corporation, xAI, Wing Aviation LLC all have edge_type='subsidiary' edges
# to a parent but are correctly kept as distinct nodes; none of their names
# reduce to a bare parent alias via these qualifiers, so they never match).
_SHELL_QUALIFIER_RE = re.compile(
    r"\s+(?:nv\s+investment|investment|mobility\s+(?:i{1,3}|iv|v)"
    r"|capital\s+markets|global\s+finance|finance|financial\s+services"
    r"|funding\s+trust|funding)\s*$",
    re.IGNORECASE,
)


def _strip_shell_qualifiers(norm: str) -> str:
    s = norm
    while True:
        stripped = _SHELL_QUALIFIER_RE.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    return s


def find_shell_parent(name: str) -> tuple[str, str] | None:
    """
    Detect a legal-shell / subsidiary-wrapper name that is a filing vehicle for
    an EXISTING parent node. Strips known shell-qualifier phrases from the
    normalized name and, only if that changed something, checks for an EXACT
    existing node_aliases match on the remainder. Returns (node_id, node_name)
    or None.
    """
    norm = normalize(name)
    base = _strip_shell_qualifiers(norm)
    if base == norm:
        return None  # no shell-qualifier suffix found — nothing to fold

    rows = query(
        """SELECT na.node_id::text, n.name AS node_name
           FROM node_aliases na JOIN nodes n ON n.id = na.node_id
           WHERE na.normalized_alias = %s
           LIMIT 1""",
        (base,),
    )
    if rows:
        return rows[0]["node_id"], rows[0]["node_name"]
    return None


@dataclass
class ResolveResult:
    node_id: str | None  # set when resolved to a known node
    action: str  # 'linked' | 'fuzzy_linked' | 'shell_folded' | 'candidate_review' | 'candidate_new'
    candidate_id: str | None = field(default=None)
    suggested_node_id: str | None = field(default=None)
    suggested_node_name: str | None = field(default=None)

    @property
    def resolved(self) -> bool:
        return self.node_id is not None


def resolve(name: str, investor_id: str | None = None, source_url: str | None = None) -> ResolveResult:
    """
    Run 5-pass entity resolution against nodes and node_aliases.

    investor_id: stored as suggested_investor / discovered_by_nodes[0] on new candidates.
    source_url: stored in discovered_urls on new candidates.
    """
    # Pass 1 — exact match on nodes.name
    rows = query("SELECT id::text FROM nodes WHERE name = %s", (name,))
    if rows:
        return ResolveResult(node_id=rows[0]["id"], action="linked")

    # Pass 2 — exact match on node_aliases.alias
    rows = query("SELECT node_id::text FROM node_aliases WHERE alias = %s", (name,))
    if rows:
        return ResolveResult(node_id=rows[0]["node_id"], action="linked")

    # Pass 3 — normalized match on node_aliases.normalized_alias
    norm = normalize(name)
    rows = query(
        "SELECT node_id::text FROM node_aliases WHERE normalized_alias = %s",
        (norm,),
    )
    if rows:
        return ResolveResult(node_id=rows[0]["node_id"], action="linked")

    # Pass 3.5 — legal-shell / subsidiary-wrapper fold. Catches
    # SEC-filing vehicle names (too different from the parent for fuzzy Pass 4
    # to reach, e.g. "amazon.com nv investment" vs "amazon.com") before they
    # fragment into a separate candidate/node.
    shell = find_shell_parent(name)
    if shell is not None:
        shell_node_id, shell_node_name = shell
        returning = execute(
            """INSERT INTO node_aliases (node_id, alias, normalized_alias, source)
               VALUES (%s, %s, %s, 'pipeline_auto')
               ON CONFLICT (normalized_alias) DO NOTHING
               RETURNING id""",
            (shell_node_id, name, norm),
        )
        if returning:
            return ResolveResult(node_id=shell_node_id, action="shell_folded")
        # conflict → normalized_alias already claimed by someone else; fall
        # through to fuzzy/candidate passes rather than guessing.

    # Pass 4 — fuzzy Levenshtein match across all normalized aliases
    all_aliases = query(
        """SELECT na.node_id::text, na.normalized_alias, n.name AS node_name
           FROM node_aliases na
           JOIN nodes n ON n.id = na.node_id"""
    )

    best_dist: int | None = None
    best_row: dict | None = None
    for row in all_aliases:
        d = Levenshtein.distance(norm, row["normalized_alias"])
        if best_dist is None or d < best_dist:
            best_dist = d
            best_row = row

    if best_dist is not None and best_dist <= 1:
        # Auto-register alias; ON CONFLICT (normalized_alias) DO NOTHING means
        # the normalized form already belongs to a different node — fall through.
        returning = execute(
            """INSERT INTO node_aliases (node_id, alias, normalized_alias, source)
               VALUES (%s, %s, %s, 'pipeline_auto')
               ON CONFLICT (normalized_alias) DO NOTHING
               RETURNING id""",
            (best_row["node_id"], name, norm),
        )
        if returning:
            return ResolveResult(node_id=best_row["node_id"], action="fuzzy_linked")
        # conflict → fall through to candidate

    if best_dist is not None and 2 <= best_dist <= 5:
        notes = (
            f"Fuzzy match: '{best_row['node_name']}'"
            f" (normalized '{best_row['normalized_alias']}', distance {best_dist})"
        )
        cid = _create_candidate(name, investor_id, notes, source_url)
        return ResolveResult(
            node_id=None,
            action="candidate_review",
            candidate_id=cid,
            suggested_node_id=best_row["node_id"],
            suggested_node_name=best_row["node_name"],
        )

    # Pass 5 — no match
    cid = _create_candidate(name, investor_id, discovered_url=source_url)
    return ResolveResult(node_id=None, action="candidate_new", candidate_id=cid)


def _create_candidate(
    name: str,
    investor_id: str | None = None,
    notes: str | None = None,
    discovered_url: str | None = None,
) -> str | None:
    norm = normalize(name)
    investor_arr = [investor_id] if investor_id else []
    url_arr = [discovered_url] if discovered_url else []
    rows = execute(
        """INSERT INTO candidates
               (name, normalized_name, discovered_via, suggested_investor, notes,
                discovered_urls, discovered_by_nodes, discovery_count)
           VALUES (%s, %s, 'pipeline', %s, %s, %s, %s::uuid[], 1)
           ON CONFLICT (normalized_name) WHERE status = 'pending'
             DO UPDATE SET
               discovered_urls     = array_append(candidates.discovered_urls,     %s),
               discovered_by_nodes = array_append(candidates.discovered_by_nodes, %s::uuid),
               discovery_count     = candidates.discovery_count + 1
           RETURNING id::text, (xmax = 0) AS inserted""",
        (name, norm, investor_id, notes, url_arr, investor_arr, discovered_url, investor_id),
    )
    if not rows:
        return None

    candidate_id, was_inserted = rows[0][0], rows[0][1]

    # Best-effort enrichment on brand-new candidates only (not the dedup-bump
    # path — no point re-fetching facts for a name we already tried).
    # Import lazily to avoid a resolve<->enrichment import cycle, and never
    # let an enrichment failure break candidate creation.
    if was_inserted:
        try:
            from . import enrichment

            facts = enrichment.enrich(name)
            if facts is not None:
                execute(
                    "UPDATE candidates SET facts = %s WHERE id = %s",
                    (psycopg2.extras.Json(facts), candidate_id),
                )
        except Exception:
            logger.exception("candidate enrichment failed for %r", name)

    return candidate_id
