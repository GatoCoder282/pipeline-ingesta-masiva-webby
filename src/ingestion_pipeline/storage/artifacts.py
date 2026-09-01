"""Filesystem artifact store with a DuckDB/Parquet projection."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

from ingestion_pipeline.domain.catalog import fields_for
from ingestion_pipeline.domain.models import (
    NormalizedRecord,
    RunManifest,
    ValidationReport,
    json_dumps,
)


class ArtifactStore:
    def __init__(self, root: Path = Path("data")) -> None:
        self.root = root
        self.raw = root / "raw"
        self.incoming = root / "incoming"
        self.extracted = root / "extracted"
        self.processed = root / "processed"
        self.quarantine = root / "quarantine"
        self.reports = root / "reports"
        self.manifests = root / "manifests"
        self.warehouse = root / "warehouse"
        for directory in (
            self.incoming,
            self.extracted,
            self.raw,
            self.processed,
            self.quarantine,
            self.reports,
            self.manifests,
            self.warehouse,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def copy_raw(self, run_id: str, path: Path) -> Path:
        destination = self.raw / f"{run_id}__{path.name}"
        shutil.copy2(path, destination)
        return destination

    def write_jsonl(
        self, directory: Path, run_id: str, suffix: str, records: Iterable[dict[str, object]]
    ) -> Path:
        destination = directory / f"{run_id}__{suffix}.jsonl"
        with destination.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json_dumps(record) + "\n")
        return destination

    def write_processed(
        self, run_id: str, entity: str, records: list[NormalizedRecord]
    ) -> tuple[Path, Path]:
        jsonl = self.write_jsonl(
            self.processed, run_id, entity, (record.as_json() for record in records)
        )
        parquet = self.processed / f"{run_id}__{entity}.parquet"
        if not records:
            import duckdb

            columns = ", ".join(
                f'CAST(NULL AS VARCHAR) AS "{field.name}"' for field in fields_for(entity)
            )
            connection = duckdb.connect()
            try:
                connection.execute(
                    f"COPY (SELECT {columns} WHERE FALSE) TO ? (FORMAT PARQUET)",
                    [str(parquet)],
                )
            finally:
                connection.close()
            return jsonl, parquet
        with NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".jsonl", delete=False, dir=self.warehouse
        ) as temp:
            for record in records:
                payload = dict(record.data)
                payload["_run_id"] = run_id
                payload["_source_row"] = record.row_number
                temp.write(json_dumps(payload) + "\n")
            temporary_path = Path(temp.name)
        try:
            import duckdb

            connection = duckdb.connect(str(self.warehouse / "ingestion.duckdb"))
            try:
                relation = connection.sql(
                    "SELECT * FROM read_json_auto(?)",
                    params=[str(temporary_path)],
                )
                relation.write_parquet(str(parquet), overwrite=True)
                connection.execute(
                    "CREATE OR REPLACE TABLE normalized_records AS SELECT * FROM read_parquet(?)",
                    [str(parquet)],
                )
            finally:
                connection.close()
        finally:
            temporary_path.unlink(missing_ok=True)
        return jsonl, parquet

    def write_report(self, report: ValidationReport) -> Path:
        destination = self.reports / f"{report.run_id}.json"
        destination.write_text(json_dumps(report.as_json()) + "\n", encoding="utf-8")
        return destination

    def write_report_dict(self, run_id: str, report: dict[str, object]) -> Path:
        destination = self.reports / f"{run_id}.json"
        destination.write_text(json_dumps(report) + "\n", encoding="utf-8")
        return destination

    def write_extraction(self, run_id: str, extraction: dict[str, object]) -> Path:
        destination = self.extracted / f"{run_id}.json"
        destination.write_text(json_dumps(extraction) + "\n", encoding="utf-8")
        return destination

    def write_extraction_markdown(self, run_id: str, content: str) -> Path:
        destination = self.extracted / f"{run_id}.md"
        destination.write_text(content, encoding="utf-8")
        return destination

    def write_catalog_result(self, run_id: str, result: dict[str, object]) -> Path:
        destination = self.processed / f"{run_id}__catalog.json"
        destination.write_text(json_dumps(result) + "\n", encoding="utf-8")
        return destination

    def write_processed_csv(self, run_id: str, entity: str, content: bytes) -> Path:
        destination = self.processed / f"{run_id}__{entity}.csv"
        destination.write_bytes(content)
        return destination

    def write_manifest(self, manifest: RunManifest) -> Path:
        destination = self.manifests / f"{manifest.run_id}.json"
        destination.write_text(json_dumps(manifest.as_json()) + "\n", encoding="utf-8")
        return destination

    def write_manifest_dict(self, manifest: dict[str, object]) -> Path:
        run_id = str(manifest["run_id"])
        destination = self.manifests / f"{run_id}.json"
        destination.write_text(json_dumps(manifest) + "\n", encoding="utf-8")
        return destination

    def load_manifest(self, run_id: str) -> dict[str, object]:
        path = self.manifests / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No existe el manifiesto para run_id={run_id}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def load_report(self, run_id: str) -> dict[str, object]:
        path = self.reports / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"No existe el reporte para run_id={run_id}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def load_processed_records(self, run_id: str, entity: str) -> list[dict[str, object]]:
        path = self.processed / f"{run_id}__{entity}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"No existe el artefacto procesado para run_id={run_id}.")
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
