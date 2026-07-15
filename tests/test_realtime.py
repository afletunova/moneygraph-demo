"""
Unit tests for (forward safety net): RealtimeBackend must persist the
OpenAI response id for every chat completion, and must set store=True since
the Chat Completions API defaults store=False (unlike the Responses API).

NO real OpenAI calls — the client is mocked throughout.
"""

import os
from unittest.mock import MagicMock, patch

# RealtimeBackend() constructs a real openai.OpenAI() client at __init__ time,
# which raises if no API key is present in the environment. Tests never make
# a live call (the client is swapped for a MagicMock right after construction).
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")

import moneygraph.ingest.extraction.pipeline as pipeline
import moneygraph.ingest.extraction.realtime as realtime
from moneygraph.ingest.extraction.backend import ExtractionRequest


def _make_request(**overrides) -> ExtractionRequest:
    defaults = dict(
        custom_id="run-1:0000320193:0001",
        cik="0000320193",
        accession="0001",
        form_type="8-K",
        node_name="Acme Corp",
        text="Acme Corp invested $1,000,000 in Widget Inc.",
    )
    defaults.update(overrides)
    return ExtractionRequest(**defaults)


def _fake_response(response_id="resp_abc123", content='{"events": []}'):
    resp = MagicMock()
    resp.id = response_id
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    return resp


def test_call_sets_store_true():
    backend = realtime.RealtimeBackend()
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_response()
    backend._client = fake_client

    with patch.object(pipeline, "execute") as ex:
        backend._call(_make_request(), "gpt-4o-mini")

    _, kwargs = fake_client.chat.completions.create.call_args
    assert kwargs["store"] is True
    ex.assert_called_once()


def test_call_logs_response_id_with_context():
    backend = realtime.RealtimeBackend()
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_response(response_id="resp_xyz")
    backend._client = fake_client
    req = _make_request()

    with patch.object(pipeline, "execute") as ex:
        backend._call(req, "gpt-4o-mini")

    args, _ = ex.call_args
    sql, params = args
    assert "openai_response_log" in sql
    endpoint, model, response_id, context_json = params
    assert endpoint == "chat.completions"
    assert model == "gpt-4o-mini"
    assert response_id == "resp_xyz"
    assert req.custom_id in context_json
    assert req.node_name in context_json


def test_log_openai_response_noop_on_missing_id():
    # No response id (e.g. SDK/mocked object without .id) — must not raise or
    # attempt an insert; a warning is logged instead so the gap is visible.
    with patch.object(pipeline, "execute") as ex:
        pipeline._log_openai_response("chat.completions", "gpt-4o-mini", None, {"x": 1})
    ex.assert_not_called()


def test_log_openai_response_swallows_db_error():
    # Best-effort — a DB failure while logging must never break extraction.
    with patch.object(pipeline, "execute", side_effect=RuntimeError("db down")):
        pipeline._log_openai_response("chat.completions", "gpt-4o-mini", "resp_1", {"x": 1})
