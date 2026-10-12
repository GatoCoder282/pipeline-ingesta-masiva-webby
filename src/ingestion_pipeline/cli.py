"""Command-line entrypoint for local and orchestrated runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from ingestion_pipeline.batch import prepare_batch_run
from ingestion_pipeline.config import (
    AppConfig,
    DocumentConfig,
    WebbyConfig,
    load_dotenv,
    load_mapping_or_empty,
)
from ingestion_pipeline.documents.extractors import ExtractionSettings, extract_document
from ingestion_pipeline.documents.review import (
    write_description_quality_report,
    write_description_review_html,
)
from ingestion_pipeline.enrichment import (
    assign_catalog_skus,
    complete_product_categories,
    enrich_products_csv,
    enrich_variants_csv,
    improve_pdf_descriptions_csv,
    merge_pdf_catalog_csvs,
)
from ingestion_pipeline.normalization.mapping import suggest_mapping
from ingestion_pipeline.pipeline import prepare_document_run, prepare_run, publish_run
from ingestion_pipeline.runs.manifest import approve_run
from ingestion_pipeline.sources.base import read_table
from ingestion_pipeline.storage.artifacts import ArtifactStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingestion", description="Pipeline local de ingesta para Webby"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="leer encabezados y sugerir un mapping")
    preview.add_argument("--input", type=Path, required=True)
    preview.add_argument("--entity", choices=("productos", "variantes", "precios", "clientes"))
    preview.add_argument("--mapping", type=Path)
    preview.add_argument("--sheet")

    validate = subparsers.add_parser("validate", help="crear run, normalizar y validar localmente")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--entity", choices=("productos", "variantes", "precios", "clientes"))
    validate.add_argument("--mapping", type=Path)
    validate.add_argument("--data-dir", type=Path)
    validate.add_argument("--max-rows", type=int)

    extract = subparsers.add_parser(
        "extract-document", help="extraer un catálogo visual desde PDF o imagen"
    )
    extract.add_argument("--input", type=Path, required=True)
    extract.add_argument("--document-config", type=Path)
    extract.add_argument("--data-dir", type=Path)
    extract.add_argument("--max-pages", type=int)
    extract.add_argument("--ocr-language")
    extract.add_argument("--dpi", type=int)

    worker = subparsers.add_parser(
        "ocr-worker", help="extraer un PDF/imagen con PaddleOCR en un worker aislado"
    )
    worker.add_argument("--input", type=Path, required=True)
    worker.add_argument("--output", type=Path, required=True)
    worker.add_argument("--document-config", type=Path)
    worker.add_argument("--runtime", choices=("cpu", "gpu", "gpu:0"), default=None)
    worker.add_argument("--ocr-language")
    worker.add_argument("--dpi", type=int)

    batch = subparsers.add_parser(
        "extract-batch", help="extraer y consolidar todos los PDFs/imágenes de un tenant"
    )
    batch.add_argument("--input-dir", type=Path, required=True)
    batch.add_argument("--document-config", type=Path)
    batch.add_argument("--batch-config", type=Path)
    batch.add_argument("--data-dir", type=Path)
    batch.add_argument("--workers", type=int, default=4)

    enrich = subparsers.add_parser(
        "enrich-catalog", help="completar productos desde listas DOCX de precios"
    )
    enrich.add_argument("--input", type=Path, required=True)
    enrich.add_argument("--profile", type=Path, required=True)
    enrich.add_argument("--prices-dir", type=Path)
    enrich.add_argument("--source-jsonl", type=Path)
    enrich.add_argument("--output", type=Path)
    enrich.add_argument("--report", type=Path)

    enrich_variants = subparsers.add_parser(
        "enrich-variants",
        help="completar precios y costos por variante desde listas DOCX",
    )
    enrich_variants.add_argument("--products", type=Path, required=True)
    enrich_variants.add_argument("--variants", type=Path, required=True)
    enrich_variants.add_argument("--profile", type=Path, required=True)
    enrich_variants.add_argument("--prices-dir", type=Path)
    enrich_variants.add_argument("--product-source-jsonl", type=Path)
    enrich_variants.add_argument("--variant-source-jsonl", type=Path)
    enrich_variants.add_argument("--output-products", type=Path)
    enrich_variants.add_argument("--output-variants", type=Path)
    enrich_variants.add_argument("--output-product-source-jsonl", type=Path)
    enrich_variants.add_argument("--output-variant-source-jsonl", type=Path)
    enrich_variants.add_argument("--report", type=Path)

    categories = subparsers.add_parser(
        "complete-categories", help="completar categorías faltantes desde reglas del perfil"
    )
    categories.add_argument("--input", type=Path, required=True)
    categories.add_argument("--profile", type=Path, required=True)
    categories.add_argument("--source-jsonl", type=Path)
    categories.add_argument("--output", type=Path)
    categories.add_argument("--report", type=Path)

    merge_pdf = subparsers.add_parser(
        "merge-pdf-catalog", help="agregar productos y precios por variante desde un PDF"
    )
    merge_pdf.add_argument("--products", type=Path, required=True)
    merge_pdf.add_argument("--variants", type=Path, required=True)
    merge_pdf.add_argument("--profile", type=Path, required=True)
    merge_pdf.add_argument("--product-source-jsonl", type=Path, required=True)
    merge_pdf.add_argument("--variant-source-jsonl", type=Path)
    merge_pdf.add_argument("--output-product-source-jsonl", type=Path)
    merge_pdf.add_argument("--output-variant-source-jsonl", type=Path)
    merge_pdf.add_argument("--report", type=Path)

    descriptions = subparsers.add_parser(
        "improve-descriptions", help="reconstruir descripciones desde bloques PDF con evidencia"
    )
    descriptions.add_argument("--products", type=Path, required=True)
    descriptions.add_argument("--profile", type=Path, required=True)
    descriptions.add_argument("--product-source-jsonl", type=Path)
    descriptions.add_argument("--output", type=Path)
    descriptions.add_argument("--report", type=Path)

    review = subparsers.add_parser(
        "review-descriptions", help="generar una revisión HTML de calidad de descripciones"
    )
    review.add_argument("--products", type=Path, required=True)
    review.add_argument("--report", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)

    quality = subparsers.add_parser(
        "quality-report", help="evaluar las descripciones ya exportadas"
    )
    quality.add_argument("--products", type=Path, required=True)
    quality.add_argument("--output", type=Path, required=True)
    quality.add_argument("--min-description-chars", type=int, default=40)

    skus = subparsers.add_parser(
        "assign-skus", help="asignar SKU estables y enlazar variantes con sus productos"
    )
    skus.add_argument("--products", type=Path, required=True)
    skus.add_argument("--variants", type=Path, required=True)
    skus.add_argument("--profile", type=Path, required=True)
    skus.add_argument("--product-source-jsonl", type=Path)
    skus.add_argument("--variant-source-jsonl", type=Path)
    skus.add_argument("--sku-map", type=Path, required=True)
    skus.add_argument("--report", type=Path)

    approve = subparsers.add_parser("approve", help="aprobar un run listo para publicar")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--by", required=True, help="persona que revisó y aprobó el lote")
    approve.add_argument("--data-dir", type=Path)

    publish = subparsers.add_parser("publish", help="dry-run remoto y publicación aprobada")
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--confirm", action="store_true", help="segunda confirmación explícita")
    publish.add_argument("--data-dir", type=Path)

    inspect = subparsers.add_parser("inspect", help="mostrar manifiesto y reporte de un run")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--data-dir", type=Path)
    return parser


def _store(path: Path | None) -> ArtifactStore:
    configured = path or AppConfig.from_environment().data_dir
    return ArtifactStore(configured)


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _cmd_preview(args: argparse.Namespace) -> None:
    mapping = load_mapping_or_empty(args.mapping, args.entity)
    table = read_table(args.input, sheet_name=args.sheet or mapping.sheet)
    _print(
        {
            "source": str(args.input),
            "entity": mapping.entity,
            "sheet": table.sheet_name,
            "headers": table.headers,
            "total_rows": table.total_rows,
            "suggested_mapping": mapping.columns or suggest_mapping(table.headers, mapping.entity),
            "sample": [dict(zip(table.headers, row, strict=False)) for row in table.rows[:5]],
        }
    )


def _cmd_validate(args: argparse.Namespace) -> None:
    mapping = load_mapping_or_empty(args.mapping, args.entity)
    manifest, report = prepare_run(
        args.input,
        mapping,
        store=_store(args.data_dir),
        max_rows=args.max_rows or AppConfig.from_environment().max_rows,
    )
    _print(
        {
            "run_id": manifest.run_id,
            "status": manifest.status,
            "report": report,
            "artifacts": manifest.artifacts,
        }
    )


def _cmd_extract_document(args: argparse.Namespace) -> None:
    config = (
        DocumentConfig.from_file(args.document_config) if args.document_config else DocumentConfig()
    )
    overrides: dict[str, object] = {}
    if args.max_pages is not None:
        overrides["max_pages"] = args.max_pages
    if args.ocr_language is not None:
        overrides["ocr_language"] = args.ocr_language
    if args.dpi is not None:
        overrides["dpi"] = args.dpi
    if overrides:
        config = replace(config, **overrides)
        config.validate()
    manifest, report = prepare_document_run(
        args.input,
        store=_store(args.data_dir),
        document_config=config,
    )
    _print(
        {
            "run_id": manifest.run_id,
            "status": manifest.status,
            "report": report,
            "artifacts": manifest.artifacts,
        }
    )


def _cmd_extract_batch(args: argparse.Namespace) -> None:
    config = (
        DocumentConfig.from_file(args.document_config) if args.document_config else DocumentConfig()
    )
    manifest, report = prepare_batch_run(
        args.input_dir,
        store=_store(args.data_dir),
        document_config=config,
        batch_config=args.batch_config,
        workers=args.workers,
    )
    _print(
        {
            "run_id": manifest.run_id,
            "status": manifest.status,
            "report": report,
            "artifacts": manifest.artifacts,
        }
    )


def _cmd_ocr_worker(args: argparse.Namespace) -> None:
    config = (
        DocumentConfig.from_file(args.document_config) if args.document_config else DocumentConfig()
    )
    overrides: dict[str, object] = {"ocr_engine": "paddleocr"}
    if args.runtime is not None:
        overrides["ocr_runtime"] = args.runtime
    if args.ocr_language is not None:
        overrides["ocr_language"] = args.ocr_language
    if args.dpi is not None:
        overrides["dpi"] = args.dpi
    config = replace(config, **overrides)
    config.validate()
    settings = ExtractionSettings(
        ocr_language=config.ocr_language,
        ocr_engine="paddleocr",
        ocr_runtime=config.ocr_runtime,
        dpi=config.dpi,
        max_pages=config.max_pages,
        tesseract_config=config.tesseract_config,
    )
    extraction = extract_document(args.input, settings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(extraction.as_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _print({"output": str(args.output), **extraction.as_json()})


def _cmd_enrich_catalog(args: argparse.Namespace) -> None:
    prices_dir = args.prices_dir or args.input.parent.parent / "incoming"
    source_jsonl = args.source_jsonl or args.input.with_suffix(".jsonl")
    report = enrich_products_csv(
        args.input,
        profile_path=args.profile,
        prices_dir=prices_dir,
        source_jsonl=source_jsonl,
        output_path=args.output,
        report_path=args.report,
    )
    _print(report)


def _cmd_enrich_variants(args: argparse.Namespace) -> None:
    prices_dir = args.prices_dir or args.products.parent.parent / "incoming"
    report = enrich_variants_csv(
        args.products,
        args.variants,
        profile_path=args.profile,
        prices_dir=prices_dir,
        product_source_jsonl=args.product_source_jsonl,
        variant_source_jsonl=args.variant_source_jsonl,
        output_products_path=args.output_products,
        output_variants_path=args.output_variants,
        output_product_source_jsonl=args.output_product_source_jsonl,
        output_variant_source_jsonl=args.output_variant_source_jsonl,
        report_path=args.report,
    )
    _print(report)


def _cmd_complete_categories(args: argparse.Namespace) -> None:
    report = complete_product_categories(
        args.input,
        profile_path=args.profile,
        source_jsonl=args.source_jsonl,
        output_path=args.output,
        report_path=args.report,
    )
    _print(report)


def _cmd_merge_pdf_catalog(args: argparse.Namespace) -> None:
    report = merge_pdf_catalog_csvs(
        args.products,
        args.variants,
        profile_path=args.profile,
        product_source_jsonl=args.product_source_jsonl,
        variant_source_jsonl=args.variant_source_jsonl,
        output_product_source_jsonl=args.output_product_source_jsonl,
        output_variant_source_jsonl=args.output_variant_source_jsonl,
        report_path=args.report,
    )
    _print(report)


def _cmd_improve_descriptions(args: argparse.Namespace) -> None:
    report = improve_pdf_descriptions_csv(
        args.products,
        profile_path=args.profile,
        product_source_jsonl=args.product_source_jsonl,
        output_path=args.output,
        report_path=args.report,
    )
    _print(report)


def _cmd_review_descriptions(args: argparse.Namespace) -> None:
    _print(write_description_review_html(args.products, args.report, args.output))


def _cmd_quality_report(args: argparse.Namespace) -> None:
    _print(
        write_description_quality_report(
            args.products,
            args.output,
            min_chars=args.min_description_chars,
        )
    )


def _cmd_assign_skus(args: argparse.Namespace) -> None:
    report = assign_catalog_skus(
        args.products,
        args.variants,
        profile_path=args.profile,
        product_source_jsonl=args.product_source_jsonl,
        variant_source_jsonl=args.variant_source_jsonl,
        sku_map_path=args.sku_map,
        report_path=args.report,
    )
    _print(report)


def _cmd_approve(args: argparse.Namespace) -> None:
    manifest = approve_run(_store(args.data_dir), args.run_id, args.by)
    _print(manifest)


def _cmd_publish(args: argparse.Namespace) -> None:
    response = asyncio.run(
        publish_run(
            _store(args.data_dir),
            args.run_id,
            webby_config=WebbyConfig.from_environment(),
            confirm=args.confirm,
        )
    )
    _print(response)


def _cmd_inspect(args: argparse.Namespace) -> None:
    store = _store(args.data_dir)
    _print({"manifest": store.load_manifest(args.run_id), "report": store.load_report(args.run_id)})


def main() -> None:
    load_dotenv()
    args = _parser().parse_args()
    try:
        {
            "preview": _cmd_preview,
            "validate": _cmd_validate,
            "extract-document": _cmd_extract_document,
            "extract-batch": _cmd_extract_batch,
            "ocr-worker": _cmd_ocr_worker,
            "enrich-catalog": _cmd_enrich_catalog,
            "enrich-variants": _cmd_enrich_variants,
            "complete-categories": _cmd_complete_categories,
            "merge-pdf-catalog": _cmd_merge_pdf_catalog,
            "improve-descriptions": _cmd_improve_descriptions,
            "review-descriptions": _cmd_review_descriptions,
            "quality-report": _cmd_quality_report,
            "assign-skus": _cmd_assign_skus,
            "approve": _cmd_approve,
            "publish": _cmd_publish,
            "inspect": _cmd_inspect,
        }[args.command](args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
