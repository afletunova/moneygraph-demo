"""
Minimal extraction eval.

Runs the extraction prompt (src/moneygraph/ingest/extraction/prompt.py) and the
real post-extraction gates (src/moneygraph/core/resolve.py: is_generic_entity /
name presence) against a small hand-labelled dataset of real SEC filing
excerpts and known compute/service-contract deals that must NOT be extracted
as investments.

Note: the prompt tested here is the simplified public prompt (see the
README's "How this was built" — the tuned production prompt stays private).
Expect these numbers to differ from — likely be lower than — whatever the
private version scores, since this prompt is deliberately stripped of the
tuned business-rule wording that would improve precision/recall.

No database required — this only exercises the pure extraction + gate logic,
not node resolution or DB writes.

Usage:
    cd evals && OPENAI_API_KEY=... python3 run_eval.py
    (or place OPENAI_API_KEY in ../.env — this script parses it manually,
    it does not `source` the file, since EDGAR_USER_AGENT and other values
    in that file can contain unquoted spaces that break shell sourcing)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")  # db.py reads this at import time; never connected

import openai

from moneygraph.core.resolve import is_generic_entity, normalize
from moneygraph.ingest.extraction.prompt import (
    _SYSTEM_PROMPT,
    WEB_SYSTEM_PROMPT,
    build_user_content,
    build_web_user_content,
    parse_extraction_response,
)

MODEL = os.environ.get("OPENAI_PRIMARY_MODEL", "gpt-4o-mini")
DATASET = Path(__file__).parent / "dataset.jsonl"


def load_env_file(path: Path) -> None:
    if not path.exists() or os.environ.get("OPENAI_API_KEY"):
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def call_model(client: openai.OpenAI, row: dict) -> list[dict]:
    if row["source_type"] == "web":
        system = WEB_SYSTEM_PROMPT
        user = build_web_user_content(row["text"], row.get("source_url", ""))
    else:
        system = _SYSTEM_PROMPT
        user = build_user_content(row["text"], row["form_type"], row["node_name"])
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return parse_extraction_response(resp.choices[0].message.content or "")


def apply_gates(raw_events: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Mirrors the early-exit gate order in pipeline.py::_process_event.

    Returns (events that survive to comparison, {gate_name: reject_count}).
    """
    gate_counts = {"missing_name": 0, "generic_entity": 0}
    survivors = []
    for ev in raw_events:
        investor = (ev.get("investor") or "").strip()
        investee = (ev.get("investee") or "").strip()
        if not investor or not investee:
            gate_counts["missing_name"] += 1
            continue
        if is_generic_entity(investor) or is_generic_entity(investee):
            gate_counts["generic_entity"] += 1
            continue
        survivors.append(ev)
    return survivors, gate_counts


def names_match(expected_name: str, actual_name: str) -> bool:
    a, b = normalize(expected_name), normalize(actual_name)
    return bool(a) and bool(b) and (a in b or b in a)


def amount_close(expected: int | None, actual, tolerance: float = 0.02) -> bool:
    if expected is None:
        return True
    try:
        actual = int(actual)
    except (TypeError, ValueError):
        return False
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / abs(expected) <= tolerance


def match_event(expected: dict, survivors: list[dict]) -> dict | None:
    for ev in survivors:
        if (
            names_match(expected["investor"], ev.get("investor", ""))
            and names_match(expected["investee"], ev.get("investee", ""))
            and amount_close(expected.get("amount_usd"), ev.get("amount_usd"))
        ):
            return ev
    return None


def main() -> None:
    load_env_file(Path(__file__).parent.parent / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set (env var or .env at repo root)")

    client = openai.OpenAI()
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]

    tp = fp = fn = 0
    gate_totals: dict[str, int] = {"missing_name": 0, "generic_entity": 0}
    results = []

    for row in rows:
        raw_events = call_model(client, row)
        survivors, gate_counts = apply_gates(raw_events)
        for k, v in gate_counts.items():
            gate_totals[k] += v

        expected = row["expected"]
        unmatched_survivors = list(survivors)
        row_tp = 0
        for exp in expected:
            hit = match_event(exp, unmatched_survivors)
            if hit:
                row_tp += 1
                unmatched_survivors.remove(hit)
            else:
                fn += 1
        row_fp = len(unmatched_survivors)
        tp += row_tp
        fp += row_fp

        status = "PASS" if row_tp == len(expected) and row_fp == 0 else "FAIL"
        results.append((row["id"], status, row_tp, len(expected), row_fp, gate_counts))
        print(
            f"[{status}] {row['id']:32s} matched={row_tp}/{len(expected)}  spurious={row_fp}  "
            f"gates_rejected={sum(gate_counts.values())}"
        )
        if status == "FAIL" and os.environ.get("EVAL_VERBOSE"):
            print(f"    expected: {expected}")
            print(f"    raw_model_events: {raw_events}")

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")

    print()
    print("=" * 60)
    print(f"model                {MODEL}")
    print(f"examples             {len(rows)}")
    print(f"true positives       {tp}")
    print(f"false positives      {fp}")
    print(f"false negatives      {fn}")
    print(f"precision            {precision:.2f}")
    print(f"recall               {recall:.2f}")
    print(f"gate: missing_name   {gate_totals['missing_name']} events rejected")
    print(f"gate: generic_entity {gate_totals['generic_entity']} events rejected")
    print("=" * 60)

    failed = [r for r in results if r[1] == "FAIL"]
    if failed:
        print(f"\n{len(failed)} failing case(s): {', '.join(r[0] for r in failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
