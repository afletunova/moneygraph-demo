# Extraction eval

A minimal precision/recall check on this repo's extraction prompt
(`src/moneygraph/ingest/extraction/prompt.py` — the simplified public version,
see the main README's "How this was built") and the real post-extraction
gates (`src/moneygraph/core/resolve.py::is_generic_entity`), run against 16
hand-labelled real source texts. No database required.

## Dataset

`dataset.jsonl` — 16 examples, ground truth picked from real SEC filings and
real deals encountered while building this project:

- **10 positive** — real SEC 8-K excerpts covering `ownership`, `subsidiary`,
  and a government-warrant edge case, spanning 9 different companies.
- **6 negative** — cases that must extract zero events: two real compute/supply
  deals (Nebius→Reflection AI, SpaceX→Reflection AI) that the prompt should
  decline because `supplier_customer` extraction isn't wired up yet (a known
  v2 gap — the schema supports the edge type, extraction doesn't offer it),
  a documented compute-contract example, a real spectrum-license purchase
  that reads like an equity deal but isn't, and two constructed IPO/underwriter
  cases testing the collective-noun gate.

Each row records the real source URL (or an explicit note that it's a
constructed test case) — see `dataset.jsonl` for provenance per example.

## Running it

```bash
cd evals
python3 run_eval.py                # uses OPENAI_API_KEY from repo-root .env
EVAL_VERBOSE=1 python3 run_eval.py  # also prints raw model output for failures
```

Exits non-zero if any example fails, so it's CI-able (not currently wired
into CI here, since it costs real API calls on every push).

## What it measures

1. Calls the system prompt + user-content builder for each example.
2. Runs the raw model output through the same two DB-free gates production
   applies before writing an edge (`missing investor/investee name`,
   `is_generic_entity` collective-noun rejection).
3. Matches surviving events against ground truth by normalised name + amount
   (2% tolerance) and computes precision/recall over the full set.

It does **not** exercise node resolution, dedup, or syndicate detection —
those require a live database and are covered by `tests/` instead.

## Results (`gpt-4o-mini`, simplified public prompt)

| metric | value |
|---|---|
| examples | 16 |
| true positives | 9 |
| false positives | 1 |
| false negatives | 1 |
| **precision** | **0.90** |
| **recall** | **0.90** |
| gate: missing_name rejected | 0 |
| gate: generic_entity rejected | 0 |

**The miss** (`pos-12-doc-intel-warrants`): a US Department of Commerce
warrant-and-disbursement agreement with Intel, real text pulled straight from
a real 8-K. The model didn't extract it — warrant/disbursement language reads
less like a stock purchase than the other examples, and the model doesn't
reliably generalise the "warrants = equity" pattern from this prompt alone.

**The spurious extraction** (`neg-06-atnt-echostar-spectrum`): a real
spectrum-license (asset) purchase that the model extracted as an ownership
edge. It isn't one — no equity changes hands. This is a genuine gap in this
prompt's negative examples, not a fluke; the private version has additional
tuned exclusion rules for asset-purchase-shaped deals that this simplified
prompt doesn't carry.

**Compared to the private version:** the private, tuned prompt scores higher
precision (1.00 vs. 0.90) on the same dataset — it correctly declines the
spectrum-license case that this simplified prompt gets wrong. Same recall
(0.90) either way; neither version generalises the warrant-as-equity pattern.
This gap is expected and is exactly what "the private version has months more
tuning" means in concrete, measured terms, rather than as a vague claim.
