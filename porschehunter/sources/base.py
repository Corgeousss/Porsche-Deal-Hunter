"""Shared shape for every ingestion source."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IngestResult:
    source_key: str
    seen: int = 0
    new: int = 0
    updated: int = 0
    status: str = "ok"          # ok | error | skipped
    message: str = ""
    listing_ids: list[int] = field(default_factory=list)
