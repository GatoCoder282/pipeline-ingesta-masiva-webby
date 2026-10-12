"""Review artifacts for visual and language-oriented description QA."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from ingestion_pipeline.documents.quality import assess_description


def write_description_quality_report(
    products_csv: Path,
    output_path: Path,
    *,
    min_chars: int = 40,
) -> dict[str, object]:
    """Assess the descriptions currently exported in a product CSV."""
    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    items: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for index, row in enumerate(rows, start=2):
        brand = str(row.get("marca") or "")
        quality = assess_description(
            row.get("descripcion", ""),
            engine="native" if brand == "ZO-É" else "tesseract",
            layout_reconstructed=True,
            strict_visual=True,
            min_chars=min_chars,
        )
        item = {
            "row_number": index,
            "sku": row.get("sku", ""),
            "marca": brand,
            "nombre": row.get("nombre", ""),
            **quality.as_json(),
        }
        items.append(item)
        counts[quality.status] = counts.get(quality.status, 0) + 1
    report = {
        "products_csv": str(products_csv),
        "product_quality": {
            "total": len(rows),
            "descriptions": {
                "gate_enabled": True,
                "strict_visual": True,
                "min_description_chars": min_chars,
                "counts": counts,
                "blocked": counts.get("quarantine", 0) > 0,
                "items": items,
            },
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def write_description_review_html(
    products_csv: Path, report_path: Path, output_path: Path
) -> dict[str, object]:
    """Write a sortable-by-brand human review page without changing catalog data."""
    with products_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    quality = report.get("product_quality", {}).get("descriptions", {})
    if not quality:
        quality = report.get("description_quality", {})
    items_by_row = {
        str(item.get("row_number")): item
        for item in quality.get("items", [])
        if isinstance(item, dict)
    }
    paired_rows = [
        (row, items_by_row.get(str(index), {})) for index, row in enumerate(rows, start=2)
    ]
    paired_rows.sort(
        key=lambda pair: (
            pair[0].get("marca", "").casefold(),
            pair[0].get("nombre", "").casefold(),
        )
    )
    body: list[str] = []
    for row, item in paired_rows:
        flags = ", ".join(str(flag) for flag in item.get("flags", []))
        status = str(item.get("status", "sin_evaluar"))
        body.append(
            "<tr>"
            f"<td>{html.escape(row.get('marca', ''))}</td>"
            f"<td>{html.escape(row.get('nombre', ''))}</td>"
            f'<td class="status-{html.escape(status)}">{html.escape(status)}</td>'
            f"<td>{html.escape(flags)}</td>"
            f'<td class="description">{html.escape(row.get("descripcion", ""))}</td>'
            "</tr>"
        )
    document = (
        """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Revisión de descripciones</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;color:#202124} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #d9dce1;padding:.55rem;vertical-align:top;text-align:left} th{background:#f1f3f4}
.description{white-space:pre-wrap;min-width:32rem;line-height:1.45}.status-pass{color:#137333}.status-reconstruct{color:#b06000}.status-quarantine{color:#a50e0e}
</style></head><body><h1>Revisión de descripciones</h1>
<p>Ordenado por marca. La evidencia original permanece en los JSONL/Markdown del run.</p>
<table><thead><tr><th>Marca</th><th>Nombre</th><th>Estado</th><th>Flags</th><th>Descripción</th></tr></thead>
<tbody>"""
        + "".join(body)
        + "</tbody></table></body></html>\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    counts: dict[str, int] = {}
    for item in items_by_row.values():
        status = str(item.get("status", "sin_evaluar"))
        counts[status] = counts.get(status, 0) + 1
    return {"output": str(output_path), "rows": len(rows), "quality_counts": counts}
