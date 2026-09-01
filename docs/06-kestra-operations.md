# Operación local de Kestra

```powershell
Copy-Item .env.example .env
docker compose up -d postgres kestra
docker compose logs -f kestra
```

Kestra se abre en `http://localhost:8080`, persiste su estado en PostgreSQL y
monta este repositorio en `/workspace`. Los flows versionados viven en
`kestra/flows/` y se sincronizan desde el volumen `/flows`.

La CLI es la misma entrada usada por desarrollo y orquestación. Si el worker
corre en contenedor, la imagen `Dockerfile.kestra` instala el proyecto en
`/opt/ingestion` y los commands usan `uv run --project /opt/ingestion`.

Para publicar, se recomienda separar el flow de validación del comando de
aprobación/publicación. La aprobación sigue siendo una acción humana fuera del
flow automático en V1.
