import csv
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from ingestion_pipeline.enrichment import (
    assign_catalog_skus,
    enrich_products_csv,
    enrich_variants_csv,
    improve_pdf_descriptions_csv,
    merge_pdf_catalog_csvs,
    normalize_description,
    normalize_title,
    parse_price_document,
    sync_source_jsonl_order,
)
from ingestion_pipeline.normalization.mapping import normalize_attributes

DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:tbl>
    <w:tr><w:tc><w:p><w:r><w:t>Producto</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Precio POR unid. bs</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Precio venta A profesional</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>Crema age firming 50g</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>36,02</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>110</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl></w:body>
</w:document>"""

MULTI_PRICE_DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:tbl>
    <w:tr><w:tc><w:p><w:r><w:t>Producto</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Precio POR unid. bs</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Precio venta A profesional</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>Producto 250g</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>20</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>50</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>Producto 500g</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>30</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>70</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>Producto Simple</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>30</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl></w:body>
</w:document>"""


def _write_docx(path: Path) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", DOC_XML)


def test_parse_price_document_reads_unit_cost_and_professional_price(tmp_path: Path) -> None:
    source = tmp_path / "precios.docx"
    _write_docx(source)

    rows = parse_price_document(source, "Libra")

    assert rows[0].raw_name == "Crema age firming 50g"
    assert str(rows[0].cost) == "36.02"
    assert str(rows[0].price) == "110"


def test_normalize_attributes_preserves_multiple_keys_and_rejects_duplicates() -> None:
    assert normalize_attributes("color=rojo;talla=M") == {"color": "rojo", "talla": "M"}
    assert normalize_attributes('{"Talla": "M", "Color": "rojo"}') == {
        "color": "rojo",
        "talla": "M",
    }
    with pytest.raises(ValueError, match="repetida"):
        normalize_attributes("color=rojo;color=azul")
    with pytest.raises(ValueError, match="repetida"):
        normalize_attributes('{"Color":"rojo","color":"azul"}')


def test_normalization_keeps_acronyms_and_formats_description_sections() -> None:
    assert normalize_title("therapy n°3 acido hialuronico") == ("Therapy N°3 Ácido Hialurónico")
    result = normalize_description("texto inicial. segundo punto. Activos: vitamina C.")
    assert result == "Texto inicial. Segundo punto.\n\nActivos: vitamina C."
    assert normalize_description("VITAMINA E: AyUda a cuidar la piel.") == (
        "Vitamina e: Ayuda a cuidar la piel."
    )
    assert normalize_title("blend AHA's tca 20%") == "Blend AHA's TCA 20%"


def test_normalization_removes_reviewed_ocr_badges_without_erasing_copy() -> None:
    result = normalize_description(
        "Texto inicial. OCROJOJO. PRECIOS\n\nODO TIPO DE PIEL.\n\nTodo tipo de piel."
    )
    assert result == "Texto inicial.\n\nTodo tipo de piel."


def test_enrich_products_uses_price_list_and_sorts_by_brand(tmp_path: Path) -> None:
    csv_path = tmp_path / "productos.csv"
    csv_path.write_text(
        "sku,nombre,descripcion,marca,precio,costo\n"
        ",Mascara Purificante,Texto inicial. activos: zinc.,,0.5,\n"
        ",Age & Firming Suppo,Texto.,,9,\n",
        encoding="utf-8",
    )
    jsonl_path = tmp_path / "productos.jsonl"
    jsonl_path.write_text(
        json.dumps({"source": {"source_file": "WhatsApp Image 2026-08-31 at 19.44.17.jpeg"}})
        + "\n"
        + json.dumps({"source": {"source_file": "WhatsApp Image 2026-08-31 at 19.44.09 (1).jpeg"}})
        + "\n",
        encoding="utf-8",
    )
    price_doc = tmp_path / "precios libra agosto 2026.docx"
    _write_docx(price_doc)
    profile = tmp_path / "profile.yml"
    profile.write_text(
        "source: test\n"
        "default_brand: Rouse Arey\n"
        "brand_order: [Libra, Rouse Arey]\n"
        "source_prefixes:\n  Libra: ['WhatsApp Image 2026-08-31 at 19.44.09']\n"
        "documents:\n  - file: 'precios libra agosto 2026.docx'\n    brand: Libra\n"
        "aliases:\n  Libra:\n    'Crema age firming 50g': 'Age & Firming Suppo'\n"
        "name_overrides:\n  'Mascara Purificante': 'Máscara Purificante'\n",
        encoding="utf-8",
    )

    report = enrich_products_csv(
        csv_path,
        profile_path=profile,
        prices_dir=tmp_path,
        source_jsonl=jsonl_path,
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    assert rows[0]["marca"] == "Libra"
    assert rows[0]["precio"] == "110"
    assert rows[0]["costo"] == "36.02"
    assert rows[1]["marca"] == "Rouse Arey"
    assert rows[1]["precio"] == ""
    assert rows[1]["nombre"] == "Máscara Purificante"
    assert report["matched_price_rows"] == 1


def test_enrich_variants_applies_docx_values_per_presentation_and_keeps_all_attributes(
    tmp_path: Path,
) -> None:
    products_csv = tmp_path / "productos.csv"
    products_csv.write_text(
        "sku,nombre,marca,tipo,precio,costo\n"
        "P-1,Producto Variable,Libra,variable,50,20\n"
        "P-2,Producto Simple,Libra,simple,,\n",
        encoding="utf-8",
    )
    variants_csv = tmp_path / "variantes.csv"
    variants_csv.write_text(
        "sku_producto,sku_variante,atributos,precio,stock\n"
        'P-1,P-1-V01,"{""presentacion"": ""250 g"", ""color"": ""rojo""}",50,\n'
        'P-1,P-1-V02,"{""talla"": ""M"", ""presentacion"": ""500 g""}",70,\n',
        encoding="utf-8",
    )
    product_source = tmp_path / "productos.jsonl"
    product_source.write_text(
        "\n".join(
            json.dumps(
                {
                    "data": {"nombre": name},
                    "source": {"source_file": "catalogo.jpg"},
                }
            )
            for name in ("Producto Variable", "Producto Simple")
        )
        + "\n",
        encoding="utf-8",
    )
    variant_source = tmp_path / "variantes.jsonl"
    variant_source.write_text(
        "\n".join(
            json.dumps(
                {
                    "data": {"sku_variante": sku},
                    "source": {"source_file": "catalogo.jpg"},
                }
            )
            for sku in ("P-1-V01", "P-1-V02")
        )
        + "\n",
        encoding="utf-8",
    )
    price_doc = tmp_path / "precios.docx"
    _write_docx(price_doc)
    with ZipFile(price_doc, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", MULTI_PRICE_DOC_XML)
    profile = tmp_path / "profile.yml"
    profile.write_text(
        "source: test\n"
        "default_brand: Libra\n"
        "brand_order: [Libra]\n"
        "documents:\n  - file: 'precios.docx'\n    brand: Libra\n"
        "aliases:\n  Libra:\n"
        "    'Producto 250g': 'Producto Variable'\n"
        "    'Producto 500g': 'Producto Variable'\n"
        "    'Producto Simple': 'Producto Simple'\n",
        encoding="utf-8",
    )
    output_products = tmp_path / "productos-final.csv"
    output_variants = tmp_path / "variantes-final.csv"
    output_product_source = tmp_path / "productos-final.jsonl"
    output_variant_source = tmp_path / "variantes-final.jsonl"

    report = enrich_variants_csv(
        products_csv,
        variants_csv,
        profile_path=profile,
        prices_dir=tmp_path,
        product_source_jsonl=product_source,
        variant_source_jsonl=variant_source,
        output_products_path=output_products,
        output_variants_path=output_variants,
        output_product_source_jsonl=output_product_source,
        output_variant_source_jsonl=output_variant_source,
    )

    products = list(csv.DictReader(output_products.open(encoding="utf-8", newline="")))
    variants = list(csv.DictReader(output_variants.open(encoding="utf-8", newline="")))
    assert products[0]["precio"] == "50"
    assert products[0]["costo"] == "20"
    assert products[1]["precio"] == "30"
    assert products[1]["costo"] == "10"
    assert "costo" not in variants[0]
    assert variants[0]["precio"] == "50"
    assert json.loads(variants[0]["atributos"]) == {
        "color": "rojo",
        "presentacion": "250 g",
    }
    assert variants[1]["precio"] == "70"
    assert report["matched_price_rows"] == 2
    assert len(report["product_only_rollups"]) == 2
    assert report["variants_with_cost_evidence"] == 2
    assert report["variant_cost_in_csv"] is False
    evidence = [
        json.loads(line) for line in output_variant_source.read_text(encoding="utf-8").splitlines()
    ]
    assert evidence[0]["data"]["atributos"]["color"] == "rojo"
    assert evidence[0]["price_evidence"]["cost_bs"] == "20"


def test_sync_source_jsonl_order_follows_sorted_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "productos.csv"
    csv_path.write_text(
        "sku,nombre,descripcion,marca,precio,costo\n"
        ",Age & Firming Suppo,Texto.,Libra,110,36.02\n"
        ",Mascara Purificante,Texto.,Rouse Arey,,\n",
        encoding="utf-8",
    )
    source_jsonl = tmp_path / "productos.jsonl"
    source_jsonl.write_text(
        json.dumps(
            {
                "data": {"nombre": "Mascara Purificante"},
                "source": {"source_file": "WhatsApp Image 2026-08-31 at 19.44.17.jpeg"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "data": {"nombre": "Age & Firming Suppo"},
                "source": {"source_file": "WhatsApp Image 2026-08-31 at 19.44.09 (1).jpeg"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.yml"
    profile.write_text(
        "source: test\n"
        "default_brand: Rouse Arey\n"
        "brand_order: [Libra, Rouse Arey]\n"
        "source_prefixes:\n  Libra: ['WhatsApp Image 2026-08-31 at 19.44.09']\n"
        "documents:\n  - file: 'precios.docx'\n    brand: Libra\n"
        "aliases: {}\n"
        "name_overrides:\n  'Mascara Purificante': 'Máscara Purificante'\n",
        encoding="utf-8",
    )

    assert sync_source_jsonl_order(csv_path, source_jsonl, profile) == 2
    records = [json.loads(line) for line in source_jsonl.read_text(encoding="utf-8").splitlines()]
    assert [record["data"]["nombre"] for record in records] == [
        "Age & Firming Suppo",
        "Mascara Purificante",
    ]


def test_merge_pdf_catalog_writes_product_and_variant_prices(tmp_path: Path) -> None:
    products_csv = tmp_path / "productos.csv"
    products_csv.write_text(
        "sku,nombre,descripcion,marca,precio,costo\n,Existing,Texto.,Rouse Arey,,\n",
        encoding="utf-8",
    )
    product_source = tmp_path / "productos.jsonl"
    product_source.write_text(
        json.dumps(
            {
                "data": {"nombre": "Existing"},
                "source": {"source_file": "old.jpeg"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    variants_csv = tmp_path / "variantes.csv"
    variants_csv.write_text(
        "sku_producto,sku_variante,atributos,precio,precio_comparacion,stock,activa\n,,, ,,,\n",
        encoding="utf-8",
    )
    variant_source = tmp_path / "variantes-candidatos.jsonl"
    variant_source.write_text(
        json.dumps(
            {
                "data": {"atributos": {"presentacion": "old"}},
                "entity": "variantes",
                "source": {"source_file": "old.jpeg"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    extraction = tmp_path / "zoe-products.jsonl"
    extraction.write_text(
        json.dumps(
            {
                "data": {"nombre": "Wrong OCR", "descripcion": "Texto de fuente."},
                "row_number": 2,
                "source": {
                    "source_file": "Catalogo ZOE.pdf",
                    "page_number": 4,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    profile = tmp_path / "zoe.yml"
    profile.write_text(
        f"source: test-zoe\n"
        f"pdf_file: 'Catalogo ZOE.pdf'\n"
        f"extraction_jsonl: '{extraction.as_posix()}'\n"
        "brand: Zoé\n"
        "brand_order: [Rouse Arey, Zoé]\n"
        "products:\n"
        "  - page: 4\n"
        "    source_row: 2\n"
        "    name: 'TCA 20%'\n"
        "    category: 'Peelings químicos'\n"
        "    variants:\n"
        "      - {presentation: '50 ml', price: 170}\n",
        encoding="utf-8",
    )

    report = merge_pdf_catalog_csvs(
        products_csv,
        variants_csv,
        profile_path=profile,
        product_source_jsonl=product_source,
        variant_source_jsonl=variant_source,
    )

    products = list(csv.DictReader(products_csv.open(encoding="utf-8", newline="")))
    variants = list(csv.DictReader(variants_csv.open(encoding="utf-8", newline="")))
    assert products[-1]["nombre"] == "TCA 20%"
    assert products[-1]["precio"] == "170"
    assert products[-1]["costo"] == ""
    assert variants[-1]["atributos"] == '{"presentacion": "50 ml"}'
    assert variants[-1]["precio"] == "170"
    assert report["products_added"] == 1
    assert report["variants_added"] == 1


def test_improve_pdf_descriptions_uses_position_aware_sections(tmp_path: Path) -> None:
    products_csv = tmp_path / "productos.csv"
    products_csv.write_text(
        "sku,nombre,descripcion,marca,precio,costo\n,TCA 20%,Texto anterior,Zoé,170,\n",
        encoding="utf-8",
    )
    source_jsonl = tmp_path / "productos.jsonl"
    source_jsonl.write_text(
        json.dumps(
            {
                "data": {"nombre": "TCA 20%", "descripcion": "Texto crudo"},
                "source": {"source_file": "Catalogo ZOE.pdf", "page_number": 42},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    extraction_json = tmp_path / "zoe.json"
    extraction_json.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page_number": 42,
                        "blocks": [
                            {
                                "block_id": "p0042-native0001",
                                "bbox": "382 165 725 296",
                                "text": "Introducción del producto.",
                            },
                            {
                                "block_id": "p0042-native0002",
                                "bbox": "57 384 500 435",
                                "text": "Caracteristicas",
                            },
                            {
                                "block_id": "p0042-native0003",
                                "bbox": "57 436 725 470",
                                "text": "Solución de Ácido Tricloroacético.",
                            },
                            {
                                "block_id": "p0042-native0004",
                                "bbox": "57 500 725 560",
                                "text": "Acción",
                            },
                            {
                                "block_id": "p0042-native0005",
                                "bbox": "57 561 725 620",
                                "text": "Actúa sobre la piel.",
                            },
                            {
                                "block_id": "p0042-native0006",
                                "bbox": "57 630 725 690",
                                "text": "Indicaciones",
                            },
                            {
                                "block_id": "p0042-native0007",
                                "bbox": "57 691 725 740",
                                "text": "Uso profesional.",
                            },
                            {
                                "block_id": "p0042-native0008",
                                "bbox": "500 800 700 850",
                                "text": "Protector Solar",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "zoe.yml"
    profile.write_text(
        f"source: test-zoe\n"
        f"pdf_file: 'Catalogo ZOE.pdf'\n"
        f"extraction_jsonl: '{source_jsonl.as_posix()}'\n"
        f"extraction_json: '{extraction_json.as_posix()}'\n"
        "brand: Zoé\n"
        "brand_order: [Zoé]\n"
        "products:\n"
        "  - page: 42\n"
        "    source_row: 1\n"
        "    name: 'TCA 20%'\n"
        "    category: 'Peelings químicos'\n"
        "    variants:\n"
        "      - {presentation: '50 ml', price: 170}\n",
        encoding="utf-8",
    )

    report = improve_pdf_descriptions_csv(
        products_csv,
        profile_path=profile,
        product_source_jsonl=source_jsonl,
    )

    products = list(csv.DictReader(products_csv.open(encoding="utf-8", newline="")))
    evidence = json.loads(source_jsonl.read_text(encoding="utf-8").splitlines()[0])
    description = products[0]["descripcion"]
    assert report["pdf_rows_improved"] == 1
    assert "Características:" in description
    assert "Acción:" in description
    assert "Indicaciones:" in description
    assert "Protector Solar" not in description
    assert evidence["enrichment"]["description_improvement"]["method"] == (
        "native_pdf_blocks_layout"
    )


def test_assign_catalog_skus_links_variants_and_reuses_persistent_map(tmp_path: Path) -> None:
    products_csv = tmp_path / "productos.csv"
    products_csv.write_text(
        "sku,nombre,descripcion,marca,tipo,precio,costo\n"
        ",Producto Variable,Texto.,Libra,variable,100,40\n"
        ",Producto Simple,Texto.,Zoé,simple,80,\n",
        encoding="utf-8",
    )
    product_source = tmp_path / "productos.jsonl"
    product_source.write_text(
        "\n".join(
            json.dumps(
                {
                    "data": {"nombre": name},
                    "source": {
                        "source_file": source,
                        "page_number": page,
                        "field_evidence": {
                            "nombre": {
                                "text": name,
                                "bbox": [10, page, 100, page + 10],
                            }
                        },
                    },
                }
            )
            for name, source, page in (
                ("Producto Variable", "libra.pdf", 1),
                ("Producto Simple", "zoe.pdf", 2),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    variants_csv = tmp_path / "variantes.csv"
    variants_csv.write_text(
        "sku_producto,sku_variante,atributos,precio,stock\n"
        ',,"{""presentacion"": ""50 ml""}",100,\n',
        encoding="utf-8",
    )
    variant_source = tmp_path / "variantes.jsonl"
    variant_source.write_text(
        json.dumps(
            {
                "data": {"atributos": {"presentacion": "50 ml"}},
                "entity": "variantes",
                "row_number": 2,
                "source": {
                    "source_file": "libra.pdf",
                    "page_number": 1,
                    "field_evidence": {"nombre": {"text": "PRODUCTO VARIABLE"}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    profile = tmp_path / "sku.yml"
    profile.write_text(
        "source: test\n"
        "product_pattern: '{prefix}-{number:04d}'\n"
        "variant_pattern: '{product_sku}-V{number:02d}'\n"
        "brand_prefixes:\n  Libra: LIB\n  Zoé: ZOE\n",
        encoding="utf-8",
    )
    sku_map = tmp_path / "sku-map.json"

    report = assign_catalog_skus(
        products_csv,
        variants_csv,
        profile_path=profile,
        product_source_jsonl=product_source,
        variant_source_jsonl=variant_source,
        sku_map_path=sku_map,
    )

    products = list(csv.DictReader(products_csv.open(encoding="utf-8", newline="")))
    variants = list(csv.DictReader(variants_csv.open(encoding="utf-8", newline="")))
    assert products[0]["sku"] == "LIB-0001"
    assert products[1]["sku"] == "ZOE-0001"
    assert variants[0]["sku_producto"] == "LIB-0001"
    assert variants[0]["sku_variante"] == "LIB-0001-V01"
    assert report["ready_for_ingestion"] is True

    # Re-running against the same evidence must not allocate new identifiers.
    second_report = assign_catalog_skus(
        products_csv,
        variants_csv,
        profile_path=profile,
        product_source_jsonl=product_source,
        variant_source_jsonl=variant_source,
        sku_map_path=sku_map,
    )
    products_after = list(csv.DictReader(products_csv.open(encoding="utf-8", newline="")))
    variants_after = list(csv.DictReader(variants_csv.open(encoding="utf-8", newline="")))
    assert products_after[0]["sku"] == "LIB-0001"
    assert products_after[1]["sku"] == "ZOE-0001"
    assert variants_after[0]["sku_variante"] == "LIB-0001-V01"
    assert second_report["products_with_sku"] == 2
