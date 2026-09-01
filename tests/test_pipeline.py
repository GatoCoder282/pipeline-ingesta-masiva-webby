from pathlib import Path

from ingestion_pipeline.config import DocumentConfig, MappingConfig
from ingestion_pipeline.documents.models import DocumentPage, ExtractedBlock, ExtractionResult
from ingestion_pipeline.pipeline import prepare_document_run, prepare_run, records_to_csv
from ingestion_pipeline.runs.manifest import approve_run
from ingestion_pipeline.storage.artifacts import ArtifactStore


def test_prepare_run_persists_valid_artifacts_and_requires_approval(tmp_path: Path) -> None:
    source = tmp_path / "catalogo.csv"
    source.write_text(
        "Código,Nombre,Precio,Existencias\nPR-1,Crema,89,4\n",
        encoding="utf-8",
    )
    store = ArtifactStore(tmp_path / "data")
    mapping = MappingConfig(
        source="test",
        entity="productos",
        columns={
            "Código": "sku",
            "Nombre": "nombre",
            "Precio": "precio",
            "Existencias": "stock",
        },
    )

    manifest, report = prepare_run(source, mapping, store=store)

    assert manifest.status == "ready"
    assert report["valid_rows"] == 1
    processed = store.load_processed_records(manifest.run_id, "productos")
    assert processed[0]["data"]["precio"] == "89"
    csv_content = records_to_csv(store, manifest.run_id, "productos").decode("utf-8")
    assert "sku,nombre,precio,stock" in csv_content
    assert "PR-1" in csv_content

    approved = approve_run(store, manifest.run_id, "tester")
    assert approved["status"] == "approved"


def test_approve_run_cannot_be_repeated_after_approval(tmp_path: Path) -> None:
    source = tmp_path / "catalogo.csv"
    source.write_text("Nombre\nCrema\n", encoding="utf-8")
    store = ArtifactStore(tmp_path / "data")
    mapping = MappingConfig(source="test", entity="productos", columns={"Nombre": "nombre"})

    manifest, _ = prepare_run(source, mapping, store=store)
    approve_run(store, manifest.run_id, "tester")

    import pytest

    with pytest.raises(ValueError, match="solo se puede aprobar"):
        approve_run(store, manifest.run_id, "tester-again")


def test_prepare_document_run_persists_evidence_and_both_entity_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "catalogo.pdf"
    source.write_bytes(b"fake-pdf-for-test")
    extraction = ExtractionResult(
        source_file=source.name,
        media_type="application/pdf",
        document_type="text_based",
        engine="test",
        pages=(
            DocumentPage(
                1,
                600,
                800,
                (ExtractedBlock("b1", 1, "Código: PR-1\nCrema\nPrecio: 89", (1, 2, 3, 4), 1.0),),
            ),
        ),
    )
    monkeypatch.setattr(
        "ingestion_pipeline.pipeline.extract_document", lambda path, settings: extraction
    )

    store = ArtifactStore(tmp_path / "data")
    manifest, report = prepare_document_run(
        source, store=store, document_config=DocumentConfig(card_vertical_gap=40)
    )

    assert manifest.input_kind == "document"
    assert manifest.status == "ready"
    assert report["document_type"] == "text_based"
    assert "extraction" in manifest.artifacts
    assert Path(manifest.artifacts["processed_productos_jsonl"]).exists()
    assert Path(manifest.artifacts["processed_variantes_parquet"]).exists()
    assert store.load_report(manifest.run_id)["valid_rows"] == 1
