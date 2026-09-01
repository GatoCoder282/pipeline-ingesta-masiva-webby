"""Small, explicit configuration loader.

Configuration is kept outside the domain so local files, environment variables
and future secret managers can be swapped without changing pipeline rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a minimal dotenv file without overwriting the process environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class MappingConfig:
    source: str
    entity: str
    columns: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, object] = field(default_factory=dict)
    sheet: str | None = None
    encoding: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> MappingConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"El mapping {path} debe ser un objeto YAML.")
        columns = raw.get("columns", {})
        if not isinstance(columns, dict):
            raise ValueError("`columns` debe ser un objeto {encabezado_origen: campo_webby}.")
        return cls(
            source=str(raw.get("source", path.stem)),
            entity=str(raw.get("entity", "")),
            columns={str(k): str(v) for k, v in columns.items()},
            defaults=dict(raw.get("defaults", {}) or {}),
            sheet=str(raw["sheet"]) if raw.get("sheet") is not None else None,
            encoding=str(raw["encoding"]) if raw.get("encoding") is not None else None,
        )


@dataclass(frozen=True)
class WebbyConfig:
    base_url: str
    api_token: str
    tenant_slug: str | None = None
    timeout_seconds: float = 60.0
    poll_interval_seconds: float = 2.0
    max_poll_seconds: float = 900.0

    @classmethod
    def from_environment(cls) -> WebbyConfig:
        token = os.getenv("WEBBY_API_TOKEN", "").strip()
        return cls(
            base_url=os.getenv("WEBBY_BASE_URL", "http://localhost:8000").rstrip("/"),
            api_token=token,
            tenant_slug=os.getenv("WEBBY_TENANT_SLUG") or None,
            timeout_seconds=float(os.getenv("WEBBY_TIMEOUT_SECONDS", "60")),
            poll_interval_seconds=float(os.getenv("WEBBY_POLL_INTERVAL_SECONDS", "2")),
            max_poll_seconds=float(os.getenv("WEBBY_MAX_POLL_SECONDS", "900")),
        )


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = Path("data")
    max_rows: int = 100_000

    @classmethod
    def from_environment(cls) -> AppConfig:
        return cls(
            data_dir=Path(os.getenv("INGESTION_DATA_DIR", "data")),
            max_rows=int(os.getenv("INGESTION_MAX_ROWS", "100000")),
        )


@dataclass(frozen=True)
class DocumentConfig:
    """Local document-extraction settings for a tenant/source profile."""

    source: str = "ad-hoc"
    ocr_language: str = "spa+eng"
    dpi: int = 220
    max_pages: int = 100
    tesseract_config: str = "--psm 11"
    columns: int = 1
    card_vertical_gap: float = 90.0
    confidence_threshold: float = 0.65
    block_low_confidence: bool = True

    @classmethod
    def from_file(cls, path: Path) -> DocumentConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"La configuración documental {path} debe ser un objeto YAML.")
        ocr = raw.get("ocr", {}) or {}
        layout = raw.get("layout", {}) or {}
        if not isinstance(ocr, dict) or not isinstance(layout, dict):
            raise ValueError("`ocr` y `layout` deben ser objetos en la configuración documental.")
        config = cls(
            source=str(raw.get("source", path.stem)),
            ocr_language=str(ocr.get("language", "spa+eng")),
            dpi=int(ocr.get("dpi", 220)),
            max_pages=int(raw.get("max_pages", 100)),
            tesseract_config=str(ocr.get("tesseract_config", "--psm 11")),
            columns=int(layout.get("columns", 1)),
            card_vertical_gap=float(layout.get("card_vertical_gap", 90.0)),
            confidence_threshold=float(layout.get("confidence_threshold", 0.65)),
            block_low_confidence=bool(layout.get("block_low_confidence", True)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.ocr_language.strip():
            raise ValueError("La configuración OCR requiere un idioma, por ejemplo `spa+eng`.")
        if self.dpi < 72 or self.dpi > 600:
            raise ValueError("`ocr.dpi` debe estar entre 72 y 600.")
        if self.max_pages < 1:
            raise ValueError("`max_pages` debe ser mayor que cero.")
        if self.columns < 1 or self.columns > 6:
            raise ValueError("`layout.columns` debe estar entre 1 y 6.")
        if self.card_vertical_gap <= 0:
            raise ValueError("`layout.card_vertical_gap` debe ser mayor que cero.")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("`layout.confidence_threshold` debe estar entre 0 y 1.")


def load_mapping_or_empty(path: Path | None, entity: str | None) -> MappingConfig:
    if path is not None:
        config = MappingConfig.from_file(path)
        if entity and config.entity and entity != config.entity:
            raise ValueError(f"El mapping declara entidad `{config.entity}`, no `{entity}`.")
        return config
    if not entity:
        raise ValueError("Debes indicar --entity o --mapping.")
    return MappingConfig(source="ad-hoc", entity=entity)
