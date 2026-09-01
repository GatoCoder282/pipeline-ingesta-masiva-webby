# ADR-002: publicación exclusivamente por API de Webby

## Estado

Aceptada

## Decisión

El pipeline llama al módulo de importación de Webby y no escribe su PostgreSQL.
La validación local, el dry-run remoto y la aprobación humana son obligatorios.

## Razón

Webby conserva las reglas de dominio, RLS, permisos, auditoría, idempotencia y
límites de tenant. El pipeline debe poder evolucionar sin acoplarse al esquema
interno de la aplicación.
