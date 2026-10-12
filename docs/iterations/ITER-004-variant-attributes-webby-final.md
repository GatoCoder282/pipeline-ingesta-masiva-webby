# ITER-004: contrato final de variantes y cierre Webby

## Objetivo

Actualizar el contrato local de ingesta para que una variante transporte una
combinacion completa de atributos en una sola columna JSON, incorporar el
costo unitario de variante y producir los dos CSV finales del ultimo lote de
Piel Radiante.

## Cambios

- `variantes.atributos` acepta uno o mas pares y se normaliza a un objeto JSON
  con claves `snake_case`, valores textuales, claves ordenadas y al menos una
  propiedad.
- La identidad de variante es `sku_producto` mas el objeto completo de
  atributos; dos SKU distintos no pueden duplicar la misma combinacion.
- `productos.costo` queda como campo de Webby; el costo por presentacion se
  conserva en la evidencia y alimenta el roll-up del padre.
- `enrich-variants` cruza las tablas DOCX por producto y presentacion. Solo
  asigna valores cuando la coincidencia es unica y conserva la evidencia por
  fila.
- Se actualizan el esquema JSON, el contrato documental, las reglas de calidad,
  la integracion Webby y la CLI.

## Resultado del lote

Entrada verificada: `28dfb57b-b1ad-4711-bc88-0299f47761c5`.

- 132 productos y 137 variantes, todos con SKU estable y evidencia alineada.
- 90 productos con precio y 40 con costo unitario respaldado.
- 100 variantes con precio: 66 ZO-É desde el PDF y 34 Libra desde DOCX.
- 34 variantes con costo unitario DOCX exacto conservado en evidencia; el CSV de
  variantes no agrega una columna fuera del contrato Webby.
- 24 variantes Rouse Arey y las presentaciones sin fila autoritativa quedan sin
  precio/costo; se reportan como cobertura faltante, sin estimacion.
- Las variantes ZO-É conservan precio del PDF, pero costo vacio porque el PDF
  no contiene una columna de costo.
- El reporte final queda con `ready_for_ingestion: true`, sin errores
  estructurales ni conflictos, y con 1:1 entre CSV y JSONL de evidencia.

## Archivos de carga

Subir al wizard unicamente, en este orden:

1. `data/processed/28dfb57b-b1ad-4711-bc88-0299f47761c5__productos-webby-final.csv`
2. `data/processed/28dfb57b-b1ad-4711-bc88-0299f47761c5__variantes-webby-final.csv`

No subir JSONL, Parquet, reportes, DOCX ni PDF. El dry-run remoto de Webby y
la confirmacion final siguen siendo pasos de la interfaz/entorno de Webby; no
se ejecutaron contra un endpoint remoto desde este entorno.

## Pruebas

- `pytest`: 37 pruebas pasan.
- La prueba nueva cubre atributos multiples, rechazo de claves repetidas,
  asignacion por presentacion, costo de variante y sincronizacion de evidencia.
