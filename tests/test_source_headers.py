"""GET, HEAD and health probes must send the same source request headers."""

import httpx
import pytest
import pytest_asyncio

import api
from config import settings
from stream_manager import StreamManager


@pytest_asyncio.fixture
async def proxy(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "API_TOKEN", None)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS", False)
    manager = StreamManager(enable_pooling=False)
    monkeypatch.setattr(api, "stream_manager", manager)
    await manager.http_client.aclose()
    await manager.live_stream_client.aclose()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://proxy.test"
    ) as client:
        yield manager, client
    await manager.stop()


def set_origin(manager, handler):
    transport = httpx.MockTransport(handler)
    manager.http_client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    manager.live_stream_client = httpx.AsyncClient(
        transport=transport, follow_redirects=True
    )


def recording_origin(manager):
    """Capture the provider requests per method and answer every one with media."""
    captured = {}

    def origin(request):
        captured[request.method] = request.headers
        return httpx.Response(200, content=b"x" * 16, headers={"Content-Length": "16"})

    set_origin(manager, origin)
    return captured


@pytest.mark.asyncio
async def test_get_head_and_health_send_identical_source_headers(proxy):
    manager, client = proxy
    captured = recording_origin(manager)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4",
        user_agent="Editor/1.0",
        headers={"Authorization": "Bearer synthetic", "X-Playback": "editor"},
    )

    assert (await client.head(f"/stream/{sid}")).status_code == 200
    head_headers = captured["HEAD"]
    assert (await client.get(f"/stream/{sid}")).status_code == 200
    assert await manager._health_check_stream(sid) is True
    health_headers = captured["HEAD"]

    assert sorted(head_headers.multi_items()) == sorted(captured["GET"].multi_items())
    assert sorted(health_headers.multi_items()) == sorted(captured["GET"].multi_items())
    assert captured["GET"]["authorization"] == "Bearer synthetic"
    assert captured["GET"]["x-playback"] == "editor"
    assert captured["GET"]["user-agent"] == "Editor/1.0"
    assert captured["GET"]["referer"] == "https://origin.test/"
    assert captured["GET"]["origin"] == "https://origin.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["User-Agent", "user-agent", "uSeR-aGeNt"])
async def test_source_headers_override_defaults_case_insensitively(proxy, key):
    manager, client = proxy
    captured = recording_origin(manager)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4",
        user_agent="Editor/1.0",
        headers={key: "Override/1.0", "referer": "https://editor.test/"},
    )
    stored = manager.streams[sid].headers.copy()

    await client.head(f"/stream/{sid}")
    await client.get(f"/stream/{sid}")

    for method in ("HEAD", "GET"):
        assert captured[method].get_list("user-agent") == ["Override/1.0"]
        assert captured[method].get_list("referer") == ["https://editor.test/"]
    assert manager.streams[sid].headers == stored


@pytest.mark.asyncio
async def test_head_authorized_by_source_headers_returns_real_metadata(proxy):
    manager, client = proxy

    def origin(request):
        authorized = request.headers.get("authorization") == "Bearer synthetic"
        return httpx.Response(
            200 if authorized else 401, headers={"Content-Length": "16"}
        )

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4", headers={"Authorization": "Bearer synthetic"}
    )

    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 200
    assert response.headers["content-length"] == "16"


@pytest.mark.asyncio
async def test_head_passes_player_range_upstream_for_vod_over_configured_range(proxy):
    manager, client = proxy
    captured = recording_origin(manager)
    sid = await manager.get_or_create_stream(
        "https://origin.test/movie.mp4", headers={"range": "bytes=0-1"}
    )

    await client.head(f"/stream/{sid}", headers={"Range": "bytes=2-5"})
    assert captured["HEAD"].get_list("range") == ["bytes=2-5"]


@pytest.mark.asyncio
async def test_head_does_not_send_player_range_for_live_streams(proxy):
    manager, client = proxy
    captured = recording_origin(manager)
    sid = await manager.get_or_create_stream("https://origin.test/live/1.ts")

    await client.head(f"/stream/{sid}", headers={"Range": "bytes=0-"})
    assert "range" not in captured["HEAD"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 405, 416, 458, 501, 503])
async def test_head_response_handling_is_unchanged_on_provider_errors(proxy, status):
    manager, client = proxy
    set_origin(manager, lambda request: httpx.Response(status))
    sid = await manager.get_or_create_stream("https://origin.test/movie.mp4")

    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_head_strict_live_shortcut_still_skips_provider(proxy):
    manager, client = proxy
    requests = []
    set_origin(manager, lambda request: requests.append(request) or httpx.Response(200))
    sid = await manager.get_or_create_stream(
        "https://origin.test/live.ts", strict_live_ts=True
    )

    response = await client.head(f"/stream/{sid}")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "none"
    assert requests == []


@pytest.mark.asyncio
async def test_hls_health_check_matches_playlist_fetch_headers(proxy):
    manager, _ = proxy
    captured = []

    def origin(request):
        captured.append(request.headers)
        return httpx.Response(200, text="#EXTM3U\n")

    set_origin(manager, origin)
    sid = await manager.get_or_create_stream(
        "https://origin.test/playlist.m3u8",
        user_agent="Editor/1.0",
        headers={"Authorization": "Bearer synthetic"},
    )

    assert await manager._health_check_stream(sid) is True
    assert captured[0]["authorization"] == "Bearer synthetic"
    assert captured[0]["user-agent"] == "Editor/1.0"
    assert "referer" not in captured[0]
