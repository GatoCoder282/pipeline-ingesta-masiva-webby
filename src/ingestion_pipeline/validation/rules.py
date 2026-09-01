"""Deterministic quality gates run before any remote side effect."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ingestion_pipeline.domain.catalog import fields_for
from ingestion_pipeline.domain.models import NormalizedRecord, ValidationIssue, ValidationReport


@dataclass
class ValidationResult:
    report: ValidationReport
    valid_records: list[NormalizedRecord]
    quarantine: list[dict[str, object]]


def _identity(record: NormalizedRecord) -> str | None:
    data = record.data
    if record.entity == "productos":
        return str(data.get("sku", "")).strip() or None
    if record.entity == "variantes":
        if not str(data.get("sku_producto", "")).strip():
            return None
        attrs = json.dumps(data.get("atributos", {}), ensure_ascii=False, sort_keys=True)
        return f"{data.get('sku_producto', '')}|{data.get('sku_variante', '')}|{attrs}"
    if record.entity == "precios":
        return f"{data.get('sku', '')}|{data.get('sku_variante', '')}|{data.get('lista', '')}"
    return str(data.get("celular") or data.get("email") or data.get("nombre") or "").strip() or None


def _numeric_rules(data: dict[str, object], row: int) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field in ("precio", "precio_comparacion", "costo", "peso", "stock", "stock_minimo"):
        value = data.get(field)
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and value < 0:
            issues.append(
                ValidationIssue(
                    row, "negative_value", f"{field} no puede ser negativo", field=field
                )
            )
    if isinstance(data.get("precio_comparacion"), Decimal) and isinstance(
        data.get("precio"), Decimal
    ):
        if data["precio_comparacion"] < data["precio"]:
            issues.append(
                ValidationIssue(
                    row,
                    "comparison_price_lower",
                    "precio_comparacion normalmente debe ser mayor o igual que precio",
                    severity="warning",
                    field="precio_comparacion",
                )
            )
    return issues


def validate_records(
    records: Iterable[NormalizedRecord],
    *,
    run_id: str,
    entity: str,
    source_file: str,
    total_rows: int,
    coercion_issues: Iterable[ValidationIssue] = (),
) -> ValidationResult:
    report = ValidationReport(run_id, entity, source_file, total_rows)
    report.issues.extend(issue for issue in coercion_issues if issue.severity == "error")
    report.warnings.extend(issue for issue in coercion_issues if issue.severity == "warning")
    invalid_row_numbers = {issue.row_number for issue in report.issues}
    valid_records: list[NormalizedRecord] = []
    quarantine: list[dict[str, object]] = []
    identities: dict[str, int] = {}
    definitions = fields_for(entity)

    for issue in report.issues:
        quarantine.append({"row_number": issue.row_number, "errors": [issue.as_json()]})

    for record in records:
        row_issues: list[ValidationIssue] = []
        for definition in definitions:
            if definition.required and not str(record.data.get(definition.name, "")).strip():
                row_issues.append(
                    ValidationIssue(
                        record.row_number,
                        "required_field_missing",
                        f"Falta el campo requerido: {definition.name}",
                        field=definition.name,
                    )
                )

        if entity == "productos" and record.data.get("tipo") not in {
            None,
            "simple",
            "variable",
            "servicio",
        }:
            row_issues.append(
                ValidationIssue(
                    record.row_number,
                    "invalid_product_type",
                    "tipo debe ser simple, variable o servicio",
                    field="tipo",
                )
            )
        if entity == "variantes" and not isinstance(record.data.get("atributos"), dict):
            row_issues.append(
                ValidationIssue(
                    record.row_number,
                    "invalid_attributes",
                    "atributos debe contener al menos un par clave=valor",
                    field="atributos",
                )
            )
        row_issues.extend(
            issue
            for issue in _numeric_rules(record.data, record.row_number)
            if issue.severity == "error"
        )
        report.warnings.extend(
            issue
            for issue in _numeric_rules(record.data, record.row_number)
            if issue.severity == "warning"
        )

        identity = _identity(record)
        if identity:
            if identity in identities:
                row_issues.append(
                    ValidationIssue(
                        record.row_number,
                        "duplicate_in_batch",
                        f"Registro duplicado de la fila {identities[identity]}",
                    )
                )
            else:
                identities[identity] = record.row_number

        if record.row_number in invalid_row_numbers:
            continue
        if row_issues:
            report.issues.extend(row_issues)
            quarantine.append(
                {
                    "row_number": record.row_number,
                    "record": record.as_json(),
                    "errors": [issue.as_json() for issue in row_issues],
                }
            )
            continue
        valid_records.append(record)

    invalid_rows = invalid_row_numbers | {item["row_number"] for item in quarantine}
    report.invalid_rows = len(invalid_rows)
    report.valid_rows = len(valid_records)
    report.omitted_rows = max(0, total_rows - report.valid_rows - report.invalid_rows)
    return ValidationResult(report, valid_records, quarantine)
