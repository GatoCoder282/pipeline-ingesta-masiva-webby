# ADR-003: raw inmutable, JSONL de replay y Parquet de análisis

## Estado

Aceptada

## Decisión

Cada run conserva el origen, un JSONL canónico para replay/publicación y una
proyección Parquet consultable con DuckDB. Los errores se separan en cuarentena.

## Evolución

MinIO/S3 puede sustituir el filesystem cuando el volumen o la ejecución
distribuida lo justifiquen, manteniendo los mismos nombres lógicos de artefacto.
