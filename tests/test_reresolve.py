"""
Unit tests for the re-resolve sweep classification logic.

Pure: no DB, no network, no OpenAI. Exercises moneygraph.core.reresolve's classify() with a
fake resolver + fake edge_exists.
"""

import os

import moneygraph.core.reresolve as rr


class _FakeResolver:
    """map: name -> (node_id, node_name, via)."""

    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, name):
        return self.mapping.get(name, (None, None, None))


def test_recoverable_new_edge():
    # both sides resolve to distinct nodes, no existing edge → NEW recoverable.
    resolver = _FakeResolver(
        {
            "Nvidia": ("n1", "NVIDIA", "exact"),
            "Intel Corp": ("n2", "Intel", "fuzzy"),
        }
    )
    rows = [{"inv": "Nvidia", "vee": "Intel Corp", "amount": 5_000_000_000}]
    out = rr.classify(rows, resolver, edge_exists=lambda f, t: False)
    assert out["both_resolve_rows"] == 1
    assert out["new_edges"] == 1
    assert out["existing_edges"] == 0
    assert out["unresolved_sides"] == 0
    assert out["samples"][0][0] == 5_000_000_000


def test_both_resolve_but_edge_exists():
    resolver = _FakeResolver(
        {
            "Nvidia": ("n1", "NVIDIA", "exact"),
            "Intel Corp": ("n2", "Intel", "fuzzy"),
        }
    )
    rows = [{"inv": "Nvidia", "vee": "Intel Corp", "amount": 1}]
    out = rr.classify(rows, resolver, edge_exists=lambda f, t: (f, t) == ("n1", "n2"))
    assert out["both_resolve_rows"] == 1
    assert out["existing_edges"] == 1
    assert out["new_edges"] == 0
    assert out["samples"] == []  # existing edges are not offered as recoverable


def test_alias_resolved_no_edge_is_recoverable():
    # Both sides resolve via non-fuzzy (e.g. link alias), but no edge yet
    # → still recoverable (keys on edge existence, not pass).
    resolver = _FakeResolver(
        {
            "Nvidia": ("n1", "NVIDIA", "exact"),
            "Intel": ("n2", "Intel", "norm"),
        }
    )
    rows = [{"inv": "Nvidia", "vee": "Intel", "amount": 1}]
    out = rr.classify(rows, resolver, edge_exists=lambda f, t: False)
    assert out["new_edges"] == 1
    assert out["unresolved_sides"] == 0


def test_unresolved_side_counted():
    # investee matches nothing → not recoverable, one unresolved side
    resolver = _FakeResolver(
        {
            "Nvidia": ("n1", "NVIDIA", "exact"),
        }
    )
    rows = [{"inv": "Nvidia", "vee": "SomeBrandNewCo", "amount": 1}]
    out = rr.classify(rows, resolver, edge_exists=lambda f, t: False)
    assert out["both_resolve_rows"] == 0
    assert out["unresolved_sides"] == 1
    assert out["new_edges"] == 0


def test_self_edge_excluded():
    # both sides resolve to the SAME node → not a real directed pair
    resolver = _FakeResolver(
        {
            "OpenAI": ("n1", "OpenAI", "exact"),
            "OpenAI, L.L.C.": ("n1", "OpenAI", "fuzzy"),
        }
    )
    rows = [{"inv": "OpenAI", "vee": "OpenAI, L.L.C.", "amount": 1}]
    out = rr.classify(rows, resolver, edge_exists=lambda f, t: False)
    assert out["both_resolve_rows"] == 0
    assert out["new_edges"] == 0


def test_exclusion_spacex_anysphere_and_self_loop():
    spacex = {
        "inv": "Space Exploration Technologies Corp.",
        "vee": "Anysphere, Inc.",
        "inv_id": "s1",
        "inv_name": "SpaceX",
        "vee_id": "a1",
        "vee_name": "Anysphere, Inc.",
        "amount": 60_000_000_000,
        "via": "exact/alias",
    }
    selfloop = {
        "inv": "X",
        "vee": "X",
        "inv_id": "n1",
        "inv_name": "X",
        "vee_id": "n1",
        "vee_name": "X",
        "amount": 1,
        "via": "exact/alias",
    }
    ok = {
        "inv": "MGX",
        "vee": "xAI",
        "inv_id": "m1",
        "inv_name": "MGX",
        "vee_id": "x1",
        "vee_name": "xAI",
        "amount": 20_000_000_000,
        "via": "exact/alias",
    }

    assert "mis-extraction" in rr.exclusion_reason(spacex)
    assert "self-loop" in rr.exclusion_reason(selfloop)
    assert rr.exclusion_reason(ok) is None

    keep, excluded = rr.split_recoverable([spacex, ok, selfloop])
    assert keep == [ok]
    assert {r[0]["inv_name"] for r in excluded} == {"SpaceX", "X"}


def test_materialize_suppresses_news_feed_write():
    # the sweep re-processes existing news_feed rows → must NOT mint new ones
    from unittest.mock import patch

    import moneygraph.ingest.extraction.pipeline as pipeline

    rec = {
        "inv": "MGX",
        "vee": "xAI",
        "amount": 20_000_000_000,
        "row": {"url": "http://x", "source_tier": 3, "source_name": "news", "published_at": None},
    }
    with patch.object(pipeline, "_process_event", return_value=(True, False, True)) as pe:
        rr._materialize(rec, "run-1")
    _, kwargs = pe.call_args
    assert kwargs.get("write_news_feed") is False


def test_run_reresolve_sweep_dry_run_writes_nothing():
    # apply=False must never touch pipeline_runs or call execute() at all.
    from unittest.mock import patch

    # query() is called 4x: nodes, node_aliases, edges, news_feed.
    with (
        patch("moneygraph.core.reresolve.query", side_effect=[[], [], [], []]),
        patch("moneygraph.core.reresolve.execute") as ex,
    ):
        result = rr.run_reresolve_sweep(apply=False)
    ex.assert_not_called()
    assert result["recoverable"] == 0
    assert "run_id" not in result


def test_no_openai_import_in_reresolve_module():
    # the sweep must never touch a model — assert no real import / attribute use
    # (docstring mentions of the word are fine; we check executable lines only)
    import re

    core_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "moneygraph", "core")
    for fname in ("reresolve.py",):
        src = open(os.path.join(core_dir, fname), encoding="utf-8").read()
        for line in src.splitlines():
            assert not re.match(r"\s*(import openai|from openai)", line), line
            assert "openai." not in line, line
