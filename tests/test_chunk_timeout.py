import asyncio
import pytest

from stream_manager import StreamManager
import httpx


class HangingIterator:
    def __aiter__(self):
        return self

    async def __anext__(self):
        # Never yield - simulate upstream that keeps connection open but sends no data
        await asyncio.Event().wait()


class MockResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "video/mp2t"}

    def raise_for_status(self):
        return None

    def aiter_bytes(self, chunk_size=32768):
        return HangingIterator()


class MockStreamCM:
    def __init__(self, response):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_per_chunk_timeout_triggers_stream_failed(monkeypatch):
    manager = StreamManager()

    # Shorten timeout for fast test
    monkeypatch.setattr('config.settings.LIVE_CHUNK_TIMEOUT_SECONDS', 0.05)
    # Disable retries for this test - we want immediate failure on timeout
    monkeypatch.setattr('config.settings.STREAM_RETRY_ATTEMPTS', 0)
    # Also set a short total timeout to ensure test completes quickly
    monkeypatch.setattr('config.settings.STREAM_TOTAL_TIMEOUT', 1.0)

    # Create a live continuous stream (.ts)
    primary_url = "http://example.com/live/stream.ts"
    stream_id = await manager.get_or_create_stream(primary_url)

    # Patch live_stream_client.stream to return a context manager that returns a hanging iterator
    mock_resp = MockResponse()
    mock_cm = MockStreamCM(mock_resp)

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        return mock_cm

    monkeypatch.setattr(manager.live_stream_client, 'stream', fake_stream)

    # Capture emitted events
    events = []

    async def fake_emit(event_type, stream_id_arg, data):
        events.append((event_type, data))

    manager._emit_event = fake_emit

    # Call the direct stream handler - it will attempt to read first chunk and timeout
    resp = await manager.stream_continuous_direct(stream_id, 'test_client')

    # Because upstream never yields, the generator should stop and STREAM_FAILED should be emitted
    assert any(
        e[0] == 'STREAM_FAILED' for e in events), f"Expected STREAM_FAILED event, got {events}"
