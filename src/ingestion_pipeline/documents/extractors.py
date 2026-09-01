"""Local PDF/image extraction with native text and Tesseract OCR fallbacks."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ingestion_pipeline.documents.models import DocumentPage, ExtractedBlock, ExtractionResult


class DocumentExtractionError(RuntimeError):
    """Raised when a document cannot be inspected with the configured local tools."""


@dataclass(frozen=True)
class ExtractionSettings:
    ocr_language: str = "spa+eng"
    dpi: int = 220
    max_pages: int = 100
    tesseract_config: str = "--psm 11"
    min_native_chars: int = 10


class OcrEngine(Protocol):
    def extract(
        self,
        image: Any,
        *,
        page_number: int,
        units: str,
        coordinate_scale: float = 1.0,
    ) -> tuple[ExtractedBlock, ...]: ...


def _require_pillow() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on local installation
        raise DocumentExtractionError(
            "Falta Pillow. Ejecuta `uv sync` para instalar las dependencias documentales."
        ) from exc
    return Image


class TesseractOcr:
    def __init__(self, settings: ExtractionSettings) -> None:
        self.settings = settings

    def extract(
        self,
        image: Any,
        *,
        page_number: int,
        units: str,
        coordinate_scale: float = 1.0,
    ) -> tuple[ExtractedBlock, ...]:
        try:
            import pytesseract
            from pytesseract import Output
        except ImportError as exc:  # pragma: no cover - depends on local installation
            raise DocumentExtractionError(
                "Falta pytesseract. Ejecuta `uv sync` para instalar OCR local."
            ) from exc
        try:
            data = pytesseract.image_to_data(
                image,
                lang=self.settings.ocr_language,
                config=self.settings.tesseract_config,
                output_type=Output.DICT,
            )
        except Exception as exc:  # pytesseract exposes platform-specific errors
            message = str(exc)
            if "tesseract" in message.lower() or isinstance(exc, FileNotFoundError):
                raise DocumentExtractionError(
                    "No se encontró Tesseract. Instálalo junto con el idioma `spa` y "
                    "asegúrate de que esté en PATH."
                ) from exc
            raise DocumentExtractionError(f"Tesseract no pudo procesar la imagen: {exc}") from exc

        grouped: dict[tuple[int, int, int], list[int]] = {}
        for index, text in enumerate(data.get("text", [])):
            if not str(text).strip():
                continue
            key = (
                int(data.get("block_num", [0])[index]),
                int(data.get("par_num", [0])[index]),
                int(data.get("line_num", [0])[index]),
            )
            grouped.setdefault(key, []).append(index)

        blocks: list[ExtractedBlock] = []
        for block_index, indexes in enumerate(grouped.values(), start=1):
            texts = [str(data["text"][index]).strip() for index in indexes]
            left = min(int(data["left"][index]) for index in indexes)
            top = min(int(data["top"][index]) for index in indexes)
            right = max(int(data["left"][index]) + int(data["width"][index]) for index in indexes)
            bottom = max(int(data["top"][index]) + int(data["height"][index]) for index in indexes)
            confidences = [
                float(data["conf"][index]) for index in indexes if float(data["conf"][index]) >= 0
            ]
            confidence = sum(confidences) / len(confidences) / 100 if confidences else None
            bbox = tuple(round(value * coordinate_scale, 3) for value in (left, top, right, bottom))
            blocks.append(
                ExtractedBlock(
                    block_id=f"p{page_number:04d}-ocr{block_index:04d}",
                    page_number=page_number,
                    text=" ".join(texts),
                    bbox=bbox,
                    confidence=confidence,
                    method="ocr",
                    units=units,
                )
            )
        return tuple(blocks)


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    raise DocumentExtractionError(
        "Formato documental no soportado. Usa .pdf, .png, .jpg, .jpeg, .webp o .tiff."
    )


def _native_pdf_blocks(page: Any, page_number: int) -> tuple[ExtractedBlock, ...]:
    blocks: list[ExtractedBlock] = []
    for index, raw in enumerate(page.get_text("blocks"), start=1):
        if len(raw) < 5 or not str(raw[4]).strip():
            continue
        bbox = tuple(round(float(value), 3) for value in raw[:4])
        blocks.append(
            ExtractedBlock(
                block_id=f"p{page_number:04d}-native{index:04d}",
                page_number=page_number,
                text=str(raw[4]).strip(),
                bbox=bbox,
                confidence=1.0,
                method="native",
                units="points",
            )
        )
    return tuple(blocks)


def _extract_pdf(path: Path, settings: ExtractionSettings) -> ExtractionResult:
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - depends on local installation
        raise DocumentExtractionError(
            "Falta PyMuPDF. Ejecuta `uv sync` para instalar el extractor PDF local."
        ) from exc

    ocr = TesseractOcr(settings)
    pages: list[DocumentPage] = []
    native_pages = 0
    with pymupdf.open(path) as document:
        if document.page_count == 0:
            raise DocumentExtractionError("El PDF no contiene páginas.")
        if document.page_count > settings.max_pages:
            raise DocumentExtractionError(
                f"El PDF tiene {document.page_count} páginas; el límite V1 es {settings.max_pages}."
            )
        for index in range(document.page_count):
            page_number = index + 1
            page = document.load_page(index)
            native_blocks = _native_pdf_blocks(page, page_number)
            native_chars = sum(len(block.text) for block in native_blocks)
            if native_chars >= settings.min_native_chars:
                blocks = native_blocks
                method = "native"
                native_pages += 1
            else:
                scale = settings.dpi / 72
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                Image = _require_pillow()
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                blocks = ocr.extract(
                    image,
                    page_number=page_number,
                    units="points",
                    coordinate_scale=1 / scale,
                )
                method = "ocr"
            pages.append(
                DocumentPage(
                    page_number=page_number,
                    width=round(float(page.rect.width), 3),
                    height=round(float(page.rect.height), 3),
                    blocks=blocks,
                    method=method,
                )
            )
    document_type = "text_based" if native_pages == len(pages) else "scanned"
    if native_pages and native_pages != len(pages):
        document_type = "mixed"
    return ExtractionResult(
        source_file=path.name,
        media_type="application/pdf",
        document_type=document_type,
        engine="pymupdf+tesseract",
        pages=tuple(pages),
    )


def _extract_image(path: Path, settings: ExtractionSettings) -> ExtractionResult:
    Image = _require_pillow()
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:
        raise DocumentExtractionError(f"No se pudo leer la imagen {path}: {exc}") from exc
    if image.width == 0 or image.height == 0:
        raise DocumentExtractionError("La imagen no tiene dimensiones válidas.")
    blocks = TesseractOcr(settings).extract(
        image,
        page_number=1,
        units="pixels",
    )
    page = DocumentPage(
        page_number=1,
        width=float(image.width),
        height=float(image.height),
        blocks=blocks,
        method="ocr",
    )
    return ExtractionResult(
        source_file=path.name,
        media_type=_media_type(path),
        document_type="image_based",
        engine="tesseract",
        pages=(page,),
    )


def extract_document(path: Path, settings: ExtractionSettings | None = None) -> ExtractionResult:
    """Extract text/layout evidence from a local PDF or raster image."""
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Archivo documental no encontrado: {path}")
    settings = settings or ExtractionSettings()
    if settings.max_pages < 1:
        raise ValueError("`max_pages` debe ser mayor que cero.")
    media_type = _media_type(path)
    if media_type == "application/pdf":
        return _extract_pdf(path, settings)
    return _extract_image(path, settings)
