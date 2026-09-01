"""Approval state and lifecycle transitions for a run."""

from __future__ import annotations

from datetime import UTC, datetime

from ingestion_pipeline.storage.artifacts import ArtifactStore


def approve_run(store: ArtifactStore, run_id: str, approved_by: str) -> dict[str, object]:
    manifest_data = store.load_manifest(run_id)
    report = store.load_report(run_id)
    if manifest_data.get("status") != "ready":
        raise ValueError(
            f"El run está en estado `{manifest_data.get('status')}`; solo se puede aprobar un run listo."
        )
    if report.get("invalid_rows", 0) != 0 or report.get("valid_rows", 0) == 0:
        raise ValueError("Solo se puede aprobar un lote con filas válidas y cero errores.")
    if not approved_by.strip():
        raise ValueError("Debes indicar quién aprueba el lote.")
    manifest_data["status"] = "approved"
    manifest_data["approved_by"] = approved_by.strip()
    manifest_data["approved_at"] = datetime.now(UTC).isoformat()
    path = store.manifests / f"{run_id}.json"
    import json

    path.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_data
