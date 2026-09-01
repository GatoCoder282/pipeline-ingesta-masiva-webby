# Adaptadores de fuentes

## Contrato tabular

Un adaptador implementa `read(path, sheet_name=None) -> ParsedTable`. Solo
interpreta el contenedor y conserva filas crudas; mapping y reglas de negocio
ocurren después.

## Estrategia de incorporación

1. Capturar un archivo representativo sin datos innecesarios.
2. Ejecutar `preview` y conservar los encabezados.
3. Crear un YAML bajo `configs/sources/`.
4. Resolver ambigüedades manualmente en `columns`.
5. Agregar fixture anonimizado y prueba.
6. Ejecutar `validate` y revisar cuarentena.

## Fuentes previstas

- CSV delimitado por coma, punto y coma, tabulador o `|`.
- XLSX/XLSM con hoja activa o hoja indicada.
- API HTTP como adaptador posterior.
- Google Drive/Sheets a través de n8n o un adaptador explícito, sin ocultar
  credenciales en mappings.

## Regla de diseño

La configuración específica de una tienda va en YAML. Se agrega código solo
cuando hay lógica de extracción que no puede expresarse como configuración.

## Documentos PDF e imagen

La V1 añade una capa documental separada de `ParsedTable`. Su salida conserva
por bloque el texto, página, bounding box, método y confianza. Esto permite
revisar un valor sin perder la ubicación original.

- PDF nativo: extracción local con PyMuPDF.
- PDF escaneado e imagen: OCR local con Tesseract (`spa+eng`).
- Catálogo visual: agrupación configurable por página, columnas y separación
  vertical antes de construir productos y variantes.

Los originales deben colocarse en `data/incoming/<tenant>/`. Los fixtures
anonimizados y pequeños van en `tests/fixtures/<tenant>/`; nunca se deben
versionar documentos reales.

El comando inicial es:

```powershell
uv run ingestion extract-document `
  --input .\data\incoming\piel-radiante-bo\catalogo.pdf `
  --document-config .\configs\documents\piel-radiante-bo.yml
```

Si el entorno local no tiene Tesseract, la ejecución falla de forma explícita.
La imagen Docker instala Tesseract con el idioma español.
