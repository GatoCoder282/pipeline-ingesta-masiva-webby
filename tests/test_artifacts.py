import json
from pathlib import Path

from ingestion_pipeline.domain.models import NormalizedRecord
from ingestion_pipeline.storage.artifacts import ArtifactStore


def test_artifact_store_keeps_manifest_and_processed_jsonl(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "data")
    record = NormalizedRecord(2, "productos", {"sku": "A-1", "nombre": "Crema"})

    jsonl, parquet = store.write_processed("run-1", "productos", [record])

    assert jsonl.exists()
    assert parquet.exists()
    payload = json.loads(jsonl.read_text(encoding="utf-8"))
    assert payload["data"]["sku"] == "A-1"


def test_artifact_store_writes_a_valid_empty_parquet(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "data")

    _, parquet = store.write_processed("run-empty", "variantes", [])

    import duckdb

    rows = duckdb.connect().execute("SELECT * FROM read_parquet(?)", [str(parquet)]).fetchall()
    assert rows == []
