"""Async HTTP adapter for Webby's existing importacion API."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from ingestion_pipeline.config import WebbyConfig


class WebbyApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(f"Webby respondió HTTP {status_code}: {message}")


class WebbyClient:
    def __init__(self, config: WebbyConfig) -> None:
        if not config.api_token:
            raise ValueError("WEBBY_API_TOKEN no está configurado.")
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Bearer {config.api_token}",
                "Accept": "application/json",
                **({"X-Tenant-Slug": config.tenant_slug} if config.tenant_slug else {}),
            },
        )

    async def __aenter__(self) -> WebbyClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        response = await self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = {"detail": response.text}
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            if isinstance(detail, dict):
                raise WebbyApiError(
                    response.status_code, str(detail.get("message", detail)), detail.get("code")
                )
            raise WebbyApiError(response.status_code, str(detail))
        return response.json()

    async def list_entities(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/importacion/entidades")
        return payload if isinstance(payload, list) else []

    async def dispatch_import(
        self,
        *,
        entity: str,
        filename: str,
        content: bytes,
        mapping: dict[str, str],
        dry_run: bool,
        sucursal_id: str | None = None,
    ) -> str:
        data = {
            "entidad": entity,
            "mapeo": json.dumps(mapping, ensure_ascii=False),
            "dry_run": str(dry_run).lower(),
        }
        if sucursal_id:
            data["sucursal_id"] = sucursal_id
        payload = await self._request(
            "POST",
            "/importacion/importar",
            data=data,
            files={"archivo": (filename, content, "text/csv")},
        )
        if not isinstance(payload, dict) or not payload.get("trabajo_id"):
            raise WebbyApiError(502, "La API no devolvió trabajo_id.")
        return str(payload["trabajo_id"])

    async def get_job(self, job_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/importacion/trabajos/{job_id}")
        return payload if isinstance(payload, dict) else {}

    async def wait_for_job(self, job_id: str) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            payload = await self.get_job(job_id)
            status = payload.get("estado")
            if status in {"completado", "error", "cancelado"}:
                if status != "completado":
                    raise WebbyApiError(
                        502, str(payload.get("error") or f"Trabajo en estado {status}")
                    )
                return payload
            if time.monotonic() - started >= self.config.max_poll_seconds:
                raise WebbyApiError(
                    504,
                    f"El trabajo {job_id} no terminó dentro de {self.config.max_poll_seconds:g} segundos.",
                )
            await asyncio.sleep(self.config.poll_interval_seconds)
