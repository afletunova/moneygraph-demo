from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExtractionRequest:
    custom_id: str  # {run_id}:{cik}:{accession}
    cik: str
    accession: str
    form_type: str
    node_name: str
    text: str
    filing_date: str | None = None
    source_url: str | None = None


@dataclass
class ExtractionResult:
    custom_id: str
    events: list[dict]
    error: str | None = None


@dataclass
class ExtractionJob:
    mode: str  # 'realtime' | 'batch'
    batch_id: str | None = None
    inline_results: list[ExtractionResult] = field(default_factory=list)


class ExtractionBackend(Protocol):
    def submit(self, requests: list[ExtractionRequest]) -> ExtractionJob: ...
    def is_ready(self, job: ExtractionJob) -> bool: ...
    def harvest(self, job: ExtractionJob) -> list[ExtractionResult]: ...


def get_backend(mode: str) -> ExtractionBackend:
    if mode == "batch":
        from .batch import BatchBackend

        return BatchBackend()
    from .realtime import RealtimeBackend

    return RealtimeBackend()
