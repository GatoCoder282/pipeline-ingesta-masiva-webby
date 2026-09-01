import httpx
import pytest

from ingestion_pipeline.config import WebbyConfig
from ingestion_pipeline.webby.client import WebbyClient


async def test_webby_client_dispatches_multipart_and_polls_to_completion() -> None:
    calls: list[tuple[str, str]] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = request.read()
            assert b'name="entidad"' in body
            assert b'name="dry_run"' in body
            assert b'name="archivo"' in body
            return httpx.Response(202, json={"trabajo_id": "job-1"})
        poll_count += 1
        if poll_count == 1:
            return httpx.Response(200, json={"estado": "procesando"})
        return httpx.Response(
            200,
            json={"estado": "completado", "resultado": {"fallidos": 0}},
        )

    config = WebbyConfig(
        base_url="http://testserver",
        api_token="dev-token",
        poll_interval_seconds=0,
    )
    async with WebbyClient(config) as client:
        await client.client.aclose()
        client.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
            headers=client.client.headers,
        )
        job_id = await client.dispatch_import(
            entity="productos",
            filename="productos.csv",
            content=b"sku,nombre\nA-1,Crema\n",
            mapping={"sku": "sku", "nombre": "nombre"},
            dry_run=True,
        )
        result = await client.wait_for_job(job_id)

    assert job_id == "job-1"
    assert result["estado"] == "completado"
    assert calls == [
        ("POST", "/importacion/importar"),
        ("GET", "/importacion/trabajos/job-1"),
        ("GET", "/importacion/trabajos/job-1"),
    ]


async def test_webby_client_stops_polling_after_timeout() -> None:
    config = WebbyConfig(
        base_url="http://testserver",
        api_token="dev-token",
        poll_interval_seconds=0,
        max_poll_seconds=0,
    )
    async with WebbyClient(config) as client:
        await client.client.aclose()
        client.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"estado": "procesando"})
            ),
            base_url="http://testserver",
        )
        with pytest.raises(Exception, match="no terminó"):
            await client.wait_for_job("job-stuck")
