"""Enrichment of product CSVs from authoritative DOCX price lists.

The price lists are deliberately parsed from the DOCX table XML instead of OCR:
they are structured source files and contain the authoritative professional sale
price and the unit cost in bolivianos.  The matching rules live in a tenant
profile so a future tenant can add aliases without changing the parser.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import yaml

from ingestion_pipeline.documents.markdown import repair_mojibake
from ingestion_pipeline.domain.catalog import fields_for
from ingestion_pipeline.normalization.mapping import normalize_attributes

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


@dataclass(frozen=True)
class PriceListRow:
    """One product row extracted from a price-list table."""

    source_file: str
    source_row: int
    brand: str
    raw_name: str
    cost: Decimal | None
    price: Decimal | None


@dataclass(frozen=True)
class PriceDocument:
    file: str
    brand: str


@dataclass(frozen=True)
class PdfVariantSpec:
    presentation: str
    price: Decimal


@dataclass(frozen=True)
class PdfProductSpec:
    page: int
    source_row: int
    name: str
    category: str
    variants: tuple[PdfVariantSpec, ...]


@dataclass(frozen=True)
class PdfCatalogProfile:
    source: str
    pdf_file: str
    extraction_jsonl: Path
    extraction_json: Path | None
    brand: str
    brand_order: tuple[str, ...]
    products: tuple[PdfProductSpec, ...]

    @classmethod
    def from_file(cls, path: Path) -> PdfCatalogProfile:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"El perfil PDF debe ser un objeto YAML: {path}")
        products: list[PdfProductSpec] = []
        for item in raw.get("products", []) or []:
            if not isinstance(item, dict):
                continue
            parsed_variants: list[PdfVariantSpec] = []
            for variant in item.get("variants", []) or []:
                if not isinstance(variant, dict):
                    continue
                if "presentation" not in variant or "price" not in variant:
                    continue
                price = _parse_decimal(str(variant["price"]))
                if price is None or price < 0:
                    raise ValueError(f"Precio PDF inválido en la ficha: {item}")
                parsed_variants.append(
                    PdfVariantSpec(
                        presentation=str(variant["presentation"]),
                        price=price,
                    )
                )
            variants = tuple(parsed_variants)
            if not variants:
                raise ValueError(f"La ficha PDF no tiene variantes/precios: {item}")
            products.append(
                PdfProductSpec(
                    page=int(item["page"]),
                    source_row=int(item["source_row"]),
                    name=str(item["name"]),
                    category=str(item.get("category") or ""),
                    variants=variants,
                )
            )
        if not products:
            raise ValueError(f"El perfil PDF no declara productos: {path}")
        brand = str(raw.get("brand") or "")
        if not brand:
            raise ValueError(f"El perfil PDF no declara marca: {path}")
        brand_order = tuple(str(value) for value in raw.get("brand_order", []) or [])
        if brand not in brand_order:
            brand_order = (*brand_order, brand)
        extraction_jsonl_value = str(raw.get("extraction_jsonl") or "").strip()
        if not extraction_jsonl_value:
            raise ValueError(f"El perfil PDF no declara `extraction_jsonl`: {path}")
        extraction_jsonl = Path(extraction_jsonl_value)
        extraction_json_value = str(raw.get("extraction_json") or "").strip()
        extraction_json = Path(extraction_json_value) if extraction_json_value else None
        return cls(
            source=str(raw.get("source") or path.stem),
            pdf_file=str(raw.get("pdf_file") or ""),
            extraction_jsonl=extraction_jsonl,
            extraction_json=extraction_json,
            brand=brand,
            brand_order=brand_order,
            products=tuple(products),
        )


@dataclass(frozen=True)
class EnrichmentProfile:
    source: str
    default_brand: str
    brand_order: tuple[str, ...]
    source_prefixes: dict[str, tuple[str, ...]]
    documents: tuple[PriceDocument, ...]
    aliases: dict[str, dict[str, str]]
    name_overrides: dict[str, str]

    @classmethod
    def from_file(cls, path: Path) -> EnrichmentProfile:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"El perfil de enriquecimiento debe ser un objeto YAML: {path}")

        documents = tuple(
            PriceDocument(str(item["file"]), str(item["brand"]))
            for item in raw.get("documents", [])
            if isinstance(item, dict) and "file" in item and "brand" in item
        )
        aliases: dict[str, dict[str, str]] = {}
        for brand, entries in (raw.get("aliases") or {}).items():
            if isinstance(entries, dict):
                aliases[str(brand)] = {
                    normalize_match_key(str(source_name)): str(catalog_name)
                    for source_name, catalog_name in entries.items()
                }
        name_overrides = {
            normalize_match_key(str(source_name)): str(catalog_name)
            for source_name, catalog_name in (raw.get("name_overrides") or {}).items()
        }
        source_prefixes = {
            str(brand): tuple(str(prefix) for prefix in prefixes or [])
            for brand, prefixes in (raw.get("source_prefixes") or {}).items()
        }
        brand_order = tuple(str(value) for value in raw.get("brand_order") or [])
        if not documents:
            raise ValueError(f"El perfil no declara documentos de precios: {path}")
        if not brand_order:
            brand_order = tuple(dict.fromkeys([document.brand for document in documents]))
        return cls(
            source=str(raw.get("source") or "unknown"),
            default_brand=str(raw.get("default_brand") or "Rouse Arey"),
            brand_order=brand_order,
            source_prefixes=source_prefixes,
            documents=documents,
            aliases=aliases,
            name_overrides=name_overrides,
        )

    def brand_for_source(self, source_file: str) -> str:
        source_folded = source_file.casefold()
        for brand in self.brand_order:
            for prefix in self.source_prefixes.get(brand, ()):
                if source_folded.startswith(prefix.casefold()):
                    return brand
        return self.default_brand


@dataclass(frozen=True)
class SkuProfile:
    """Stable product/variant SKU rules for one catalog source."""

    source: str
    product_pattern: str
    variant_pattern: str
    brand_prefixes: dict[str, str]

    @classmethod
    def from_file(cls, path: Path) -> SkuProfile:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"El perfil de SKU debe ser un objeto YAML: {path}")
        prefixes = raw.get("brand_prefixes") or {}
        if not isinstance(prefixes, dict) or not prefixes:
            raise ValueError(f"El perfil de SKU no declara `brand_prefixes`: {path}")
        brand_prefixes = {
            str(brand): str(prefix).strip().upper()
            for brand, prefix in prefixes.items()
            if str(prefix).strip()
        }
        if len(set(brand_prefixes.values())) != len(brand_prefixes):
            raise ValueError(f"Los prefijos de marca se repiten en el perfil: {path}")
        product_pattern = str(raw.get("product_pattern") or "{prefix}-{number:04d}")
        variant_pattern = str(raw.get("variant_pattern") or "{product_sku}-V{number:02d}")
        if "{prefix}" not in product_pattern or "{number" not in product_pattern:
            raise ValueError("`product_pattern` debe incluir {prefix} y {number}")
        if "{product_sku}" not in variant_pattern or "{number" not in variant_pattern:
            raise ValueError("`variant_pattern` debe incluir {product_sku} y {number}")
        return cls(
            source=str(raw.get("source") or path.stem),
            product_pattern=product_pattern,
            variant_pattern=variant_pattern,
            brand_prefixes=brand_prefixes,
        )


def _cell_text(cell: ET.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.findall(f".//{W}p"):
        text = "".join(node.text or "" for node in paragraph.findall(f".//{W}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _read_first_table(path: Path) -> list[list[str]]:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Documento de precios no encontrado: {path}")
    if path.suffix.casefold() != ".docx":
        raise ValueError(f"El documento de precios debe ser DOCX: {path}")
    try:
        with ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError(f"No se pudo leer el DOCX {path}: {exc}") from exc

    root = ET.fromstring(document_xml)
    table = root.find(f".//{W}body/{W}tbl")
    if table is None:
        table = root.find(f".//{W}tbl")
    if table is None:
        raise ValueError(f"El DOCX no contiene una tabla de precios: {path.name}")
    rows = []
    for row in table.findall(f"./{W}tr"):
        cells = [_cell_text(cell) for cell in row.findall(f"./{W}tc")]
        if cells:
            rows.append(cells)
    if len(rows) < 2:
        raise ValueError(f"La tabla de precios no tiene encabezado y filas: {path.name}")
    return rows


def _parse_decimal(value: str) -> Decimal | None:
    text = value.strip().replace(" ", "")
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        return Decimal(number)
    except InvalidOperation:
        return None


def _header_key(value: str) -> str:
    repaired = repair_mojibake(value).casefold()
    decomposed = unicodedata.normalize("NFKD", repaired)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _find_header_index(headers: list[str], *required_terms: str) -> int | None:
    for index, header in enumerate(headers):
        key = _header_key(header)
        if all(term in key for term in required_terms):
            return index
    return None


def parse_price_document(path: Path, brand: str) -> tuple[PriceListRow, ...]:
    """Read product, unit cost in Bs and professional sale price from a DOCX."""
    rows = _read_first_table(path)
    headers = rows[0]
    cost_index = _find_header_index(headers, "precio", "unid", "bs")
    price_index = _find_header_index(headers, "precio venta")
    if cost_index is None or price_index is None:
        raise ValueError(
            f"No se localizaron las columnas de costo unitario Bs y precio profesional en {path.name}"
        )

    parsed: list[PriceListRow] = []
    for source_row, row in enumerate(rows[1:], start=2):
        raw_name = repair_mojibake(html.unescape(row[0])).strip() if row else ""
        if not raw_name:
            continue
        cells = row + [""] * max(0, len(headers) - len(row))
        parsed.append(
            PriceListRow(
                source_file=path.name,
                source_row=source_row,
                brand=brand,
                raw_name=raw_name,
                cost=_parse_decimal(cells[cost_index]),
                price=_parse_decimal(cells[price_index]),
            )
        )
    return tuple(parsed)


def normalize_match_key(value: str) -> str:
    """Create a conservative accent/punctuation-insensitive matching key."""
    repaired = repair_mojibake(html.unescape(value)).casefold()
    decomposed = unicodedata.normalize("NFKD", repaired)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", without_accents)


_TITLE_STOPWORDS = {
    "a",
    "al",
    "con",
    "de",
    "del",
    "el",
    "en",
    "la",
    "las",
    "los",
    "para",
    "por",
    "y",
}
_TITLE_PROTECTED = {
    "adn": "ADN",
    "aha's": "AHA's",
    "be": "BE",
    "cicaboost": "CICABOOST",
    "dmae": "DMAE",
    "dna": "DNA",
    "exos+": "EXOS+",
    "fps": "FPS",
    "inci": "INCI",
    "pdrn+": "PDRN+",
    "pert+": "PERT+",
    "q10": "Q10",
    "tca": "TCA",
    "zn": "ZN",
}
_TITLE_WORD_CORRECTIONS = {
    "acido": "Ácido",
    "acidos": "Ácidos",
    "argan": "Argán",
    "colageno": "Colágeno",
    "emulsion": "Emulsión",
    "emulsiones": "Emulsiones",
    "glicolico": "Glicólico",
    "hialuronico": "Hialurónico",
    "locion": "Loción",
    "lociones": "Lociones",
    "mascara": "Máscara",
    "mascaras": "Máscaras",
    "prebioica": "Prebiótica",
    "salicilico": "Salicílico",
    "tonico": "Tónico",
}


def _normalize_title_token(token: str, *, first: bool) -> str:
    if "-" in token and not token.startswith("-"):
        return "-".join(
            _normalize_title_token(part, first=first and index == 0)
            for index, part in enumerate(token.split("-"))
        )
    if "/" in token and token != "/":
        return "/".join(
            _normalize_title_token(part, first=first and index == 0)
            for index, part in enumerate(token.split("/"))
        )
    key = normalize_match_key(token)
    if key in _TITLE_WORD_CORRECTIONS:
        return _TITLE_WORD_CORRECTIONS[key]
    if token.casefold() in _TITLE_PROTECTED:
        return _TITLE_PROTECTED[token.casefold()]
    if token in {"&", "+", "/", "P/"}:
        return token
    if not first and key in _TITLE_STOPWORDS:
        return key
    if token.isupper() or token.islower() or token[:1].isupper():
        return token[:1].upper() + token[1:].lower()
    return token


def normalize_title(value: str) -> str:
    """Normalize product titles while retaining acronyms and numeric notation."""
    text = repair_mojibake(html.unescape(value)).replace("|", " ")
    text = re.sub(r"\s+", " ", text).strip(" -\t")
    text = re.sub(r"(?i)\bN\s*[º°o]?\s*(\d+)\b", r"N°\1", text)
    return " ".join(
        _normalize_title_token(token, first=index == 0) for index, token in enumerate(text.split())
    )


_DESCRIPTION_HEADINGS = re.compile(
    r"(?i)(?=(?:activos|ingredientes(?: clave)?|inci|puede contener|advertencias|"
    r"biotipos|modo de uso|sugerencia de uso|presentaci[oó]nes?|almacenamiento|guardado|"
    r"protecci[oó]n solar|caracter[ií]sticas y beneficios|piel radiante y vitalizada|"
    r"correcci[oó]n de tono|frescura y suavidad)\s*:)"
)
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-záéíóúñ])")


_DESCRIPTION_NOISE_PATTERNS = (
    # OCR of badges, decorative labels and column fragments. The source block
    # evidence remains untouched in JSONL; only the export copy is cleaned.
    re.compile(r"(?i)(?<!\w)ocrojojo[,:;.!]?\s*"),
    re.compile(r"(?i)(?<!\w)aono[,:;.!]?\s*"),
    re.compile(r"(?i)(?<!t)\bodo tipo de piel\b"),
    re.compile(r"(?i)\bfragance mineral cruelty\b"),
    re.compile(r"(?i)\bparaben free\b"),
    re.compile(r"(?i)\boil free\b"),
    re.compile(r"(?i)\bfree free\b"),
    re.compile(r"(?i)(?<!\w)ono(?!\w)"),
    re.compile(r"(?i)(?<!\w)precios(?!\w)"),
    re.compile(r"(?im)^\s*crema,?\s*[‘'\"]?gel ligera\s*$"),
    re.compile(r"(?i)\bhidra\s*-\s*protect\s+hidrataci[oó]n profunda todo tipo de peles\b"),
    re.compile(r"(?i)\bindustra argentina\b"),
    re.compile(r"(?i)\bcont neto:\s*\d+(?:[.,]\d+)?\s*g\b"),
)
_DESCRIPTION_NOISE_LINES = re.compile(r"(?im)^\s*(?:vaya|wyy|dy vena|sede lea paca)\s*[.!,:;]?\s*$")
_DESCRIPTION_ORPHAN_LINES = re.compile(r"(?im)^\s*[.\-,:;]+\s*$")
_DESCRIPTION_EMPTY_HEADINGS = re.compile(r"(?im)^\s*(?:almacenamiento|guardado)\s*:\s*$")


def _description_case(value: str) -> str:
    """Repair all-caps OCR lines while preserving ordinary mixed-case copy."""
    text = value
    if text.isupper() and len(text) > 2:
        text = text.lower()
    label_match = re.match(r"^([^:]{2,}):", text)
    if label_match and label_match.group(1).isupper():
        label = label_match.group(1).lower()
        label = _SENTENCE_START.sub(
            lambda match: f"{match.group(1)}{match.group(2).upper()}", label
        )
        text = f"{label}:{text[label_match.end() :]}"

    def repair_mixed_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.isupper() or not any(char.isupper() for char in token[1:]):
            return token
        return token[:1].upper() + token[1:].lower()

    text = re.sub(r"\b[\wÁÉÍÓÚÑáéíóúñÜü]{3,}\b", repair_mixed_token, text)
    return _SENTENCE_START.sub(lambda match: f"{match.group(1)}{match.group(2).upper()}", text)


def normalize_description(value: str) -> str:
    """Format paragraphs and sentence starts without rewriting source meaning."""
    text = repair_mojibake(html.unescape(value or ""))
    text = text.replace("\u00a0", " ").replace("\ufb02", "fl")
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _DESCRIPTION_NOISE_LINES.sub("", text)
    for pattern in _DESCRIPTION_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(
        r"(?i)\bcrema\s+gel,\s*es\s+ideal\s+para\s+todo\s+tipo\s+de\s+pieles\b",
        "Crema gel, es ideal para todo tipo de pieles",
        text,
    )
    text = re.sub(r"(?i)\bpor\s+sus\s+caracter[ií]sticas\b", "Por sus características", text)
    text = re.sub(
        r"(?i)\bmetabolizarse\s+convirti[eé]ndose\b",
        "metabolizarse, convirtiéndose",
        text,
    )
    text = re.sub(
        r"(?i)\bde agua,\s*tal motivo lo convierte en un componente esencial\b",
        "de agua; por tal motivo, se convierte en un componente esencial",
        text,
    )
    text = re.sub(
        r"(?i)\bLas proteínas de trigo hidrolizadas, posee\b",
        "Las proteínas de trigo hidrolizadas poseen",
        text,
    )
    text = re.sub(r"(?i)\bduradera\s+(?=el\s+sodio\b)", "duradera. ", text)
    text = re.sub(r"(?i)\bpiel\)\s+(?=la\s+hidroxiprolina\b)", "piel). ", text)
    text = re.sub(r"(?i)\ba\s+a\s+la\s+piel\b", "a la piel", text)
    text = re.sub(r"(?i)\babsorver\b", "absorber", text)
    text = re.sub(r"(?im)^\s*guardado\s+(?=guardar\b)", "Guardado: ", text)
    text = _DESCRIPTION_EMPTY_HEADINGS.sub("", text)
    text = _DESCRIPTION_ORPHAN_LINES.sub("", text)
    text = _DESCRIPTION_HEADINGS.sub("\n\n", text)
    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", text):
        lines = [re.sub(r"\s+", " ", line).strip() for line in paragraph.splitlines()]
        compact = " ".join(line for line in lines if line)
        if compact:
            compact = _description_case(compact)
            compact = _SENTENCE_START.sub(
                lambda match: f"{match.group(1)}{match.group(2).upper()}", compact
            )
            paragraphs.append(compact)
    return "\n\n".join(paragraphs)


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _load_source_records(path: Path | None, expected_count: int) -> list[dict[str, Any] | None]:
    if path is None:
        return [None] * expected_count
    if not path.exists():
        raise FileNotFoundError(f"JSONL de evidencia no encontrado: {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
    if len(records) != expected_count:
        raise ValueError(
            f"El CSV tiene {expected_count} filas y el JSONL de evidencia tiene {len(records)}; "
            "no se puede asignar marca por fuente con seguridad."
        )
    return records


def _source_files(records: list[dict[str, Any] | None]) -> list[str | None]:
    return [
        str(record.get("source", {}).get("source_file") or "") or None if record else None
        for record in records
    ]


def _write_source_records(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    temporary.replace(path)


def sync_source_jsonl_order(csv_path: Path, source_jsonl: Path, profile_path: Path) -> int:
    """Reorder an existing evidence JSONL to match an already sorted CSV.

    This recovery helper is useful when a previous enrichment run sorted the
    CSV before source-order synchronization was added.  It matches each
    evidence record by its source-derived brand and normalized output name,
    while keeping duplicate names in their original stable order.
    """
    profile = EnrichmentProfile.from_file(profile_path)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    records = _load_source_records(source_jsonl, len(rows))
    if any(record is None for record in records):
        raise ValueError("El JSONL de evidencia no puede contener registros vacíos.")

    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for record in records:
        assert record is not None
        source_file = str(record.get("source", {}).get("source_file") or "")
        before_name = str(record.get("data", {}).get("nombre") or "")
        name = profile.name_overrides.get(normalize_match_key(before_name), before_name)
        brand = profile.brand_for_source(source_file)
        buckets[(brand, normalize_match_key(normalize_title(name)))].append(record)

    reordered: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("marca", ""), normalize_match_key(row.get("nombre", "")))
        if not buckets[key]:
            raise ValueError(
                "No se pudo alinear el JSONL con el CSV para "
                f"marca={key[0]!r}, nombre={row.get('nombre', '')!r}."
            )
        reordered.append(buckets[key].popleft())
    if any(buckets.values()):
        raise ValueError("El JSONL contiene registros que no aparecen en el CSV.")
    _write_source_records(source_jsonl, reordered)
    return len(reordered)


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"JSONL no encontrado: {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def _write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile_record_map(
    records: list[dict[str, Any]], profile: PdfCatalogProfile
) -> dict[int, dict[str, Any]]:
    by_row: dict[int, dict[str, Any]] = {}
    for record in records:
        row_number = record.get("row_number")
        if isinstance(row_number, int):
            by_row[row_number] = record
    selected: dict[int, dict[str, Any]] = {}
    for product in profile.products:
        record = by_row.get(product.source_row)
        if record is None:
            raise ValueError(
                f"No se encontró source_row={product.source_row} para la página {product.page}."
            )
        source = record.get("source", {})
        if source.get("source_file") != profile.pdf_file:
            raise ValueError(
                f"source_row={product.source_row} no pertenece a {profile.pdf_file!r}."
            )
        if source.get("page_number") != product.page:
            raise ValueError(
                f"source_row={product.source_row} esperaba página {product.page}, "
                f"pero tiene {source.get('page_number')}."
            )
        selected[product.page] = record
    return selected


@dataclass(frozen=True)
class _PdfDescriptionBlock:
    block_id: str
    text: str
    left: float
    top: float
    bottom: float


_PDF_DESCRIPTION_LABEL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(activos?|modo\s+de\s+uso|beneficios|"
    r"caracter[ií]sticas?|acci[oó]n|i{1,2}ndicaciones?)\s*:"
)
_PDF_DESCRIPTION_EXACT_LABEL_RE = re.compile(
    r"(?i)^(?:activos?|modo\s+de\s+uso|beneficios|caracter[ií]sticas?|"
    r"acci[oó]n|i{1,2}ndicaciones?)$"
)
_PDF_DESCRIPTION_ORDER = (
    "caracteristicas",
    "activos",
    "modo_de_uso",
    "accion",
    "beneficios",
    "indicaciones",
)
_PDF_DESCRIPTION_LABELS = {
    "caracteristicas": "Características",
    "activos": "Activos",
    "modo_de_uso": "Modo de uso",
    "accion": "Acción",
    "beneficios": "Beneficios",
    "indicaciones": "Indicaciones",
}


def _pdf_bbox(value: object) -> tuple[float, float, float, float] | None:
    if isinstance(value, str):
        parts = value.split()
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) != 4:
        return None
    try:
        return tuple(float(part) for part in parts)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _read_pdf_description_pages(path: Path) -> dict[int, list[_PdfDescriptionBlock]]:
    if not path.exists():
        raise FileNotFoundError(f"JSON de extracción PDF no encontrado: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    pages: dict[int, list[_PdfDescriptionBlock]] = {}
    for page in raw.get("pages", []) if isinstance(raw, dict) else []:
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page_number") or 0)
        blocks: list[_PdfDescriptionBlock] = []
        for block in page.get("blocks", []) or []:
            if not isinstance(block, dict):
                continue
            bbox = _pdf_bbox(block.get("bbox"))
            if bbox is None:
                continue
            blocks.append(
                _PdfDescriptionBlock(
                    block_id=str(block.get("block_id") or ""),
                    text=str(block.get("text") or ""),
                    left=bbox[0],
                    top=bbox[1],
                    bottom=bbox[3],
                )
            )
        pages[page_number] = sorted(blocks, key=lambda block: (block.top, block.left))
    return pages


def _pdf_description_section(label: str) -> str | None:
    key = normalize_match_key(label)
    aliases = {
        "caracteristica": "caracteristicas",
        "caracteristicas": "caracteristicas",
        "activo": "activos",
        "activos": "activos",
        "mododeuso": "modo_de_uso",
        "beneficio": "beneficios",
        "beneficios": "beneficios",
        "accion": "accion",
        "indicacion": "indicaciones",
        "indicaciones": "indicaciones",
        "iindicacion": "indicaciones",
        "iindicaciones": "indicaciones",
    }
    return aliases.get(key)


def _clean_pdf_description_fragment(value: str) -> str:
    text = repair_mojibake(html.unescape(value or ""))
    text = text.replace("\u00a0", " ").replace("\ufb02", "fl")
    # Price and presentation boxes can share a native PDF block with the
    # characteristics text. They are variant evidence, not description copy.
    text = re.sub(r"(?i)\b\d+(?:[.,]\d+)?\s*(?:bs\.?|b\.s\.?)\b", " ", text)
    text = re.sub(r"(?i)\b\d+(?:[.,]\d+)?\s*(?:ml|grs?|kg|lt|l)\b", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text


def _is_pdf_description_noise(block: _PdfDescriptionBlock) -> bool:
    text = _clean_pdf_description_fragment(block.text)
    if not text or block.top < 130 or block.top > 770:
        return True
    key = normalize_match_key(text)
    if key in {
        "precio",
        "facial",
        "domiciliario",
        "peelingsquimicos",
        "lineacorporal",
    }:
        return True
    if key.startswith("ph"):
        return True
    return not re.sub(r"(?i)\b(?:bs|b\.s\.)\b", "", text).strip(" /.-")


def _append_pdf_description_text(target: dict[str, list[str]], section: str, value: str) -> None:
    cleaned = _clean_pdf_description_fragment(value)
    if cleaned:
        target.setdefault(section, []).append(cleaned)


def _description_from_pdf_page(
    blocks: list[_PdfDescriptionBlock], page: int
) -> tuple[str, dict[str, object]]:
    sections: dict[str, list[str]] = {}
    intro: list[str] = []
    current_section: str | None = None
    used_block_ids: list[str] = []
    for block in blocks:
        if _is_pdf_description_noise(block):
            continue
        block_used = False
        pending_lines: list[str] = []
        for raw_line in block.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            exact_label = _PDF_DESCRIPTION_EXACT_LABEL_RE.fullmatch(line.rstrip(":"))
            label_match = _PDF_DESCRIPTION_LABEL_RE.search(line)
            if exact_label:
                section = _pdf_description_section(exact_label.group(0))
                if section:
                    if pending_lines:
                        for pending in pending_lines:
                            _append_pdf_description_text(sections, section, pending)
                        pending_lines = []
                    current_section = section
                    block_used = True
                    continue
            if label_match:
                section = _pdf_description_section(label_match.group(1))
                if section:
                    prefix = line[: label_match.start()]
                    suffix = line[label_match.end() :]
                    if prefix:
                        _append_pdf_description_text(
                            target=sections,
                            section=current_section or section,
                            value=prefix,
                        )
                    current_section = section
                    if suffix:
                        _append_pdf_description_text(sections, section, suffix)
                    block_used = True
                    continue
            if current_section:
                _append_pdf_description_text(sections, current_section, line)
                block_used = True
            elif block.left >= 300:
                cleaned = _clean_pdf_description_fragment(line)
                if cleaned:
                    intro.append(cleaned)
                    block_used = True
            else:
                pending_lines.append(line)
        if pending_lines and block.left >= 300:
            intro.extend(
                cleaned
                for cleaned in (_clean_pdf_description_fragment(line) for line in pending_lines)
                if cleaned
            )
        if block_used and block.block_id:
            used_block_ids.append(block.block_id)

    parts: list[str] = []
    if intro:
        parts.append(normalize_description(" ".join(intro)))
    for section in _PDF_DESCRIPTION_ORDER:
        content = sections.get(section, [])
        if not content:
            continue
        normalized = normalize_description(" ".join(content))
        if normalized:
            parts.append(f"{_PDF_DESCRIPTION_LABELS[section]}: {normalized}")
    description = "\n\n".join(part for part in parts if part)
    return description, {
        "method": "native_pdf_blocks_layout",
        "page": page,
        "block_ids": list(dict.fromkeys(used_block_ids)),
        "sections": [section for section in _PDF_DESCRIPTION_ORDER if sections.get(section)],
        "source_meaning_rewritten": False,
    }


def improve_pdf_descriptions_csv(
    products_csv: Path,
    *,
    profile_path: Path,
    product_source_jsonl: Path | None = None,
    output_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Rebuild PDF product descriptions from position-aware native blocks.

    This stage is intentionally local and deterministic. It uses the reviewed
    product/page profile to separate the introductory copy and labeled sections
    from prices, category headings and footer badges. It does not use OCR or
    invent missing copy.
    """
    profile = PdfCatalogProfile.from_file(profile_path)
    if profile.extraction_json is None:
        raise ValueError(
            "El perfil PDF debe declarar `extraction_json` para mejorar descripciones."
        )
    page_blocks = _read_pdf_description_pages(profile.extraction_json)
    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "nombre" not in reader.fieldnames:
            raise ValueError("El CSV de productos debe contener la columna `nombre`.")
        headers = list(reader.fieldnames)
        rows = [dict(row) for row in reader]
    source_records = _load_source_records(product_source_jsonl, len(rows))
    row_indexes = {
        (row.get("marca", ""), normalize_match_key(row.get("nombre", ""))): index
        for index, row in enumerate(rows)
    }
    before_nonempty = sum(bool(row.get("descripcion", "").strip()) for row in rows)
    improved_pages: list[dict[str, object]] = []
    missing_pages: list[int] = []
    section_counts: dict[str, int] = defaultdict(int)
    improved_rows = 0
    for product in profile.products:
        name = normalize_title(product.name)
        key = (profile.brand, normalize_match_key(name))
        index = row_indexes.get(key)
        if index is None:
            raise ValueError(
                f"No se encontró en el CSV el producto PDF: marca={profile.brand!r}, nombre={name!r}."
            )
        description, metadata = _description_from_pdf_page(
            page_blocks.get(product.page, []), product.page
        )
        if description:
            rows[index]["descripcion"] = description
            improved_rows += 1
            for section in metadata["sections"]:
                section_counts[str(section)] += 1
        else:
            missing_pages.append(product.page)
        improved_pages.append(
            {
                "page": product.page,
                "name": name,
                "description_chars": len(description),
                "metadata": metadata,
            }
        )

    for index, row in enumerate(rows):
        if row.get("marca") != profile.brand:
            row["descripcion"] = normalize_description(row.get("descripcion", ""))
        evidence = source_records[index]
        if evidence is not None:
            enrichment = dict(evidence.get("enrichment") or {})
            page_metadata = next(
                (
                    item["metadata"]
                    for item in improved_pages
                    if normalize_match_key(str(item["name"]))
                    == normalize_match_key(row.get("nombre", ""))
                    and row.get("marca") == profile.brand
                ),
                None,
            )
            if page_metadata is not None:
                enrichment["description_improvement"] = page_metadata
                evidence["enrichment"] = enrichment

    destination = output_path or products_csv
    _write_csv_rows(destination, headers, rows)
    source_jsonl_written = False
    if product_source_jsonl is not None and destination == products_csv:
        records = [record for record in source_records if record is not None]
        if len(records) != len(source_records):
            raise ValueError("El JSONL de evidencia contiene registros vacíos.")
        _write_source_records(product_source_jsonl, records)
        source_jsonl_written = True

    if report_path is None:
        report_path = (
            products_csv.parent.parent
            / "reports"
            / (f"{products_csv.stem}__description-improvement.json")
        )
    report = {
        "source": profile.source,
        "pdf_file": profile.pdf_file,
        "input_csv": str(products_csv),
        "output_csv": str(destination),
        "extraction_json": str(profile.extraction_json),
        "method": "native_pdf_blocks_layout",
        "ocr_used": False,
        "rows_total": len(rows),
        "rows_with_description_before": before_nonempty,
        "pdf_rows_considered": len(profile.products),
        "pdf_rows_improved": improved_rows,
        "rows_with_description_after": sum(
            bool(row.get("descripcion", "").strip()) for row in rows
        ),
        "missing_pdf_pages": missing_pages,
        "section_counts": dict(section_counts),
        "pages": improved_pages,
        "source_jsonl_written": source_jsonl_written,
        "decision": "preserve_source_evidence_and_rebuild_csv_description_from_reviewed_page_blocks",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def merge_pdf_catalog_csvs(
    products_csv: Path,
    variants_csv: Path,
    *,
    profile_path: Path,
    product_source_jsonl: Path,
    variant_source_jsonl: Path | None = None,
    output_product_source_jsonl: Path | None = None,
    output_variant_source_jsonl: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Append reviewed PDF products and exact presentation prices to a batch.

    The PDF extraction JSONL supplies page-level evidence while the profile
    supplies the reviewed page/name/presentation interpretation.  Product
    prices are the minimum presentation price until the SKU/variant pass; all
    exact prices are written to the variant CSV.  No cost is inferred.
    """
    profile = PdfCatalogProfile.from_file(profile_path)
    extraction_records = _read_jsonl_records(profile.extraction_jsonl)
    selected_records = _profile_record_map(extraction_records, profile)

    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        product_reader = csv.DictReader(handle)
        if not product_reader.fieldnames or "nombre" not in product_reader.fieldnames:
            raise ValueError("El CSV de productos debe contener la columna `nombre`.")
        product_headers = list(product_reader.fieldnames)
        existing_products = [dict(row) for row in product_reader]
    existing_product_evidence = _load_source_records(product_source_jsonl, len(existing_products))
    if any(record is None for record in existing_product_evidence):
        raise ValueError("El JSONL de productos debe contener evidencia para cada fila.")

    existing_keys = {
        (row.get("marca", ""), normalize_match_key(row.get("nombre", "")))
        for row in existing_products
    }
    product_pairs: list[tuple[dict[str, str], dict[str, Any]]] = [
        (row, record)
        for row, record in zip(
            existing_products,
            existing_product_evidence,
            strict=True,
        )
        if record is not None
    ]
    variant_specs: list[tuple[PdfProductSpec, PdfVariantSpec, dict[str, Any]]] = []
    added_products: list[dict[str, str]] = []
    for product in profile.products:
        name = normalize_title(product.name)
        key = (profile.brand, normalize_match_key(name))
        if key in existing_keys:
            raise ValueError(
                f"El producto PDF ya existe en el CSV: marca={profile.brand!r}, nombre={name!r}."
            )
        record = selected_records[product.page]
        raw_data = record.get("data", {})
        row = {header: "" for header in product_headers}
        row["nombre"] = name
        row["descripcion"] = normalize_description(str(raw_data.get("descripcion") or ""))
        if "descripcion_corta" in row:
            row["descripcion_corta"] = ""
        if "categoria" in row:
            row["categoria"] = product.category
        if "marca" in row:
            row["marca"] = profile.brand
        if "tipo" in row:
            row["tipo"] = "variable" if len(product.variants) > 1 else "simple"
        if "precio" in row:
            row["precio"] = _decimal_text(min(variant.price for variant in product.variants))
        if "costo" in row:
            row["costo"] = ""
        added_products.append(row)
        evidence = dict(record)
        evidence["enrichment"] = {
            "canonical_name": name,
            "brand": profile.brand,
            "price_source": profile.pdf_file,
            "page": product.page,
            "variants": [
                {
                    "presentation": variant.presentation,
                    "price_bs": _decimal_text(variant.price),
                }
                for variant in product.variants
            ],
            "cost": "not_present_in_source",
        }
        product_pairs.append((row, evidence))
        existing_keys.add(key)
        variant_specs.extend((product, variant, record) for variant in product.variants)

    brand_rank = {brand: index for index, brand in enumerate(profile.brand_order)}
    product_pairs.sort(
        key=lambda pair: (
            brand_rank.get(pair[0].get("marca", ""), len(brand_rank)),
            normalize_match_key(pair[0].get("nombre", "")),
        )
    )
    _write_csv_rows(products_csv, product_headers, [row for row, _ in product_pairs])

    product_evidence_output = output_product_source_jsonl or product_source_jsonl
    _write_source_records(
        product_evidence_output,
        [record for _, record in product_pairs],
    )

    with variants_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        variant_reader = csv.DictReader(handle)
        if not variant_reader.fieldnames:
            raise ValueError("El CSV de variantes no contiene encabezados.")
        variant_headers = list(variant_reader.fieldnames)
        existing_variants = [dict(row) for row in variant_reader]

    existing_variant_evidence: list[dict[str, Any]] = []
    if variant_source_jsonl is not None:
        existing_variant_evidence = _read_jsonl_records(variant_source_jsonl)
        if len(existing_variant_evidence) != len(existing_variants):
            raise ValueError(
                "El JSONL de variantes y el CSV de variantes no tienen el mismo número de filas."
            )
    elif existing_variants:
        raise ValueError(
            "Se requiere JSONL de evidencia para las variantes existentes antes de agregarlas."
        )

    variant_rows = list(existing_variants)
    variant_evidence = list(existing_variant_evidence)
    price_report: list[dict[str, object]] = []
    for index, (product, variant, record) in enumerate(variant_specs, start=1):
        row = {header: "" for header in variant_headers}
        if "atributos" in row:
            row["atributos"] = json.dumps(
                {"presentacion": variant.presentation}, ensure_ascii=False
            )
        if "precio" in row:
            row["precio"] = _decimal_text(variant.price)
        if "sku_producto" in row:
            row["sku_producto"] = ""
        if "sku_variante" in row:
            row["sku_variante"] = ""
        variant_rows.append(row)
        source = dict(record.get("source", {}))
        source["price_evidence"] = {
            "source_file": profile.pdf_file,
            "page_number": product.page,
            "presentation": variant.presentation,
        }
        variant_evidence.append(
            {
                "data": {
                    "sku_producto": "",
                    "sku_variante": "",
                    "atributos": {"presentacion": variant.presentation},
                    "precio": _decimal_text(variant.price),
                },
                "entity": "variantes",
                "row_number": 100000 + product.page * 100 + index,
                "source": source,
            }
        )
        price_report.append(
            {
                "page": product.page,
                "product": product.name,
                "presentation": variant.presentation,
                "price_bs": _decimal_text(variant.price),
            }
        )

    _write_csv_rows(variants_csv, variant_headers, variant_rows)
    variant_evidence_output = output_variant_source_jsonl
    if variant_evidence_output is None and variant_source_jsonl is not None:
        variant_evidence_output = variants_csv.with_suffix(".jsonl")
    if variant_evidence_output is not None:
        _write_source_records(variant_evidence_output, variant_evidence)

    report: dict[str, object] = {
        "source": profile.source,
        "pdf_file": profile.pdf_file,
        "extraction_jsonl": str(profile.extraction_jsonl),
        "products_added": len(added_products),
        "variants_added": len(variant_specs),
        "products_total": len(product_pairs),
        "variants_total": len(variant_rows),
        "brand": profile.brand,
        "cost": {
            "field": "costo",
            "filled": 0,
            "decision": "not_present_in_source",
        },
        "price_source": "PDF page price by presentation",
        "prices": price_report,
        "evidence_alignment": {
            "products_csv_rows": len(product_pairs),
            "products_jsonl_rows": len(product_pairs),
            "variants_csv_rows": len(variant_rows),
            "variants_jsonl_rows": len(variant_evidence),
            "errors": 0,
        },
        "sku": "unchanged_for_second_pass",
    }
    if report_path is None:
        report_path = (
            products_csv.parent.parent
            / "reports"
            / (f"{products_csv.stem}__zoe-pdf-enrichment.json")
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _target_index(
    rows: list[dict[str, str]],
    catalog_name: str,
    source_files: list[str | None],
    brand: str,
    profile: EnrichmentProfile,
) -> int | None:
    candidates = [
        index
        for index, row in enumerate(rows)
        if normalize_match_key(row.get("nombre", "")) == normalize_match_key(catalog_name)
    ]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    branded = [
        index
        for index in candidates
        if profile.brand_for_source(source_files[index] or "") == brand
    ]
    return branded[0] if len(branded) == 1 else None


def enrich_products_csv(
    input_path: Path,
    *,
    profile_path: Path,
    prices_dir: Path,
    source_jsonl: Path | None = None,
    output_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Enrich and deterministically reorder a product CSV in place by default."""
    if not input_path.exists():
        raise FileNotFoundError(f"CSV de productos no encontrado: {input_path}")
    profile = EnrichmentProfile.from_file(profile_path)
    with input_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "nombre" not in reader.fieldnames:
            raise ValueError("El CSV debe contener la columna `nombre`.")
        headers = list(reader.fieldnames)
        rows = [dict(row) for row in reader]
    source_records = _load_source_records(source_jsonl, len(rows))
    source_files = _source_files(source_records)

    price_rows: list[PriceListRow] = []
    for document in profile.documents:
        price_rows.extend(parse_price_document(prices_dir / document.file, document.brand))

    matches: dict[int, list[PriceListRow]] = {}
    unmatched_price_rows: list[dict[str, object]] = []
    for price_row in price_rows:
        alias = profile.aliases.get(price_row.brand, {}).get(
            normalize_match_key(price_row.raw_name)
        )
        if alias is None:
            unmatched_price_rows.append(
                {
                    "source_file": price_row.source_file,
                    "source_row": price_row.source_row,
                    "brand": price_row.brand,
                    "raw_name": price_row.raw_name,
                    "reason": "no_alias_or_catalog_record",
                }
            )
            continue
        target = _target_index(rows, alias, source_files, price_row.brand, profile)
        if target is None:
            unmatched_price_rows.append(
                {
                    "source_file": price_row.source_file,
                    "source_row": price_row.source_row,
                    "brand": price_row.brand,
                    "raw_name": price_row.raw_name,
                    "catalog_name": alias,
                    "reason": "catalog_target_not_unique_or_missing",
                }
            )
            continue
        matches.setdefault(target, []).append(price_row)

    enriched_rows: list[dict[str, str]] = []
    enriched_evidence: list[dict[str, Any] | None] = []
    match_report: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        source_file = source_files[index]
        brand = profile.brand_for_source(source_file or "")
        before_name = row.get("nombre", "")
        name = profile.name_overrides.get(normalize_match_key(before_name), before_name)
        row["nombre"] = normalize_title(name)
        row["marca"] = brand
        row["descripcion"] = normalize_description(row.get("descripcion", ""))
        # Existing OCR prices can be active concentrations or percentages.  In
        # this pass only the price-list values are authoritative.
        if "precio" in row:
            row["precio"] = ""
        if "costo" in row:
            row["costo"] = ""

        entries = matches.get(index, [])
        if entries:
            costs = [entry.cost for entry in entries if entry.cost is not None]
            prices = [entry.price for entry in entries if entry.price is not None]
            # A product row can represent multiple presentations until the
            # variant pass.  The parent uses the lowest presentation value.
            if prices and "precio" in row:
                row["precio"] = _decimal_text(min(prices))
            if costs and "costo" in row:
                row["costo"] = _decimal_text(min(costs))
            for entry in entries:
                match_report.append(
                    {
                        "original_csv_row": index + 2,
                        "catalog_name": row["nombre"],
                        "source_file": entry.source_file,
                        "source_row": entry.source_row,
                        "brand": entry.brand,
                        "raw_name": entry.raw_name,
                        "cost_bs": _decimal_text(entry.cost),
                        "professional_price_bs": _decimal_text(entry.price),
                        "aggregation": "minimum_until_variants_pass"
                        if len(entries) > 1
                        else "direct",
                    }
                )
        enriched_rows.append(row)
        enriched_evidence.append(source_records[index])

    brand_rank = {brand: index for index, brand in enumerate(profile.brand_order)}
    sorted_pairs = sorted(
        zip(enriched_rows, enriched_evidence, strict=True),
        key=lambda pair: (
            brand_rank.get(pair[0].get("marca", ""), len(brand_rank)),
            normalize_match_key(pair[0].get("nombre", "")),
        ),
    )
    enriched_rows = [row for row, _ in sorted_pairs]
    enriched_evidence = [evidence for _, evidence in sorted_pairs]
    destination = output_path or input_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched_rows)

    source_jsonl_reordered = False
    if source_jsonl is not None and destination == input_path:
        records_to_write = [record for record in enriched_evidence if record is not None]
        if len(records_to_write) == len(enriched_evidence):
            _write_source_records(source_jsonl, records_to_write)
            source_jsonl_reordered = True

    report: dict[str, object] = {
        "source": profile.source,
        "input_csv": str(input_path),
        "output_csv": str(destination),
        "source_jsonl": str(source_jsonl) if source_jsonl else None,
        "source_jsonl_reordered": source_jsonl_reordered,
        "price_documents": [document.__dict__ for document in profile.documents],
        "total_catalog_rows": len(rows),
        "rows_with_price": sum(1 for row in enriched_rows if row.get("precio", "").strip()),
        "rows_with_cost": sum(1 for row in enriched_rows if row.get("costo", "").strip()),
        "brand_counts": {
            brand: sum(1 for row in enriched_rows if row.get("marca") == brand)
            for brand in profile.brand_order
        },
        "matched_price_rows": len(match_report),
        "unmatched_price_rows": unmatched_price_rows,
        "matches": match_report,
        "decisions": {
            "cost_field": "Precio POR unid. bs",
            "price_field": "Precio venta A profesional",
            "multi_presentation_parent_value": "minimum_cost_and_professional_price_until_variants_pass",
            "unmatched_catalog_brand": profile.default_brand,
            "sku": "unchanged_for_second_pass",
        },
    }
    if report_path is None:
        report_path = (
            input_path.parent.parent / "reports" / f"{input_path.stem}__iter2-enrichment.json"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


_PRESENTATION_ATTRIBUTE_KEYS = {
    "presentacion",
    "presentation",
    "tamano",
    "size",
    "volumen",
    "volume",
}
_PRESENTATION_SUFFIX_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|kgs|g|gr|grs|ml|lt|l|oz|u|ud|uds|unidad|unidades)?\s*$",
    re.IGNORECASE,
)


def _presentation_unit(value: str | None) -> str:
    key = normalize_match_key(value or "")
    return {
        "g": "g",
        "gr": "g",
        "grs": "g",
        "kg": "kg",
        "kgs": "kg",
        "ml": "ml",
        "l": "l",
        "lt": "l",
        "oz": "oz",
        "u": "u",
        "ud": "u",
        "uds": "u",
        "unidad": "u",
        "unidades": "u",
    }.get(key, key)


def _presentation_signature(value: object) -> tuple[str, str] | None:
    """Extract a comparable quantity/unit from a presentation or price name."""
    text = repair_mojibake(str(value or "")).casefold().strip()
    match = _PRESENTATION_SUFFIX_RE.search(text)
    if not match:
        return None
    number = _parse_decimal(match.group(1))
    if number is None:
        return None
    return _decimal_text(number), _presentation_unit(match.group(2))


def _presentation_attribute(attributes: dict[str, str]) -> str | None:
    for key, value in attributes.items():
        if normalize_match_key(key) in _PRESENTATION_ATTRIBUTE_KEYS:
            return value
    return None


def _canonical_variant_headers(fieldnames: list[str]) -> list[str]:
    """Return only the Webby variant headers in canonical order."""
    canonical = [field.name for field in fields_for("variantes")]
    return [
        *[name for name in canonical if name in fieldnames],
        *[name for name in fieldnames if name not in canonical and name != "costo"],
    ]


def _price_row_json(price_row: PriceListRow) -> dict[str, object]:
    return {
        "source_file": price_row.source_file,
        "source_row": price_row.source_row,
        "brand": price_row.brand,
        "raw_name": price_row.raw_name,
        "cost_bs": _decimal_text(price_row.cost),
        "professional_price_bs": _decimal_text(price_row.price),
    }


def enrich_variants_csv(
    products_csv: Path,
    variants_csv: Path,
    *,
    profile_path: Path,
    prices_dir: Path,
    product_source_jsonl: Path | None = None,
    variant_source_jsonl: Path | None = None,
    output_products_path: Path | None = None,
    output_variants_path: Path | None = None,
    output_product_source_jsonl: Path | None = None,
    output_variant_source_jsonl: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Apply DOCX variant prices and roll up supported costs to products.

    The parent product keeps its existing rollup while each unambiguous
    presentation receives its exact price. A variant can carry any number of
    attributes in the single ``atributos`` JSON object. Per-presentation costs
    remain in the evidence report because Webby's variant contract exposes
    ``costo`` on the product entity, not on the variant CSV.
    """
    if not products_csv.exists():
        raise FileNotFoundError(f"CSV de productos no encontrado: {products_csv}")
    if not variants_csv.exists():
        raise FileNotFoundError(f"CSV de variantes no encontrado: {variants_csv}")
    profile = EnrichmentProfile.from_file(profile_path)

    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        product_reader = csv.DictReader(handle)
        if not product_reader.fieldnames or not {"sku", "nombre", "marca"}.issubset(
            product_reader.fieldnames
        ):
            raise ValueError("El CSV de productos debe contener `sku`, `nombre` y `marca`.")
        product_headers = list(product_reader.fieldnames)
        product_rows = [dict(row) for row in product_reader]
    with variants_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        variant_reader = csv.DictReader(handle)
        required = {"sku_producto", "sku_variante", "atributos"}
        if not variant_reader.fieldnames or not required.issubset(variant_reader.fieldnames):
            raise ValueError(
                "El CSV de variantes debe contener `sku_producto`, `sku_variante` y `atributos`."
            )
        variant_headers = _canonical_variant_headers(list(variant_reader.fieldnames))
        variant_rows = [dict(row) for row in variant_reader]

    product_evidence = _load_source_records(product_source_jsonl, len(product_rows))
    variant_evidence = _load_source_records(variant_source_jsonl, len(variant_rows))
    product_source_files = _source_files(product_evidence)

    product_by_sku: dict[str, int] = {}
    for index, row in enumerate(product_rows):
        sku = str(row.get("sku") or "").strip()
        if sku:
            if sku in product_by_sku:
                raise ValueError(f"SKU de producto duplicado {sku!r} en la fila {index + 2}.")
            product_by_sku[sku] = index

    variant_attributes: list[dict[str, str]] = []
    variants_by_parent: dict[str, list[int]] = defaultdict(list)
    invalid_attribute_rows: list[dict[str, object]] = []
    for index, row in enumerate(variant_rows):
        try:
            attributes = normalize_attributes(row.get("atributos"))
        except ValueError as exc:
            invalid_attribute_rows.append(
                {"row": index + 2, "reason": str(exc), "value": row.get("atributos", "")}
            )
            attributes = None
        if not attributes:
            invalid_attribute_rows.append(
                {
                    "row": index + 2,
                    "reason": "atributos debe contener al menos un par clave=valor",
                    "value": row.get("atributos", ""),
                }
            )
            attributes = {}
        variant_attributes.append(attributes)
        row["atributos"] = json.dumps(attributes, ensure_ascii=False, sort_keys=True)
        parent = str(row.get("sku_producto") or "").strip()
        if parent:
            variants_by_parent[parent].append(index)

    if invalid_attribute_rows:
        raise ValueError(
            "El CSV de variantes contiene atributos inválidos: "
            + ", ".join(str(item["row"]) for item in invalid_attribute_rows)
        )

    price_rows: list[PriceListRow] = []
    for document in profile.documents:
        price_rows.extend(parse_price_document(prices_dir / document.file, document.brand))

    assignments: dict[int, list[PriceListRow]] = defaultdict(list)
    unmatched_price_rows: list[dict[str, object]] = []
    product_only_price_rows: list[dict[str, object]] = []
    product_only_assignments: dict[int, list[PriceListRow]] = defaultdict(list)
    conflicts: list[dict[str, object]] = []
    for price_row in price_rows:
        raw_key = normalize_match_key(price_row.raw_name)
        catalog_name = profile.aliases.get(price_row.brand, {}).get(raw_key)
        if catalog_name is None:
            exact = [
                index
                for index, row in enumerate(product_rows)
                if normalize_match_key(row.get("nombre", "")) == raw_key
                and str(row.get("marca") or "").strip() == price_row.brand
            ]
            if len(exact) == 1:
                target = exact[0]
                catalog_name = product_rows[target].get("nombre", "")
            else:
                unmatched_price_rows.append(
                    {**_price_row_json(price_row), "reason": "no_alias_or_catalog_record"}
                )
                continue
        catalog_name = profile.name_overrides.get(normalize_match_key(catalog_name), catalog_name)
        target = _target_index(
            product_rows, catalog_name, product_source_files, price_row.brand, profile
        )
        if target is None:
            candidates = [
                index
                for index, row in enumerate(product_rows)
                if normalize_match_key(row.get("nombre", "")) == normalize_match_key(catalog_name)
                and str(row.get("marca") or "").strip() == price_row.brand
            ]
            target = candidates[0] if len(candidates) == 1 else None
        if target is None:
            unmatched_price_rows.append(
                {
                    **_price_row_json(price_row),
                    "catalog_name": catalog_name,
                    "reason": "catalog_target_not_unique_or_missing",
                }
            )
            continue

        parent_sku = str(product_rows[target].get("sku") or "").strip()
        candidates = variants_by_parent.get(parent_sku, [])
        if not candidates:
            product_only_assignments[target].append(price_row)
            product_only_price_rows.append(
                {
                    **_price_row_json(price_row),
                    "catalog_name": product_rows[target].get("nombre", ""),
                    "sku_producto": parent_sku,
                    "reason": "no_variant_row_product_price_retained",
                }
            )
            continue

        if len(candidates) == 1:
            variant_index = candidates[0]
        else:
            source_signature = _presentation_signature(price_row.raw_name)
            matching = []
            for candidate in candidates:
                value = _presentation_attribute(variant_attributes[candidate])
                candidate_signature = _presentation_signature(value)
                if (
                    source_signature is not None
                    and candidate_signature is not None
                    and source_signature[0] == candidate_signature[0]
                    and (
                        not source_signature[1]
                        or not candidate_signature[1]
                        or source_signature[1] == candidate_signature[1]
                    )
                ):
                    matching.append(candidate)
            if len(matching) != 1:
                unmatched_price_rows.append(
                    {
                        **_price_row_json(price_row),
                        "catalog_name": product_rows[target].get("nombre", ""),
                        "sku_producto": parent_sku,
                        "variant_candidates": [
                            variant_rows[index].get("sku_variante", "") for index in candidates
                        ],
                        "reason": (
                            "variant_presentation_missing_or_ambiguous"
                            if source_signature is None
                            else "variant_presentation_not_found_or_ambiguous"
                        ),
                    }
                )
                continue
            variant_index = matching[0]
        assignments[variant_index].append(price_row)

    product_only_rollups: list[dict[str, object]] = []
    for product_index, entries in product_only_assignments.items():
        for field, source_values in (
            ("precio", [entry.price for entry in entries]),
            ("costo", [entry.cost for entry in entries]),
        ):
            values = [value for value in source_values if value is not None]
            if not values or field not in product_headers:
                continue
            authoritative = _decimal_text(min(values))
            existing = str(product_rows[product_index].get(field) or "").strip()
            if existing and existing != authoritative:
                conflicts.append(
                    {
                        "row": product_index + 2,
                        "sku": product_rows[product_index].get("sku", ""),
                        "field": field,
                        "existing": existing,
                        "source": authoritative,
                        "reason": "product_only_price_conflict",
                    }
                )
                continue
            product_rows[product_index][field] = authoritative
            product_only_rollups.append(
                {
                    "sku": product_rows[product_index].get("sku", ""),
                    "field": field,
                    "value": authoritative,
                    "source_rows": [
                        entry.source_row
                        for entry in entries
                        if getattr(entry, "cost" if field == "costo" else "price") is not None
                    ],
                    "method": "minimum_until_variants_are_available",
                }
            )

    applied: list[dict[str, object]] = []
    for variant_index, entries in assignments.items():
        row = variant_rows[variant_index]
        seen_values: dict[str, str] = {}
        for entry in entries:
            for field, value in (("precio", entry.price), ("costo", entry.cost)):
                if value is None:
                    continue
                text = _decimal_text(value)
                existing = str(row.get(field) or "").strip()
                if existing and existing != text:
                    conflicts.append(
                        {
                            "row": variant_index + 2,
                            "sku_variante": row.get("sku_variante", ""),
                            "field": field,
                            "existing": existing,
                            "source": text,
                            "source_file": entry.source_file,
                            "source_row": entry.source_row,
                        }
                    )
                    continue
                previous = seen_values.get(field)
                if previous and previous != text:
                    conflicts.append(
                        {
                            "row": variant_index + 2,
                            "sku_variante": row.get("sku_variante", ""),
                            "field": field,
                            "existing": previous,
                            "source": text,
                            "source_file": entry.source_file,
                            "source_row": entry.source_row,
                        }
                    )
                    continue
                row[field] = text
                seen_values[field] = text
            applied.append(
                {
                    "variant_csv_row": variant_index + 2,
                    "sku_producto": row.get("sku_producto", ""),
                    "sku_variante": row.get("sku_variante", ""),
                    "atributos": json.loads(row["atributos"]),
                    **_price_row_json(entry),
                }
            )

    if conflicts:
        raise ValueError(
            "Se detectaron conflictos entre valores existentes y la lista DOCX: "
            + ", ".join(str(item["row"]) for item in conflicts)
        )

    parent_rollups: list[dict[str, object]] = []
    product_assignments: dict[str, set[int]] = defaultdict(set)
    for variant_index in assignments:
        parent = str(variant_rows[variant_index].get("sku_producto") or "").strip()
        if parent in product_by_sku:
            product_assignments[parent].add(variant_index)
    for parent_sku, variant_indexes in product_assignments.items():
        product_index = product_by_sku[parent_sku]
        for field in ("precio", "costo"):
            values = [
                _parse_decimal(str(variant_rows[index].get(field) or ""))
                for index in variant_indexes
            ]
            values = [value for value in values if value is not None]
            if not values or field not in product_headers:
                continue
            rollup = _decimal_text(min(values))
            before = str(product_rows[product_index].get(field) or "").strip()
            if not before or before != rollup:
                product_rows[product_index][field] = rollup
            parent_rollups.append(
                {
                    "sku_producto": parent_sku,
                    "field": field,
                    "value": rollup,
                    "previous": before,
                    "method": "minimum_of_authoritative_variant_values",
                }
            )

    for product_index, record in enumerate(product_evidence):
        if record is None:
            continue
        data = record.setdefault("data", {})
        if isinstance(data, dict):
            for field in product_headers:
                raw_value = product_rows[product_index].get(field, "")
                if field == "atributos":
                    attributes = normalize_attributes(raw_value)
                    if attributes:
                        data[field] = attributes
                    else:
                        data.pop(field, None)
                elif str(raw_value or "").strip():
                    data[field] = raw_value
                else:
                    data.pop(field, None)
        parent_sku = str(product_rows[product_index].get("sku") or "").strip()
        if parent_sku in product_assignments:
            enrichment = record.setdefault("enrichment", {})
            if isinstance(enrichment, dict):
                enrichment["variant_price_rollup"] = {
                    "method": "minimum_of_authoritative_variant_values",
                    "fields": [
                        item for item in parent_rollups if item["sku_producto"] == parent_sku
                    ],
                }
        elif product_index in product_only_assignments:
            enrichment = record.setdefault("enrichment", {})
            if isinstance(enrichment, dict):
                enrichment["product_price_enrichment"] = {
                    "method": "docx_price_list_product_match",
                    "fields": [item for item in product_only_rollups if item["sku"] == parent_sku],
                }

    variant_pairs = sorted(
        zip(variant_rows, variant_evidence, strict=True),
        key=lambda pair: (
            str(pair[0].get("sku_producto") or ""),
            str(pair[0].get("sku_variante") or ""),
            pair[0].get("atributos", ""),
        ),
    )
    variant_rows = [row for row, _ in variant_pairs]
    variant_evidence = [record for _, record in variant_pairs]

    for record in variant_evidence:
        if record is None:
            continue
        data = record.setdefault("data", {})
        if not isinstance(data, dict):
            continue
        sku = str(data.get("sku_variante") or "").strip()
        matching = next(
            (row for row in variant_rows if str(row.get("sku_variante") or "").strip() == sku),
            None,
        )
        if matching is None:
            continue
        for field in variant_headers:
            raw_value = matching.get(field, "")
            if field == "atributos":
                data[field] = normalize_attributes(raw_value) or {}
            elif str(raw_value or "").strip():
                data[field] = raw_value
            else:
                data.pop(field, None)
        applied_for_record = next(
            (item for item in applied if item["sku_variante"] == matching.get("sku_variante", "")),
            None,
        )
        if applied_for_record:
            record["price_evidence"] = {
                "source_file": applied_for_record["source_file"],
                "source_row": applied_for_record["source_row"],
                "brand": applied_for_record["brand"],
                "raw_name": applied_for_record["raw_name"],
                "cost_bs": applied_for_record["cost_bs"],
                "professional_price_bs": applied_for_record["professional_price_bs"],
            }
            enrichment = record.setdefault("enrichment", {})
            if isinstance(enrichment, dict):
                enrichment["variant_price_enrichment"] = {
                    "method": "docx_price_list_variant_match",
                    "source_file": applied_for_record["source_file"],
                    "source_row": applied_for_record["source_row"],
                    "attributes": applied_for_record["atributos"],
                }

    output_products = output_products_path or products_csv
    output_variants = output_variants_path or variants_csv
    _write_csv_rows(output_products, product_headers, product_rows)
    _write_csv_rows(output_variants, variant_headers, variant_rows)
    if output_product_source_jsonl is not None:
        records = [record for record in product_evidence if record is not None]
        if len(records) != len(product_evidence):
            raise ValueError("El JSONL de productos contiene registros vacíos.")
        _write_source_records(output_product_source_jsonl, records)
    if output_variant_source_jsonl is not None:
        records = [record for record in variant_evidence if record is not None]
        if len(records) != len(variant_evidence):
            raise ValueError("El JSONL de variantes contiene registros vacíos.")
        _write_source_records(output_variant_source_jsonl, records)

    attribute_counts = Counter(
        len(attributes)
        for attributes in (normalize_attributes(row["atributos"]) for row in variant_rows)
    )
    missing_product_skus = [
        {"row": index + 2, "nombre": row.get("nombre", "")}
        for index, row in enumerate(product_rows)
        if not str(row.get("sku") or "").strip()
    ]
    missing_variant_parents = [
        {"row": index + 2, "sku_variante": row.get("sku_variante", "")}
        for index, row in enumerate(variant_rows)
        if not str(row.get("sku_producto") or "").strip()
    ]
    unknown_variant_parents = [
        {
            "row": index + 2,
            "sku_producto": row.get("sku_producto", ""),
            "sku_variante": row.get("sku_variante", ""),
        }
        for index, row in enumerate(variant_rows)
        if str(row.get("sku_producto") or "").strip()
        and str(row.get("sku_producto") or "").strip() not in product_by_sku
    ]
    missing_variant_skus = [
        {"row": index + 2, "sku_producto": row.get("sku_producto", "")}
        for index, row in enumerate(variant_rows)
        if not str(row.get("sku_variante") or "").strip()
    ]
    duplicate_variant_combinations: list[dict[str, object]] = []
    seen_combinations: dict[str, int] = {}
    for index, row in enumerate(variant_rows):
        identity = f"{row.get('sku_producto', '')}|{row.get('atributos', '')}"
        if identity in seen_combinations:
            duplicate_variant_combinations.append(
                {"row": index + 2, "previous_row": seen_combinations[identity]}
            )
        else:
            seen_combinations[identity] = index + 2

    structural_errors = (
        missing_product_skus
        + missing_variant_parents
        + unknown_variant_parents
        + missing_variant_skus
        + duplicate_variant_combinations
    )
    if report_path is None:
        report_path = output_variants.parent.parent / (
            f"{output_variants.stem}__variant-price-enrichment.json"
        )
    report: dict[str, object] = {
        "source": profile.source,
        "input_products_csv": str(products_csv),
        "input_variants_csv": str(variants_csv),
        "profile": str(profile_path),
        "products_csv": str(output_products),
        "variants_csv": str(output_variants),
        "artifacts": {
            "products_csv": {"path": str(output_products), "sha256": _sha256(output_products)},
            "variants_csv": {"path": str(output_variants), "sha256": _sha256(output_variants)},
        },
        "price_documents": [document.__dict__ for document in profile.documents],
        "products_total": len(product_rows),
        "variants_total": len(variant_rows),
        "products_with_price": sum(bool(row.get("precio", "").strip()) for row in product_rows),
        "products_with_cost": sum(bool(row.get("costo", "").strip()) for row in product_rows),
        "variants_with_price": sum(bool(row.get("precio", "").strip()) for row in variant_rows),
        "variants_with_cost_evidence": sum(
            bool(row.get("costo", "").strip()) for row in variant_rows
        ),
        "variant_cost_in_csv": "costo" in variant_headers,
        "matched_price_rows": len(applied),
        "product_only_price_rows": product_only_price_rows,
        "unmatched_price_rows": unmatched_price_rows,
        "matches": applied,
        "product_only_rollups": product_only_rollups,
        "parent_rollups": parent_rollups,
        "attributes": {
            "transport": "single JSON object column atributos",
            "rows_by_attribute_count": dict(sorted(attribute_counts.items())),
            "multiple_attribute_rows": sum(
                count for length, count in attribute_counts.items() if length > 1
            ),
            "max_attributes": max(attribute_counts, default=0),
        },
        "optional_fields_missing": {
            "products_cost": [
                {"row": index + 2, "sku": row.get("sku", ""), "nombre": row.get("nombre", "")}
                for index, row in enumerate(product_rows)
                if not str(row.get("costo") or "").strip()
            ],
            "variants_price": [
                {
                    "row": index + 2,
                    "sku_producto": row.get("sku_producto", ""),
                    "sku_variante": row.get("sku_variante", ""),
                }
                for index, row in enumerate(variant_rows)
                if not str(row.get("precio") or "").strip()
            ],
        },
        "quality": {
            "invalid_attribute_rows": invalid_attribute_rows,
            "duplicate_variant_combinations": duplicate_variant_combinations,
            "unknown_variant_parents": unknown_variant_parents,
            "conflicts": conflicts,
            "structural_errors": structural_errors,
        },
        "evidence_alignment": {
            "products_csv_rows": len(product_rows),
            "products_jsonl_rows": len(product_evidence) if product_source_jsonl else None,
            "variants_csv_rows": len(variant_rows),
            "variants_jsonl_rows": len(variant_evidence) if variant_source_jsonl else None,
            "errors": 0,
        },
        "decisions": {
            "cost_field": "Precio POR unid. bs",
            "price_field": "Precio venta A profesional",
            "variant_cost_source": "DOCX price-list per-unit cost retained in evidence and parent rollup",
            "variant_cost_storage": "evidence_only; product.costo is the Webby target field",
            "attributes_contract": "all attributes remain in one non-empty JSON object",
            "ambiguous_presentation": "left unresolved; never copied to a guessed variant",
            "missing_cost": "left blank when no authoritative source exists",
            "parent_values": "minimum of authoritative variant values when available",
        },
        "ready_for_ingestion": not structural_errors and not conflicts,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def complete_product_categories(
    products_csv: Path,
    *,
    profile_path: Path,
    source_jsonl: Path | None = None,
    output_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Fill missing categories using tenant-profiled, name-based rules.

    Existing non-empty categories are preserved unless an explicit override is
    present. Every inferred value is recorded in source evidence so a later
    review can replace it without losing provenance.
    """
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"El perfil de enriquecimiento debe ser un objeto YAML: {profile_path}")
    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"nombre", "marca", "categoria"}.issubset(
            reader.fieldnames
        ):
            raise ValueError("El CSV debe contener `nombre`, `marca` y `categoria`.")
        headers = list(reader.fieldnames)
        rows = [dict(row) for row in reader]
    source_records = _load_source_records(source_jsonl, len(rows))

    overrides = {
        normalize_match_key(str(name)): str(category)
        for name, category in (raw.get("category_overrides") or {}).items()
    }
    rules: list[tuple[str, str]] = []
    for item in raw.get("category_rules", []) or []:
        if not isinstance(item, dict) or not item.get("contains") or not item.get("category"):
            continue
        rules.append((normalize_match_key(str(item["contains"])), str(item["category"])))
    defaults = {
        str(brand): str(category)
        for brand, category in (raw.get("category_defaults") or {}).items()
    }
    global_default = str(raw.get("category_default") or "Línea facial > Tratamientos")

    assignments: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for index, row in enumerate(rows):
        name = str(row.get("nombre") or "")
        brand = str(row.get("marca") or "")
        name_key = normalize_match_key(name)
        before = str(row.get("categoria") or "").strip()
        category = before
        method = "existing"
        if name_key in overrides:
            category = overrides[name_key]
            method = "profile_override"
        elif not category:
            for needle, candidate in rules:
                if needle and needle in name_key:
                    category = candidate
                    method = "profile_rule"
                    break
            if not category:
                category = defaults.get(brand, global_default)
                method = "profile_default"
        row["categoria"] = category
        counts[category] += 1
        assignments.append(
            {
                "row": index + 2,
                "sku": row.get("sku", ""),
                "brand": brand,
                "name": name,
                "before": before,
                "category": category,
                "method": method,
            }
        )
        evidence = source_records[index]
        if evidence is not None:
            data = evidence.setdefault("data", {})
            if isinstance(data, dict):
                data["categoria"] = category
            enrichment = evidence.setdefault("enrichment", {})
            if isinstance(enrichment, dict):
                enrichment["category_assignment"] = {
                    "category": category,
                    "method": method,
                    "previous": before,
                }

    destination = output_path or products_csv
    _write_csv_rows(destination, headers, rows)
    source_jsonl_written = False
    if source_jsonl is not None and destination == products_csv:
        records = [record for record in source_records if record is not None]
        if len(records) != len(source_records):
            raise ValueError("El JSONL de evidencia contiene registros vacíos.")
        _write_source_records(source_jsonl, records)
        source_jsonl_written = True
    if report_path is None:
        report_path = (
            products_csv.parent.parent
            / "reports"
            / (f"{products_csv.stem}__category-completion.json")
        )
    report = {
        "input_csv": str(products_csv),
        "output_csv": str(destination),
        "profile": str(profile_path),
        "rows_total": len(rows),
        "rows_with_category": sum(bool(row.get("categoria", "").strip()) for row in rows),
        "rows_inferred": sum(item["method"] != "existing" for item in assignments),
        "category_counts": dict(counts),
        "assignments": assignments,
        "source_jsonl_written": source_jsonl_written,
        "decision": "profiled_category_completion_with_evidence",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


_PRODUCT_SKU_RE = re.compile(r"^(?P<prefix>[A-Z0-9]+)-(?P<number>\d{4})$")
_VARIANT_SKU_RE = re.compile(r"^(?P<product>.+)-V(?P<number>\d{2})$")


def _sku_stable_text(value: object) -> str:
    """Normalize source locators without dropping meaningful file distinctions."""
    text = repair_mojibake(str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _evidence_source(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record or not isinstance(record.get("source"), dict):
        return {}
    return record["source"]


def _evidence_source_file(record: dict[str, Any] | None) -> str:
    return _sku_stable_text(_evidence_source(record).get("source_file"))


def _evidence_page(record: dict[str, Any] | None) -> object:
    source = _evidence_source(record)
    if source.get("page_number") is not None:
        return source.get("page_number")
    field_evidence = source.get("field_evidence")
    if isinstance(field_evidence, dict):
        name_evidence = field_evidence.get("nombre")
        if isinstance(name_evidence, dict) and name_evidence.get("page_number") is not None:
            return name_evidence.get("page_number")
    price_evidence = source.get("price_evidence")
    if isinstance(price_evidence, dict):
        return price_evidence.get("page_number")
    return None


def _evidence_name(record: dict[str, Any] | None) -> str:
    source = _evidence_source(record)
    field_evidence = source.get("field_evidence")
    if isinstance(field_evidence, dict):
        name_evidence = field_evidence.get("nombre")
        if isinstance(name_evidence, dict) and name_evidence.get("text"):
            return str(name_evidence["text"])
    data = record.get("data") if record else None
    if isinstance(data, dict):
        return str(data.get("nombre") or "")
    return ""


def _evidence_bbox(record: dict[str, Any] | None) -> str:
    source = _evidence_source(record)
    field_evidence = source.get("field_evidence")
    value: object = None
    if isinstance(field_evidence, dict):
        name_evidence = field_evidence.get("nombre")
        if isinstance(name_evidence, dict):
            value = name_evidence.get("bbox")
    if value is None:
        value = source.get("bbox")
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value or "").strip()


def _product_sku_identity_key(row: dict[str, str], record: dict[str, Any] | None) -> str:
    """Build a stable key from source evidence, with a catalog fallback."""
    source_file = _evidence_source_file(record)
    page = _evidence_page(record)
    name_key = normalize_match_key(row.get("nombre", ""))
    bbox = _evidence_bbox(record)
    if source_file and page is not None and bbox:
        return f"source|{source_file}|page|{page}|name|{name_key}|bbox|{bbox}"
    row_number = record.get("row_number") if record else None
    if source_file and row_number is not None:
        return f"source|{source_file}|row|{row_number}|name|{name_key}"
    brand_key = normalize_match_key(row.get("marca", ""))
    return f"catalog|brand|{brand_key}|name|{name_key}"


def _variant_sku_identity_key(
    product_key: str,
    row: dict[str, str],
    record: dict[str, Any],
) -> str:
    attributes = _variant_attributes(row.get("atributos", ""))
    attributes_key = json.dumps(attributes, ensure_ascii=False, sort_keys=True)
    source_file = _evidence_source_file(record)
    page = _evidence_page(record)
    row_number = record.get("row_number")
    return (
        f"{product_key}|variant|source|{source_file}|page|{page}|row|{row_number}"
        f"|attributes|{attributes_key}"
    )


def _read_sku_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"products": {}, "variants": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el mapa persistente de SKU {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"El mapa persistente de SKU debe ser un objeto JSON: {path}")
    normalized: dict[str, Any] = {
        key: raw.get(key) for key in raw if key not in {"products", "variants"}
    }
    for section in ("products", "variants"):
        entries = raw.get(section) or {}
        if isinstance(entries, list):
            entries = {
                str(item.get("identity_key")): dict(item)
                for item in entries
                if isinstance(item, dict) and item.get("identity_key")
            }
        if not isinstance(entries, dict):
            raise ValueError(f"La sección `{section}` del mapa de SKU no es un objeto: {path}")
        normalized[section] = {
            str(key): dict(value) for key, value in entries.items() if isinstance(value, dict)
        }
    return normalized


def _write_sku_map(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _variant_attributes(value: object) -> dict[str, str]:
    """Return the canonical multi-attribute object used by SKU identity."""
    try:
        attributes = normalize_attributes(value)
    except ValueError as exc:
        raise ValueError(f"Los atributos de variante no son válidos: {exc}") from exc
    if not attributes:
        raise ValueError("Los atributos de variante deben ser un objeto no vacío.")
    return attributes


def _variant_attributes_legacy(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        attributes = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("La variante no tiene atributos.")
        try:
            attributes = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Los atributos de variante no son JSON válido.") from exc
    if not isinstance(attributes, dict) or not attributes:
        raise ValueError("Los atributos de variante deben ser un objeto no vacío.")
    return attributes


def _update_sku_evidence(
    record: dict[str, Any],
    *,
    sku: str,
    entity: str,
    identity_key: str,
    parent_sku: str | None = None,
    presentation: str | None = None,
) -> None:
    data = record.setdefault("data", {})
    if not isinstance(data, dict):
        data = {}
        record["data"] = data
    if entity == "productos":
        data["sku"] = sku
    else:
        data["sku_producto"] = parent_sku or ""
        data["sku_variante"] = sku
    enrichment = record.setdefault("enrichment", {})
    if not isinstance(enrichment, dict):
        enrichment = {}
        record["enrichment"] = enrichment
    assignment: dict[str, object] = {
        "method": "brand_prefix_counter",
        "pattern": "{prefix}-{number:04d}"
        if entity == "productos"
        else "{product_sku}-V{number:02d}",
        "identity_key": identity_key,
        "sku": sku,
    }
    if parent_sku:
        assignment["sku_producto"] = parent_sku
    if presentation:
        assignment["presentation"] = presentation
    enrichment["sku_assignment"] = assignment


def assign_catalog_skus(
    products_csv: Path,
    variants_csv: Path,
    *,
    profile_path: Path,
    product_source_jsonl: Path | None = None,
    variant_source_jsonl: Path | None = None,
    sku_map_path: Path,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Assign stable brand-prefixed SKUs and link every variant to its parent."""
    if not products_csv.exists():
        raise FileNotFoundError(f"CSV de productos no encontrado: {products_csv}")
    if not variants_csv.exists():
        raise FileNotFoundError(f"CSV de variantes no encontrado: {variants_csv}")
    profile = SkuProfile.from_file(profile_path)
    sku_map = _read_sku_map(sku_map_path)
    product_map = sku_map.setdefault("products", {})
    variant_map = sku_map.setdefault("variants", {})

    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        product_reader = csv.DictReader(handle)
        if not product_reader.fieldnames or not {"sku", "nombre", "marca"}.issubset(
            product_reader.fieldnames
        ):
            raise ValueError("El CSV de productos debe contener `sku`, `nombre` y `marca`.")
        product_headers = list(product_reader.fieldnames)
        product_rows = [dict(row) for row in product_reader]
    product_evidence = _load_source_records(product_source_jsonl, len(product_rows))
    if any(record is None for record in product_evidence):
        raise ValueError("El JSONL de productos debe contener evidencia para cada fila.")

    product_identity_keys: list[str] = []
    product_skus: dict[int, str] = {}
    used_product_numbers: dict[str, set[int]] = defaultdict(set)
    for entry in product_map.values():
        if not isinstance(entry, dict):
            continue
        match = _PRODUCT_SKU_RE.fullmatch(str(entry.get("sku") or ""))
        if match:
            used_product_numbers[match.group("prefix")].add(int(match.group("number")))
    seen_product_identity_keys: set[str] = set()
    seen_product_skus: dict[str, int] = {}
    for index, row in enumerate(product_rows):
        brand = str(row.get("marca") or "").strip()
        prefix = profile.brand_prefixes.get(brand)
        if not prefix:
            raise ValueError(f"No hay prefijo de SKU configurado para la marca {brand!r}.")
        record = product_evidence[index]
        if record is None:
            raise ValueError("Falta evidencia de producto para asignar SKU.")
        identity_key = _product_sku_identity_key(row, record)
        if identity_key in seen_product_identity_keys:
            raise ValueError(f"La identidad de producto se repite y no es segura: {identity_key}")
        seen_product_identity_keys.add(identity_key)
        product_identity_keys.append(identity_key)
        mapped = product_map.get(identity_key, {})
        mapped_sku = str(mapped.get("sku") or "") if isinstance(mapped, dict) else ""
        current_sku = str(row.get("sku") or "").strip()
        if mapped_sku and current_sku and mapped_sku != current_sku:
            raise ValueError(
                f"El CSV y el mapa discrepan para {row.get('nombre', '')!r}: "
                f"{current_sku!r} != {mapped_sku!r}."
            )
        sku = current_sku or mapped_sku
        if not sku:
            number = 1
            while number in used_product_numbers[prefix]:
                number += 1
            sku = profile.product_pattern.format(prefix=prefix, number=number)
            used_product_numbers[prefix].add(number)
        match = _PRODUCT_SKU_RE.fullmatch(sku)
        if not match or match.group("prefix") != prefix:
            raise ValueError(
                f"SKU de producto inválido para {brand!r}: {sku!r}; se esperaba {prefix}-0000."
            )
        if sku in seen_product_skus:
            raise ValueError(
                f"SKU de producto duplicado {sku!r} en las filas "
                f"{seen_product_skus[sku] + 2} y {index + 2}."
            )
        seen_product_skus[sku] = index
        product_skus[index] = sku
        row["sku"] = sku
        product_map[identity_key] = {
            "sku": sku,
            "brand": brand,
            "name": row.get("nombre", ""),
            "source_file": _evidence_source(record).get("source_file", ""),
            "source_page": _evidence_page(record),
        }
        _update_sku_evidence(
            record,
            sku=sku,
            entity="productos",
            identity_key=identity_key,
        )

    with variants_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        variant_reader = csv.DictReader(handle)
        required_variant_headers = {"sku_producto", "sku_variante", "atributos"}
        if not variant_reader.fieldnames or not required_variant_headers.issubset(
            variant_reader.fieldnames
        ):
            raise ValueError(
                "El CSV de variantes debe contener `sku_producto`, `sku_variante` y `atributos`."
            )
        variant_headers = list(variant_reader.fieldnames)
        variant_rows = [dict(row) for row in variant_reader]
    variant_evidence = _load_source_records(variant_source_jsonl, len(variant_rows))
    if variant_rows and any(record is None for record in variant_evidence):
        raise ValueError("El JSONL de variantes debe contener evidencia para cada fila.")

    by_source_name: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_source_page: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, (row, record) in enumerate(zip(product_rows, product_evidence, strict=True)):
        if record is None:
            continue
        source_file = _evidence_source_file(record)
        name_key = normalize_match_key(_evidence_name(record) or row.get("nombre", ""))
        page = _evidence_page(record)
        if source_file and name_key:
            by_source_name[(source_file, name_key)].append(index)
        if source_file and page is not None:
            by_source_page[(source_file, str(page))].append(index)

    used_variant_numbers: dict[str, set[int]] = defaultdict(set)
    for entry in variant_map.values():
        if not isinstance(entry, dict):
            continue
        parent = str(entry.get("sku_producto") or "")
        match = _VARIANT_SKU_RE.fullmatch(str(entry.get("sku") or ""))
        if parent and match and match.group("product") == parent:
            used_variant_numbers[parent].add(int(match.group("number")))
    seen_variant_identity_keys: set[str] = set()
    seen_variant_skus: dict[str, int] = {}
    variants_by_product: dict[int, int] = defaultdict(int)
    promoted_to_variable: set[int] = set()
    for index, (row, record) in enumerate(zip(variant_rows, variant_evidence, strict=True)):
        if record is None:
            raise ValueError("Falta evidencia de variante para enlazarla con su producto.")
        attributes = _variant_attributes(row.get("atributos", ""))
        presentation = str(
            attributes.get("presentacion")
            or attributes.get("presentation")
            or json.dumps(attributes, ensure_ascii=False, sort_keys=True)
        )
        source_file = _evidence_source_file(record)
        source_name = normalize_match_key(_evidence_name(record))
        page = _evidence_page(record)
        candidates = by_source_name.get((source_file, source_name), []) if source_name else []
        if len(candidates) != 1 and source_file and page is not None:
            page_candidates = by_source_page.get((source_file, str(page)), [])
            if len(page_candidates) == 1:
                candidates = page_candidates
        if len(candidates) != 1:
            descriptor = _evidence_name(record) or f"página {page}"
            raise ValueError(
                f"No se pudo enlazar la variante {descriptor!r} con un único producto "
                f"(fuente={source_file!r}, página={page!r})."
            )
        product_index = candidates[0]
        parent_sku = product_skus[product_index]
        product_type = str(product_rows[product_index].get("tipo") or "simple").strip()
        if product_type not in {"simple", "variable"}:
            raise ValueError(
                f"La variante {index + 2} apunta al producto no variable {parent_sku!r}."
            )
        if product_type == "simple":
            product_rows[product_index]["tipo"] = "variable"
            promoted_to_variable.add(product_index)
            product_record = product_evidence[product_index]
            if product_record is not None:
                product_data = product_record.setdefault("data", {})
                if isinstance(product_data, dict):
                    product_data["tipo"] = "variable"
        current_parent = str(row.get("sku_producto") or "").strip()
        if current_parent and current_parent != parent_sku:
            raise ValueError(
                f"La variante {index + 2} tiene padre {current_parent!r}, "
                f"pero la evidencia indica {parent_sku!r}."
            )
        row["sku_producto"] = parent_sku
        identity_key = _variant_sku_identity_key(product_identity_keys[product_index], row, record)
        if identity_key in seen_variant_identity_keys:
            raise ValueError(f"La identidad de variante se repite: {identity_key}")
        seen_variant_identity_keys.add(identity_key)
        mapped = variant_map.get(identity_key, {})
        mapped_sku = str(mapped.get("sku") or "") if isinstance(mapped, dict) else ""
        current_sku = str(row.get("sku_variante") or "").strip()
        if mapped_sku and current_sku and mapped_sku != current_sku:
            raise ValueError(
                f"El CSV y el mapa discrepan para la variante {index + 2}: "
                f"{current_sku!r} != {mapped_sku!r}."
            )
        sku = current_sku or mapped_sku
        if not sku:
            number = 1
            while number in used_variant_numbers[parent_sku]:
                number += 1
            sku = profile.variant_pattern.format(product_sku=parent_sku, number=number)
            used_variant_numbers[parent_sku].add(number)
        match = _VARIANT_SKU_RE.fullmatch(sku)
        if not match or match.group("product") != parent_sku:
            raise ValueError(f"SKU de variante inválido {sku!r}; se esperaba {parent_sku}-V00.")
        if sku in seen_variant_skus:
            raise ValueError(
                f"SKU de variante duplicado {sku!r} en las filas "
                f"{seen_variant_skus[sku] + 2} y {index + 2}."
            )
        seen_variant_skus[sku] = index
        row["sku_variante"] = sku
        variants_by_product[product_index] += 1
        variant_map[identity_key] = {
            "sku": sku,
            "sku_producto": parent_sku,
            "presentation": presentation,
            "source_file": _evidence_source(record).get("source_file", ""),
            "source_page": page,
        }
        _update_sku_evidence(
            record,
            sku=sku,
            entity="variantes",
            identity_key=identity_key,
            parent_sku=parent_sku,
            presentation=presentation,
        )

    missing_variant_products = [
        product_skus[index]
        for index, row in enumerate(product_rows)
        if str(row.get("tipo") or "simple").strip() == "variable"
        and variants_by_product.get(index, 0) == 0
    ]
    if missing_variant_products:
        raise ValueError(
            "Productos variables sin ninguna variante: " + ", ".join(missing_variant_products)
        )
    all_product_skus = set(product_skus.values())
    all_variant_skus = set(seen_variant_skus)
    if all_product_skus & all_variant_skus:
        raise ValueError("Un SKU de producto también aparece como SKU de variante.")

    variant_pairs = sorted(
        zip(variant_rows, variant_evidence, strict=True),
        key=lambda pair: (pair[0].get("sku_producto", ""), pair[0].get("sku_variante", "")),
    )
    variant_rows = [row for row, _ in variant_pairs]
    variant_evidence = [record for _, record in variant_pairs]
    _write_csv_rows(products_csv, product_headers, product_rows)
    _write_csv_rows(variants_csv, variant_headers, variant_rows)
    if product_source_jsonl is not None:
        _write_source_records(
            product_source_jsonl,
            [record for record in product_evidence if record is not None],
        )
    if variant_source_jsonl is not None:
        _write_source_records(
            variant_source_jsonl,
            [record for record in variant_evidence if record is not None],
        )

    sku_map.update(
        {
            "schema": "piel-radiante/catalog-sku-map/v1",
            "source": profile.source,
            "product_pattern": profile.product_pattern,
            "variant_pattern": profile.variant_pattern,
            "brand_prefixes": profile.brand_prefixes,
            "products": product_map,
            "variants": variant_map,
        }
    )
    _write_sku_map(sku_map_path, sku_map)

    if report_path is None:
        report_path = (
            products_csv.parent.parent / "reports" / (f"{products_csv.stem}__sku-assignment.json")
        )
    report: dict[str, object] = {
        "source": profile.source,
        "products_csv": str(products_csv),
        "variants_csv": str(variants_csv),
        "sku_map": str(sku_map_path),
        "patterns": {
            "product": profile.product_pattern,
            "variant": profile.variant_pattern,
            "brand_prefixes": profile.brand_prefixes,
        },
        "products_total": len(product_rows),
        "products_with_sku": sum(bool(row.get("sku", "").strip()) for row in product_rows),
        "variants_total": len(variant_rows),
        "variants_with_sku": sum(bool(row.get("sku_variante", "").strip()) for row in variant_rows),
        "products_promoted_to_variable": len(promoted_to_variable),
        "brand_counts": {
            brand: sum(1 for row in product_rows if row.get("marca") == brand)
            for brand in profile.brand_prefixes
        },
        "evidence_alignment": {
            "products_csv_rows": len(product_rows),
            "products_jsonl_rows": len(product_evidence) if product_source_jsonl else None,
            "variants_csv_rows": len(variant_rows),
            "variants_jsonl_rows": len(variant_evidence) if variant_source_jsonl else None,
            "errors": 0,
        },
        "decisions": {
            "product_sku": "brand_prefix_plus_four_digit_counter",
            "variant_sku": "parent_product_sku_plus_two_digit_variant_counter",
            "presentation_in_sku": False,
            "presentation_source_of_truth": "atributos.presentacion",
            "stable_identity": "source_locator_and_normalized_name",
            "single_presentation_products": "promote_to_variable_when_variant_row_exists",
        },
        "ready_for_ingestion": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
