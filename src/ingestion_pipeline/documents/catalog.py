"""Deterministic first-pass extraction of product cards from visual catalogs.

The extractor intentionally returns evidence-backed candidates. It does not try
to infer missing business facts; ambiguous cards are kept in the output and are
marked for review through validation issues.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median

from ingestion_pipeline.config import DocumentConfig
from ingestion_pipeline.documents.markdown import page_markdown_lines, repair_mojibake
from ingestion_pipeline.documents.models import DocumentPage, ExtractionResult
from ingestion_pipeline.domain.catalog import FieldDefinition, fields_for
from ingestion_pipeline.domain.models import NormalizedRecord, ValidationIssue
from ingestion_pipeline.normalization.mapping import coerce_value


@dataclass(frozen=True)
class _Line:
    text: str
    page_number: int
    bbox: tuple[float, float, float, float] | None
    confidence: float | None
    block_id: str
    units: str

    @property
    def top(self) -> float:
        return self.bbox[1] if self.bbox else 0.0

    @property
    def bottom(self) -> float:
        return self.bbox[3] if self.bbox else self.top


@dataclass(frozen=True)
class CatalogExtraction:
    products: tuple[NormalizedRecord, ...]
    variants: tuple[NormalizedRecord, ...]
    issues: tuple[ValidationIssue, ...]
    product_issues: tuple[ValidationIssue, ...] = ()
    variant_issues: tuple[ValidationIssue, ...] = ()
    warnings: tuple[str, ...] = ()
    unparsed_cards: tuple[dict[str, object], ...] = ()


_SKU_RE = re.compile(
    r"(?i)\b(?:sku|c[oó]digo|codigo|ref(?:erencia)?|item)\s*[:#\-]?\s*"
    r"([A-Z0-9][A-Z0-9._/\-]{2,})\b"
)
_PRICE_RE = re.compile(
    r"(?i)(?:precio|pvp|oferta|desde|bs\.?|b\.?s\.?)\s*[:.]?\s*"
    r"(?:bs\.?\s*)?([0-9][0-9., ]*)"
)
_STOCK_RE = re.compile(r"(?i)\b(?:stock|existencias|inventario|cantidad)\s*[:#-]?\s*([0-9., ]+)")
_VARIANT_PREFIX_RE = re.compile(r"(?i)^\s*(?:variante|variaci[oó]n|opciones?)\s*[:#-]?\s*(.*)$")
_LABEL_RE = re.compile(
    r"(?i)^\s*(?:sku|c[oó]digo|codigo|ref(?:erencia)?|item|precio|pvp|oferta|desde|"
    r"bs\.?|stock|existencias|inventario|cantidad|variante|variaci[oó]n|opciones?|"
    r"atributos?|ficha)\b"
)
_ATTRIBUTE_PAIR_RE = re.compile(
    r"(?P<key>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 _/-]{1,30})"
    r"\s*(?:=|:)\s*(?P<value>[^;|]+)"
)


# Real PDFs can contain proper UTF-8 text while older fixtures contain
# mojibake. Keep both spellings in the matching expressions.
_SKU_RE = re.compile(
    r"(?i)\b(?:sku|codigo|c\u00f3digo|c\u00c3\u00b3digo|ref(?:erencia)?|item)\b\s*[:#\-]?\s*"
    r"([A-Z0-9][A-Z0-9._/\-]{2,})\b"
)
_PRICE_RE = re.compile(
    r"(?i)(?:\b(?:precio|pvp|oferta|desde)\b|\b(?:bs|b\.s\.)\b)\s*[:.]?\s*"
    r"(?:bs\.?\s*)?([0-9][0-9., ]*)"
)
_VARIANT_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:variante|variacion|variaci\u00f3n|variaci\u00c3\u00b3n|opciones?)\s*[:#-]?\s*(.*)$"
)
_PRESENTATION_RE = re.compile(
    r"(?i)^\s*presentaci(?:on|\u00f3n|\u00c3\u00b3n)(?:es)?\s*[:#-]?\s*(.*)$"
)
_PRESENTATION_VALUE_RE = re.compile(
    r"(?i)\b[0-9]+(?:[.,][0-9]+)?\s*(?:kg|g|grs?|ml|l|oz|unidades?|uds?|u)\b"
)
_LABEL_RE = re.compile(
    r"(?i)^\s*(?:sku|codigo|c\u00f3digo|c\u00c3\u00b3digo|ref(?:erencia)?|item|precio|pvp|oferta|desde|"
    r"bs\.?|stock|existencias|inventario|cantidad|variante|variacion|variaci\u00f3n|variaci\u00c3\u00b3n|"
    r"opciones?|presentacion(?:es)?|presentaci\u00f3n(?:es)?|presentaci\u00c3\u00b3n(?:es)?|atributos?|ficha)\b"
)


def _page_lines(page: DocumentPage) -> list[_Line]:
    return [
        line
        for block in page.blocks
        for line in (
            _Line(
                text=text.strip(),
                page_number=block.page_number,
                bbox=block.bbox,
                confidence=block.confidence,
                block_id=block.block_id,
                units=block.units,
            )
            for text in block.text.splitlines()
            if text.strip()
        )
    ]


def _looks_like_title_anchor(text: str) -> bool:
    """Recognize a product title without matching descriptive body copy."""
    normalized = re.sub(r"(?i)(m[áa]scara)(?=[A-ZÁÉÍÓÚÜÑ])", r"\1 ", text.strip())
    lowered = normalized.casefold()
    if lowered.startswith(("máscara", "mascara")):
        words = lowered.split(maxsplit=1)
        tail = words[1] if len(words) > 1 else ""
        return not tail.startswith(("descongestiva", "de tratamiento"))
    return bool(re.match(r"(?i)^therapy\b", normalized))


def _looks_like_visual_title(line: _Line) -> bool:
    """Detect large title typography used by designed product sheets."""
    text = line.text.strip()
    lowered = text.casefold().rstrip(":")
    if _looks_like_title_anchor(text):
        return True
    if _LABEL_RE.match(text):
        return False
    if lowered.startswith(
        (
            "linea ",
            "línea ",
            "productos ",
            "descripcion",
            "ingredientes",
            "modo de uso",
            "sugerencia ",
            "presentacion",
            "activos ",
            "pieles ",
            "es ",
            "para ",
        )
    ):
        return False
    if not re.search(r"[A-Za-zÃÃ‰ÃÃ“ÃšÃœÃ‘Ã¡Ã©Ã­Ã³ÃºÃ¼Ã±]{3}", text):
        return False
    return bool(line.bbox and line.bbox[3] - line.bbox[1] >= 24)


def _title_anchor_count(lines: list[_Line]) -> int:
    mask_titles = sum(
        1
        for line in lines
        if re.match(r"(?i)^\s*m[áa]scara\b", line.text) and _looks_like_title_anchor(line.text)
    )
    if mask_titles:
        return mask_titles
    return sum(1 for line in lines if _looks_like_title_anchor(line.text))


def _is_single_page_card(lines: list[_Line]) -> bool:
    """Treat a designed product-sheet page as one card when unambiguous."""
    has_explicit_marker = any(
        _SKU_RE.search(line.text) or _PRICE_RE.search(line.text) for line in lines
    )
    presentation_count = sum(1 for line in lines if _is_presentation_line(line.text))
    return not has_explicit_marker and (_title_anchor_count(lines) == 1 or presentation_count == 1)


def _presentation_match(text: str) -> re.Match[str] | None:
    match = _PRESENTATION_RE.match(text)
    if match:
        return match
    return re.search(
        r"(?i)\bpresentaci(?:on|\u00f3n|\u00c3\u00b3n)(?:es)?\s*[:#-]?\s*(.*)$",
        text,
    )


def _is_presentation_line(text: str) -> bool:
    return _presentation_match(text) is not None


def _effective_columns(lines: list[_Line], page: DocumentPage, config: DocumentConfig) -> int:
    """Infer horizontal cards from presentation anchors when the profile allows it."""
    if config.columns <= 1:
        return 1
    anchors = [line for line in lines if _is_presentation_line(line.text) and line.bbox]
    if len(anchors) < 2:
        return 1
    # Diagonal cards can move their presentation anchors horizontally. A
    # larger separation is required before interpreting that movement as a
    # real two-column layout.
    threshold = max(260.0, page.width * 0.25)
    positions = sorted(line.bbox[0] for line in anchors if line.bbox)
    clusters = 1
    previous = positions[0]
    for position in positions[1:]:
        if position - previous > threshold:
            clusters += 1
        previous = position
    return min(config.columns, clusters)


def _looks_like_new_card(text: str) -> bool:
    return bool(_SKU_RE.search(text)) or bool(
        re.match(r"(?i)^\s*(?:producto|art[ií]culo)\s*[:#-]", text)
    )


def _is_section_heading(text: str) -> bool:
    lowered = text.strip().casefold()
    return lowered.startswith(("linea ", "línea ", "lÃ­nea ", "productos "))


def _group_cards(result: ExtractionResult, config: DocumentConfig) -> list[list[_Line]]:
    """Group nearby blocks into configurable visual cards by page and column."""
    cards: list[list[_Line]] = []
    for page in result.pages:
        page_lines = sorted(
            _page_lines(page), key=lambda line: (line.top, line.bbox[0] if line.bbox else 0)
        )
        if not page_lines:
            continue
        if _is_single_page_card(page_lines):
            cards.append(page_lines)
            continue
        columns: dict[int, list[_Line]] = {}
        effective_columns = _effective_columns(page_lines, page, config)
        for line in page_lines:
            x = line.bbox[0] if line.bbox else 0
            column = min(
                effective_columns - 1,
                max(0, int((x / max(page.width, 1)) * effective_columns)),
            )
            columns.setdefault(column, []).append(line)
        for column_lines in columns.values():
            column_lines.sort(key=lambda line: (line.top, line.bbox[0] if line.bbox else 0))
            # A single presentation marker in a column is a strong signal
            # that the complete column is one designed product sheet. The
            # marker is not necessarily the end of the card: descriptions and
            # legal copy often continue below it.
            if _is_single_page_card(column_lines):
                cards.append(column_lines)
                continue
            current: list[_Line] = []
            for line in column_lines:
                previous = current[-1] if current else None
                vertical_gap = line.top - previous.bottom if previous else 0
                has_marker = any(
                    _SKU_RE.search(item.text) or _PRICE_RE.search(item.text) for item in current
                )
                has_title = any(_looks_like_visual_title(item) for item in current)
                starts_new = bool(current) and (
                    (
                        vertical_gap > config.card_vertical_gap
                        and any(not _is_section_heading(item.text) for item in current)
                    )
                    or (_looks_like_new_card(line.text) and has_marker)
                    or (
                        _looks_like_visual_title(line)
                        and has_title
                        and any(_is_presentation_line(item.text) for item in current)
                    )
                    or (
                        _looks_like_visual_title(line)
                        and has_title
                        and vertical_gap > config.card_vertical_gap * 0.6
                    )
                )
                if starts_new:
                    cards.append(current)
                    current = []
                current.append(line)
            if current:
                cards.append(current)
    return cards


def _number(value: str) -> Decimal | None:
    text = value.strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _attributes(text: str) -> dict[str, str] | None:
    pairs = _ATTRIBUTE_PAIR_RE.findall(text)
    if not pairs:
        return None
    excluded = {
        "sku",
        "codigo",
        "precio",
        "pvp",
        "stock",
        "existencias",
        "inventario",
        "cantidad",
        "variante",
        "variacion",
        "opciones",
    }
    result = {
        key.strip(): value.strip()
        for key, value in pairs
        if key.strip().lower() not in excluded and value.strip()
    }
    return result or None


def _field_definitions(entity: str) -> dict[str, FieldDefinition]:
    return {field.name: field for field in fields_for(entity)}


def _source(lines: Iterable[_Line], run_id: str, source_file: str) -> dict[str, object]:
    materialized = list(lines)
    bboxes = [line.bbox for line in materialized if line.bbox]
    bbox = None
    units = materialized[0].units if materialized else "points"
    if bboxes:
        bbox = (
            min(box[0] for box in bboxes),
            min(box[1] for box in bboxes),
            max(box[2] for box in bboxes),
            max(box[3] for box in bboxes),
        )
    confidences = [line.confidence for line in materialized if line.confidence is not None]
    # Isolated OCR noise from icons should remain in block evidence without
    # blocking an otherwise legible product card.
    confidence = median(confidences) if confidences else None
    return {
        "run_id": run_id,
        "source_file": source_file,
        "page_number": materialized[0].page_number if materialized else None,
        "bbox": list(bbox) if bbox else None,
        "bbox_units": units,
        "block_ids": sorted({line.block_id for line in materialized}),
        "confidence": confidence,
    }


def _coerce_candidate(
    *,
    raw: dict[str, object],
    entity: str,
    row_number: int,
    source: dict[str, object],
) -> tuple[NormalizedRecord, list[ValidationIssue]]:
    definitions = _field_definitions(entity)
    data: dict[str, object] = {}
    issues: list[ValidationIssue] = []
    for field, value in raw.items():
        if value is None or field not in definitions:
            continue
        try:
            coerced = coerce_value(value, definitions[field])
        except ValueError as exc:
            issues.append(
                ValidationIssue(row_number, "invalid_type", f"{field}: {exc}", field=field)
            )
            continue
        if coerced is not None:
            data[field] = coerced
    return NormalizedRecord(row_number, entity, data, source), issues


def _find_name(lines: list[_Line]) -> str | None:
    normalized_lines = [
        _Line(
            text=re.sub(r"(?i)(m[áa]scara)(?=[A-ZÁÉÍÓÚÜÑ])", r"\1 ", line.text),
            page_number=line.page_number,
            bbox=line.bbox,
            confidence=line.confidence,
            block_id=line.block_id,
            units=line.units,
        )
        for line in lines
    ]
    for index, line in enumerate(normalized_lines):
        if not _looks_like_title_anchor(line.text):
            continue
        parts = [line.text.strip()]
        if index + 1 < len(normalized_lines):
            next_line = normalized_lines[index + 1]
            gap = next_line.top - line.bottom
            if (
                gap <= 70
                and len(next_line.text.strip()) <= 40
                and not _LABEL_RE.match(next_line.text)
                and not _looks_like_title_anchor(next_line.text)
                and not next_line.text.casefold().startswith(
                    ("piel ", "es ", "para ", "activos ", "ingredientes", "modo de uso")
                )
            ):
                parts.append(next_line.text.strip())
        return " ".join(parts)
    common_headings = {
        "productos faciales",
        "productos corporales",
        "cremas",
        "geles",
        "lociones",
        "emulsiones",
        "descripcion",
        "descripción",
        "ingredientes",
        "modo de uso",
        "sugerencia de uso",
        "productos relacionados",
        "presentacion",
        "presentación",
        "presentaciones",
        "presentaciones:",
    }
    candidates: list[tuple[int, int, str]] = []
    for index, line in enumerate(normalized_lines):
        text = line.text.strip()
        lowered = text.casefold().rstrip(":")
        if _LABEL_RE.match(text) or lowered in common_headings:
            continue
        if _number(text) is not None or len(text) < 3 or len(text) > 90:
            continue
        if lowered in {"uses", "use", "ear", "o e", "a", "e", "o"}:
            continue
        if not re.search(r"[A-Za-zÃÃ‰ÃÃ“ÃšÃœÃ‘Ã¡Ã©Ã­Ã³ÃºÃ¼Ã±]{3}", text):
            continue
        if lowered.startswith(("línea ", "linea ", "productos ")):
            continue
        score = 1
        if not text.isupper():
            score += 3
        else:
            score += 2
        if ":" not in text:
            score += 1
        if line.bbox:
            height = line.bbox[3] - line.bbox[1]
            width = line.bbox[2] - line.bbox[0]
            if height >= 24:
                score += 3
            if height >= 34:
                score += 2
            if width >= 200:
                score += 2
            if width >= 350:
                score += 1
        if text.endswith((".", ",", ":")) or lowered.startswith(
            (
                "es ",
                "para ",
                "hidratacion ",
                "pieles ",
                "activos ",
                "la ",
                "una ",
            )
        ):
            score -= 5
        if index == 0:
            score -= 1
        candidates.append((score, index, text))
    if candidates:
        return max(candidates, key=lambda candidate: (candidate[0], -candidate[1]))[2]
    for line in normalized_lines:
        if _LABEL_RE.match(line.text):
            continue
        if _number(line.text) is not None:
            continue
        if len(line.text) >= 2:
            return line.text
    return None


def _find_price(lines: list[_Line]) -> Decimal | None:
    for line in lines:
        match = _PRICE_RE.search(line.text)
        if match:
            number = _number(match.group(1))
            if number is not None:
                return number
        if re.search(r"(?i)\bbs\.?", line.text) or re.search(r"[0-9]+[,.][0-9]{1,2}", line.text):
            number_match = re.search(r"[0-9][0-9., ]*", line.text)
            if number_match:
                number = _number(number_match.group(0))
                if number is not None:
                    return number
    return None


def _find_stock(lines: list[_Line]) -> Decimal | None:
    for line in lines:
        match = _STOCK_RE.search(line.text)
        if match:
            return _number(match.group(1))
    return None


def _find_attributes(lines: list[_Line]) -> dict[str, str] | None:
    for line in lines:
        if re.match(r"(?i)^\s*(?:atributos?|ficha)\b", line.text):
            parsed = _attributes(line.split(":", 1)[-1])
            if parsed:
                return parsed
    return None


def _variant_raw(line: _Line) -> dict[str, object] | None:
    match = _VARIANT_PREFIX_RE.match(line.text)
    if not match:
        return None
    payload = match.group(1)
    sku_match = _SKU_RE.search(payload)
    attrs = _attributes(payload)
    if not attrs:
        return None
    raw: dict[str, object] = {"atributos": attrs}
    if sku_match:
        raw["sku_variante"] = sku_match.group(1)
    price = _find_price([line])
    if price is not None:
        raw["precio"] = price
    stock = _find_stock([line])
    if stock is not None:
        raw["stock"] = stock
    return raw


def _presentation_values(line: _Line) -> list[str]:
    match = _presentation_match(line.text)
    if not match:
        return []
    values = [
        re.sub(r"\s+", " ", value).strip()
        for value in _PRESENTATION_VALUE_RE.findall(match.group(1))
    ]
    # A single presentation is a product fact; two or more are variants.
    return values if len(values) >= 2 else []


def _variant_candidates(card: list[_Line]) -> list[tuple[dict[str, object], _Line]]:
    candidates: list[tuple[dict[str, object], _Line]] = []
    for line in card:
        raw = _variant_raw(line)
        if raw is not None:
            candidates.append((raw, line))
        for presentation in _presentation_values(line):
            candidates.append(({"atributos": {"presentacion": presentation}}, line))
    return candidates


def extract_catalog(
    extraction: ExtractionResult,
    *,
    run_id: str,
    config: DocumentConfig,
) -> CatalogExtraction:
    """Extract product/variant candidates from document blocks."""
    cards = _group_cards(extraction, config)
    if not cards:
        return CatalogExtraction(
            products=(),
            variants=(),
            issues=(
                ValidationIssue(
                    2,
                    "no_product_cards_detected",
                    "No se detectaron tarjetas de producto en el documento.",
                ),
            ),
            product_issues=(
                ValidationIssue(
                    2,
                    "no_product_cards_detected",
                    "No se detectaron tarjetas de producto en el documento.",
                ),
            ),
        )

    products: list[NormalizedRecord] = []
    variants: list[NormalizedRecord] = []
    issues: list[ValidationIssue] = []
    product_issues: list[ValidationIssue] = []
    variant_issues: list[ValidationIssue] = []
    unparsed: list[dict[str, object]] = []
    warnings: list[str] = []
    for card_index, card in enumerate(cards, start=2):
        variant_candidates = _variant_candidates(card)
        name = _find_name(card)
        description, description_lines = _find_description(card, name)
        raw_product: dict[str, object] = {
            "sku": (
                _SKU_RE.search(" ".join(line.text for line in card)).group(1)
                if _SKU_RE.search(" ".join(line.text for line in card))
                else None
            ),
            "nombre": _normalize_name(name),
            "descripcion": description,
            "precio": _find_price(card),
            "stock": _find_stock(card),
            "atributos": _find_attributes(card),
            "tipo": "variable" if variant_candidates else "simple",
        }
        source = _source(card, run_id, extraction.source_file)
        name_lines = _name_evidence_lines(card, name)
        source["field_evidence"] = {
            "nombre": _field_evidence(name_lines, name),
            "descripcion": _field_evidence(description_lines, description),
        }
        product, product_coercion_issues = _coerce_candidate(
            raw=raw_product,
            entity="productos",
            row_number=card_index,
            source=source,
        )
        products.append(product)
        issues.extend(product_coercion_issues)
        product_issues.extend(product_coercion_issues)

        confidence = source.get("confidence")
        if (
            config.block_low_confidence
            and isinstance(confidence, float)
            and confidence < config.confidence_threshold
        ):
            issue = ValidationIssue(
                card_index,
                "low_confidence",
                f"La tarjeta tiene confianza OCR {confidence:.0%}, inferior al umbral "
                f"{config.confidence_threshold:.0%}.",
                field="source",
            )
            issues.append(issue)
            product_issues.append(issue)

        if not raw_product["nombre"]:
            unparsed.append(
                {
                    "row_number": card_index,
                    "source": source,
                    "text": " ".join(line.text for line in card),
                }
            )

        for variant_index, (raw_variant, line) in enumerate(variant_candidates):
            raw_variant["sku_producto"] = raw_product["sku"]
            variant_source = dict(source)
            variant_source["block_ids"] = [line.block_id]
            variant_source["page_number"] = line.page_number
            variant, variant_coercion_issues = _coerce_candidate(
                raw=raw_variant,
                entity="variantes",
                row_number=card_index * 100 + variant_index,
                source=variant_source,
            )
            variants.append(variant)
            issues.extend(variant_coercion_issues)
            variant_issues.extend(variant_coercion_issues)

    return CatalogExtraction(
        products=tuple(products),
        variants=tuple(variants),
        issues=tuple(issues),
        product_issues=tuple(product_issues),
        variant_issues=tuple(variant_issues),
        warnings=tuple(warnings),
        unparsed_cards=tuple(unparsed),
    )


# The functions below form the Markdown-aware second pass. They are kept close
# to the first-pass heuristics so tenant profiles can later replace them with a
# source-specific parser without changing the public catalog contract.
_NAME_HEADINGS = {
    "productos faciales",
    "productos corporales",
    "cremas",
    "geles",
    "lociones",
    "emulsiones",
    "limpieza",
    "mascaras",
    "mascaras superfood",
    "faciales y corporales",
    "programa",
    "descripcion",
    "ingredientes",
    "modo de uso",
    "sugerencia de uso",
    "productos relacionados",
    "presentacion",
    "presentaciones",
    "activos",
}
_DESCRIPTION_STOP = ("modo de uso", "modo de aplicacion", "modo de aplicación")
_NAME_NOISE = {
    "ojsjojo",
    "uses",
    "use",
    "ear",
    "o e",
    "a",
    "e",
    "o",
    "i",
    "©",
}
_NAME_PROTECTED = {
    "adn",
    "be",
    "c",
    "cicaboost",
    "dna",
    "inci",
    "map",
    "n°",
    "pdrn",
    "pdrn+",
    "exos+",
    "pert+",
    "q10",
    "sp f",
    "spf",
    "uv",
    "vit-c",
    "zn",
}
_NAME_STOPWORDS = {"a", "al", "con", "de", "del", "el", "en", "la", "las", "los", "para", "y"}
_NAME_BODY_STARTERS = (
    "accion ",
    "acumulada ",
    "aconseja ",
    "antioxidantes ",
    "arandano ",
    "arandanos ",
    "apariencia ",
    "aplicar ",
    "brinda ",
    "combina ",
    "con ",
    "contiene ",
    "convierte ",
    "crema gel,",
    "debe ",
    "del ",
    "disenada ",
    "domiciliario ",
    "domiciliario",
    "enrojecimiento ",
    "equilibra ",
    "arrugas ",
    "cicatrices ",
    "intense ",
    "marine ",
    "enrojecimiento ",
    "edad ",
    "el ",
    "en ",
    "emulsion con ",
    "especialmente ",
    "esta ",
    "este ",
    "fels ",
    "formula ",
    "formula ",
    "formulado ",
    "hidrata ",
    "ideal ",
    "la ",
    "las ",
    "limpiar ",
    "los ",
    "mas ",
    "mejora ",
    "homogeneizar ",
    "nutre ",
    "para ",
    "peles ",
    "pieles ",
    "por que ",
    "poros ",
    "protector solar ",
    "prevenir ",
    "proporciona ",
    "reduce ",
    "refuerza ",
    "producto ",
    "se ",
    "serum ",
    "serum antiage ",
    "serum concentrado ",
    "serum descongestivo ",
    "serum hidratante ",
    "serum s ",
    "seborregulador ",
    "skin ",
    "su ",
    "tiene ",
    "una ",
    "visiblemente ",
)
_NAME_BODY_MARKERS = (
    "acumulada ",
    "aconseja ",
    "antiedad",
    "apariencia ",
    "aplicacion ",
    "brinda ",
    "combina ",
    "corrector de tono",
    "de textura ",
    "con alto poder de",
    "especialmente ",
    "en gabinete ",
    "esta mascara",
    "este serum",
    "formulada ",
    "formulado ",
    "faciales y corporales",
    "humecta",
    "ideal para ",
    "limpia ",
    "locion secativa",
    "multifuncion",
    "nutre",
    "pieles ",
    "reparador",
    "triple hidratacion",
    "tonifica",
    "prevenir y corregir",
)
_PRODUCT_NAME_PREFIXES = (
    "aceite ",
    "booster ",
    "calm+",
    "crema ",
    "emulsion",
    "gel ",
    "hydra ",
    "jabon ",
    "locion ",
    "mascara",
    "oleo ",
    "peptona ",
    "pdrn",
    "remove ",
    "resorcina ",
    "serum ",
    "triabe ",
    "triana ",
    "trigac ",
    "tripab ",
    "therapy ",
    "tonico ",
)


def _fold_catalog_text(value: str) -> str:
    repaired = repair_mojibake(value)
    decomposed = unicodedata.normalize("NFKD", repaired)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _starts_like_name_body(text: str) -> bool:
    folded = _fold_catalog_text(text).strip().lstrip("-–—•·()[]¿¡ ")
    for starter in _NAME_BODY_STARTERS:
        prefix = starter.strip()
        if re.match(rf"^{re.escape(prefix)}(?:\b|[\s,:;.!?()\-–—])", folded):
            return True
    return False


def _page_lines(page: DocumentPage) -> list[_Line]:
    """Read parser lines from the same Markdown projection that is persisted."""
    return [
        _Line(
            text=line.text,
            page_number=line.page_number,
            bbox=line.bbox,
            confidence=line.confidence,
            block_id=line.block_id,
            units=line.units,
        )
        for line in page_markdown_lines(page)
    ]


def _looks_like_title_anchor(text: str) -> bool:
    folded = _fold_catalog_text(text).strip()
    if _starts_like_name_body(text):
        return False
    if folded.startswith("mascara"):
        tail = folded.removeprefix("mascara").strip()
        if tail.startswith(("descongestiva", "de tratamiento")):
            return False
        if any(marker in tail for marker in _NAME_BODY_MARKERS):
            return False
        return True
    return bool(re.match(r"^therapy\b", folded))


def _looks_like_visual_title(line: _Line) -> bool:
    text = line.text.strip()
    folded = _fold_catalog_text(text).rstrip(":")
    if _looks_like_title_anchor(text):
        return True
    if not re.search(r"[a-z]{3}", folded):
        return False
    if folded in _NAME_HEADINGS or folded in _NAME_NOISE:
        return False
    if folded.startswith(("mascaras superfood", "mascara s superfood")):
        return False
    if _starts_like_name_body(text):
        return False
    if folded.startswith(
        (
            "linea ",
            "productos ",
            "descripcion",
            "ingredientes",
            "modo de uso",
            "sugerencia ",
            "presentacion",
            "activos ",
            "pieles ",
            "es ",
            "para ",
        )
    ):
        return False
    if len(text) > 80 or text.endswith((".", ",", ":")):
        return False
    if not line.bbox:
        return False
    height = line.bbox[3] - line.bbox[1]
    if height < 22:
        return False
    return height >= 28 or text.isupper() or folded.startswith(_PRODUCT_NAME_PREFIXES)


def _title_anchor_count(lines: list[_Line]) -> int:
    return sum(1 for line in lines if _looks_like_title_anchor(line.text))


def _is_section_heading(text: str) -> bool:
    folded = _fold_catalog_text(text).strip()
    return folded.startswith(("linea ", "productos "))


def _is_short_title_fragment(line: _Line) -> bool:
    text = line.text.strip()
    folded = _fold_catalog_text(text).rstrip(":")
    if not line.bbox or len(text) > 30 or len(text) < 3:
        return False
    if _is_name_noise(text) or _starts_like_name_body(text):
        return False
    if folded in _NAME_HEADINGS or _LABEL_RE.match(text):
        return False
    if text.endswith((".", ",", ":")):
        return False
    return line.bbox[3] - line.bbox[1] >= 22


def _is_name_noise(text: str) -> bool:
    folded = _fold_catalog_text(text).strip()
    if folded in _NAME_NOISE or len(folded) < 3:
        return True
    if re.fullmatch(r"[0-9 ./_-]+", folded):
        return True
    if not re.search(r"[a-z]{3}", folded):
        return True
    # Short OCR fragments such as OJSJOJO are not promoted when they have no
    # title typography or product marker; the source evidence remains available
    # in the Markdown/JSON artifacts for later review.
    letters = re.sub(r"[^a-z]", "", folded)
    if len(letters) >= 6 and len(set(letters)) <= 2:
        return True
    return len(letters) >= 6 and len(set(letters)) / len(letters) < 0.5 and text.isupper()


def _name_group(lines: list[_Line], start: int) -> tuple[int, str]:
    current = lines[start]
    parts = [current.text.strip()]
    end = start
    if _looks_like_title_anchor(current.text):
        for index in range(start + 1, min(start + 2, len(lines))):
            candidate = lines[index]
            gap = candidate.top - lines[end].bottom
            if (
                gap <= 70
                and len(candidate.text) <= 40
                and not _LABEL_RE.match(candidate.text)
                and not _looks_like_title_anchor(candidate.text)
                and not _is_description_heading(candidate.text)
                and not candidate.text.endswith((".", ",", ":"))
                and not _fold_catalog_text(candidate.text).startswith(
                    ("piel ", "es ", "para ", "activos ", "ingredientes", "modo de uso")
                )
            ):
                parts.append(candidate.text.strip())
                end = index
            else:
                break
    elif _looks_like_visual_title(current):
        for index in range(start + 1, min(start + 3, len(lines))):
            candidate = lines[index]
            gap = candidate.top - lines[end].bottom
            x_gap = abs(
                (candidate.bbox[0] if candidate.bbox else 0)
                - (current.bbox[0] if current.bbox else 0)
            )
            if _is_name_noise(candidate.text) or re.fullmatch(r"[\W_]+", candidate.text):
                end = index
                continue
            if (
                gap <= 48
                and x_gap <= 70
                and (_looks_like_visual_title(candidate) or _is_short_title_fragment(candidate))
            ):
                parts.append(candidate.text.strip())
                end = index
            else:
                break
    return end, " ".join(parts)


def _trim_name_candidate(value: str) -> str:
    text = repair_mojibake(value).replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip(" -–—•·\t")
    if ":" in text:
        prefix = text.split(":", 1)[0].rstrip(" -–—")
        if len(prefix) >= 5:
            text = prefix
    folded = _fold_catalog_text(text)
    positions = [folded.find(marker) for marker in _NAME_BODY_MARKERS if folded.find(marker) > 2]
    if positions:
        text = text[: min(positions)].rstrip(" -–—,:;.")
    text = re.sub(
        r"(?i)\bm.{0,1}scara(?=[a-z])",
        lambda match: f"{match.group(0)} ",
        text,
    )
    if _fold_catalog_text(text).startswith(("mascaras superfood", "mascara s superfood")):
        return ""
    text = re.sub(r"(?i)\.e$", "", text).rstrip(" -–—•·")
    return text


def _select_name(lines: list[_Line]) -> tuple[str | None, int | None, int | None]:
    description_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _fold_catalog_text(line.text) in {"descripcion"}
        ),
        None,
    )
    candidates: list[tuple[int, float, int, int, str]] = []
    for index, line in enumerate(lines):
        text = line.text.strip()
        folded = _fold_catalog_text(text).rstrip(":")
        if _is_name_noise(text) or folded in _NAME_HEADINGS:
            continue
        if folded.startswith(
            (
                "modo de uso",
                "ingredientes",
                "activos",
                "presentacion",
                "linea ",
                "productos ",
                "programa biotech fusion",
                "biotech fusion",
            )
        ):
            continue
        if _SKU_RE.search(text) or _PRICE_RE.search(text) or _LABEL_RE.match(text):
            continue
        is_anchor = _looks_like_title_anchor(text)
        is_visual = _looks_like_visual_title(line)
        if not is_anchor and not is_visual:
            if index > (description_index if description_index is not None else len(lines)):
                continue
            if len(text) > 70 or text.endswith((".", ",", ":")):
                continue
            if _fold_catalog_text(text).startswith(("es ", "para ", "pieles ", "activos ")):
                continue
        end, grouped = _name_group(lines, index)
        grouped = _trim_name_candidate(grouped)
        grouped_folded = _fold_catalog_text(grouped)
        score = 8 if is_anchor else 4
        height = line.bbox[3] - line.bbox[1] if line.bbox else 0.0
        if is_visual:
            score += 7
        if height >= 28:
            score += 4
        elif height >= 22:
            score += 2
        if any(char.isdigit() for char in grouped):
            score += 3
        if len(grouped.split()) <= 6:
            score += 2
        if end > index:
            score += 8
        if _has_previous_title_line(lines, index):
            score -= 8
        if grouped_folded.startswith(_PRODUCT_NAME_PREFIXES):
            score += 5
        if grouped_folded.startswith(("linea ", "productos ")):
            score -= 20
        if description_index is not None and index > description_index:
            score -= 15
        if index == 0:
            score -= 1
        candidates.append((score, height, -index, -end, grouped))
    if not candidates:
        return None, None, None
    selected = max(candidates)
    text = selected[4]
    start = -selected[2]
    end = -selected[3]
    return text, start, end


def _has_previous_title_line(lines: list[_Line], index: int) -> bool:
    if index <= 0 or not lines[index].bbox:
        return False
    current = lines[index]
    for previous in reversed(lines[:index]):
        if not previous.bbox:
            continue
        gap = current.top - previous.bottom
        if gap > 55:
            break
        if (
            abs(current.bbox[0] - previous.bbox[0]) <= 80
            and (
                _looks_like_visual_title(previous)
                or (
                    previous.bbox[3] - previous.bbox[1] >= 22
                    and len(previous.text.strip()) <= 25
                    and not _starts_like_name_body(previous.text)
                    and not previous.text.rstrip().endswith((".", ",", ":"))
                )
            )
        ):
            return True
    return False


def _find_name(lines: list[_Line]) -> str | None:
    return _select_name(lines)[0]


def _format_name_token(token: str) -> str:
    folded = _fold_catalog_text(token)
    if folded in _NAME_STOPWORDS:
        return folded
    if folded in _NAME_PROTECTED or any(char.isdigit() for char in token):
        return token.upper() if folded in _NAME_PROTECTED else token
    if token in {"&", "+", "/"}:
        return token
    if token.isupper() or token.islower() or token[:1].isupper():
        if "-" in token:
            return "-".join(_format_name_token(part) for part in token.split("-"))
        return token[:1].upper() + token[1:].lower()
    return token


def _normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    text = repair_mojibake(value).replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip(" -–—•·\t")
    text = re.sub(
        r"(?i)\bN\s*(?:[^A-Za-z0-9\s]|o)\s*(\d+)",
        lambda match: f"N{chr(176)}{match.group(1)}",
        text,
    )
    if not text:
        return None
    return " ".join(_format_name_token(token) for token in text.split())


def _is_description_heading(text: str) -> bool:
    return _fold_catalog_text(text).strip().rstrip(":") == "descripcion"


def _is_description_line(line: _Line, *, name: str | None, title_range: tuple[int, int]) -> bool:
    text = line.text.strip()
    folded = _fold_catalog_text(text).strip()
    if _is_name_noise(text) or folded in _NAME_HEADINGS:
        return False
    if _PRESENTATION_RE.match(text) or _SKU_RE.search(text) or _PRICE_RE.search(text):
        return False
    if folded == _fold_catalog_text(name or ""):
        return False
    if title_range[0] <= 0 and title_range[1] >= 0 and _looks_like_visual_title(line):
        return False
    if len(text) < 3:
        return False
    return True


def _find_description(lines: list[_Line], name: str | None) -> tuple[str | None, list[_Line]]:
    name_text, name_start, name_end = _select_name(lines)
    selected_name = name or name_text
    title_range = (
        name_start if name_start is not None else -1,
        name_end if name_end is not None else -1,
    )
    description_heading = next(
        (index for index, line in enumerate(lines) if _is_description_heading(line.text)),
        None,
    )
    if description_heading is not None:
        start = description_heading + 1
        selected: list[_Line] = []
        for line in lines[start:]:
            if _fold_catalog_text(line.text).startswith(_DESCRIPTION_STOP):
                break
            if _is_description_line(line, name=selected_name, title_range=title_range):
                selected.append(line)
    else:
        start = (name_end + 1) if name_end is not None else 0
        selected = [
            line
            for line in lines[start:]
            if _is_description_line(line, name=selected_name, title_range=title_range)
        ]
    if not selected:
        return None, []
    value = re.sub(r"\s+", " ", " ".join(line.text for line in selected)).strip()
    return value or None, selected


def _name_evidence_lines(lines: list[_Line], name: str | None) -> list[_Line]:
    _, start, end = _select_name(lines)
    if start is None or end is None:
        return []
    return lines[start : end + 1]


def _field_evidence(lines: list[_Line], value: object) -> dict[str, object]:
    if not lines:
        return {"text": value, "block_ids": [], "bbox": None, "confidence": None}
    boxes = [line.bbox for line in lines if line.bbox]
    bbox = None
    if boxes:
        bbox = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
    confidences = [line.confidence for line in lines if line.confidence is not None]
    return {
        "text": value,
        "page_number": lines[0].page_number,
        "bbox": bbox,
        "bbox_units": lines[0].units,
        "block_ids": sorted({line.block_id for line in lines}),
        "confidence": median(confidences) if confidences else None,
    }
