"""Value objects shared by the adapters, validator and artifact store."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


def json_default(value: object) -> object:
    if isinstance(value, (date, datetime, UUID)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"No se puede serializar {type(value).__name__}")


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=json_default, sort_keys=True)


@dataclass(frozen=True)
class ParsedTable:
    headers: list[str]
    rows: list[list[object | None]]
    source_path: Path
    sheet_name: str | None = None

    @property
    def total_rows(self) -> int:
        return len(self.rows)


@dataclass
class NormalizedRecord:
    row_number: int
    entity: str
    data: dict[str, object]
    source: dict[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "entity": self.entity,
            "data": self.data,
            "source": self.source,
        }


@dataclass(frozen=True)
class ValidationIssue:
    row_number: int
    code: str
    message: str
    severity: str = "error"
    field: str | None = None

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ValidationReport:
    run_id: str
    entity: str
    source_file: str
    total_rows: int
    valid_rows: int = 0
    invalid_rows: int = 0
    omitted_rows: int = 0
    issues: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)

    @property
    def blocking_errors(self) -> int:
        return len(self.issues)

    @property
    def approved_for_publish(self) -> bool:
        return self.invalid_rows == 0 and self.total_rows > 0

    def as_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "entity": self.entity,
            "source_file": self.source_file,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "omitted_rows": self.omitted_rows,
            "blocking_errors": self.blocking_errors,
            "approved_for_publish": self.approved_for_publish,
            "issues": [issue.as_json() for issue in self.issues],
            "warnings": [issue.as_json() for issue in self.warnings],
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class RunManifest:
    run_id: str
    entity: str
    source_file: str
    source_path: str
    source_sha256: str
    created_at: datetime = field(default_factory=utc_now)
    status: str = "validated"
    approved_by: str | None = None
    approved_at: datetime | None = None
    published_at: datetime | None = None
    webby_job_id: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    input_kind: str = "table"
    document_type: str | None = None
    extraction_engine: str | None = None

    @classmethod
    def new(
        cls, entity: str, source_file: str, source_path: Path, source_sha256: str
    ) -> RunManifest:
        return cls(
            run_id=str(uuid4()),
            entity=entity,
            source_file=source_file,
            source_path=str(source_path),
            source_sha256=source_sha256,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "entity": self.entity,
            "source_file": self.source_file,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "webby_job_id": self.webby_job_id,
            "artifacts": self.artifacts,
            "input_kind": self.input_kind,
            "document_type": self.document_type,
            "extraction_engine": self.extraction_engine,
        }
