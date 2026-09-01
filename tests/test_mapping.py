from decimal import Decimal
from pathlib import Path

from ingestion_pipeline.config import MappingConfig
from ingestion_pipeline.domain.models import ParsedTable
from ingestion_pipeline.normalization.mapping import normalize_table, suggest_mapping


def test_suggest_mapping_uses_webby_aliases() -> None:
    mapping = suggest_mapping(["Código", "Nombre del producto", "Existencias"], "productos")

    assert mapping == {"Código": "sku", "Nombre del producto": "nombre", "Existencias": "stock"}


def test_normalize_table_handles_bolivian_numbers_and_booleans() -> None:
    table = ParsedTable(
        headers=["Código", "Nombre", "Precio", "Activo"],
        rows=[["PR-1", "Crema", "89,90", "sí"]],
        source_path=Path("catalogo.csv"),
    )
    config = MappingConfig(
        source="test",
        entity="productos",
        columns={"Código": "sku", "Nombre": "nombre", "Precio": "precio", "Activo": "activo"},
    )

    records, issues, mapping = normalize_table(table, config, run_id="run-1")

    assert not issues
    assert mapping["Precio"] == "precio"
    assert records[0].data["precio"] == Decimal("89.90")
    assert records[0].data["activo"] is True


def test_normalize_table_reports_invalid_type_without_emitting_bad_record() -> None:
    table = ParsedTable(["Nombre", "Precio"], [["Crema", "no-es-numero"]], Path("x.csv"))
    config = MappingConfig(
        source="test", entity="productos", columns={"Nombre": "nombre", "Precio": "precio"}
    )

    records, issues, _ = normalize_table(table, config, run_id="run-1")

    assert records == []
    assert issues[0].code == "invalid_type"
    assert issues[0].row_number == 2
