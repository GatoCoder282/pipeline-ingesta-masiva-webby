"""Column mapping, type coercion and canonical row construction."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from ingestion_pipeline.config import MappingConfig
from ingestion_pipeline.domain.catalog import FieldDefinition, field_names, fields_for
from ingestion_pipeline.domain.models import NormalizedRecord, ParsedTable, ValidationIssue


def normalize_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _definitions(entity: str) -> dict[str, FieldDefinition]:
    return {field.name: field for field in fields_for(entity)}


def suggest_mapping(headers: list[str], entity: str) -> dict[str, str]:
    """Suggest source-header -> Webby-field mappings without guessing ambiguously."""
    definitions = _definitions(entity)
    choices: list[tuple[str, str]] = []
    for name, definition in definitions.items():
        for candidate in (name, *definition.aliases):
            choices.append((normalize_key(candidate), name))

    suggestions: dict[str, str] = {}
    used: set[str] = set()
    for header in headers:
        normalized = normalize_key(header)
        if not normalized:
            continue
        exact = sorted(
            {destination for candidate, destination in choices if candidate == normalized}
        )
        if len(exact) == 1 and exact[0] not in used:
            suggestions[header] = exact[0]
            used.add(exact[0])
            continue
        scored = sorted(
            (
                SequenceMatcher(None, normalized, candidate).ratio(),
                destination,
            )
            for candidate, destination in choices
            if destination not in used
        )
        if not scored:
            continue
        best_score, best_destination = scored[-1]
        second_score = scored[-2][0] if len(scored) > 1 else 0.0
        if best_score >= 0.82 and best_score - second_score >= 0.04:
            suggestions[header] = best_destination
            used.add(best_destination)
    return suggestions


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        if isinstance(value, bool):
            raise ValueError("no es un número válido")
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    text = re.sub(r"(?i)^(bs\.?|usd|\$)", "", text)
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("no es un número válido") from exc


def _boolean(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "verdadero", "si", "sí", "x", "y", "yes"}:
        return True
    if text in {"0", "false", "falso", "no", "n"}:
        return False
    raise ValueError("debe ser sí/no, true/false o 1/0")


def _date(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError("fecha inválida; usa aaaa-mm-dd o dd/mm/aaaa")


def normalize_attribute_key(value: object) -> str:
    """Return the stable key representation used inside a variant attribute object."""
    text = unicodedata.normalize("NFKD", str(value)).strip()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text:
        raise ValueError("las claves de atributos no pueden estar vacías")
    return text


def _attribute_object_pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for raw_key, raw_value in pairs:
        key = normalize_attribute_key(raw_key)
        if key in parsed:
            raise ValueError(f"la clave de atributo {key!r} está repetida")
        parsed[key] = raw_value
    return parsed


def normalize_attributes(value: object) -> dict[str, str] | None:
    """Parse one or more non-empty key/value attributes deterministically.

    Webby receives the complete combination in one JSON object.  Values stay
    textual because an attribute such as ``talla`` or ``color`` is a label,
    not a numeric measure, even when it contains digits.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        parsed: object = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = json.loads(text, object_pairs_hook=_attribute_object_pairs)
        except json.JSONDecodeError:
            parsed = None
        if parsed is None:
            pairs: list[str] = []
            for pair in re.split(r"[;|\n]", text):
                pair = pair.strip()
                if pair:
                    pairs.append(pair)
            parsed = {}
            for pair in pairs:
                separator = "=" if "=" in pair else ":" if ":" in pair else None
                if separator is None:
                    raise ValueError("atributos deben ser JSON o clave=valor;clave=valor")
                key, item_value = pair.split(separator, 1)
                normalized_key = normalize_attribute_key(key)
                if normalized_key in parsed:
                    raise ValueError(f"la clave de atributo {normalized_key!r} está repetida")
                parsed[normalized_key] = item_value
    if not isinstance(parsed, dict):
        raise ValueError("atributos debe ser un objeto JSON de clave y valor")

    result: dict[str, str] = {}
    for raw_key, raw_value in parsed.items():
        key = normalize_attribute_key(raw_key)
        if raw_value is None or isinstance(raw_value, (dict, list, tuple, set)):
            raise ValueError(f"el valor del atributo {key!r} debe ser texto o escalar")
        item_value = str(raw_value).strip()
        if not item_value:
            raise ValueError(f"el valor del atributo {key!r} no puede estar vacío")
        if key in result:
            raise ValueError(f"la clave de atributo {key!r} está repetida")
        result[key] = item_value
    return result or None


def _attributes(value: object) -> dict[str, str] | None:
    return normalize_attributes(value)


def coerce_value(value: object, definition: FieldDefinition) -> object | None:
    if value is None:
        return None
    if definition.type == "number":
        return _decimal(value)
    if definition.type == "boolean":
        return _boolean(value)
    if definition.type == "date":
        return _date(value)
    if definition.type == "attributes":
        return _attributes(value)
    text = str(value).strip()
    return text or None


def _mapping_for(table: ParsedTable, config: MappingConfig) -> dict[str, str]:
    mapping = config.columns or suggest_mapping(table.headers, config.entity)
    valid_names = field_names(config.entity)
    unknown = sorted(set(mapping.values()) - valid_names)
    if unknown:
        raise ValueError(f"Campos destino no soportados para {config.entity}: {', '.join(unknown)}")
    missing_headers = sorted(set(mapping) - set(table.headers))
    if missing_headers:
        raise ValueError(f"El mapping referencia columnas ausentes: {', '.join(missing_headers)}")
    destinations = list(mapping.values())
    duplicates = sorted({name for name in destinations if destinations.count(name) > 1})
    if duplicates:
        raise ValueError(f"Un campo destino está repetido en el mapping: {', '.join(duplicates)}")
    return mapping


def normalize_table(
    table: ParsedTable,
    config: MappingConfig,
    *,
    run_id: str,
) -> tuple[list[NormalizedRecord], list[ValidationIssue], dict[str, str]]:
    """Return normalized rows, coercion issues, and the mapping used."""
    mapping = _mapping_for(table, config)
    definitions = _definitions(config.entity)
    position = {header: index for index, header in enumerate(table.headers)}
    records: list[NormalizedRecord] = []
    issues: list[ValidationIssue] = []

    for offset, row in enumerate(table.rows, start=2):
        if not any(value is not None and str(value).strip() for value in row):
            continue
        data: dict[str, object] = dict(config.defaults)
        source_metadata: dict[str, object] = {
            "run_id": run_id,
            "source_file": table.source_path.name,
            "source_row": offset,
        }
        row_has_error = False
        for source_header, destination in mapping.items():
            value = row[position[source_header]] if position[source_header] < len(row) else None
            try:
                coerced = coerce_value(value, definitions[destination])
            except ValueError as exc:
                issues.append(
                    ValidationIssue(
                        offset, "invalid_type", f"{destination}: {exc}", field=destination
                    )
                )
                row_has_error = True
                continue
            if coerced is not None:
                data[destination] = coerced
        if not row_has_error:
            records.append(NormalizedRecord(offset, config.entity, data, source_metadata))
    return records, issues, mapping
