"""Local PDF/image extraction with native text and Tesseract OCR fallbacks."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ingestion_pipeline.documents.models import DocumentPage, ExtractedBlock, ExtractionResult


class DocumentExtractionError(RuntimeError):
    """Raised when a document cannot be inspected with the configured local tools."""


@dataclass(frozen=True)
class ExtractionSettings:
    ocr_language: str = "spa+eng"
    ocr_engine: str = "tesseract"
    ocr_runtime: str = "cpu"
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
    engine_name = "tesseract"

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


def _to_python(value: Any) -> Any:
    """Convert numpy-like values returned by PaddleOCR to Python values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _result_payload(value: Any) -> Any:
    payload = value
    if hasattr(payload, "json"):
        payload = payload.json
        if callable(payload):
            payload = payload()
    payload = _to_python(payload)
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


def _box_from_paddle(raw_box: Any) -> tuple[float, float, float, float] | None:
    raw_box = _to_python(raw_box)
    if not isinstance(raw_box, (list, tuple)):
        return None
    if len(raw_box) == 4 and all(isinstance(item, (int, float)) for item in raw_box):
        x0, y0, x1, y1 = (float(item) for item in raw_box)
        return x0, y0, x1, y1
    points: list[tuple[float, float]] = []
    for point in raw_box:
        point = _to_python(point)
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                points.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _paddle_items(
    payload: Any,
) -> list[tuple[str, tuple[float, float, float, float], float | None]]:
    """Read PaddleOCR 3.x JSON and the legacy ``ocr`` list response."""
    payload = _to_python(payload)
    if isinstance(payload, dict):
        nested = payload.get("res")
        if isinstance(nested, dict):
            payload = nested
        texts = _to_python(payload.get("rec_texts"))
        scores = _to_python(payload.get("rec_scores", []))
        boxes = payload.get("rec_boxes")
        if boxes is None:
            boxes = payload.get("dt_polys")
        if boxes is None:
            boxes = payload.get("rec_polys")
        boxes = _to_python(boxes)
        if isinstance(texts, list) and isinstance(boxes, list):
            items: list[tuple[str, tuple[float, float, float, float], float | None]] = []
            for index, text in enumerate(texts):
                if not str(text).strip() or index >= len(boxes):
                    continue
                bbox = _box_from_paddle(boxes[index])
                if bbox is None:
                    continue
                score = scores[index] if isinstance(scores, list) and index < len(scores) else None
                try:
                    confidence = float(score) if score is not None else None
                    if confidence is not None and confidence > 1:
                        confidence /= 100
                except (TypeError, ValueError):
                    confidence = None
                items.append((str(text).strip(), bbox, confidence))
            return items
        return []
    if not isinstance(payload, list):
        return []
    items = []
    for entry in payload:
        entry = _to_python(entry)
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        bbox = _box_from_paddle(entry[0])
        recognition = _to_python(entry[1])
        if bbox is None or not isinstance(recognition, (list, tuple)) or not recognition:
            continue
        text = str(recognition[0]).strip()
        if not text:
            continue
        try:
            confidence = float(recognition[1]) if len(recognition) > 1 else None
        except (TypeError, ValueError):
            confidence = None
        items.append((text, bbox, confidence))
    return items


class PaddleOcr:
    """Lazy PaddleOCR adapter with a stable extraction contract."""

    engine_name = "paddleocr"

    def __init__(self, settings: ExtractionSettings) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise DocumentExtractionError(
                "Falta PaddleOCR. Usa la imagen `Dockerfile.ocr` o instala el extra OCR."
            ) from exc
        language = (
            "es" if settings.ocr_language.casefold().startswith("spa") else settings.ocr_language
        )
        device = (
            "cpu"
            if settings.ocr_runtime == "cpu"
            else ("gpu:0" if settings.ocr_runtime == "gpu" else settings.ocr_runtime)
        )
        try:
            self._runner = PaddleOCR(
                lang=language,
                device=device,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except TypeError:  # PaddleOCR 2.x compatibility for local installations
            try:
                self._runner = PaddleOCR(
                    lang=language,
                    use_angle_cls=True,
                    use_gpu=device != "cpu",
                )
            except TypeError:
                self._runner = PaddleOCR(lang=language)

    def extract(
        self,
        image: Any,
        *,
        page_number: int,
        units: str,
        coordinate_scale: float = 1.0,
    ) -> tuple[ExtractedBlock, ...]:
        try:
            input_image = image
            if not isinstance(image, (str, bytes)):
                try:
                    import numpy as np
                except ImportError as exc:  # pragma: no cover - optional runtime
                    raise DocumentExtractionError(
                        "PaddleOCR requiere numpy en el worker OCR."
                    ) from exc
                input_image = np.asarray(image)
            raw_results = (
                self._runner.predict(input_image)
                if hasattr(self._runner, "predict")
                else self._runner.ocr(input_image, cls=True)
            )
            if isinstance(raw_results, (dict, str, bytes, bytearray)) or hasattr(
                raw_results, "json"
            ):
                raw_results = [raw_results]
            else:
                raw_results = list(raw_results or [])
        except Exception as exc:
            raise DocumentExtractionError(f"PaddleOCR no pudo procesar la imagen: {exc}") from exc

        blocks: list[ExtractedBlock] = []
        block_index = 1
        for raw_result in raw_results:
            payload = _result_payload(raw_result)
            for text, raw_bbox, confidence in _paddle_items(payload):
                bbox = tuple(round(value * coordinate_scale, 3) for value in raw_bbox)
                blocks.append(
                    ExtractedBlock(
                        block_id=f"p{page_number:04d}-paddle{block_index:04d}",
                        page_number=page_number,
                        text=text,
                        bbox=bbox,
                        confidence=confidence,
                        method="paddleocr",
                        units=units,
                    )
                )
                block_index += 1
        if not blocks:
            raise DocumentExtractionError("PaddleOCR no reconoció bloques de texto en la imagen.")
        return tuple(blocks)


class _AutoOcr:
    """Use PaddleOCR first and fall back if inference fails after startup."""

    def __init__(self, primary: OcrEngine, settings: ExtractionSettings) -> None:
        self._active = primary
        self._settings = settings
        self.warnings: list[str] = []

    @property
    def engine_name(self) -> str:
        return getattr(self._active, "engine_name", "tesseract")

    def extract(
        self,
        image: Any,
        *,
        page_number: int,
        units: str,
        coordinate_scale: float = 1.0,
    ) -> tuple[ExtractedBlock, ...]:
        try:
            return self._active.extract(
                image,
                page_number=page_number,
                units=units,
                coordinate_scale=coordinate_scale,
            )
        except DocumentExtractionError as exc:
            if self.engine_name != "paddleocr":
                raise
            self._active = TesseractOcr(self._settings)
            self.warnings.append(f"PaddleOCR falló durante inferencia; se usa Tesseract: {exc}")
            return self._active.extract(
                image,
                page_number=page_number,
                units=units,
                coordinate_scale=coordinate_scale,
            )


def _make_ocr(settings: ExtractionSettings) -> tuple[OcrEngine, tuple[str, ...]]:
    engine = settings.ocr_engine.casefold()
    if engine == "tesseract":
        return TesseractOcr(settings), ()
    if engine == "paddleocr":
        return PaddleOcr(settings), ()
    if engine != "auto":
        raise ValueError(f"Motor OCR no soportado: {settings.ocr_engine}")
    try:
        return _AutoOcr(PaddleOcr(settings), settings), ()
    except Exception as exc:
        return TesseractOcr(settings), (
            f"PaddleOCR no disponible; se usa Tesseract como fallback: {exc}",
        )


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

    ocr: OcrEngine | None = None
    ocr_warnings: tuple[str, ...] = ()
    ocr_engine_name: str | None = None

    def get_ocr() -> OcrEngine:
        nonlocal ocr, ocr_warnings, ocr_engine_name
        if ocr is None:
            ocr, ocr_warnings = _make_ocr(settings)
            ocr_engine_name = getattr(ocr, "engine_name", settings.ocr_engine)
        return ocr

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
                blocks = get_ocr().extract(
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
    engine = "pymupdf" if ocr_engine_name is None else f"pymupdf+{ocr_engine_name}"
    return ExtractionResult(
        source_file=path.name,
        media_type="application/pdf",
        document_type=document_type,
        engine=engine,
        pages=tuple(pages),
        warnings=(*ocr_warnings, *getattr(ocr, "warnings", ())),
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
    ocr, ocr_warnings = _make_ocr(settings)
    blocks = ocr.extract(
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
        engine=getattr(ocr, "engine_name", settings.ocr_engine),
        pages=(page,),
        warnings=(*ocr_warnings, *getattr(ocr, "warnings", ())),
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
