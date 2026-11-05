"""Metadata/health probes must use the registered playback context."""

import asyncio

import httpx
import pytest
import pytest_asyncio

import api
from config import settings
from stream_manager import StreamManager


@pytest_asyncio.fixture
async def probes(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "API_TOKEN", None)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS", False)
    manager = StreamManager(enable_pooling=False)
    monkeypatch.setattr(api, "stream_manager", manager)
    await manager.http_client.aclose()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://proxy.test"
    ) as client:
        yield manager, client
    await manager.stop()


def set_origin(manager, handler):
    manager.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ua_key", ["User-Agent", "user-agent", "uSeR-aGeNt"])
async def test_head_uses_source_context_and_player_range(probes, ua_key):
    manager, client = probes
    captured = []
    source = {
        "Authorization": "Bearer synthetic",
        "Referer": "https://editor.test/player",
        "X-Playback": "editor-context",
        ua_key: "Override/1.0",
        "range": "bytes=0-1",
    }

    def origin(request):
        captured.append(request)
        authorized = (
            request.headers.get("authorization") == "Bearer synthetic"
            and request.headers.get("referer") == "https://editor.test/player"
            and request.headers.get("x-playback") == "editor-context"
            and request.headers.get_list("user-agent") == ["Override/1.0"]
        )
        return httpx.Response(
            206 if authorized else 401,
            headers={
                "Content-Length": "4",
                "Content-Range": "bytes 2-5/16",
                "Content-Type": "video/custom",
                "Accept-Ranges": "none",
            },
        )

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4", user_agent="Editor/1.0", headers=source
    )
    stored = manager.streams[sid].headers.copy()
    response = await client.head(f"/stream/{sid}", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.headers["content-length"] == "4"
    assert response.headers["content-range"] == "bytes 2-5/16"
    assert response.headers["content-type"] == "video/custom"
    assert response.headers["accept-ranges"] == "none"
    assert response.content == b""
    assert captured[0].headers.get_list("range") == ["bytes=2-5"]
    assert manager.streams[sid].headers == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206, 401, 403, 404, 416, 500])
async def test_head_preserves_origin_status_and_metadata(probes, status):
    manager, client = probes
    upstream = httpx.Response(
        status,
        headers={
            "Content-Length": "16",
            "Content-Type": "video/mp4",
            "Content-Range": "bytes */16" if status == 416 else "bytes 0-15/16",
            "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        },
    )
    set_origin(manager, lambda request: upstream)
    sid = await manager.get_or_create_stream("https://origin.test/movie.mp4")
    response = await client.head(f"/stream/{sid}")
    assert response.status_code == status
    assert response.headers["content-length"] == "16"
    assert response.headers["content-range"] == upstream.headers["content-range"]
    assert response.headers["last-modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert upstream.headers["content-length"] == "16"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 405, 501])
async def test_head_unknown_length_does_not_claim_empty_file_or_open_get(
    probes, status
):
    manager, client = probes
    requests = []

    def origin(request):
        requests.append(request)
        # A rejected HEAD's length describes an error, not the media.
        return httpx.Response(
            status, headers={"Content-Length": "123"} if status != 200 else {}
        )

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream("https://origin.test/movie.mp4")
    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 200
    assert "content-length" not in response.headers
    assert response.content == b""
    assert [r.method for r in requests] == ["HEAD"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_head_network_failure_is_bad_gateway(probes, error):
    manager, client = probes

    def origin(request):
        raise error("synthetic network failure", request=request)

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream("https://origin.test/movie.mp4")
    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 502
    assert response.content == b""
    assert await manager._health_check_stream(sid) is False
    assert manager.streams[sid].last_health_check is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method", [("playlist.m3u8", "GET"), ("movie.mp4", "HEAD")]
)
@pytest.mark.parametrize("ua_key", [None, "User-Agent", "user-agent"])
@pytest.mark.parametrize("auth_status", [401, 403])
async def test_health_uses_playback_context_and_rejects_auth_errors(
    probes, path, method, ua_key, auth_status
):
    manager, _ = probes
    requests = []
    source = {
        "Authorization": "Bearer synthetic",
        "Referer": "https://editor.test/player",
        "X-Playback": "editor",
    }
    if ua_key:
        source[ua_key] = "Override/1.0"
    expected_ua = "Override/1.0" if ua_key else "Editor/1.0"

    def origin(request):
        requests.append(request)
        authorized = (
            request.headers.get("authorization") == "Bearer synthetic"
            and request.headers.get("referer") == "https://editor.test/player"
            and request.headers.get("x-playback") == "editor"
            and request.headers.get_list("user-agent") == [expected_ua]
        )
        return httpx.Response(200 if authorized else auth_status, text="#EXTM3U\n")

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        f"https://origin.test/{path}", user_agent="Editor/1.0", headers=source
    )
    stored = manager.streams[sid].headers.copy()
    assert await manager._health_check_stream(sid) is True
    assert manager.streams[sid].last_health_check is not None
    assert manager.streams[sid].headers == stored
    manager.streams[sid].headers["Authorization"] = "Bearer invalid"
    assert await manager._health_check_stream(sid) is False
    assert [r.method for r in requests] == [method, method]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["head", "hls-health", "vod-health"])
@pytest.mark.parametrize("host", ["origin.test", "other.test"])
async def test_probes_keep_native_redirect_auth_handling(probes, operation, host):
    manager, client = probes
    requests = []

    def origin(request):
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(
                302, headers={"Location": f"https://{host}/movie.mp4"}
            )
        return httpx.Response(200, headers={"Content-Length": "16"})

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        "https://origin.test/redirect", headers={"Authorization": "Bearer synthetic"}
    )
    manager.streams[sid].is_hls = operation == "hls-health"
    if operation == "head":
        assert (await client.head(f"/stream/{sid}")).status_code == 200
    else:
        assert await manager._health_check_stream(sid) is True
    assert len(requests) == 2
    assert requests[0].headers.get("authorization") == "Bearer synthetic"
    assert requests[1].headers.get("authorization") == (
        "Bearer synthetic" if host == "origin.test" else None
    )


@pytest.mark.asyncio
async def test_periodic_health_does_not_fail_over_working_authenticated_source(probes):
    manager, _ = probes
    requests = []

    def origin(request):
        requests.append(request)
        return httpx.Response(
            200 if request.headers.get("authorization") == "Bearer synthetic" else 401
        )

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4",
        headers={"Authorization": "Bearer synthetic"},
        failover_urls=["https://backup.test/movie.mp4"],
    )
    manager.streams[sid].client_count = 1
    manager._running = True
    task = asyncio.create_task(manager._periodic_health_check())
    try:
        for _ in range(100):
            if requests:
                break
            await asyncio.sleep(0.01)
        assert len(requests) == 1
        assert manager.streams[sid].current_url == "https://origin.test/movie.mp4"
        assert manager.streams[sid].last_health_check is not None
    finally:
        task.cancel()
        await task
        manager._running = False


@pytest.mark.asyncio
async def test_head_missing_stream_and_strict_live_shortcut_unchanged(probes):
    manager, client = probes
    requests = []
    set_origin(manager, lambda request: requests.append(request) or httpx.Response(200))
    assert (await client.head("/stream/missing")).status_code == 404
    sid = await manager.get_or_create_stream(
        "https://origin.test/live.ts", strict_live_ts=True
    )
    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "none"
    assert requests == []
