"""Compatibility catalog for Webby's current bulk-import contract."""

from __future__ import annotations

from dataclasses import dataclass

ENTITIES = ("productos", "variantes", "precios", "clientes")


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    type: str = "text"
    required: bool = False
    aliases: tuple[str, ...] = ()


_PRICE_COMPARISON = FieldDefinition(
    "precio_comparacion", aliases=("precio comparacion", "precio anterior", "precio tachado")
)

FIELDS: dict[str, tuple[FieldDefinition, ...]] = {
    "productos": (
        FieldDefinition("sku", aliases=("codigo", "código", "cod", "clave")),
        FieldDefinition(
            "nombre",
            required=True,
            aliases=("producto", "nombres", "nombre producto", "nombre del producto"),
        ),
        FieldDefinition("descripcion", aliases=("descripción", "detalle")),
        FieldDefinition("descripcion_corta", aliases=("descripcion corta", "resumen")),
        FieldDefinition("categoria", aliases=("categoría", "categoria nombre")),
        FieldDefinition("marca", aliases=("marca nombre",)),
        FieldDefinition("tipo", aliases=("tipo producto",)),
        FieldDefinition("destacado", type="boolean"),
        FieldDefinition("es_digital", type="boolean", aliases=("digital",)),
        FieldDefinition("tags", aliases=("etiquetas", "tag")),
        FieldDefinition("atributos", type="attributes", aliases=("ficha", "especificaciones")),
        FieldDefinition("precio", type="number"),
        _PRICE_COMPARISON,
        FieldDefinition("costo", type="number", aliases=("costo unitario", "precio costo")),
        FieldDefinition("activo", type="boolean", aliases=("publicado", "visible", "habilitado")),
        FieldDefinition("peso", type="number", aliases=("peso kg",)),
        FieldDefinition("stock", type="number", aliases=("existencias", "cantidad", "inventario")),
        FieldDefinition(
            "stock_minimo", type="number", aliases=("stock minimo", "minimo", "punto de reposicion")
        ),
    ),
    "variantes": (
        FieldDefinition(
            "sku_producto",
            required=True,
            aliases=("sku padre", "sku padre", "producto", "sku producto"),
        ),
        FieldDefinition("sku_variante", aliases=("sku", "codigo variante", "cod variante")),
        FieldDefinition(
            "atributos",
            type="attributes",
            required=True,
            aliases=("atributo", "opciones", "variacion"),
        ),
        FieldDefinition("precio", type="number"),
        _PRICE_COMPARISON,
        FieldDefinition("stock", type="number", aliases=("existencias", "cantidad", "inventario")),
        FieldDefinition("activa", type="boolean", aliases=("activo", "habilitada", "disponible")),
    ),
    "precios": (
        FieldDefinition("sku", required=True, aliases=("codigo", "código", "sku producto")),
        FieldDefinition("sku_variante", aliases=("variante", "sku variante")),
        FieldDefinition("lista", aliases=("lista de precios", "lista precios", "lista precios")),
        FieldDefinition("precio", type="number", required=True),
        _PRICE_COMPARISON,
    ),
    "clientes": (
        FieldDefinition("nombre", required=True, aliases=("nombres", "cliente")),
        FieldDefinition("apellido", aliases=("apellidos",)),
        FieldDefinition("razon_social", aliases=("razon social", "empresa")),
        FieldDefinition("tipo", aliases=("tipo cliente",)),
        FieldDefinition("email", aliases=("correo", "e-mail", "mail")),
        FieldDefinition("celular", aliases=("telefono", "whatsapp", "wsp", "phone", "tel")),
        FieldDefinition("nit_ci", aliases=("nit", "ci", "documento", "carnet")),
        FieldDefinition("profesion", aliases=("profesión",)),
        FieldDefinition("titulo", aliases=("título",)),
        FieldDefinition(
            "fecha_nacimiento",
            type="date",
            aliases=("nacimiento", "cumpleanos", "fecha nacimiento"),
        ),
        FieldDefinition("direccion", aliases=("direccion completa", "domicilio")),
        FieldDefinition("ciudad", aliases=("localidad",)),
        FieldDefinition("lista_precios", aliases=("lista", "audiencia", "tarifa")),
        FieldDefinition("notas", aliases=("nota", "observaciones", "comentarios")),
        FieldDefinition("activo", type="boolean", aliases=("habilitado",)),
        FieldDefinition("origen"),
    ),
}


def fields_for(entity: str) -> tuple[FieldDefinition, ...]:
    try:
        return FIELDS[entity]
    except KeyError as exc:
        raise ValueError(f"Entidad no soportada: {entity}. Usa: {', '.join(ENTITIES)}") from exc


def field_names(entity: str) -> set[str]:
    return {field.name for field in fields_for(entity)}
