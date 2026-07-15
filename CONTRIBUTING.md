# Contributing

This is primarily a portfolio piece — it's not actively seeking feature contributions, and the underlying pipeline logic is developed against a private version with much more real-world tuning behind it (see the README for why). That said, issues and PRs are welcome, especially for bugs, unclear docs, or genuine architecture questions.

## Running things locally

```bash
pip install -e ".[dev]"
pytest              # 17 test files, fully mocked at the DB/network boundary
ruff check .
ruff format --check .
```

Frontend:

```bash
cd frontend
npm install
npm run build
```

Full stack: `docker compose up --build` from the repo root (see the README quickstart).

Extraction eval (needs `OPENAI_API_KEY`, costs real API calls — not run in CI):

```bash
cd evals
python3 run_eval.py
```

## Before opening a PR

- Tests and `ruff check`/`ruff format --check` should pass — CI runs the same checks.
- Keep changes scoped; this repo favours small, readable diffs over broad refactors.
- If you're touching `core/`, remember it's meant to stay dependency-free of FastAPI — that boundary is deliberate, not an oversight.
