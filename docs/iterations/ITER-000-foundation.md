# ITER-000 — Foundation

## Entregado

- CLI `preview`, `validate`, `approve`, `publish`, `inspect`.
- CSV/XLSX y mappings YAML.
- contratos canónicos de catálogo compatibles con Webby.
- validación local, cuarentena, reportes y manifiestos.
- Parquet/DuckDB.
- Webby HTTP client con dry-run y polling.
- Docker Compose, Kestra y n8n opcional.

## Evidencia esperada

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## Siguiente iteración

Probar con dos archivos anonimizados de tiendas distintas, medir qué mappings
requieren código y añadir pruebas de contrato contra un Webby local.

## V1 documental implementada

- extracción local de PDF nativo con PyMuPDF;
- OCR local de PDF escaneado e imágenes con Tesseract;
- evidencia por página, bloque, coordenadas y confianza;
- candidatos deterministas de productos y variantes;
- salida JSON/CSV y bloqueo de tarjetas ambiguas;
- configuración inicial en `configs/documents/piel-radiante-bo.yml`.
