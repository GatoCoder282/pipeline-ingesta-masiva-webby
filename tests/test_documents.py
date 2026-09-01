from pathlib import Path

from ingestion_pipeline.config import DocumentConfig
from ingestion_pipeline.documents.catalog import extract_catalog
from ingestion_pipeline.documents.markdown import extraction_to_markdown, page_markdown_lines
from ingestion_pipeline.documents.models import DocumentPage, ExtractedBlock, ExtractionResult


def _document(*blocks: ExtractedBlock) -> ExtractionResult:
    return ExtractionResult(
        source_file="catalogo.pdf",
        media_type="application/pdf",
        document_type="text_based",
        engine="test",
        pages=(DocumentPage(1, 1000, 1000, tuple(blocks), "native"),),
    )


def test_catalog_extraction_keeps_product_variant_and_locator() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("b1", 1, "Código: PR-001", (10, 10, 300, 25), 1.0),
            ExtractedBlock("b2", 1, "Crema hidratante", (10, 30, 300, 45), 1.0),
            ExtractedBlock("b3", 1, "Precio: Bs 89,90", (10, 50, 300, 65), 1.0),
            ExtractedBlock("b4", 1, "variante: color=rojo;talla=M", (10, 70, 300, 85), 1.0),
            ExtractedBlock("b5", 1, "Código: PR-002", (10, 160, 300, 175), 1.0),
            ExtractedBlock("b6", 1, "Protector solar", (10, 180, 300, 195), 1.0),
            ExtractedBlock("b7", 1, "Precio: 125.50", (10, 200, 300, 215), 1.0),
        ),
        run_id="run-1",
        config=DocumentConfig(card_vertical_gap=40),
    )

    assert len(result.products) == 2
    assert result.products[0].data["sku"] == "PR-001"
    assert result.products[0].data["tipo"] == "variable"
    assert result.products[0].source["page_number"] == 1
    assert result.products[0].source["block_ids"] == ["b1", "b2", "b3", "b4"]
    assert len(result.variants) == 1
    assert result.variants[0].data["sku_producto"] == "PR-001"
    assert result.variants[0].data["atributos"] == {"color": "rojo", "talla": "M"}
    assert not result.issues


def test_catalog_extraction_blocks_low_confidence_cards() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("b1", 1, "Código: PR-001", (10, 10, 300, 25), 0.4),
            ExtractedBlock("b2", 1, "Crema hidratante", (10, 30, 300, 45), 0.4),
        ),
        run_id="run-1",
        config=DocumentConfig(confidence_threshold=0.65),
    )

    assert any(issue.code == "low_confidence" for issue in result.product_issues)


def test_catalog_extraction_reads_product_sheet_presentations_as_variants() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("b1", 1, "Hidratación inteligente y profunda.", (10, 10, 300, 20), 0.95),
            ExtractedBlock("b2", 1, "Máscara\nHidratante", (10, 30, 300, 70), 0.95),
            ExtractedBlock("b3", 1, "Presentaciones: 250grs y 500grs", (10, 80, 300, 95), 0.95),
        ),
        run_id="run-sheet",
        config=DocumentConfig(),
    )

    assert len(result.products) == 1
    assert result.products[0].data["nombre"] == "Máscara Hidratante"
    assert result.products[0].data["tipo"] == "variable"
    assert [variant.data["atributos"] for variant in result.variants] == [
        {"presentacion": "250grs"},
        {"presentacion": "500grs"},
    ]


def test_extraction_result_is_json_serializable() -> None:
    result = _document(ExtractedBlock("b1", 1, "Crema", (1, 2, 3, 4), 1.0))

    payload = result.as_json()

    assert payload["page_count"] == 1
    assert payload["pages"][0]["blocks"][0]["bbox"] == [1, 2, 3, 4]


def test_markdown_projection_keeps_text_and_locator() -> None:
    result = _document(ExtractedBlock("b1", 1, "M\u00c3\u00a1scara", (1, 2, 3, 4), 0.91))

    markdown = extraction_to_markdown(result)
    lines = page_markdown_lines(result.pages[0])

    assert "M\u00e1scara" in markdown
    assert '"block_id":"b1"' in markdown
    assert lines[0].text == "M\u00e1scara"


def test_catalog_extraction_separates_normalized_name_and_description() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("b1", 1, "M\u00c1SCARA DE LIM\u00d3N", (10, 10, 400, 40), 1.0),
            ExtractedBlock("b2", 1, "Descripci\u00f3n", (10, 60, 300, 75), 1.0),
            ExtractedBlock(
                "b3",
                1,
                "Mascarilla facial que hidrata y mejora la luminosidad.",
                (10, 80, 500, 100),
                1.0,
            ),
            ExtractedBlock("b4", 1, "Presentaciones: 50 ml y 100 ml", (10, 120, 350, 135), 1.0),
            ExtractedBlock(
                "b5", 1, "Modo de uso: Aplicar sobre la piel limpia.", (10, 150, 400, 165), 1.0
            ),
        ),
        run_id="run-description",
        config=DocumentConfig(),
    )

    product = result.products[0]

    assert product.data["nombre"] == "M\u00e1scara de Lim\u00f3n"
    assert product.data["descripcion"] == ("Mascarilla facial que hidrata y mejora la luminosidad.")
    assert product.source["field_evidence"]["nombre"]["block_ids"] == ["b1"]
    assert product.source["field_evidence"]["descripcion"]["block_ids"] == ["b3"]


def test_document_config_loads_tenant_profile(tmp_path: Path) -> None:
    path = tmp_path / "document.yml"
    path.write_text(
        "source: test\nocr:\n  language: spa\n  dpi: 200\nlayout:\n  columns: 2\n",
        encoding="utf-8",
    )

    config = DocumentConfig.from_file(path)

    assert config.source == "test"
    assert config.ocr_language == "spa"
    assert config.columns == 2
