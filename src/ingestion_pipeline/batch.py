"""Batch orchestration for mixed visual catalog sources.

The batch runner keeps the document extractor deterministic and source-aware,
then writes one reviewable product file and one reviewable variant file. It is
deliberately a local preparation step: blank SKUs and unresolved variants are
preserved as candidates, but the batch is blocked from publication until a
reviewer resolves them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import yaml

from ingestion_pipeline.config import DocumentConfig
from ingestion_pipeline.documents.catalog import CatalogExtraction, extract_catalog
from ingestion_pipeline.documents.extractors import (
    ExtractionResult,
    ExtractionSettings,
    extract_document,
)
from ingestion_pipeline.documents.markdown import (
    extraction_to_markdown,
    page_markdown_lines,
    repair_mojibake,
)
from ingestion_pipeline.domain.catalog import fields_for
from ingestion_pipeline.domain.models import (
    NormalizedRecord,
    RunManifest,
    ValidationIssue,
    json_dumps,
)
from ingestion_pipeline.storage.artifacts import ArtifactStore
from ingestion_pipeline.validation.rules import validate_records

SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


@dataclass(frozen=True)
class BatchRules:
    """Source-specific exceptions for a repeatable tenant batch."""

    source: str
    category_files: tuple[str, ...] = ()
    keep_bottom_only: tuple[str, ...] = ()
    excluded_files: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: Path | None, *, default_source: str) -> BatchRules:
        if path is None:
            return cls(source=default_source)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"La configuración batch {path} debe ser un objeto YAML.")

        def names(key: str) -> tuple[str, ...]:
            values = raw.get(key, []) or []
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"`{key}` debe ser una lista de nombres de archivo.")
            return tuple(value.strip() for value in values if value.strip())

        return cls(
            source=str(raw.get("source", default_source)),
            category_files=names("category_files"),
            keep_bottom_only=names("keep_bottom_only"),
            excluded_files=names("excluded_files"),
        )


@dataclass(frozen=True)
class _SourceResult:
    path: Path
    relative_path: str
    role: str
    extraction: ExtractionResult | None = None
    catalog: CatalogExtraction | None = None
    error: str | None = None
    filter_warning: str | None = None


def _source_name_matches(path: Path, relative_path: str, configured: tuple[str, ...]) -> bool:
    return path.name in configured or relative_path in configured


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "source"


def _aggregate_sha256(paths: list[tuple[str, Path]], store: ArtifactStore) -> str:
    digest = hashlib.sha256()
    for relative_path, path in paths:
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(store.sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fold_text(value: str) -> str:
    """Make OCR text comparable across proper UTF-8 and common mojibake."""
    replacements = {
        "Ã¡": "á",
        "Ã©": "é",
        "Ã­": "í",
        "Ã³": "ó",
        "Ãº": "ú",
        "Ã±": "ñ",
        "Ã¼": "ü",
        "Ã": "Á",
        "Ã‰": "É",
        "Ã": "Í",
        "Ã“": "Ó",
        "Ãš": "Ú",
        "Ã‘": "Ñ",
        "Ãœ": "Ü",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


_CATEGORY_LABELS = {
    "geles corporales": "Geles corporales",
    "linea masajes": "Línea masajes",
    "oleos": "Óleos",
    "cremas corporales": "Cremas corporales",
    "emulsiones corporales": "Emulsiones corporales",
    "linea facial": "Línea facial",
    "linea para manos y pies": "Línea para manos y pies",
    "linea de booster": "Línea de booster",
}
_CATEGORY_SUBLABELS = {
    "limpieza": "Limpieza",
    "lociones": "Lociones",
    "cremas": "Cremas",
    "mascaras": "Máscaras",
}


def _category_entries(extraction: ExtractionResult) -> list[dict[str, object]]:
    """Parse the explicitly identified index image without inventing categories."""
    entries: list[dict[str, object]] = []
    for page in extraction.pages:
        lines: list[tuple[str, object, object]] = []
        for markdown_line in page_markdown_lines(page):
            raw_line = markdown_line.text
            text = re.sub(r"\s+", " ", raw_line).strip(" -•\t")
            if not text:
                continue
            lines.append((text, markdown_line.block_id, markdown_line.bbox))
        columns: dict[int, list[tuple[str, object, object]]] = {}
        midpoint = page.width / 2
        for line in lines:
            bbox = line[2]
            x = float(bbox[0]) if bbox else 0.0
            columns.setdefault(0 if x < midpoint else 1, []).append(line)
        for column_lines in columns.values():
            main_category: str | None = None
            subcategory: str | None = None
            column_lines.sort(key=lambda line: float(line[2][1]) if line[2] else 0.0)
            for text, block_id, bbox in column_lines:
                folded = _fold_text(text)
                if folded in _CATEGORY_LABELS:
                    main_category = _CATEGORY_LABELS[folded]
                    subcategory = None
                    continue
                if folded in _CATEGORY_SUBLABELS and main_category == "Línea facial":
                    subcategory = _CATEGORY_SUBLABELS[folded]
                    continue
                if folded in {"indice", "tabla de contenido", "contenido"}:
                    continue
                if re.fullmatch(r"\d+(?:\s*/\s*\d+)?", text):
                    continue
                if main_category is None or len(text) < 3 or len(text) > 120:
                    continue
                category = main_category
                if subcategory:
                    category = f"{main_category} > {subcategory}"
                entries.append(
                    {
                        "categoria": repair_mojibake(category),
                        "producto_referencia": text,
                        "source_file": extraction.source_file,
                        "page_number": page.page_number,
                        "source_block_id": block_id,
                        "bbox": list(bbox) if bbox else None,
                    }
                )
    return entries


def _category_for_product(name: object, entries: list[dict[str, object]]) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    product = _fold_text(name)
    matches: list[tuple[int, str]] = []
    for entry in entries:
        reference = _fold_text(str(entry["producto_referencia"]))
        if not reference:
            continue
        if product == reference:
            matches.append((3, str(entry["categoria"])))
        elif product in reference or reference in product:
            matches.append((2, str(entry["categoria"])))
    if not matches:
        return None
    best_score = max(score for score, _ in matches)
    categories = {category for score, category in matches if score == best_score}
    return next(iter(categories)) if len(categories) == 1 else None


def _apply_categories(
    records: tuple[NormalizedRecord, ...], entries: list[dict[str, object]]
) -> tuple[NormalizedRecord, ...]:
    result: list[NormalizedRecord] = []
    for record in records:
        category = _category_for_product(record.data.get("nombre"), entries)
        if category is None:
            result.append(record)
            continue
        data = dict(record.data)
        data["categoria"] = category
        result.append(replace(record, data=data))
    return tuple(result)


def _page_height(extraction: ExtractionResult, page_number: object) -> float | None:
    if not isinstance(page_number, int):
        return None
    for page in extraction.pages:
        if page.page_number == page_number:
            return page.height
    return None


def _is_bottom_record(record: NormalizedRecord, extraction: ExtractionResult) -> bool:
    bbox = record.source.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return True
    height = _page_height(extraction, record.source.get("page_number"))
    if not height:
        return True
    center_y = (float(bbox[1]) + float(bbox[3])) / 2
    return center_y >= height * 0.5


def _keep_bottom_only(
    catalog: CatalogExtraction, extraction: ExtractionResult
) -> tuple[CatalogExtraction, str]:
    products = tuple(record for record in catalog.products if _is_bottom_record(record, extraction))
    variants = tuple(record for record in catalog.variants if _is_bottom_record(record, extraction))
    product_rows = {record.row_number for record in products}
    variant_rows = {record.row_number for record in variants}
    product_issues = tuple(
        issue for issue in catalog.product_issues if issue.row_number in product_rows
    )
    variant_issues = tuple(
        issue for issue in catalog.variant_issues if issue.row_number in variant_rows
    )
    issues = (*product_issues, *variant_issues)
    unparsed = tuple(
        item for item in catalog.unparsed_cards if item.get("row_number") in product_rows
    )
    warning = f"Se conservó únicamente la tarjeta inferior de {extraction.source_file}."
    return (
        CatalogExtraction(
            products=products,
            variants=variants,
            issues=issues,
            product_issues=product_issues,
            variant_issues=variant_issues,
            warnings=(*catalog.warnings, warning),
            unparsed_cards=unparsed,
        ),
        warning,
    )


def _rebase_catalog(
    catalog: CatalogExtraction, *, product_start: int, variant_start: int
) -> CatalogExtraction:
    product_row_map = {
        record.row_number: product_start + index for index, record in enumerate(catalog.products)
    }
    variant_row_map = {
        record.row_number: variant_start + index for index, record in enumerate(catalog.variants)
    }

    def rebase(record: NormalizedRecord, row_map: dict[int, int]) -> NormalizedRecord:
        return replace(record, row_number=row_map.get(record.row_number, record.row_number))

    def rebase_issue(issue: ValidationIssue, row_map: dict[int, int]) -> ValidationIssue:
        return replace(issue, row_number=row_map.get(issue.row_number, issue.row_number))

    return CatalogExtraction(
        products=tuple(rebase(record, product_row_map) for record in catalog.products),
        variants=tuple(rebase(record, variant_row_map) for record in catalog.variants),
        issues=tuple(rebase_issue(issue, product_row_map) for issue in catalog.product_issues)
        + tuple(rebase_issue(issue, variant_row_map) for issue in catalog.variant_issues),
        product_issues=tuple(
            rebase_issue(issue, product_row_map) for issue in catalog.product_issues
        ),
        variant_issues=tuple(
            rebase_issue(issue, variant_row_map) for issue in catalog.variant_issues
        ),
        warnings=catalog.warnings,
        unparsed_cards=tuple(
            {
                **item,
                "row_number": product_row_map.get(item.get("row_number"), item.get("row_number")),
            }
            for item in catalog.unparsed_cards
        ),
    )


def _process_source(
    path: Path,
    *,
    input_dir: Path,
    settings: ExtractionSettings,
    config: DocumentConfig,
    rules: BatchRules,
    run_id: str,
) -> _SourceResult:
    relative_path = path.relative_to(input_dir).as_posix()
    is_category = _source_name_matches(path, relative_path, rules.category_files)
    is_excluded = _source_name_matches(path, relative_path, rules.excluded_files)
    role = "category_index" if is_category else ("excluded" if is_excluded else "catalog_source")
    if is_excluded:
        return _SourceResult(path, relative_path, role)
    try:
        extraction = extract_document(path, settings)
        if is_category:
            return _SourceResult(path, relative_path, role, extraction=extraction)
        catalog = extract_catalog(extraction, run_id=run_id, config=config)
        filter_warning = None
        if _source_name_matches(path, relative_path, rules.keep_bottom_only):
            catalog, filter_warning = _keep_bottom_only(catalog, extraction)
        return _SourceResult(
            path,
            relative_path,
            role,
            extraction=extraction,
            catalog=catalog,
            filter_warning=filter_warning,
        )
    except Exception as exc:  # Keep the batch moving and report the exact source.
        return _SourceResult(path, relative_path, role, error=str(exc))


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    return str(value)


def _records_to_csv(
    records: tuple[NormalizedRecord, ...] | list[NormalizedRecord], entity: str
) -> bytes:
    headers = [field.name for field in fields_for(entity)]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({header: _stringify(record.data.get(header)) for header in headers})
    return output.getvalue().encode("utf-8")


def _categories_to_csv(entries: list[dict[str, object]]) -> bytes:
    headers = ["categoria", "producto_referencia", "source_file", "page_number", "source_block_id"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for entry in entries:
        writer.writerow({header: _stringify(entry.get(header)) for header in headers})
    return output.getvalue().encode("utf-8")


def _source_summary(result: _SourceResult) -> dict[str, object]:
    extraction = result.extraction
    catalog = result.catalog
    return {
        "source_file": result.path.name,
        "relative_path": result.relative_path,
        "role": result.role,
        "status": "failed" if result.error else "processed",
        "document_type": extraction.document_type if extraction else None,
        "engine": extraction.engine if extraction else None,
        "pages": len(extraction.pages) if extraction else 0,
        "blocks": extraction.block_count if extraction else 0,
        "products": len(catalog.products) if catalog else 0,
        "variants": len(catalog.variants) if catalog else 0,
        "warnings": list(catalog.warnings) if catalog else [],
        "filter_warning": result.filter_warning,
        "error": result.error,
    }


def prepare_batch_run(
    input_dir: Path,
    *,
    store: ArtifactStore,
    document_config: DocumentConfig,
    batch_config: Path | None = None,
    workers: int = 4,
) -> tuple[RunManifest, dict[str, object]]:
    """Extract all supported files in a tenant directory as one review batch."""
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Directorio documental no encontrado: {input_dir}")
    document_config.validate()
    if workers < 1 or workers > 16:
        raise ValueError("`workers` debe estar entre 1 y 16.")

    rules = BatchRules.from_file(batch_config, default_source=document_config.source)
    paths = sorted(
        (
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ),
        key=lambda path: path.relative_to(input_dir).as_posix().casefold(),
    )
    if not paths:
        raise ValueError(f"No hay PDFs o imágenes soportados en {input_dir}.")
    source_paths = [(path.relative_to(input_dir).as_posix(), path) for path in paths]
    batch_id = str(uuid4())
    manifest = RunManifest.new(
        "catalogo", f"{batch_id}__catalogo", input_dir, _aggregate_sha256(source_paths, store)
    )
    # RunManifest generates its own UUID by default; a batch must have exactly
    # one ID across report, evidence, raw copies and manifest.
    manifest.run_id = batch_id
    manifest.input_kind = "document"
    manifest.document_type = "mixed"
    manifest.extraction_engine = "pymupdf+tesseract"

    settings = ExtractionSettings(
        ocr_language=document_config.ocr_language,
        dpi=document_config.dpi,
        max_pages=document_config.max_pages,
        tesseract_config=document_config.tesseract_config,
    )
    results: list[_SourceResult] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(paths))) as executor:
        futures = {
            executor.submit(
                _process_source,
                path,
                input_dir=input_dir,
                settings=settings,
                config=document_config,
                rules=rules,
                run_id=batch_id,
            ): path
            for path in paths
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda result: result.relative_path.casefold())

    source_extractions: list[str] = []
    markdown_artifacts: dict[str, str] = {}
    raw_paths: list[str] = []
    for result in results:
        raw_destination = store.raw / f"{batch_id}__{_safe_name(result.relative_path)}"
        shutil.copy2(result.path, raw_destination)
        raw_paths.append(str(raw_destination))
        if result.extraction is not None:
            extraction_id = f"{batch_id}__{_safe_name(result.relative_path)}"
            source_extractions.append(
                str(store.write_extraction(extraction_id, result.extraction.as_json()))
            )
            markdown_artifacts[result.relative_path] = str(
                store.write_extraction_markdown(
                    extraction_id, extraction_to_markdown(result.extraction)
                )
            )

    category_entries: list[dict[str, object]] = []
    for result in results:
        if result.role == "category_index" and result.extraction is not None:
            category_entries.extend(_category_entries(result.extraction))

    products: list[NormalizedRecord] = []
    variants: list[NormalizedRecord] = []
    product_issues: list[ValidationIssue] = []
    variant_issues: list[ValidationIssue] = []
    unparsed_cards: list[dict[str, object]] = []
    extraction_warnings: list[str] = []
    product_row = 2
    variant_row = 2
    for result in results:
        if result.catalog is None:
            continue
        catalog = _rebase_catalog(
            result.catalog, product_start=product_row, variant_start=variant_row
        )
        product_row += len(catalog.products)
        variant_row += len(catalog.variants)
        products.extend(catalog.products)
        variants.extend(catalog.variants)
        product_issues.extend(catalog.product_issues)
        variant_issues.extend(catalog.variant_issues)
        unparsed_cards.extend(catalog.unparsed_cards)
        extraction_warnings.extend(catalog.warnings)

    # Cards with no name remain auditable in unparsed_cards/quarantine, but do
    # not become blank product rows in the review CSV.
    named_products = tuple(record for record in products if record.data.get("nombre"))
    product_candidates = _apply_categories(named_products, category_entries)
    product_result = validate_records(
        product_candidates,
        run_id=batch_id,
        entity="productos",
        source_file=f"{batch_id}__catalogo",
        total_rows=len(product_candidates),
        coercion_issues=product_issues,
    )
    variant_issue_rows = {issue.row_number for issue in variant_issues}
    variant_record_rows = {record.row_number for record in variants}
    variant_total = len(variants) + len(variant_issue_rows - variant_record_rows)
    variant_result = validate_records(
        variants,
        run_id=batch_id,
        entity="variantes",
        source_file=f"{batch_id}__catalogo",
        total_rows=variant_total,
        coercion_issues=variant_issues,
    )

    processed_products = store.write_processed(batch_id, "productos", product_result.valid_records)
    processed_variants = store.write_processed(batch_id, "variantes", variant_result.valid_records)
    candidate_products_jsonl = store.write_jsonl(
        store.processed,
        batch_id,
        "productos-candidatos",
        (record.as_json() for record in product_candidates),
    )
    candidate_variants_jsonl = store.write_jsonl(
        store.processed, batch_id, "variantes-candidatos", (record.as_json() for record in variants)
    )
    products_csv = store.write_processed_csv(
        batch_id, "productos", _records_to_csv(product_candidates, "productos")
    )
    variants_csv = store.write_processed_csv(
        batch_id, "variantes", _records_to_csv(variants, "variantes")
    )
    categories_csv = store.write_processed_csv(
        batch_id, "categorias", _categories_to_csv(category_entries)
    )

    quarantine_items = [{"entity": "productos", **item} for item in product_result.quarantine] + [
        {"entity": "variantes", **item} for item in variant_result.quarantine
    ]
    quarantine_path = store.write_jsonl(store.quarantine, batch_id, "catalogo", quarantine_items)
    source_summaries = [_source_summary(result) for result in results]
    failed_sources = [summary for summary in source_summaries if summary["status"] == "failed"]
    blocking_errors = len(product_result.report.issues) + len(variant_result.report.issues)
    approved_for_publish = (
        not failed_sources
        and product_result.report.valid_rows > 0
        and product_result.report.invalid_rows == 0
        and variant_result.report.invalid_rows == 0
    )
    report: dict[str, object] = {
        "run_id": batch_id,
        "input_kind": "document_batch",
        "source": rules.source,
        "input_dir": str(input_dir),
        "source_count": len(paths),
        "processed_source_count": len(paths) - len(failed_sources),
        "failed_source_count": len(failed_sources),
        "total_pages": sum(int(summary["pages"]) for summary in source_summaries),
        "total_cards": len(product_candidates),
        "total_variant_candidates": len(variants),
        "valid_rows": product_result.report.valid_rows + variant_result.report.valid_rows,
        "invalid_rows": product_result.report.invalid_rows + variant_result.report.invalid_rows,
        "blocking_errors": blocking_errors,
        "approved_for_publish": approved_for_publish,
        "sku_policy": {
            "sku_producto": "blank_for_review",
            "sku_variante": "blank_for_review",
            "must_be_resolved_before_publish": True,
        },
        "special_rules": {
            "category_files": list(rules.category_files),
            "keep_bottom_only": list(rules.keep_bottom_only),
            "excluded_files": list(rules.excluded_files),
        },
        "categories": {
            "entries_detected": len(category_entries),
            "products_mapped": sum(
                1 for record in product_candidates if record.data.get("categoria")
            ),
        },
        "product_quality": {
            "total": len(product_candidates),
            "names_missing": sum(
                1 for record in product_candidates if not record.data.get("nombre")
            ),
            "descriptions_missing": sum(
                1
                for record in product_candidates
                if not record.data.get("descripcion")
            ),
        },
        "entities": {
            "productos": product_result.report.as_json(),
            "variantes": variant_result.report.as_json(),
        },
        "issues": [
            issue.as_json()
            for issue in (*product_result.report.issues, *variant_result.report.issues)
        ],
        "warnings": [
            issue.as_json()
            for issue in (*product_result.report.warnings, *variant_result.report.warnings)
        ],
        "unparsed_cards": unparsed_cards,
        "extraction_warnings": extraction_warnings,
        "markdown_artifacts": markdown_artifacts,
        "sources": source_summaries,
    }
    report_path = store.write_report_dict(batch_id, report)
    catalog_path = store.write_catalog_result(
        batch_id,
        {
            "run_id": batch_id,
            "source": rules.source,
            "products": [record.as_json() for record in product_candidates],
            "variants": [record.as_json() for record in variants],
            "categories": category_entries,
            "validation": {
                "approved_for_publish": approved_for_publish,
                "productos": product_result.report.as_json(),
                "variantes": variant_result.report.as_json(),
            },
        },
    )
    config_path = store.reports / f"{batch_id}__batch-config.json"
    config_path.write_text(
        json_dumps(
            {
                "document_config": document_config.__dict__,
                "batch_rules": {
                    "source": rules.source,
                    "category_files": list(rules.category_files),
                    "keep_bottom_only": list(rules.keep_bottom_only),
                    "excluded_files": list(rules.excluded_files),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    manifest.status = "ready" if approved_for_publish else "blocked"
    manifest.artifacts.update(
        {
            "raw_count": str(len(raw_paths)),
            "extractions_dir": str(store.extracted),
            "source_extractions_count": str(len(source_extractions)),
            "markdown_count": str(len(markdown_artifacts)),
            "productos_csv": str(products_csv),
            "variantes_csv": str(variants_csv),
            "categorias_csv": str(categories_csv),
            "productos_candidatos_jsonl": str(candidate_products_jsonl),
            "variantes_candidatos_jsonl": str(candidate_variants_jsonl),
            "processed_productos_jsonl": str(processed_products[0]),
            "processed_productos_parquet": str(processed_products[1]),
            "processed_variantes_jsonl": str(processed_variants[0]),
            "processed_variantes_parquet": str(processed_variants[1]),
            "catalog_json": str(catalog_path),
            "quarantine": str(quarantine_path),
            "report": str(report_path),
            "batch_config": str(config_path),
        }
    )
    store.write_manifest(manifest)
    return manifest, report
