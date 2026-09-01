"""Markdown intermediate representation for visual-document extraction.

The Markdown file is a reviewable, human-friendly projection of the OCR/native
extraction. It deliberately keeps the original locator in an HTML comment so
every parsed field can still be traced back to a page, block and bounding box.
The catalog parser consumes the same line objects used to render this file;
Markdown is therefore an actual intermediate stage, not only a debug export.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from ingestion_pipeline.documents.models import DocumentPage, ExtractionResult


@dataclass(frozen=True)
class MarkdownLine:
    """One normalized Markdown-visible line with source evidence."""

    text: str
    page_number: int
    bbox: tuple[float, float, float, float] | None
    confidence: float | None
    block_id: str
    method: str
    units: str


def repair_mojibake(value: str) -> str:
    """Repair common UTF-8-as-legacy-codepage OCR artifacts conservatively."""
    current = value
    for _ in range(3):
        before_score = _mojibake_score(current)
        if before_score == 0:
            break
        candidates: list[str] = []
        for codec in ("cp1252", "latin1"):
            try:
                candidates.append(current.encode(codec).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if not candidates:
            break
        candidate = min(candidates, key=_mojibake_score)
        if _mojibake_score(candidate) >= before_score:
            break
        current = candidate
    return current


def clean_markdown_text(value: str) -> str:
    """Normalize visible OCR spacing without removing source information."""
    repaired = repair_mojibake(value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", repaired).strip()


def page_markdown_lines(page: DocumentPage) -> list[MarkdownLine]:
    """Convert one extracted page into ordered Markdown-visible lines."""
    lines: list[MarkdownLine] = []
    for block in page.blocks:
        for raw_line in block.text.splitlines():
            text = clean_markdown_text(raw_line)
            if not text:
                continue
            lines.append(
                MarkdownLine(
                    text=text,
                    page_number=block.page_number,
                    bbox=block.bbox,
                    confidence=block.confidence,
                    block_id=block.block_id,
                    method=block.method,
                    units=block.units,
                )
            )
    return lines


def extraction_to_markdown(extraction: ExtractionResult) -> str:
    """Render the complete extraction as a reviewable Markdown document."""
    lines = [
        f"# Documento: {html.escape(extraction.source_file)}",
        "",
        f"- Tipo: `{html.escape(extraction.document_type)}`",
        f"- Motor: `{html.escape(extraction.engine)}`",
        f"- Paginas: {len(extraction.pages)}",
        "",
    ]
    for page in extraction.pages:
        lines.extend(
            [
                f"## Pagina {page.page_number}",
                "",
                f"_Dimensiones: {page.width:g} x {page.height:g}_",
                "",
            ]
        )
        for line in page_markdown_lines(page):
            metadata = json.dumps(
                {
                    "block_id": line.block_id,
                    "bbox": list(line.bbox) if line.bbox else None,
                    "confidence": line.confidence,
                    "method": line.method,
                    "units": line.units,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            lines.extend([f"<!-- {metadata} -->", f"- {line.text}"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _mojibake_score(value: str) -> int:
    markers = "\u00c3\u00c2\u00e2\u00f0\ufffd"
    controls = sum(1 for char in value if 0x80 <= ord(char) < 0xA0)
    return sum(value.count(marker) for marker in markers) + controls
