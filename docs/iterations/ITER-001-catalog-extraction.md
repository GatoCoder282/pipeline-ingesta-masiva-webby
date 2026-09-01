# ITER-001 — Extracción documental de catálogo

## Estado

- Fecha de cierre: 2026-09-01
- Tenant de prueba: `piel-radiante-bo`
- Rama: `main`
- Tipo de entrada: PDF e imágenes JPEG
- Estado de los servicios al cerrar: Docker apagado; volúmenes preservados
- Publicación en Webby: no realizada

Esta iteración construye y prueba la V1 del pipeline documental para convertir
catálogos visuales en candidatos estructurados de productos, variantes y
categorías. El resultado se dejó en CSV/JSON para revisión; no se consideró
aprobado para publicación mientras los SKU permanezcan vacíos.

## Objetivo y alcance

El objetivo fue aceptar archivos PDF o imagen, extraer texto con evidencia,
identificar tarjetas de producto aunque la composición visual cambie y
consolidar los resultados en el formato que puede revisar el wizard de carga
masiva de Webby.

El alcance de esta iteración fue deliberadamente de un solo tenant y una sola
familia de fuente. No se implementó todavía una interfaz de usuario ni una
publicación automática. La entrada real quedó local y fuera de Git.

## Resultado de la corrida final

Run final: `c41edffa-c5e6-4c1f-bcb7-f473032ce2c8`

| Métrica | Resultado |
|---|---:|
| Archivos de entrada | 55 en `data/incoming` |
| Documentos procesados | 54 |
| Páginas procesadas | 59 |
| Productos candidatos | 82 |
| Productos con nombre vacío | 0 |
| Productos con descripción vacía | 0 |
| Candidatos de variantes | 71 |
| Categorías detectadas | 51 |
| Archivos Markdown de evidencia | 54 |
| Errores de productos | 0 |
| Errores de variantes | 71 por `sku_producto` vacío |

El producto quedó validado como entidad independiente. El estado global del
lote fue `blocked` porque las variantes requieren `sku_producto`; esto es
esperado según la decisión de dejar SKU vacío en la V1.

## Artefactos generados

Todos los artefactos corresponden al `run_id` anterior y se mantienen fuera de
Git mediante `.gitignore`:

- `data/processed/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8__productos.csv`
- `data/processed/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8__variantes.csv`
- `data/processed/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8__categorias.csv`
- `data/processed/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8__catalog.json`
- `data/processed/*__productos.jsonl`
- `data/processed/*__variantes.jsonl`
- `data/processed/*__productos.parquet`
- `data/processed/*__variantes.parquet`
- `data/extracted/*__*.md` como proyección Markdown auditable
- `data/reports/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8.json`
- `data/quarantine/c41edffa-c5e6-4c1f-bcb7-f473032ce2c8__catalogo.jsonl`

Los originales bajo `data/incoming` no se borraron. La limpieza realizada
antes del reproceso solo vació artefactos generados en `raw`, `extracted`,
`processed`, `quarantine`, `reports`, `manifests` y `warehouse`.

## Workflow implementado

```text
data/incoming
    |
    v
raw inmutable + checksum
    |
    v
extractor documental
  - PDF nativo: PyMuPDF
  - PDF escaneado / imagen: Tesseract OCR
    |
    v
extraction JSON: páginas, bloques, bbox, confianza, método
    |
    v
proyección Markdown con comentarios de evidencia
    |
    v
parser visual de catálogo
  - agrupación de tarjetas
  - columnas y separación vertical
  - títulos y descripciones
  - variantes y categorías
    |
    v
normalización + mapping del tenant
    |
    v
validación y cuarentena
    |
    +--> productos / variantes / categorías en CSV y JSONL
    +--> Parquet / DuckDB para análisis local
    +--> reporte y manifiesto por run
    |
    v
revisión humana -> aprobación -> dry-run Webby -> publicación
```

La proyección Markdown no reemplaza la extracción JSON. Es una representación
intermedia legible que permite depurar el orden visual y conservar la relación
entre una línea y su página, bounding box, confianza y bloque OCR.

## Cambios principales realizados

### Extracción y evidencia

- Se mantuvo la ruta de PDF nativo con PyMuPDF.
- Se mantuvo el fallback de OCR para PDF escaneado e imágenes.
- La imagen Docker instala `tesseract-ocr` y `tesseract-ocr-spa`.
- Se agregó `documents/markdown.py` para convertir bloques extraídos a
  Markdown sin perder `page_number`, `bbox`, `confidence`, `method`, `units` y
  `block_id`.
- Se agregó escritura de Markdown en el almacenamiento de artefactos y en el
  flujo batch.
- El reporte incluye `markdown_count` y el mapa de archivos Markdown por
  fuente.
- Los productos conservan evidencia de campo para `nombre` y `descripcion`.

### Nombres de producto

- Se dejó de tomar cualquier línea grande como nombre.
- La selección considera posición, altura, mayúsculas, prefijos conocidos y
  continuidad de título.
- Se separan líneas de descripción, subtítulos, encabezados, categorías,
  sellos y texto de uso.
- Se reparan mojibake y caracteres OCR frecuentes sin inventar contenido.
- Se conservaron números y acrónimos útiles como `N°`, `PDRN+`, `PERT+` y
  `CICABOOST`.
- Se corrigió la separación de `Emulsión de Limpieza Facial` y `Emulsión
  Exfoliante Facial`.
- Se corrigieron títulos `TRIABE`, `TRIANA`, `TRIGAC` y `TRIPAB Booster`.
- Las tarjetas sin nombre no se convierten en filas vacías: quedan en la
  evidencia/cuarentena para revisión.

### Reglas específicas del tenant

La configuración versionada está en:

- `configs/documents/piel-radiante-bo.yml`
- `configs/documents/piel-radiante-bo-batch.yml`

Incluye OCR `spa+eng`, 220 DPI, `--psm 11`, umbral de confianza 0.65 y
layout inicial de dos columnas. También registra dos decisiones visuales:

- `WhatsApp Image 2026-08-31 at 19.44.10 (2).jpeg` es índice de categorías,
  no una página de productos.
- `WhatsApp Image 2026-08-31 at 19.44.22.jpeg` conserva únicamente la tarjeta
  inferior porque la tarjeta superior está cruzada.

Estas reglas no deben trasladarse automáticamente a otra tienda; deben vivir
en el perfil del tenant y probarse con fixtures propios.

## Herramientas y servicios usados

### Dentro de la imagen `pipeline`

- Python 3.12.
- `uv` para entorno, lockfile y ejecución reproducible.
- PyMuPDF para PDF nativo y renderizado.
- Tesseract OCR con idiomas español e inglés.
- Pillow para imágenes.
- PyYAML para perfiles de tenant.
- DuckDB y Parquet para almacenamiento analítico local.
- OpenPyXL para fuentes tabulares futuras.
- HTTPX para la frontera de integración con Webby.

### Calidad y operación

- Pytest: 21 pruebas automatizadas pasando.
- Ruff: todos los checks pasando.
- Docker Compose para aislar runtime y dependencias OCR.
- Kestra para orquestación local futura y PostgreSQL para su estado.
- n8n quedó definido como integración opcional, pero no fue necesario para esta
  corrida.
- Git para versionar código, configuración, contratos y documentación, nunca
  los documentos reales del tenant.

Durante la prueba estuvieron activos los servicios `webby-ingestion-kestra` y
`webby-ingestion-postgres`. El servicio `pipeline` se ejecutó como contenedor
de una sola corrida con `--rm`. Al cerrar la iteración se ejecutó
`docker compose down`: los contenedores y la red se detuvieron, sin eliminar
los volúmenes nombrados.

## Obstáculos, causa y solución

| Obstáculo | Causa | Solución aplicada | Estado |
|---|---|---|---|
| Imágenes con uno, dos o tres productos | Composiciones verticales, horizontales y diagonales | Agrupación por bloques, bbox, columnas, separación vertical y señales de presentación | Resuelto para este lote |
| Tarjeta cruzada | Elemento promocional inválido encima de un producto válido | Regla `keep_bottom_only` específica del archivo | Resuelto para este lote |
| Imagen de categorías confundida con productos | El índice tiene texto y layout parecido a una ficha | Regla `category_files` y parser separado de categorías | Resuelto para este lote |
| Nombre contaminado con descripción | OCR entrega líneas visualmente próximas y algunas tienen tipografía similar | Proyección Markdown, selección por anclas/tipografía/posición, marcadores de cuerpo y máximo de líneas | Mejorado; requiere más fixtures |
| Encabezados como `Modo de uso` o `Línea Corporal` como productos | Encabezados con tamaño visual de título | Lista de headings y filtros de ruido | Resuelto en las muestras revisadas |
| Separación incompleta de una emulsión | Un bloque OCR con `=` interrumpía el título | Se ignoran símbolos aislados intermedios al agrupar el título | Resuelto en este lote |
| Mojibake y errores de OCR | Mezcla de codificaciones, letras parecidas y baja calidad visual | Reparación conservadora de mojibake y normalización de nombre | Parcial; el contenido original no se adivina |
| Lote global bloqueado | `sku_producto` vacío en las variantes | Se mantienen SKU vacíos por decisión de negocio y se bloquea la publicación | Pendiente de la siguiente etapa |
| Reproceso costoso | OCR de 54 documentos dentro del contenedor | Corridas limpias, workers configurables y validación posterior del artefacto | Aceptable para V1 |

## Pendiente detectado: calidad de descripciones

Aunque las 82 filas tienen una descripción no vacía, la presencia de texto no
equivale a una descripción limpia. La revisión final encontró problemas como:

- fragmentos de títulos parcialmente reconocidos al inicio de la descripción;
- caracteres sustituidos (`e`, `ONO`, letras separadas o unidas);
- columnas laterales mezcladas con el texto principal;
- sellos visuales como `PARABEN`, `MINERAL`, `OIL FREE` y `CRUELTY FREE`
  insertados en medio del párrafo;
- ingredientes `INCI`, activos, advertencias y modo de uso mezclados sin
  segmentación estable;
- palabras cortadas o truncadas por OCR;
- orden de lectura incorrecto en fichas con dos columnas;
- entidades HTML o símbolos conservados literalmente, por ejemplo
  `&quot;`, `&amp;` o `ﬂ`.

Por esta razón, la descripción se considera **capturada con evidencia**, pero
no todavía **normalizada editorialmente**. No se corrigieron manualmente frases
ni se inventaron letras faltantes, porque eso destruiría la trazabilidad al
documento original.

## Plan para ITER-002

Prioridad P0: mejorar la captura y segmentación de descripción.

1. Construir fixtures anonimizados por patrón de layout: una columna, dos
   columnas, tarjetas diagonales, badges, INCI y modo de uso.
2. Detectar regiones de contenido antes de concatenar texto: título, resumen,
   activos, ingredientes, aplicación, advertencias y sellos.
3. Ordenar cada región por columna y coordenada, no solo por `block_id` o
   posición global.
4. Ejecutar preprocesamiento OCR por región: escala, contraste, deskew y
   eliminación de ruido visual.
5. Comparar configuraciones Tesseract por región (`psm`) y conservar la mejor
   evidencia sin reemplazar silenciosamente el texto fuente.
6. Aplicar limpieza segura de texto: espacios, ligaduras, entidades HTML y
   mojibake; no hacer paráfrasis ni completar palabras inciertas.
7. Añadir `description_quality` por campo con señales como orden de lectura,
   proporción de caracteres inválidos, mezcla de columnas y confianza media.
8. Crear una muestra dorada revisada por una persona y pruebas de regresión
   para no volver a mezclar nombres y descripciones.

Prioridad P1: resolver identidad y variantes.

1. Definir el formato de SKU del tenant o una tabla de asignación manual.
2. Asignar `sku_producto` a cada variante.
3. Generar o validar `sku_variante` de manera determinista.
4. Reejecutar la puerta de validación y comprobar que el lote pueda aprobarse.

Prioridad P2: madurez multi-tenant.

1. Extraer reglas específicas a perfiles YAML versionados.
2. Añadir fixtures pequeños por tienda y pruebas de contrato.
3. Separar configuración, almacenamiento y estado de ejecución para poder mover
   artefactos a S3/MinIO y workers de Kestra en una etapa posterior.

## Reproducción de la prueba

Con Docker Desktop iniciado y los documentos ubicados en `data/incoming`:

```powershell
docker compose up -d postgres kestra

docker compose --profile tools run --rm pipeline extract-batch `
  --input-dir /workspace/data/incoming `
  --document-config /workspace/configs/documents/piel-radiante-bo.yml `
  --batch-config /workspace/configs/documents/piel-radiante-bo-batch.yml `
  --workers 4
```

Después se revisa el `run_id` en `data/reports/`, los CSV en
`data/processed/` y los Markdown en `data/extracted/`. Antes de cualquier
publicación se deben completar SKU, revisar evidencias y ejecutar la secuencia
de aprobación y dry-run definida en `docs/04-validation-quality.md` y
`docs/05-webby-integration.md`.

## Criterio de cierre

La iteración queda cerrada porque el pipeline documental procesa el caso de
uso inicial dentro de Docker, genera artefactos reproducibles, conserva
evidencia, separa productos de variantes y pasa las pruebas automatizadas. No
queda cerrada la publicación: las variantes necesitan identidad de producto y
las descripciones necesitan una iteración específica de segmentación y
limpieza OCR.
