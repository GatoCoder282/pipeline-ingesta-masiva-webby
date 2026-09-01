# Webby Ingestion Pipeline

Herramienta local para extraer, organizar, validar y publicar catálogos de
tiendas hacia Webby. El repositorio conserva los archivos originales, genera
artefactos reproducibles en Parquet y publica únicamente después de una
aprobación explícita.

## Inicio rápido

Requisitos: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker Desktop y
Git.

Para ejecutar PDF/imagen directamente en Windows, instala Tesseract OCR y el
idioma español (`spa`) y agrega Tesseract al `PATH`. La imagen Docker ya
incluye ese runtime.

```powershell
uv sync
Copy-Item .env.example .env
uv run ingestion preview --input .\examples\catalogo-tienda-ejemplo.csv --entity productos
uv run ingestion validate --input .\examples\catalogo-tienda-ejemplo.csv --entity productos

# PDF o imagen: conserva evidencia y genera JSON canónico + CSV
uv run ingestion extract-document `
  --input .\data\incoming\piel-radiante-bo\catalogo.pdf `
  --document-config .\configs\documents\piel-radiante-bo.yml

# Batch mixto de un tenant: PDF + imágenes, con consolidación para el wizard
docker compose --profile tools run --rm pipeline extract-batch `
  --input-dir /workspace/data/incoming `
  --document-config /workspace/configs/documents/piel-radiante-bo.yml `
  --batch-config /workspace/configs/documents/piel-radiante-bo-batch.yml `
  --workers 4
```

La validación devuelve un `run_id`. Revisa el reporte y apruébalo solo cuando
sea correcto:

```powershell
uv run ingestion approve --run-id <run_id> --by "tu-nombre"
uv run ingestion publish --run-id <run_id> --confirm
```

La publicación usa la API de importación existente de Webby, ejecuta primero un
dry-run remoto y luego espera el resultado del trabajo asíncrono. El pipeline
nunca escribe directamente en PostgreSQL de Webby.

La V1 documental escribe el archivo original en `data/raw/`, la extracción con
texto y coordenadas en `data/extracted/`, los productos y variantes en
`data/processed/`, y el reporte/cuarentena en `data/reports/` y
`data/quarantine/`. Los archivos reales bajo `data/incoming/` están fuera de Git.

El comando `extract-batch` genera un CSV consolidado de productos y otro de
variantes con los encabezados canónicos del wizard de Webby. En esta V1 los
SKU se dejan vacíos para revisión: el lote queda bloqueado y no se debe
publicar hasta completar `sku`/`sku_producto` y revisar las evidencias OCR.

## Servicios locales

```powershell
docker compose up -d postgres kestra
# n8n es opcional:
docker compose --profile integrations up -d n8n
```

- Kestra: <http://localhost:8080>
- n8n opcional: <http://localhost:5678>
- PostgreSQL de herramientas: `localhost:55432`

Los flows montan este repositorio en `/workspace`. Para una prueba simple se
puede ejecutar la CLI directamente y después conectar el mismo comando desde
Kestra.

## Principios

- raw es inmutable y nunca se versionan datos reales.
- los mapeos de cada tienda son configuración versionada.
- cada ejecución tiene manifiesto, checksum, reporte y artefactos.
- los errores de fila van a cuarentena y no se publican.
- el tenant y el endpoint se configuran por ambiente.
- las decisiones agentic se habilitarán después de medir primero el flujo
  determinista.

Consulta [`docs/00-product-spec.md`](docs/00-product-spec.md) para el alcance,
[`docs/01-architecture.md`](docs/01-architecture.md) para la arquitectura y
[`docs/decisions/ADR-001-orchestration.md`](docs/decisions/ADR-001-orchestration.md)
para las decisiones tecnológicas.
