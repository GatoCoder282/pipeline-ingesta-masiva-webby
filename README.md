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

Para catálogos visuales, el perfil de Piel Radiante usa `ocr.engine: auto`:
intenta PaddleOCR y conserva Tesseract como fallback. La reconstrucción ordena
columnas y párrafos antes de normalizar mayúsculas; el quality gate deja el lote
bloqueado cuando encuentra ruido, texto insuficiente o una descripción sin
estructura confiable.

El worker OCR opcional mantiene la misma CLI y contrato de evidencia. En CPU:

```powershell
docker compose --profile ocr build ocr-worker
docker compose --profile ocr run --rm ocr-worker `
  ocr-worker `
  --input /workspace/data/incoming/catalogo.pdf `
  --output /workspace/data/extracted/catalogo-paddle.json `
  --document-config /workspace/configs/documents/piel-radiante-bo.yml `
  --runtime cpu
```

El mismo contenedor puede ejecutar el lote completo (incluye reconstrucción de
tarjetas y quality gate), cambiando el subcomando:

```powershell
docker compose --profile ocr run --rm ocr-worker `
  extract-batch `
  --input-dir /workspace/data/incoming `
  --document-config /workspace/configs/documents/piel-radiante-bo.yml `
  --batch-config /workspace/configs/documents/piel-radiante-bo-batch.yml `
  --workers 1
```

El parámetro `--runtime gpu` ya forma parte del contrato para una futura imagen
con PaddlePaddle GPU; el camino soportado actualmente es CPU. Los modelos se
guardan en el volumen `paddlex_models` para no descargarlos en cada ejecución.

Para revisar las descripciones ordenadas por marca, genera un HTML local:

```powershell
uv run ingestion review-descriptions `
  --products .\data\processed\<run>__productos.csv `
  --report .\data\reports\<run>__report.json `
  --output .\data\reports\<run>__description-review.html
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

## Cierre del catalogo con variantes

Despues de asignar los SKU estables, el cierre de precios por presentacion se
puede repetir con `enrich-variants`. El comando conserva los CSV de entrada y
genera los archivos listos para el wizard:

```powershell
uv run ingestion enrich-variants `
  --products .\data\processed\<run>__productos.csv `
  --variants .\data\processed\<run>__variantes.csv `
  --profile .\configs\enrichment\piel-radiante-bo.yml `
  --prices-dir .\data\incoming `
  --product-source-jsonl .\data\processed\<run>__productos-final.jsonl `
  --variant-source-jsonl .\data\processed\<run>__variantes-final.jsonl `
  --output-products .\data\processed\<run>__productos-webby-final.csv `
  --output-variants .\data\processed\<run>__variantes-webby-final.csv `
  --output-product-source-jsonl .\data\processed\<run>__productos-webby-final.jsonl `
  --output-variant-source-jsonl .\data\processed\<run>__variantes-webby-final.jsonl `
  --report .\data\reports\<run>__webby-finalization.json
```

La interfaz de Webby recibe solo los dos CSV `__webby-final`: primero
productos y luego variantes. Los JSONL, Parquet, reportes, DOCX y PDF se
conservan como evidencia y no se cargan al wizard.

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
