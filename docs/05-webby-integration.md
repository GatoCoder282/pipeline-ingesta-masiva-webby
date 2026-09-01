# Integración con Webby

La integración usa el módulo existente `apps/api/modules/importacion` de Webby.
El pipeline no conoce SQL interno ni modifica migraciones.

## Secuencia HTTP

1. `GET /importacion/entidades` para comprobar el catálogo disponible.
2. `POST /importacion/preview` para inspección opcional.
3. `POST /importacion/importar` con `dry_run=true`.
4. `GET /importacion/trabajos/{trabajo_id}` hasta estado terminal.
5. `POST /importacion/importar` con `dry_run=false`.
6. Polling del trabajo final y persistencia del reporte.

El archivo publicado es CSV UTF-8 con encabezados canónicos y mapping identidad.
La autenticación usa `Authorization: Bearer` y, opcionalmente, `X-Tenant-Slug`.
El token debe ser de desarrollo y estar limitado al tenant correspondiente.

## Compatibilidad

Si Webby cambia campos, estados o rutas, se actualiza el cliente y la prueba de
contrato; no se parchea el pipeline con escrituras directas.
