# Validación y calidad

## Severidades

- `error`: bloquea aprobación y publicación.
- `warning`: se muestra y se conserva, pero no bloquea por sí solo.

## Reglas V1

- columnas destino válidas según la entidad;
- columnas requeridas presentes;
- números con formato decimal boliviano;
- valores numéricos no negativos para precio, costo, peso y stock;
- booleanos en formatos comunes (`sí/no`, `true/false`, `1/0`);
- atributos en JSON o `clave=valor;clave=valor`;
- tipos de producto permitidos por Webby;
- duplicados dentro del lote según la identidad de la entidad;
- filas vacías omitidas;
- errores detallados por número de fila y campo.

## Puerta de publicación

La secuencia obligatoria es:

```text
validate local -> review report -> approve run -> dry-run Webby -> publish Webby
```

Si el dry-run remoto falla, el lote no se publica aunque el lote local haya sido
aprobado.
