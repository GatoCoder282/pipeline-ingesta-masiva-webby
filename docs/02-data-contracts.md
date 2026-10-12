# Contratos de datos

## Modelo canónico

La V1 usa los nombres de campo que ya acepta Webby para evitar una traducción
innecesaria al publicar.

| Entidad | Campos clave | Requeridos locales |
|---|---|---|
| `productos` | `sku`, `nombre`, `precio`, `costo`, `stock`, `categoria`, `marca` | `nombre` en extracción; `sku` antes de Webby |
| `variantes` | `sku_producto`, `sku_variante`, `atributos`, `precio`, `stock` | `sku_producto`, `atributos` en extracción; ambos SKU antes de Webby |
| `precios` | `sku`, `sku_variante`, `lista`, `precio` | `sku`, `precio` |
| `clientes` | `nombre`, `email`, `celular` | `nombre` |

Los campos opcionales se conservan solo si están presentes. `Decimal`, fechas y
atributos se serializan de forma determinista en JSONL y CSV.

## Contrato objetivo de ingesta masiva Webby

El cierre de migracion usa dos archivos CSV UTF-8: uno de `productos` y otro de
`variantes`. Los encabezados son los nombres canonicos de Webby. En
`productos`, `costo` es el costo unitario en Bs y `precio` es el precio
profesional de venta en Bs; ambos son valores decimales no negativos y no se
calculan desde el margen. El CSV de variantes transporta precio, stock y estado;
el costo por presentacion queda auditado en la evidencia y se consolida en
`productos.costo`.

### Atributos de variantes

`variantes.atributos` es una sola columna que contiene un objeto JSON no vacio.
El objeto representa la combinacion completa de opciones y puede tener uno o
varios atributos, por ejemplo:

```json
{"color":"rojo","presentacion":"250 g","talla":"M"}
```

Las claves se normalizan a minusculas `snake_case`; los valores permanecen como
texto. El JSON se escribe con claves ordenadas y sin columnas separadas como
`color`, `talla` o `presentacion`. Tambien se acepta temporalmente
`clave=valor;clave=valor` en la normalizacion local, pero el CSV final siempre
transporta JSON. Un objeto vacio, claves repetidas o valores vacios bloquean el
lote.

La identidad de una variante es `sku_producto` mas el objeto completo de
`atributos` normalizado. `sku_variante` es el identificador estable para
actualizaciones, pero no permite duplicar la misma combinacion bajo el mismo
producto.

### Reglas de precios y costos

- En productos con variantes, `precio` y `costo` del padre son el minimo de los
  valores autorizados de sus variantes hasta que Webby defina otro roll-up.
- Una lista DOCX solo puede completar una variante cuando el producto y la
  presentacion coinciden sin ambiguedad. Los valores no encontrados permanecen
  vacios y se reportan; no se inventan costos.
- El costo se toma exclusivamente de `Precio POR unid. bs`; el precio se toma
  de `Precio venta A profesional`. Los costos DOCX por presentacion se
  conservan en la evidencia y el padre recibe el minimo autorizado. Un PDF sin
  columna de costos puede aportar precio de variante, pero no costo.

## Metadatos por registro

```json
{
  "run_id": "uuid",
  "source_file": "catalogo.xlsx",
  "source_row": 42,
  "record_hash": "sha256-futuro",
  "extracted_at": "2026-08-30T00:00:00Z"
}
```

## Manifiesto

El manifiesto identifica el archivo, su checksum, estado de aprobación,
artefactos generados y trabajo remoto asociado. Es la unidad de replay y
auditoría del pipeline.

## Contrato documental V1

La extracción documental produce un artefacto JSON con páginas y bloques:

```json
{
  "block_id": "p0001-ocr0001",
  "page_number": 1,
  "text": "Crema hidratante",
  "bbox": [10, 20, 240, 60],
  "confidence": 0.94,
  "method": "ocr",
  "units": "pixels"
}
```

Cada registro canónico conserva `source.page_number`, `source.bbox`,
`source.block_ids` y `source.confidence`. Los valores no interpretables o con
confianza menor al umbral quedan bloqueados para revisión.
