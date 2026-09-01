# Arquitectura

## Capas

```text
sources       -> parsean CSV/XLSX/API y producen ParsedTable
documents     -> inspeccionan PDF/imagen y producen páginas/bloques con evidencia
normalization -> aplican mapping, aliases y coerción de tipos
validation    -> ejecuta reglas deterministas y produce reporte/cuarentena
storage       -> guarda raw, JSONL, Parquet, DuckDB, reportes y manifiestos
webby         -> adapta el modelo canónico al contrato HTTP de Webby
orchestration -> Kestra coordina comandos; n8n integra eventos externos
```

Las capas dependen de contratos pequeños. Un adaptador de fuente no conoce
PostgreSQL ni Webby; el cliente Webby no conoce cómo se leyó el archivo.

## Flujo de estados

```text
created -> validated/blocked -> approved -> publishing -> published
                              \-> rejected
```

`blocked` significa que existe al menos una fila inválida. `approved` requiere
revisión humana y cero errores bloqueantes. `published` solo se asigna después
de que Webby termina el trabajo asíncrono.

## Escalabilidad

- V1: un proceso local, filesystem y DuckDB.
- Escala vertical: aumentar memoria/CPU y procesar por lotes.
- Escala horizontal: mover artefactos a S3/MinIO, externalizar el estado de
  runs y ejecutar workers idempotentes desde Kestra.
- La partición natural es `run_id` + tenant + entidad.
- No se usa estado mutable global en el proceso.

## Seguridad

- secretos solo por ambiente;
- tokens con alcance de tenant y desarrollo;
- no se imprimen tokens en logs;
- raw y reportes pueden contener información sensible y quedan fuera de Git;
- la API de Webby sigue siendo responsable de permisos, RLS y reglas de negocio.
