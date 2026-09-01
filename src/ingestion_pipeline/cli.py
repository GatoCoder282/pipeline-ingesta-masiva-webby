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

    batch = subparsers.add_parser(
        "extract-batch", help="extraer y consolidar todos los PDFs/imágenes de un tenant"
    )
    batch.add_argument("--input-dir", type=Path, required=True)
    batch.add_argument("--document-config", type=Path)
    batch.add_argument("--batch-config", type=Path)
    batch.add_argument("--data-dir", type=Path)
    batch.add_argument("--workers", type=int, default=4)

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
            "approve": _cmd_approve,
            "publish": _cmd_publish,
            "inspect": _cmd_inspect,
        }[args.command](args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
