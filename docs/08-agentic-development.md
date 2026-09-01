# Evolución agentic

## Fase determinista

El pipeline actual produce señales estructuradas: encabezados, mappings,
errores, métricas, artefactos y estados. Estas señales son la base de cualquier
agente y deben estabilizarse antes de delegar decisiones.

## Tools futuras

- `inspect_file`: leer estructura sin publicar;
- `profile_columns`: calcular tipos, nulos, cardinalidad y ejemplos;
- `suggest_mapping`: proponer mapping con confianza;
- `validate_run`: ejecutar reglas sin efectos externos;
- `generate_adapter`: proponer YAML o código con diff;
- `webby_dry_run`: validar remotamente;
- `request_approval`: crear una pausa humana.

## Guardrails

- el agente no recibe acceso SQL directo a Webby;
- toda tool declara entradas, salidas y efectos;
- acciones de red se permiten solo en adapters autorizados;
- publicar requiere aprobación humana y `--confirm`;
- cada decisión registra modelo, prompt, tool, argumentos y resultado;
- mappings con baja confianza se bloquean para revisión.

## Evaluación

Se medirán exactitud de mapping, filas corregidas, falsos positivos, tiempo de
operación y tasa de publicación sin intervención. El agente se habilita por
capacidad específica, no como un reemplazo global del pipeline.
