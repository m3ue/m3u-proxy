"""Real loopback HTTP API/private-input/resolver; no encoder or provider."""
import asyncio
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn

from tests.test_live_normalization import manager as manager
from tests.test_qsv_ts_integration import dvb_fixture
from tests.test_ts_canonicalizer import fixture
from config import settings
import api
import live_normalization


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_private_input_http_recovers_or_ends_after_bounded_resolver(manager, monkeypatch, exhausted):
    monkeypatch.setattr(live_normalization, "validate_qsv_encoder", AsyncMock())
    monkeypatch.setattr(settings, "API_TOKEN", "")
    monkeypatch.setattr(settings, "MAX_FAILOVER_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS_PREBUFFER_SIZE", 0)
    monkeypatch.setattr(settings, "LIVE_CHUNK_TIMEOUT_SECONDS", 2)
    opened, closed, resolutions, tasks = [], [], [], set()
    base = ""

    async def source(reader, writer):
        tasks.add(asyncio.current_task())
        path = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, path, _ = lines[0].split()
            if method == "POST":
                size = int(next(line.split(":", 1)[1] for line in lines if line.lower().startswith("content-length:")))
                resolutions.append(json.loads(await reader.readexactly(size)))
                next_path = "/live/b.ts" if len(resolutions) == 1 else "/live/c.ts"
                body = json.dumps({"next_url": base + next_path}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            else:
                opened.append(path)
                # Unknown private PES must still fail closed, not become raw TS.
                data = b"!" * 188 if path == "/live/a.ts" else dvb_fixture(b"") if exhausted or path == "/live/b.ts" else fixture()
                # httpx upstream aiter_bytes uses fixed-size proxy chunks.
                data *= 1024
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nTransfer-Encoding: chunked\r\n\r\n" + f"{len(data):x}\r\n".encode() + data + b"\r\n")
                await writer.drain()
                await reader.read()
        finally:
            if path and path.startswith("/live/"):
                closed.append(path)
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    upstream = await asyncio.start_server(source, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{upstream.sockets[0].getsockname()[1]}"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, lifespan="off", log_level="critical", timeout_graceful_shutdown=1))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
            created = await client.post("/streams", json={
                "url": base + "/live/a.ts", "normalize_live_output": True,
                "normalization_encoder": "qsv", "failover_resolver_url": base + "/resolver",
            })
            assert created.status_code == 200, created.text
            outer = manager.streams[created.json()["stream_id"]]
            inner = manager.streams[outer.normalization_input_id]
            async with client.stream("GET", f"/stream/{inner.stream_id}") as response:
                assert response.status_code == 200
                assert response.headers.get("transfer-encoding") == "chunked"
                if exhausted:
                    with pytest.raises(httpx.RemoteProtocolError):
                        await response.aread()
                else:
                    data = await anext(response.aiter_bytes())
                    assert data and len(data) % 188 == 0
                    assert all(data[pos] == 0x47 for pos in range(0, len(data), 188))
                assert inner.failover_attempts == 2
                assert outer.failover_attempts == 0
                assert outer.transcode_stream_key is None
                assert len(resolutions) == 2
                assert all(row["reason"] == "normalization_input_error" for row in resolutions)
            async with asyncio.timeout(3):
                while len(closed) < 3:
                    await asyncio.sleep(0.01)
            assert opened == ["/live/a.ts", "/live/b.ts", "/live/c.ts"]
            assert closed == opened
            assert not manager.connection_cancel_events
            assert not manager._direct_broadcast_primary
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        upstream.close()
        await upstream.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
