"""Serializable contracts for document extraction evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ingestion_pipeline.domain.models import json_dumps


@dataclass(frozen=True)
class SourceLocator:
    """Location of extracted content in the original document."""

    page_number: int
    bbox: tuple[float, float, float, float] | None = None
    units: str = "points"
    block_ids: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "bbox": list(self.bbox) if self.bbox else None,
            "units": self.units,
            "block_ids": list(self.block_ids),
        }


@dataclass(frozen=True)
class ExtractedBlock:
    """One ordered text block produced by native parsing or OCR."""

    block_id: str
    page_number: int
    text: str
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    method: str = "native"
    units: str = "points"

    def as_json(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "page_number": self.page_number,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "confidence": self.confidence,
            "method": self.method,
            "units": self.units,
        }

    @property
    def locator(self) -> SourceLocator:
        return SourceLocator(
            page_number=self.page_number,
            bbox=self.bbox,
            units=self.units,
            block_ids=(self.block_id,),
        )


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[ExtractedBlock, ...] = ()
    method: str = "native"

    def as_json(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "method": self.method,
            "blocks": [block.as_json() for block in self.blocks],
        }


@dataclass(frozen=True)
class ExtractionResult:
    source_file: str
    media_type: str
    document_type: str
    engine: str
    pages: tuple[DocumentPage, ...]
    warnings: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def block_count(self) -> int:
        return sum(len(page.blocks) for page in self.pages)

    @property
    def text(self) -> str:
        return "\n".join(
            block.text for page in self.pages for block in page.blocks if block.text.strip()
        )

    def as_json(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "media_type": self.media_type,
            "document_type": self.document_type,
            "engine": self.engine,
            "page_count": len(self.pages),
            "block_count": self.block_count,
            "warnings": list(self.warnings),
            "created_at": self.created_at.isoformat(),
            "pages": [page.as_json() for page in self.pages],
        }

    def write_json(self, path: Path) -> None:
        path.write_text(json_dumps(self.as_json()) + "\n", encoding="utf-8")
