# Contratos de datos

## Modelo canónico

La V1 usa los nombres de campo que ya acepta Webby para evitar una traducción
innecesaria al publicar.

| Entidad | Campos clave | Requeridos locales |
|---|---|---|
| `productos` | `sku`, `nombre`, `precio`, `stock`, `categoria`, `marca` | `nombre` |
| `variantes` | `sku_producto`, `sku_variante`, `atributos`, `precio`, `stock` | `sku_producto`, `atributos` |
| `precios` | `sku`, `sku_variante`, `lista`, `precio` | `sku`, `precio` |
| `clientes` | `nombre`, `email`, `celular` | `nombre` |

Los campos opcionales se conservan solo si están presentes. `Decimal`, fechas y
atributos se serializan de forma determinista en JSONL y CSV.

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
