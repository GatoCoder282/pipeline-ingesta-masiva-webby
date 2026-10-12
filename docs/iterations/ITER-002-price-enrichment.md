# ITER-002 — Enriquecimiento de precios y normalización

## Estado

- Paso ejecutado: 1 de 2.
- Tenant: `piel-radiante-bo`.
- Entradas adicionales: `precios libra agosto 2026.docx`,
  `precios vital blue.docx` y `Catalogo Profesional 2026 ZOÉ.pdf`.
- Publicación en Webby: no realizada.
- SKU: se mantienen vacíos para el paso 2.

## Resultado de la corrida

| Métrica | Resultado |
|---|---:|
| Filas de productos | 132 |
| Filas con `marca` | 132 |
| Filas con `costo` | 41 |
| Filas con `precio` profesional | 91 |
| Filas de precios DOCX vinculadas | 57 |
| Filas de precios DOCX sin destino inequívoco | 9 |
| Productos ZOÉ agregados desde PDF | 50 |
| Variantes ZOÉ con precio agregadas | 66 |
| Marcas `Libra` / `Vital Blue` / `Rouse Arey` / `Zoé` | 36 / 12 / 34 / 50 |
| Errores de alineación CSV–JSONL | 0 |

Las 9 filas sin destino quedaron visibles en el reporte; no se crearon
productos nuevos a partir de ellas. El JSONL de evidencia fue reordenado junto
con el CSV para conservar la relación posicional usada por el siguiente paso.
El PDF de ZOÉ no contiene costos: sus 50 productos conservan `costo` vacío y
sus 66 precios por presentación quedaron en variantes.

## Objetivo

Completar el CSV de productos de ITER-001 con `marca`, `costo` y `precio`,
usando las tablas DOCX y el PDF como fuentes autorizadas. También se normalizan
títulos, descripciones y el orden de salida; las presentaciones del PDF se
agregan al CSV de variantes sin asignar todavía SKU.

## Decisiones

- `costo` = columna `Precio POR unid. bs` de la lista.
- `precio` = columna `Precio venta A profesional`.
- No se usan descuentos, cantidades ni totales.
- Para presentaciones múltiples en una fila padre se toma el mínimo de costo y
  precio hasta la adaptación de variantes.
- La marca se obtiene de las fuentes de ITER-001 y el perfil del tenant:
  `Libra`, `Vital Blue` y `Rouse Arey` para los productos restantes.
- Un nombre del DOCX sin fila de catálogo no crea una fila nueva; queda en el
  reporte de enriquecimiento para revisión.
- En ZOÉ, `precio` del producto es el menor precio de sus presentaciones y cada
  precio exacto se conserva en `variantes`; no se calcula ningún costo.
- Las SKU quedan intactas para el segundo paso.

## Implementación

- `src/ingestion_pipeline/enrichment.py` lee tablas DOCX desde OOXML, aplica
  alias revisados, normaliza títulos/descripciones, ordena por marca, mantiene
  alineada la evidencia JSONL y genera un reporte auditable.
- `configs/enrichment/piel-radiante-bo.yml` contiene prefijos de fuente, alias
  y correcciones de título específicas del tenant.
- `configs/enrichment/zoe-catalog-2026.yml` contiene el mapeo revisado de 50
  páginas de producto y sus 66 precios por presentación.
- La CLI añade `ingestion enrich-catalog`.
- La CLI añade `ingestion merge-pdf-catalog` para incorporar un PDF ya
  revisado sin confundir texto de portada, secciones, pH o concentraciones con
  productos/precios.
- La skill de proyecto `.agents/skills/catalog-iteration-enrichment/` conserva
  las decisiones reutilizables de esta iteración y los obstáculos de ITER-001.

## Pendientes y límites

- Los nombres del DOCX que no aparecen en el CSV no se inventan como productos.
- Los productos con varias presentaciones todavía necesitan filas de variantes.
- La normalización de descripción de este paso es de formato y segmentación
  segura; el texto OCR mezclado entre columnas queda pendiente de la mejora
  semántica y de lectura por regiones.
- El extractor genérico del PDF deja 12 descripciones ZOÉ vacías en esta
  incorporación; sus nombres y precios sí tienen evidencia de página. Se
  mantienen pendientes para la mejora de descripción, no se completan por
  inferencia.

## Actualizacion: mejora de descripciones

- Se anadio `ingestion improve-descriptions` para reconstruir el texto de las
  fichas PDF usando bloques nativos, coordenadas y etiquetas de seccion.
- La etapa se aplico a las 50 fichas de Zoe: recupero las 12 descripciones que
  estaban vacias y separo introduccion, activos/caracteristicas, modo de uso,
  accion, beneficios e indicaciones cuando existian.
- Se conservaron precios, nombres y evidencia originales; el CSV recibe la
  proyeccion normalizada y el JSONL conserva los identificadores de bloques.
- No se uso OCR alternativo en esta pasada. `pdf-inspector` queda como
  candidato para un benchmark local futuro, no como reemplazo automatico.
- Debe ejecutarse la segunda parte: identidad, adaptación de producto/variante,
  formato de SKU y validación final del lote.

## Estado posterior: identidad, variantes y SKU

- Se aplico la opcion 1 de SKU estable: `LIB`, `VBL`, `RAR` y `ZOE` por marca;
  el producto usa cuatro digitos y la variante agrega `-V01`, `-V02`, etc.
- Se asignaron 132 SKU de producto y 137 SKU de variante. La presentacion queda
  en `atributos.presentacion` y no forma parte del identificador.
- Se enlazaron todas las variantes con un `sku_producto` existente. Los 41
  productos de una sola presentacion que tenian variante fueron promovidos a
  `tipo=variable`.
- El mapa persistente esta en
  `configs/enrichment/piel-radiante-bo-sku-map.json`; conserva la identidad de
  fuente para no cambiar SKU en futuras corridas.
- Validacion local: productos 132/132 y variantes 137/137 aprobados, sin
  duplicados, padres faltantes, colisiones entre SKU ni desalineacion JSONL.
- No se ejecuto la publicacion remota en Webby; el lote queda listo para el
  dry-run y la aprobacion explicita de ingesta.
