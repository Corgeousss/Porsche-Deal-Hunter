"""Shared shape for every ingestion source."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IngestResult:
    source_key: str
    seen: int = 0
    new: int = 0
    updated: int = 0
    rejected: int = 0           # candidates confidently identified as non-911
    quarantined: int = 0        # model could not be confidently established
    status: str = "ok"          # ok | error | skipped
    message: str = ""
    listing_ids: list[int] = field(default_factory=list)
