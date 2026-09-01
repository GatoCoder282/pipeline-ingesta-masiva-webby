from ingestion_pipeline.domain.models import NormalizedRecord
from ingestion_pipeline.validation.rules import validate_records


def _product(row: int, sku: str, **data: object) -> NormalizedRecord:
    return NormalizedRecord(
        row,
        "productos",
        {"sku": sku, "nombre": "Producto", **data},
        {"source_row": row},
    )


def test_validation_rejects_negative_stock_and_duplicates() -> None:
    records = [_product(2, "SKU-1", stock=-1), _product(3, "SKU-1", stock=2)]

    result = validate_records(
        records,
        run_id="run-1",
        entity="productos",
        source_file="catalogo.csv",
        total_rows=2,
    )

    assert result.report.valid_rows == 0
    assert result.report.invalid_rows == 2
    assert {issue.code for issue in result.report.issues} == {
        "negative_value",
        "duplicate_in_batch",
    }


def test_validation_accepts_product_without_sku_for_webby_generation() -> None:
    result = validate_records(
        [_product(2, "")],
        run_id="run-1",
        entity="productos",
        source_file="catalogo.csv",
        total_rows=1,
    )

    assert result.report.valid_rows == 1
    assert result.report.invalid_rows == 0


def test_validation_does_not_report_duplicate_variants_without_parent_sku() -> None:
    records = [
        NormalizedRecord(200, "variantes", {"atributos": {"presentacion": "250grs"}}),
        NormalizedRecord(201, "variantes", {"atributos": {"presentacion": "500grs"}}),
    ]

    result = validate_records(
        records,
        run_id="run-1",
        entity="variantes",
        source_file="catalogo.pdf",
        total_rows=2,
    )

    assert result.report.invalid_rows == 2
    assert {issue.code for issue in result.report.issues} == {"required_field_missing"}
