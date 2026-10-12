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
- atributos como un objeto JSON no vacio en el CSV final. La entrada local
  tambien acepta `clave=valor;clave=valor` y la normaliza a JSON;
- las claves de atributos se normalizan a `snake_case`, los valores son texto y
  no puede haber claves repetidas ni valores vacios;
- la identidad de variante es el SKU padre mas el JSON completo de atributos,
  por lo que se bloquean duplicados aunque tengan distinto `sku_variante`;
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

## Cierre de variantes

Antes de cargar el CSV final se verifica que:

- cada variante tenga `sku_producto`, `sku_variante` y al menos un atributo;
- `sku_producto` exista en el CSV de productos;
- `costo` y `precio` sean no negativos cuando la fuente los respalda;
- el reporte de enriquecimiento no tenga conflictos estructurales y conserve
  alineacion 1:1 entre CSV y JSONL de evidencia.

Un campo de precio o costo sin fuente autoritativa se deja vacio y se incluye en
`optional_fields_missing`; esto es una advertencia de cobertura, no permiso
para estimarlo.
