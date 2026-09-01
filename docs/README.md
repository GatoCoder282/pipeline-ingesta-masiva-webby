# Documentación del pipeline

La documentación sigue un enfoque spec-driven: primero se define el contrato,
después se implementa una iteración pequeña y finalmente se registra la
evidencia de validación.

## Mapa

- `00-product-spec.md`: objetivo, actores, alcance y criterios de aceptación.
- `01-architecture.md`: arquitectura, límites y flujo de datos.
- `02-data-contracts.md`: modelo canónico y metadatos de trazabilidad.
- `03-source-adapters.md`: estrategia para fuentes heterogéneas.
- `configs/documents/`: perfiles de OCR y layout por tenant/fuente.
- `04-validation-quality.md`: reglas y puerta de calidad.
- `05-webby-integration.md`: contrato de publicación.
- `06-kestra-operations.md`: operación local del orquestador.
- `07-n8n-boundaries.md`: responsabilidades de n8n.
- `08-agentic-development.md`: evolución determinista a agentic.
- `decisions/`: ADRs de decisiones técnicas.
- `schemas/`: contratos legibles por máquinas.
- `schemas/document-extraction.schema.json`: evidencia de PDF/imagen por bloque.
- `schemas/catalog-result.schema.json`: reporte agregado de productos/variantes.
- `schemas/catalog.schema.json`: JSON canónico de productos y variantes.
- `diagrams/`: diagramas Mermaid versionados.
- `iterations/`: alcance y evidencia de cada iteración.
