import io
import json
import logging
import os
from pathlib import Path

import openai

from .backend import ExtractionJob, ExtractionRequest, ExtractionResult
from .prompt import _SYSTEM_PROMPT, build_user_content, parse_extraction_response

logger = logging.getLogger(__name__)

_PRIMARY = os.environ.get("OPENAI_PRIMARY_MODEL", "gpt-4o-mini")
_BATCH_DIR = Path("/app/data/batch")


class BatchBackend:
    def __init__(self) -> None:
        self._client = openai.OpenAI()

    def submit(self, requests: list[ExtractionRequest]) -> ExtractionJob:
        _BATCH_DIR.mkdir(parents=True, exist_ok=True)
        jsonl_bytes = self._build_jsonl(requests)
        file_obj = self._client.files.create(
            file=("batch.jsonl", io.BytesIO(jsonl_bytes), "application/jsonl"),
            purpose="batch",
        )
        batch = self._client.batches.create(
            input_file_id=file_obj.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        logger.info("batch submitted: %s (%d requests)", batch.id, len(requests))
        return ExtractionJob(mode="batch", batch_id=batch.id)

    def is_ready(self, job: ExtractionJob) -> bool:
        batch = self._client.batches.retrieve(job.batch_id)
        return batch.status in ("completed", "failed", "expired", "cancelled")

    def harvest(self, job: ExtractionJob) -> list[ExtractionResult]:
        batch = self._client.batches.retrieve(job.batch_id)
        if batch.status != "completed":
            logger.warning("batch %s status=%s — no output to harvest", job.batch_id, batch.status)
            return []
        if not batch.output_file_id:
            logger.warning("batch %s completed but has no output_file_id", job.batch_id)
            return []
        content = self._client.files.content(batch.output_file_id)
        return self._parse_output(content.text)

    # ------------------------------------------------------------------

    def _build_jsonl(self, requests: list[ExtractionRequest]) -> bytes:
        lines = []
        for req in requests:
            lines.append(
                json.dumps(
                    {
                        "custom_id": req.custom_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": {
                            "model": _PRIMARY,
                            "max_tokens": 2048,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": _SYSTEM_PROMPT},
                                {
                                    "role": "user",
                                    "content": build_user_content(req.text, req.form_type, req.node_name),
                                },
                            ],
                        },
                    }
                )
            )
        return "\n".join(lines).encode("utf-8")

    def _parse_output(self, text: str) -> list[ExtractionResult]:
        results = []
        for line in text.strip().split("\n"):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("malformed output JSONL line: %s", line[:200])
                continue
            custom_id = row.get("custom_id", "unknown")
            if row.get("error"):
                logger.warning("batch request %s failed: %s", custom_id, row["error"])
                results.append(ExtractionResult(custom_id=custom_id, events=[], error=str(row["error"])))
                continue
            try:
                raw = row["response"]["body"]["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                logger.warning("unexpected batch output shape for %s: %s", custom_id, exc)
                results.append(ExtractionResult(custom_id=custom_id, events=[], error=str(exc)))
                continue
            results.append(ExtractionResult(custom_id=custom_id, events=parse_extraction_response(raw)))
        return results
