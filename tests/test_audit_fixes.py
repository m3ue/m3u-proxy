"""
Regressions for the confirmed, narrowly scoped findings from the Nov 2026 backend
audit (#75, #78, #80, #81, #83, #84, #88, #89, #90, #91). Every fixture is local:
synthetic credentials, MockTransport and a benign Python child process.
"""

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.connection import parse_url

import api
from api import app
from config import settings
from models import WebhookConfig
from log_anonymizer import AnonymizingFilter
from pooled_stream_manager import SharedTranscodingProcess
from redis_config import get_redis_config
from stream_manager import StreamManager
from url_signing import ROLE_DASH_FILE, ROLE_HLS_SEGMENT, sign_url


# --- #89 Redis password encoding -------------------------------------------


@pytest.mark.parametrize(
    "password",
    [
        "plain-token",
        "synthetic/secret",
        "synthetic#secret",
        "synthetic%40secret",
        "a@b:c d",
        "pässwörd",
    ],
)
def test_redis_password_round_trips_through_url(monkeypatch, password):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", password)
    parsed = parse_url(get_redis_config()["redis_url"])
    assert parsed["password"] == password
    assert parsed["port"] == settings.REDIS_SERVER_PORT


def test_explicit_redis_url_is_used_verbatim(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:already%2Fencoded@cache:6380/2")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "ignored/value")
    assert get_redis_config()["redis_url"] == "redis://:already%2Fencoded@cache:6380/2"


# --- #90 health status + liveness probe ------------------------------------


def test_health_failure_returns_http_500():
    with patch("api.stream_manager") as manager:
        manager.get_stats = Mock(side_effect=RuntimeError("SYNTHETIC_STATS_FAILURE"))
        response = TestClient(app).get("/health")
    assert response.status_code == 500
    assert response.json()["status"] == "error"


def test_liveness_probe_needs_no_token(monkeypatch):
    monkeypatch.setattr(settings, "API_TOKEN", "synthetic-management-token")
    client = TestClient(app)
    assert client.get("/health").status_code == 401
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}


# --- #91 webhook test outcome ----------------------------------------------


def _webhook_test(status_code):
    webhook = WebhookConfig(
        url="http://hooks.invalid/synthetic",
        events=["stream_started"],
        retry_attempts=0,
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code))
    real_client = httpx.AsyncClient

    with (
        patch.object(api.event_manager, "webhooks", [webhook]),
        patch(
            "events.httpx.AsyncClient",
            lambda **kw: real_client(transport=transport, **kw),
        ),
    ):
        return TestClient(app).post(
            "/webhooks/test", params={"webhook_url": str(webhook.url)}
        )


def test_webhook_test_reports_failed_delivery():
    response = _webhook_test(500)
    assert response.status_code == 502
    assert "failed" in response.json()["detail"]


def test_webhook_test_reports_successful_delivery():
    response = _webhook_test(204)
    assert response.status_code == 200
    assert "event_id" in response.json()


# --- #88 yt-dlp child reaped on cancellation -------------------------------


@pytest.mark.asyncio
async def test_ytdlp_metadata_cancellation_reaps_child(monkeypatch):
    monkeypatch.setattr(settings, "YTDLP_ENABLED", True)
    monkeypatch.setattr(api, "get_resolver_version", AsyncMock(return_value="2026.1.1"))
    spawned = []
    real_exec = asyncio.create_subprocess_exec

    async def benign_child(*args, **kwargs):
        proc = await real_exec(
            sys.executable, "-c", "import time; time.sleep(60)", **kwargs
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", benign_child)

    task = asyncio.create_task(
        api.ytdlp_metadata(api.YtDlpMetadataRequest(url="http://media.invalid/x"))
    )
    while not spawned:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawned[0].returncode is not None


# --- #78 signed URLs are not decoded twice ---------------------------------


@pytest.mark.parametrize("route", ["/hls/s1/segment", "/dash/s1/file"])
def test_signed_segment_url_keeps_reserved_escapes(route):
    target = "http://cdn.invalid/seg.ts?token=a%2Fb%26c%3Dd"
    role = ROLE_HLS_SEGMENT if "hls" in route else ROLE_DASH_FILE
    with patch("api.stream_manager") as manager:
        manager.register_client = AsyncMock()
        manager.proxy_hls_segment = AsyncMock(return_value=api.Response(b""))
        manager.proxy_dash_segment = AsyncMock(return_value=api.Response(b""))
        response = TestClient(app).get(
            f"{route}?client_id=c1&url={quote(target, safe='')}&sig={sign_url(target, stream_id='s1', role=role)}"
        )
        assert response.status_code == 200
        proxied = (
            manager.proxy_hls_segment if "hls" in route else manager.proxy_dash_segment
        )
        assert proxied.call_args.args[2] == target


def test_dash_template_query_is_forwarded():
    from stream_manager import DashProcessor

    base = "http://cdn.invalid/live/"
    encoded = DashProcessor.sign_base(base, "s1")
    with patch("api.stream_manager") as manager:
        manager.register_client = AsyncMock()
        manager.proxy_dash_segment = AsyncMock(return_value=api.Response(b""))
        response = TestClient(app).get(
            f"/dash/s1/segment/c1/{encoded}/seg-5.m4s?token=abc%2Fdef"
        )
        assert response.status_code == 200
        assert (
            manager.proxy_dash_segment.call_args.args[2]
            == "http://cdn.invalid/live/seg-5.m4s?token=abc%2Fdef"
        )


# --- #83 no replay of a partially delivered segment -------------------------


# A full 32 KiB chunk: aiter_bytes(chunk_size=32768) buffers anything smaller.
FIRST_CHUNK = b"x" * 32768


class _BreaksAfterFirstChunk(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield FIRST_CHUNK
        raise httpx.ReadError("synthetic mid-body failure")


@pytest.mark.parametrize("method", ["proxy_hls_segment", "proxy_dash_segment"])
@pytest.mark.asyncio
async def test_partial_segment_is_not_restarted(method):
    manager = StreamManager()
    stream_id = await manager.get_or_create_stream(
        "http://src.invalid/live.m3u8",
        failover_urls=["http://backup.invalid/live.m3u8"],
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=_BreaksAfterFirstChunk())

    manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager._try_update_failover_url = AsyncMock()

    response = await getattr(manager, method)(
        stream_id, "c1", "http://src.invalid/seg1.ts"
    )
    body = b""
    with pytest.raises(httpx.ReadError):
        async for chunk in response.body_iterator:
            body += chunk

    assert body == FIRST_CHUNK
    assert len(requests) == 1
    manager._try_update_failover_url.assert_not_awaited()


# --- #84 end marker reaches a full client queue -----------------------------


@pytest.mark.asyncio
async def test_end_marker_replaces_oldest_chunk_when_queue_full():
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(b"chunk")
    SharedTranscodingProcess._put_end_marker(queue)
    assert queue.get_nowait() is None


# --- #80 / #81 variant playlists -------------------------------------------


@pytest.mark.asyncio
async def test_variant_inherits_parent_source_headers():
    manager = StreamManager()
    parent = await manager.get_or_create_stream(
        "http://src.invalid/master.m3u8", headers={"Authorization": "Bearer synthetic"}
    )
    variant = await manager.get_or_create_stream(
        "http://src.invalid/720p.m3u8", parent_stream_id=parent
    )
    assert manager.streams[variant].headers == {"Authorization": "Bearer synthetic"}


@pytest.mark.asyncio
async def test_sticky_redirect_does_not_replace_variant_with_master():
    manager = StreamManager()
    parent = await manager.get_or_create_stream("http://lb.invalid/master.m3u8")
    variant = await manager.get_or_create_stream(
        "http://lb.invalid/720p.m3u8", parent_stream_id=parent
    )
    # Sticky session pinned the parent to its redirect target; no failover ran.
    manager.streams[parent].current_url = "http://edge-3.invalid/master.m3u8"
    manager.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )

    await manager.get_playlist_content(variant, "c1", "/hls/" + variant)

    assert manager.streams[variant].original_url == "http://lb.invalid/720p.m3u8"


# --- #75 log redaction gaps -------------------------------------------------


@pytest.mark.parametrize(
    "message, secret",
    [
        (
            "GET /health?api_token=SYNTHETIC_MANAGEMENT_SECRET",
            "SYNTHETIC_MANAGEMENT_SECRET",
        ),
        ("header authorization=Bearer SYNTHETIC_AUTH_SECRET", "SYNTHETIC_AUTH_SECRET"),
        (
            "redis failed: redis://:SYNTHETIC_REDIS_SECRET@localhost:6379/0",
            "SYNTHETIC_REDIS_SECRET",
        ),
    ],
)
def test_anonymizer_redacts_remaining_secret_forms(message, secret):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    AnonymizingFilter().filter(record)
    assert secret not in logging.Formatter().format(record)


def test_anonymizer_redacts_exception_text():
    try:
        raise RuntimeError("fetch https://example.invalid/SYNTHETIC_TRACEBACK_SECRET")
    except RuntimeError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    AnonymizingFilter().filter(record)
    rendered = logging.Formatter().format(record)
    assert "SYNTHETIC_TRACEBACK_SECRET" not in rendered
    assert "RuntimeError" in rendered
