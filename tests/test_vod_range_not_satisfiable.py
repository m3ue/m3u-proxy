"""
Regression tests: an upstream 416 on a VOD/catchup range request must be passed
straight through to the client instead of being retried.

Players probing the end of a catchup/VOD file (mpv/ffmpeg request
``bytes=<size>-``) get a legitimate 416 from the provider. Treating it as a
transient stream error made the proxy retry it several times (seconds each)
before the client ever saw a response, stalling seeks.
"""

import httpx
import pytest

from stream_manager import StreamManager


class _MockResponse:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = {"content-type": "video/mp2t", **(headers or {})}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://provider.example.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"{self.status_code} error", request=request, response=response
            )

    def aiter_bytes(self, chunk_size=32768):
        raise AssertionError("416 response body must not be read")


class _MockStreamCM:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_vod_416_is_passed_through_without_retrying(monkeypatch):
    manager = StreamManager()
    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr("config.settings.STREAM_RETRY_DELAY", 0.0)

    url = "http://provider.example.com/timeshift/u/p/60/2026-10-02:00-00/42.ts"
    stream_id = await manager.get_or_create_stream(url)
    assert manager.streams[stream_id].is_vod

    calls = []

    def fake_stream(method, url, headers=None, follow_redirects=True):
        calls.append(headers.get("Range"))
        return _MockStreamCM(
            _MockResponse(416, {"content-range": "bytes */1674280420"})
        )

    monkeypatch.setattr(manager.http_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(
        stream_id, "test_client", range_header="bytes=1674280420-"
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */1674280420"
    assert calls == ["bytes=1674280420-"]


@pytest.mark.asyncio
async def test_live_416_still_goes_through_retry_path(monkeypatch):
    """The pass-through is VOD-only; live streams keep their existing retry behaviour."""
    manager = StreamManager()
    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr("config.settings.STREAM_RETRY_DELAY", 0.0)

    url = "http://provider.example.com/live/u/p/42.ts"
    stream_id = await manager.get_or_create_stream(url)
    assert not manager.streams[stream_id].is_vod

    calls = []

    def fake_stream(method, url, headers=None, follow_redirects=True):
        calls.append(url)
        return _MockStreamCM(_MockResponse(416))

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")

    assert response.status_code != 416
    assert len(calls) > 1
