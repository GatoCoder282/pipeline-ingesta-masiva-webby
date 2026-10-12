from pathlib import Path

from ingestion_pipeline.config import DocumentConfig
from ingestion_pipeline.documents.catalog import extract_catalog
from ingestion_pipeline.documents.extractors import _paddle_items
from ingestion_pipeline.documents.markdown import extraction_to_markdown, page_markdown_lines
from ingestion_pipeline.documents.models import DocumentPage, ExtractedBlock, ExtractionResult
from ingestion_pipeline.documents.quality import assess_description
from ingestion_pipeline.documents.review import write_description_review_html


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
        "source: test\nocr:\n  language: spa\n  engine: auto\n  runtime: cpu\n  dpi: 200\nlayout:\n  columns: 2\nquality:\n  gate: true\n  strict_visual: true\n  noise_tokens: [ocrojojo]\n",
        encoding="utf-8",
    )

    config = DocumentConfig.from_file(path)

    assert config.source == "test"
    assert config.ocr_language == "spa"
    assert config.columns == 2
    assert config.ocr_engine == "auto"
    assert config.quality_gate is True
    assert config.description_noise_tokens == ("ocrojojo",)


def test_catalog_reconstructs_parallel_columns_before_normalization() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("title", 1, "Aceite de Limpieza", (100, 10, 450, 40), 0.95),
            ExtractedBlock(
                "left1",
                1,
                "Nuestro aceite limpia, suaviza y protege la piel.",
                (70, 90, 500, 112),
                0.95,
            ),
            ExtractedBlock("left2", 1, "¿Por qué elegirlo?", (70, 140, 400, 162), 0.95),
            ExtractedBlock(
                "left3",
                1,
                "Contiene aceites cuidadosamente seleccionados.",
                (70, 170, 500, 192),
                0.95,
            ),
            ExtractedBlock("right1", 1, "¿Cómo se utiliza?", (560, 95, 900, 117), 0.95),
            ExtractedBlock(
                "right2",
                1,
                "Aplica la cantidad necesaria sobre el rostro seco.",
                (560, 130, 980, 152),
                0.95,
            ),
            ExtractedBlock(
                "right3", 1, "Retira con abundante agua templada.", (560, 165, 900, 187), 0.95
            ),
            ExtractedBlock("badge", 1, "FRAGANCE FREE", (70, 220, 300, 238), 0.95),
        ),
        run_id="run-columns",
        config=DocumentConfig(quality_gate=True, strict_visual=True),
    )

    description = result.products[0].data["descripcion"]
    assert description.index("Nuestro") < description.index("¿Cómo")
    assert "FRAGANCE" not in description
    assert "\n\n" in description
    assert result.products[0].source["field_evidence"]["descripcion_quality"]["status"] == "pass"


def test_catalog_reads_lower_callout_columns_without_interleaving() -> None:
    result = extract_catalog(
        _document(
            ExtractedBlock("title", 1, "Máscara Iluminadora", (320, 110, 520, 180), 0.95),
            ExtractedBlock("body", 1, "Descripción principal legible.", (75, 330, 300, 345), 0.95),
            ExtractedBlock("warning", 1, "ADVERTENCIAS:", (75, 700, 150, 715), 0.95),
            ExtractedBlock("warning-text", 1, "Uso externo.", (75, 725, 150, 740), 0.95),
            ExtractedBlock("sensitivity", 1, "SENSIBILIDAD:", (210, 700, 300, 715), 0.95),
            ExtractedBlock(
                "sensitivity-text", 1, "Suspender si irrita.", (210, 725, 320, 740), 0.95
            ),
            ExtractedBlock("solar", 1, "PROTECCIÓN SOLAR:", (345, 700, 450, 715), 0.95),
            ExtractedBlock("solar-text", 1, "Usar protector.", (345, 725, 460, 740), 0.95),
            ExtractedBlock("storage", 1, "GUARDADO:", (480, 700, 550, 715), 0.95),
            ExtractedBlock("storage-text", 1, "Lugar fresco.", (480, 725, 570, 740), 0.95),
        ),
        run_id="run-footer-columns",
        config=DocumentConfig(quality_gate=True, strict_visual=True),
    )

    description = result.products[0].data["descripcion"]
    assert description.index("Uso externo") < description.index("Suspender")
    assert description.index("Suspender") < description.index("Usar protector")
    assert description.index("Usar protector") < description.index("Lugar fresco")
    assert "Usar protector Suspender" not in description


def test_paddle_result_parser_supports_v3_and_legacy_shapes() -> None:
    modern = _paddle_items(
        {
            "res": {
                "rec_texts": ["Crema", "Hidratante"],
                "rec_scores": [0.98, 96],
                "rec_boxes": [[1, 2, 30, 12], [[40, 2], [90, 2], [90, 12], [40, 12]]],
            }
        }
    )
    legacy = _paddle_items([[[[1, 2], [30, 2], [30, 12], [1, 12]], ("Crema", 0.9)]])

    assert modern[0][0] == "Crema"
    assert modern[1][1] == (40.0, 2.0, 90.0, 12.0)
    assert modern[1][2] == 0.96
    assert legacy[0][0] == "Crema"


def test_description_quality_flags_ocr_noise() -> None:
    quality = assess_description("Texto legible. FREE FREE ocrojojo", engine="tesseract")

    assert quality.status == "reconstruct"
    assert "ocr_noise_tokens" in quality.flags


def test_description_review_is_sorted_by_brand(tmp_path: Path) -> None:
    products = tmp_path / "productos.csv"
    products.write_text(
        "marca,nombre,descripcion\nRouse Arey,Zeta,Texto legible.\nLibra,Alpha,Otra descripción.\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    report.write_text(
        '{"product_quality":{"descriptions":{"items":[{"row_number":2,"status":"pass"},{"row_number":3,"status":"pass"}]}}}',
        encoding="utf-8",
    )
    output = tmp_path / "review.html"

    result = write_description_review_html(products, report, output)

    content = output.read_text(encoding="utf-8")
    assert result["rows"] == 2
    assert content.index("Libra") < content.index("Rouse Arey")
