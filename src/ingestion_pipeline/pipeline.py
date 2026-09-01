"""Application orchestration for local validation and remote publication."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingestion_pipeline.config import DocumentConfig, MappingConfig, WebbyConfig
from ingestion_pipeline.documents.catalog import extract_catalog
from ingestion_pipeline.documents.extractors import ExtractionSettings, extract_document
from ingestion_pipeline.documents.markdown import extraction_to_markdown
from ingestion_pipeline.domain.catalog import fields_for
from ingestion_pipeline.domain.models import RunManifest, json_dumps
from ingestion_pipeline.normalization.mapping import normalize_table
from ingestion_pipeline.sources.base import read_table
from ingestion_pipeline.storage.artifacts import ArtifactStore
from ingestion_pipeline.validation.rules import validate_records
from ingestion_pipeline.webby.client import WebbyClient


def prepare_run(
    input_path: Path,
    mapping: MappingConfig,
    *,
    store: ArtifactStore,
    max_rows: int = 100_000,
) -> tuple[RunManifest, dict[str, object]]:
    """Parse, normalize, validate and persist a complete local run."""
    table = read_table(input_path, sheet_name=mapping.sheet)
    nonblank_rows = sum(
        1 for row in table.rows if any(value is not None and str(value).strip() for value in row)
    )
    if nonblank_rows > max_rows:
        raise ValueError(f"El archivo tiene {nonblank_rows} filas; el límite local es {max_rows}.")

    manifest = RunManifest.new(
        mapping.entity, input_path.name, input_path, store.sha256(input_path)
    )
    raw_path = store.copy_raw(manifest.run_id, input_path)
    records, coercion_issues, used_mapping = normalize_table(table, mapping, run_id=manifest.run_id)
    result = validate_records(
        records,
        run_id=manifest.run_id,
        entity=mapping.entity,
        source_file=input_path.name,
        total_rows=nonblank_rows,
        coercion_issues=coercion_issues,
    )
    processed_jsonl, processed_parquet = store.write_processed(
        manifest.run_id, mapping.entity, result.valid_records
    )
    quarantine = store.write_jsonl(
        store.quarantine, manifest.run_id, mapping.entity, result.quarantine
    )
    report_path = store.write_report(result.report)
    mapping_path = store.reports / f"{manifest.run_id}__mapping.json"
    mapping_path.write_text(json_dumps(used_mapping) + "\n", encoding="utf-8")

    manifest.status = "ready" if result.report.approved_for_publish else "blocked"
    manifest.artifacts.update(
        {
            "raw": str(raw_path),
            "processed_jsonl": str(processed_jsonl),
            "processed_parquet": str(processed_parquet),
            "quarantine": str(quarantine),
            "report": str(report_path),
            "mapping": str(mapping_path),
        }
    )
    store.write_manifest(manifest)
    return manifest, result.report.as_json()


def prepare_document_run(
    input_path: Path,
    *,
    store: ArtifactStore,
    document_config: DocumentConfig,
) -> tuple[RunManifest, dict[str, object]]:
    """Extract a visual catalog and persist products, variants and evidence."""
    settings = ExtractionSettings(
        ocr_language=document_config.ocr_language,
        dpi=document_config.dpi,
        max_pages=document_config.max_pages,
        tesseract_config=document_config.tesseract_config,
    )
    manifest = RunManifest.new("productos", input_path.name, input_path, store.sha256(input_path))
    manifest.input_kind = "document"
    raw_path = store.copy_raw(manifest.run_id, input_path)
    extraction = extract_document(input_path, settings)
    extraction_path = store.write_extraction(manifest.run_id, extraction.as_json())
    markdown_path = store.write_extraction_markdown(
        manifest.run_id, extraction_to_markdown(extraction)
    )
    catalog = extract_catalog(extraction, run_id=manifest.run_id, config=document_config)

    product_result = validate_records(
        catalog.products,
        run_id=manifest.run_id,
        entity="productos",
        source_file=input_path.name,
        total_rows=len(catalog.products),
        coercion_issues=catalog.product_issues,
    )
    variant_issue_rows = {issue.row_number for issue in catalog.variant_issues}
    variant_record_rows = {record.row_number for record in catalog.variants}
    variant_total = len(catalog.variants) + len(variant_issue_rows - variant_record_rows)
    variant_result = validate_records(
        catalog.variants,
        run_id=manifest.run_id,
        entity="variantes",
        source_file=input_path.name,
        total_rows=variant_total,
        coercion_issues=catalog.variant_issues,
    )
    processed_products = store.write_processed(
        manifest.run_id, "productos", product_result.valid_records
    )
    processed_variants = store.write_processed(
        manifest.run_id, "variantes", variant_result.valid_records
    )
    catalog_result_path = store.write_catalog_result(
        manifest.run_id,
        {
            "run_id": manifest.run_id,
            "source_file": input_path.name,
            "products": [record.as_json() for record in product_result.valid_records],
            "variants": [record.as_json() for record in variant_result.valid_records],
        },
    )
    products_csv_path = store.write_processed_csv(
        manifest.run_id,
        "productos",
        records_to_csv(store, manifest.run_id, "productos"),
    )
    variants_csv_path = None
    if variant_result.valid_records:
        variants_csv_path = store.write_processed_csv(
            manifest.run_id,
            "variantes",
            records_to_csv(store, manifest.run_id, "variantes"),
        )
    quarantine_items = [{"entity": "productos", **item} for item in product_result.quarantine] + [
        {"entity": "variantes", **item} for item in variant_result.quarantine
    ]
    quarantine_path = store.write_jsonl(
        store.quarantine, manifest.run_id, "catalogo", quarantine_items
    )
    invalid_rows = product_result.report.invalid_rows + variant_result.report.invalid_rows
    valid_rows = product_result.report.valid_rows + variant_result.report.valid_rows
    report: dict[str, object] = {
        "run_id": manifest.run_id,
        "input_kind": "document",
        "source_file": input_path.name,
        "document_type": extraction.document_type,
        "extraction_engine": extraction.engine,
        "total_pages": len(extraction.pages),
        "total_cards": len(catalog.products),
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "omitted_rows": product_result.report.omitted_rows + variant_result.report.omitted_rows,
        "blocking_errors": len(product_result.report.issues) + len(variant_result.report.issues),
        "approved_for_publish": (
            product_result.report.valid_rows > 0
            and product_result.report.invalid_rows == 0
            and variant_result.report.invalid_rows == 0
        ),
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
        "unparsed_cards": list(catalog.unparsed_cards),
        "extraction_warnings": list(extraction.warnings) + list(catalog.warnings),
        "created_at": datetime.now(UTC).isoformat(),
    }
    report_path = store.write_report_dict(manifest.run_id, report)
    mapping_path = store.reports / f"{manifest.run_id}__document-config.json"
    mapping_path.write_text(json_dumps(document_config.__dict__) + "\n", encoding="utf-8")

    manifest.document_type = extraction.document_type
    manifest.extraction_engine = extraction.engine
    manifest.status = "ready" if report["approved_for_publish"] else "blocked"
    manifest.artifacts.update(
        {
            "raw": str(raw_path),
            "extraction": str(extraction_path),
            "extraction_markdown": str(markdown_path),
            "processed_productos_jsonl": str(processed_products[0]),
            "processed_productos_parquet": str(processed_products[1]),
            "processed_variantes_jsonl": str(processed_variants[0]),
            "processed_variantes_parquet": str(processed_variants[1]),
            "catalog_json": str(catalog_result_path),
            "productos_csv": str(products_csv_path),
            "quarantine": str(quarantine_path),
            "report": str(report_path),
            "document_config": str(mapping_path),
        }
    )
    if variants_csv_path:
        manifest.artifacts["variantes_csv"] = str(variants_csv_path)
    store.write_manifest(manifest)
    return manifest, report


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def records_to_csv(store: ArtifactStore, run_id: str, entity: str) -> bytes:
    records = store.load_processed_records(run_id, entity)
    headers = [field.name for field in fields_for(entity)]
    present = [
        header for header in headers if any(header in record.get("data", {}) for record in records)
    ]
    if not present:
        if records:
            raise ValueError("El lote procesado no contiene columnas publicables.")
        present = headers
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=present, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        data = record.get("data", {})
        writer.writerow({header: _stringify(data.get(header)) for header in present})
    return output.getvalue().encode("utf-8")


def _csv_headers(content: bytes) -> list[str]:
    return next(csv.reader(io.StringIO(content.decode("utf-8"))))


async def publish_run(
    store: ArtifactStore,
    run_id: str,
    *,
    webby_config: WebbyConfig,
    confirm: bool = False,
) -> dict[str, Any]:
    """Dry-run remotely, then publish a previously approved local run."""
    if not confirm:
        raise ValueError("La publicación requiere --confirm como segunda confirmación explícita.")
    manifest = store.load_manifest(run_id)
    if manifest.get("status") != "approved":
        raise ValueError(
            "El run no está aprobado. Ejecuta `ingestion approve` después de revisar el reporte."
        )
    entity = str(manifest["entity"])
    content = records_to_csv(store, run_id, entity)
    mapping = {field: field for field in _csv_headers(content)}
    filename = f"{run_id}__{entity}.csv"

    manifest["status"] = "publishing"
    store.write_manifest_dict(manifest)
    try:
        async with WebbyClient(webby_config) as client:
            validation_job = await client.dispatch_import(
                entity=entity,
                filename=filename,
                content=content,
                mapping=mapping,
                dry_run=True,
            )
            validation_response = await client.wait_for_job(validation_job)
            remote_report = validation_response.get("resultado")
            if not isinstance(remote_report, dict) or "fallidos" not in remote_report:
                raise ValueError(
                    "Webby no devolvió un reporte completo para el dry-run; no se ejecutó la publicación."
                )
            if int(remote_report["fallidos"]) != 0:
                raise ValueError("Webby rechazó el dry-run remoto; no se ejecutó la publicación.")
            publication_job = await client.dispatch_import(
                entity=entity,
                filename=filename,
                content=content,
                mapping=mapping,
                dry_run=False,
            )
            publication_response = await client.wait_for_job(publication_job)
    except Exception as exc:
        manifest["status"] = "publish_failed"
        manifest["publish_error"] = str(exc)
        store.write_manifest_dict(manifest)
        raise

    manifest["status"] = "published"
    manifest["published_at"] = datetime.now(UTC).isoformat()
    manifest["webby_job_id"] = str(publication_response.get("id") or publication_job)
    manifest["webby_report"] = publication_response.get("resultado")
    store.write_manifest_dict(manifest)
    return publication_response
