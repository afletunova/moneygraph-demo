"""
Unit tests for entity resolution (app/resolve.py).

DB calls are mocked — these test resolution logic, not SQL correctness.
Each resolve() call triggers up to 4 query() calls (passes 1-3 + all_aliases)
and optionally 1 execute() call (alias registration or candidate creation).
"""

from unittest.mock import patch

import pytest

from moneygraph.core.resolve import is_generic_entity, normalize, resolve


@pytest.fixture(autouse=True)
def _no_real_enrichment():
    """
    _create_candidate() best-effort calls moneygraph.core.enrichment.enrich() on brand-new
    candidates. Stub it out here so resolve tests stay DB/network-free;
    enrichment itself is covered by test_enrichment.py.
    """
    with patch("moneygraph.core.enrichment.enrich", return_value=None):
        yield


# ---------------------------------------------------------------------------
# Seed aliases used across fuzzy / no-match test fixtures.
# Each entry mirrors what the DB would return from the all_aliases query.
# ---------------------------------------------------------------------------
_SEED_ALIASES = [
    {"node_id": "uid-nvidia", "normalized_alias": "nvidia", "node_name": "NVIDIA Corporation"},
    {"node_id": "uid-nvidia", "normalized_alias": "nvda", "node_name": "NVIDIA Corporation"},
    {"node_id": "uid-msft", "normalized_alias": "microsoft", "node_name": "Microsoft Corporation"},
    {"node_id": "uid-msft", "normalized_alias": "msft", "node_name": "Microsoft Corporation"},
    {"node_id": "uid-alphabet", "normalized_alias": "alphabet", "node_name": "Alphabet Inc."},
    {"node_id": "uid-alphabet", "normalized_alias": "googl", "node_name": "Alphabet Inc."},
    {"node_id": "uid-amazon", "normalized_alias": "amazon.com", "node_name": "Amazon.com Inc."},
    {"node_id": "uid-amazon", "normalized_alias": "amzn", "node_name": "Amazon.com Inc."},
    {
        "node_id": "uid-meta",
        "normalized_alias": "meta platforms",
        "node_name": "Meta Platforms Inc.",
    },
    {"node_id": "uid-meta", "normalized_alias": "meta", "node_name": "Meta Platforms Inc."},
    {"node_id": "uid-apple", "normalized_alias": "apple", "node_name": "Apple Inc."},
    {"node_id": "uid-apple", "normalized_alias": "aapl", "node_name": "Apple Inc."},
    {"node_id": "uid-oracle", "normalized_alias": "oracle", "node_name": "Oracle Corporation"},
    {"node_id": "uid-arm", "normalized_alias": "arm", "node_name": "ARM Holdings plc"},
    {"node_id": "uid-nebius", "normalized_alias": "nebius", "node_name": "Nebius Group N.V."},
    {"node_id": "uid-intel", "normalized_alias": "intel", "node_name": "Intel Corporation"},
    {"node_id": "uid-openai", "normalized_alias": "openai", "node_name": "OpenAI"},
    {"node_id": "uid-anthropic", "normalized_alias": "anthropic", "node_name": "Anthropic"},
]


# ---------------------------------------------------------------------------
# normalize() — pure function, no mocking required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Legal-suffix stripping
        ("NVIDIA Corporation", "nvidia"),
        ("Microsoft Corporation", "microsoft"),
        ("Microsoft Corp.", "microsoft"),
        ("Alphabet Inc.", "alphabet"),
        ("Alphabet Inc", "alphabet"),
        ("Apple Inc.", "apple"),
        ("Oracle Corporation", "oracle"),
        ("Intel Corporation", "intel"),
        ("CoreWeave Inc.", "coreweave"),
        ("Applied Digital Corp.", "applied digital"),
        ("SoftBank Group Corp.", "softbank"),
        # Multi-suffix stripping (iterative)
        ("ARM Holdings plc", "arm"),
        ("arm holdings", "arm"),
        ("Nebius Group N.V.", "nebius"),
        # Ticker / no suffix
        ("MSFT", "msft"),
        ("GOOGL", "googl"),
        ("ARM", "arm"),
        # Name variants from filing text
        ("msft inc", "msft"),
        ("Amazon.com Inc.", "amazon.com"),
        ("Meta Platforms Inc.", "meta platforms"),
        # PBC (Public Benefit Corporation) suffix
        ("OpenAI Group PBC", "openai"),
        # Trailing punctuation
        ("OpenAI,", "openai"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


# ---------------------------------------------------------------------------
# is_generic_entity() — collective-noun gate, pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # Observed in the wild (SpaceX IPO run, 2026-06-19)
        "Various Underwriters",
        "Public Market Investors",
        "Public Investors",
        # Variants the same failure mode would produce
        "Institutional Investors",
        "Certain Existing Shareholders",
        "The Underwriters",
        "Undisclosed Investors",
        "Retail Investors",
        "Underwriting Syndicate",
        "Multiple Investors",
        "Public investors through initial public offering",
        # Nationality-qualified collectives (leaked live, RSS run 2026-07-08)
        "unnamed Chinese investors",
        "Foreign Institutional Investors",
        "Saudi sovereign investors",
        # Placeholders (observed 2026-06-01 run)
        "Not specified",
        "N/A",
        "unknown",
        "",
        # Affiliate / fund-pool fragments (, triage 2026-07-09)
        "affiliate of Silver Lake",
        "an affiliate of Silver Lake",
        "Silver Lake affiliate",
        "Silver Lake affiliates",
        "Apollo-managed funds and affiliates",
        "Blackstone affiliates and funds",
        "certain affiliates of Blackstone",
        "KKR-managed funds",
    ],
)
def test_is_generic_entity_rejects(name):
    assert is_generic_entity(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # Real organizations whose head token overlaps generic vocabulary
        "Fidelity Investors",  # specific token "fidelity" saves it
        "Deutsche Bank",
        "Silicon Valley Bank",
        # Ordinary company names
        "Goldman Sachs",
        "Morgan Stanley",
        "OpenAI Group PBC",
        "SpaceX",
        "Silver Lake Partners",
        # Real entities that merely contain fragment-ish tokens — must NOT reject
        "Affiliated Managers Group",  # normalizes to "affiliated managers"
        "SoftBank Vision Fund",  # a named fund vehicle, not a pool fragment
        "Dragoneer Investment Group",
    ],
)
def test_is_generic_entity_passes(name):
    assert is_generic_entity(name) is False


# ---------------------------------------------------------------------------
# resolve() helpers
# ---------------------------------------------------------------------------


def _q(*return_values):
    """Build a side_effect list for sequential query() calls."""
    values = list(return_values)

    def side_effect(sql, params=None):
        return values.pop(0)

    return side_effect


# ---------------------------------------------------------------------------
# Pass 1 — exact nodes.name match
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_exact_node_name(mock_q, mock_ex):
    mock_q.side_effect = _q([{"id": "uid-openai"}])
    result = resolve("OpenAI")
    assert result.node_id == "uid-openai"
    assert result.action == "linked"
    assert mock_q.call_count == 1


# ---------------------------------------------------------------------------
# Pass 2 — exact alias match
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_msft_ticker_alias(mock_q, mock_ex):
    mock_q.side_effect = _q(
        [],  # Pass 1
        [{"node_id": "uid-msft"}],  # Pass 2
    )
    result = resolve("MSFT")
    assert result.node_id == "uid-msft"
    assert result.action == "linked"
    assert mock_q.call_count == 2


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_googl_ticker_alias(mock_q, mock_ex):
    mock_q.side_effect = _q(
        [],
        [{"node_id": "uid-alphabet"}],
    )
    result = resolve("GOOGL")
    assert result.node_id == "uid-alphabet"
    assert result.action == "linked"


# ---------------------------------------------------------------------------
# Pass 3 — normalized alias match
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_microsoft_corp_normalized(mock_q, mock_ex):
    # normalize("Microsoft Corp.") = "microsoft" → matches alias "microsoft"
    mock_q.side_effect = _q(
        [],  # Pass 1: no exact node name
        [],  # Pass 2: no exact alias
        [{"node_id": "uid-msft"}],  # Pass 3: normalized match
    )
    result = resolve("Microsoft Corp.")
    assert result.node_id == "uid-msft"
    assert result.action == "linked"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_msft_inc_normalized(mock_q, mock_ex):
    # normalize("msft inc") = "msft" → matches alias "msft"
    mock_q.side_effect = _q([], [], [{"node_id": "uid-msft"}])
    result = resolve("msft inc")
    assert result.node_id == "uid-msft"
    assert result.action == "linked"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_alphabet_inc_normalized(mock_q, mock_ex):
    mock_q.side_effect = _q([], [], [{"node_id": "uid-alphabet"}])
    result = resolve("Alphabet Inc")
    assert result.node_id == "uid-alphabet"
    assert result.action == "linked"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_arm_ticker_normalized(mock_q, mock_ex):
    # normalize("ARM") = "arm" → matches normalized alias "arm"
    mock_q.side_effect = _q([], [], [{"node_id": "uid-arm"}])
    result = resolve("ARM")
    assert result.node_id == "uid-arm"
    assert result.action == "linked"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_arm_holdings_normalized(mock_q, mock_ex):
    mock_q.side_effect = _q([], [], [{"node_id": "uid-arm"}])
    result = resolve("arm holdings")
    assert result.node_id == "uid-arm"
    assert result.action == "linked"


# ---------------------------------------------------------------------------
# Pass 3.5 — legal-shell / subsidiary-wrapper fold
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_amazon_shell_folded(mock_q, mock_ex):
    # normalize("Amazon.com NV Investment Holdings LLC") strips legal suffixes
    # (llc, holdings) down to "amazon.com nv investment"; shell-qualifier
    # stripping then removes "nv investment" -> "amazon.com", which matches
    # the existing alias for Amazon.com Inc.
    mock_q.side_effect = _q(
        [],  # Pass 1
        [],  # Pass 2
        [],  # Pass 3
        [{"node_id": "uid-amazon", "node_name": "Amazon.com Inc."}],  # Pass 3.5
    )
    mock_ex.return_value = [("uid-new-alias",)]  # INSERT alias RETURNING id
    result = resolve("Amazon.com NV Investment Holdings LLC")
    assert result.node_id == "uid-amazon"
    assert result.action == "shell_folded"
    mock_ex.assert_called_once()


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_att_mobility_shell_folded(mock_q, mock_ex):
    # normalize("AT&T Mobility II LLC") -> "at&t mobility ii"; shell-qualifier
    # stripping removes "mobility ii" -> "at&t", matching the AT&T Inc. alias.
    mock_q.side_effect = _q(
        [],
        [],
        [],
        [{"node_id": "uid-att", "node_name": "AT&T Inc."}],
    )
    mock_ex.return_value = [("uid-new-alias",)]
    result = resolve("AT&T Mobility II LLC")
    assert result.node_id == "uid-att"
    assert result.action == "shell_folded"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_shell_conflict_falls_through_to_fuzzy(mock_q, mock_ex):
    # Shell match found, but normalized_alias INSERT conflicts (already claims
    # a different node) — must fall through to Pass 4/5, not silently drop.
    mock_q.side_effect = _q(
        [],
        [],
        [],
        [{"node_id": "uid-amazon", "node_name": "Amazon.com Inc."}],  # Pass 3.5 match
        _SEED_ALIASES,  # Pass 4 all_aliases
    )
    mock_ex.side_effect = [
        [],  # shell alias INSERT → conflict
        [("uid-candidate", True)],  # candidate INSERT RETURNING id
    ]
    result = resolve("Amazon.com NV Investment Holdings LLC")
    assert result.node_id is None
    assert result.action in ("candidate_review", "candidate_new")


@pytest.mark.parametrize(
    "name",
    [
        "Waymo LLC",  # real subsidiary, own investors — must NOT fold
        "Altera Corporation",
        "xAI",
        "Wing Aviation LLC",
        "Intel Capital",  # distinct investing arm, not a filing shell
    ],
)
def test_find_shell_parent_ignores_real_entities(name):
    # No shell-qualifier suffix ("investment", "mobility ii", "finance", ...)
    # on these names, so find_shell_parent must short-circuit to None without
    # even issuing a query — these keep their own node identity.
    from moneygraph.core.resolve import find_shell_parent

    with patch("moneygraph.core.resolve.query") as mock_q:
        assert find_shell_parent(name) is None
        mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# Pass 4 — fuzzy match, distance ≤ 2 → auto-register alias
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_google_fuzzy_linked(mock_q, mock_ex):
    # normalize("Google") = "google"; Levenshtein("google", "googl") = 1 → auto-register
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.return_value = [("uid-new-alias",)]  # INSERT alias RETURNING id
    result = resolve("Google")
    assert result.node_id == "uid-alphabet"
    assert result.action == "fuzzy_linked"
    mock_ex.assert_called_once()


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_fuzzy_conflict_falls_through_to_candidate(mock_q, mock_ex):
    # Alias conflict: ON CONFLICT DO NOTHING returns no rows → candidate
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.side_effect = [
        [],  # INSERT alias → conflict, no RETURNING rows
        [("uid-candidate", True)],  # INSERT candidate RETURNING id
    ]
    result = resolve("Google")
    assert result.node_id is None
    assert result.action in ("candidate_review", "candidate_new")
    assert result.candidate_id == "uid-candidate"


# ---------------------------------------------------------------------------
# Pass 4 — fuzzy, distance 3–5 → candidate_review
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_fuzzy_candidate_review(mock_q, mock_ex):
    # normalize("Appliance Inc.") → "appliance"
    # Levenshtein("appliance", "apple") = 4 → in the 3-5 range → candidate_review
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.return_value = [("uid-candidate", True)]
    result = resolve("Appliance Inc.")
    assert result.node_id is None
    assert result.action == "candidate_review"
    assert result.candidate_id == "uid-candidate"
    assert result.suggested_node_name == "Apple Inc."


# ---------------------------------------------------------------------------
# Pass 5 — no match → candidate_new
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_unknown_entity(mock_q, mock_ex):
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.return_value = [("uid-candidate", True)]
    result = resolve("Zybertronics LLC")
    assert result.node_id is None
    assert result.action == "candidate_new"
    assert result.candidate_id == "uid-candidate"


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolve_unknown_entity_with_investor_id(mock_q, mock_ex):
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.return_value = [("uid-candidate", True)]
    result = resolve("Zybertronics LLC", investor_id="uid-nvidia")
    assert result.action == "candidate_new"
    # Confirm investor_id was passed through to _create_candidate
    call_args = mock_ex.call_args
    assert "uid-nvidia" in call_args[0][1]


# ---------------------------------------------------------------------------
# resolved property
# ---------------------------------------------------------------------------


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolved_property_true_when_linked(mock_q, mock_ex):
    mock_q.side_effect = _q([{"id": "uid-nvidia"}])
    result = resolve("NVIDIA Corporation")
    assert result.resolved is True


@patch("moneygraph.core.resolve.execute")
@patch("moneygraph.core.resolve.query")
def test_resolved_property_false_when_candidate(mock_q, mock_ex):
    mock_q.side_effect = _q([], [], [], _SEED_ALIASES)
    mock_ex.return_value = [("uid-candidate", True)]
    result = resolve("Zybertronics LLC")
    assert result.resolved is False
