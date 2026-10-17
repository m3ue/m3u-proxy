"""
Failover resilience regressions.

1. When a live stream is closed by the provider and the failover resolver has no
   URL to offer, the proxy must not reconnect straight back to the dead URL in a
   tight loop (hundreds of attempts a minute against an off-air channel).
2. A transcode input error must advance exactly one failover, not two
   (input error in the startup wait loop, then the final checks seeing the same
   failed process and failing over again).
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from pooled_stream_manager import PooledStreamManager, SharedTranscodingProcess
from stream_manager import StreamManager


class _MockStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _response(chunk: bytes, count: int):
    class _Iter:
        def __init__(self):
            self._sent = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent >= count:
                raise StopAsyncIteration
            self._sent += 1
            return chunk

    class _Resp:
        status_code = 200
        headers = {"content-type": "video/mp2t"}

        def raise_for_status(self):
            pass

        def aiter_bytes(self, chunk_size=32768):
            return _Iter()

    return _Resp()


async def _collect(response) -> list[bytes]:
    return [chunk async for chunk in response.body_iterator]


@pytest.mark.asyncio
async def test_silent_close_with_no_failover_url_does_not_loop(monkeypatch):
    """The resolver returns nothing: the proxy ends the stream instead of
    hammering the closed URL until MAX_FAILOVER_ATTEMPTS runs out."""
    manager = StreamManager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 50)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/offair.ts"
    stream_id = await manager.get_or_create_stream(
        url, failover_resolver_url="http://editor.example.com/resolver"
    )
    # The resolver has nothing to offer (every failover is off air / rate limited).
    monkeypatch.setattr(
        manager, "_resolve_next_failover_url", AsyncMock(return_value=None)
    )

    chunk = b"T" * 32768
    requests = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal requests
        requests += 1
        # First connection plays, then the channel goes off air: 200, empty body.
        return _MockStreamCM(_response(chunk, 10 if requests == 1 else 0))

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await asyncio.wait_for(_collect(response), timeout=10)

    assert len(chunks) == 10
    # Initial connection + the one same-URL retry; the resolver is asked once and
    # the stream then ends. Before the fix this was ~50 upstream requests.
    assert requests == 2, f"Expected 2 upstream requests, got {requests}"
    assert manager._resolve_next_failover_url.await_count == 1


@pytest.mark.asyncio
async def test_silent_close_with_no_failover_url_keeps_healthy_stream(monkeypatch):
    """No failover URL, but the connection itself was healthy: reconnect to the
    same URL (as a channel without failovers would) rather than ending."""
    manager = StreamManager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 5)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/flaky.ts"
    stream_id = await manager.get_or_create_stream(
        url, failover_resolver_url="http://editor.example.com/resolver"
    )
    monkeypatch.setattr(
        manager, "_resolve_next_failover_url", AsyncMock(return_value=None)
    )

    chunk = b"U" * 32768
    plan = [10, 10, 0]
    requests = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal requests
        count = plan[min(requests, len(plan) - 1)]
        requests += 1
        return _MockStreamCM(_response(chunk, count))

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await asyncio.wait_for(_collect(response), timeout=10)

    assert len(chunks) == 20
    assert requests == 3


def _mock_process(status: str, queue_items: list) -> Mock:
    process = Mock(spec=SharedTranscodingProcess)
    process.status = status
    process.process = Mock()
    process.process.pid = 1000
    process.process.returncode = None
    process.process.stdout = Mock()
    queue: asyncio.Queue = asyncio.Queue()
    for item in queue_items:
        queue.put_nowait(item)
    process.client_queues = {"test_client": queue}
    return process


@pytest.mark.asyncio
async def test_transcode_input_error_advances_one_failover(monkeypatch):
    """Regression: an input error used to skip a failover (#1 -> #2 instead of
    #1), because the final checks re-inspected the killed process."""
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 0)

    manager = StreamManager()
    manager.pooled_manager = Mock(spec=PooledStreamManager)

    primary = "http://provider.example.com/live/primary.ts"
    failovers = [
        "http://provider.example.com/live/failover1.ts",
        "http://provider.example.com/live/failover2.ts",
        "http://provider.example.com/live/failover3.ts",
    ]
    stream_id = await manager.get_or_create_stream(
        primary,
        failover_urls=failovers,
        is_transcoded=True,
        transcode_profile="h264_720p",
        transcode_ffmpeg_args=["-c:v", "libx264"],
    )

    failed = _mock_process("input_failed", [])
    good = _mock_process("running", [b"V" * 1024, None])

    async def force_stop(stream_key):
        # Killing ffmpeg sets a return code, as in production.
        failed.process.returncode = -9

    requested_urls = []

    async def get_or_create_shared_stream(**kwargs):
        requested_urls.append(kwargs["url"])
        process = failed if len(requested_urls) == 1 else good
        return f"key_{len(requested_urls)}", process

    manager.pooled_manager.get_or_create_shared_stream = AsyncMock(
        side_effect=get_or_create_shared_stream
    )
    manager.pooled_manager.force_stop_stream = AsyncMock(side_effect=force_stop)
    manager.pooled_manager.remove_client_from_stream = AsyncMock()

    response = await manager.stream_transcoded(stream_id, "test_client")
    chunks = await asyncio.wait_for(_collect(response), timeout=10)

    assert requested_urls[:2] == [primary, failovers[0]], (
        f"Expected primary then failover #1, got {requested_urls}"
    )
    assert manager.streams[stream_id].failover_attempts == 1
    assert b"".join(chunks) == b"V" * 1024
