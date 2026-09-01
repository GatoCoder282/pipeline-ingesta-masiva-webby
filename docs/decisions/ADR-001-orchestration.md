# ADR-001: Kestra como orquestador principal y n8n periférico

## Estado

Aceptada

## Contexto

El flujo necesita reproducibilidad, reintentos, ejecución manual/programada,
artefactos y una futura expansión horizontal. También puede necesitar conectar
Drive, webhooks y notificaciones.

## Decisión

Kestra coordina los pipelines de datos versionados. n8n es opcional y coordina
integraciones externas. Python contiene la lógica de parsing, normalización y
validación.

## Consecuencias

Hay tres superficies, pero cada una tiene un límite claro. Se evita esconder
reglas críticas en nodos visuales y se conserva la capacidad de operar la CLI
sin Kestra ni n8n.
