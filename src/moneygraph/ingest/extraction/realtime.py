import logging
import os

import openai

from .backend import ExtractionJob, ExtractionRequest, ExtractionResult
from .pipeline import _log_openai_response
from .prompt import _SYSTEM_PROMPT, build_user_content, parse_extraction_response

logger = logging.getLogger(__name__)

_PRIMARY = os.environ.get("OPENAI_PRIMARY_MODEL", "gpt-4o-mini")
_ESCALATION = os.environ.get("OPENAI_ESCALATION_MODEL", "gpt-4o")


class RealtimeBackend:
    def __init__(self) -> None:
        self._client = openai.OpenAI()

    def submit(self, requests: list[ExtractionRequest]) -> ExtractionJob:
        results = [self._extract_one(req) for req in requests]
        return ExtractionJob(mode="realtime", inline_results=results)

    def is_ready(self, job: ExtractionJob) -> bool:
        return True

    def harvest(self, job: ExtractionJob) -> list[ExtractionResult]:
        return job.inline_results

    # ------------------------------------------------------------------

    def _extract_one(self, req: ExtractionRequest) -> ExtractionResult:
        try:
            events = self._call(req, _PRIMARY)
            if events and any(e.get("confidence") == "low" for e in events):
                logger.info("escalating to %s — %s/%s", _ESCALATION, req.node_name, req.form_type)
                events = self._call(req, _ESCALATION)
            return ExtractionResult(custom_id=req.custom_id, events=events)
        except Exception as exc:
            logger.warning("realtime extraction failed for %s: %s", req.custom_id, exc)
            return ExtractionResult(custom_id=req.custom_id, events=[], error=str(exc))

    def _call(self, req: ExtractionRequest, model: str) -> list[dict]:
        resp = self._client.chat.completions.create(
            model=model,
            max_tokens=2048,
            response_format={"type": "json_object"},
            # Chat Completions API defaults store=False (unlike the Responses
            # API, which defaults to storing) — set explicitly so this
            # response is retrievable via GET /chat/completions/{id} for the
            # forward safety net.
            store=True,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_content(req.text, req.form_type, req.node_name),
                },
            ],
        )
        logger.info(
            "tokens input=%d output=%d  model=%-15s  %s/%s",
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            model,
            req.node_name,
            req.form_type,
        )
        _log_openai_response(
            "chat.completions",
            model,
            resp.id,
            {
                "custom_id": req.custom_id,
                "cik": req.cik,
                "accession": req.accession,
                "node_name": req.node_name,
                "form_type": req.form_type,
            },
        )
        raw = resp.choices[0].message.content or ""
        return parse_extraction_response(raw)
