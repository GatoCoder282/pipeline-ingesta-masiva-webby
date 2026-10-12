# Iteración 003: OCR y calidad de descripciones

## Objetivo

Reducir la mezcla de columnas y el ruido OCR en catálogos visuales sin
correcciones manuales por producto, manteniendo trazabilidad a página, bbox y
bloques originales.

## Implementación

- PaddleOCR 3.x queda aislado en `Dockerfile.ocr` y en el servicio compose
  `ocr-worker`; la CLI base no instala el runtime pesado.
- `ocr.engine: auto` intenta PaddleOCR y registra un fallback explícito a
  Tesseract cuando el runtime o los modelos no están disponibles.
- La reconstrucción geométrica ordena cada columna de arriba hacia abajo,
  concatena columnas en orden de lectura y conserva saltos de párrafo por
  separación vertical o encabezado.
- Se eliminan del campo normalizado únicamente badges OCR identificables; los
  bloques descartados siguen en la extracción JSON/Markdown.
- El quality gate clasifica `pass`, `reconstruct` y `quarantine`, agrega la
  clasificación a la evidencia y bloquea aprobación cuando está habilitado.
- `review-descriptions` genera una vista HTML ordenada por marca para validar
  visualmente el lote y la estructura del lenguaje.

## Criterios de aceptación

1. La extracción conserva todos los bloques y localizadores de la fuente.
2. En una página de dos columnas, una descripción no alterna líneas de la
   columna izquierda y derecha.
3. Títulos y comienzos de oración se normalizan con mayúscula inicial; los
   párrafos y encabezados quedan separados con líneas en blanco.
4. El lote se bloquea si una descripción tiene ruido OCR crítico, mojibake,
   longitud insuficiente o no pasa la estructura configurada.
5. CSV, JSONL y Parquet conservan el mismo orden y contrato canónico.
6. Las pruebas unitarias cubren parser PaddleOCR moderno/legacy, geometría,
   ruido, quality gate y regresión del pipeline existente.

## Decisiones futuras

La imagen GPU y el benchmark con `pdf-inspector` quedan como extensiones
posteriores. `pdf-inspector` puede compararse con el extractor local para PDFs
originales, pero no sustituye la reconstrucción necesaria para JPEG/PNG.
