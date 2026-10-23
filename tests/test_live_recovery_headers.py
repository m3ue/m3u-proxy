"""Session-end responses must not promise a truncated upstream entity length."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import settings
from stream_manager import StreamManager
from tests.test_silent_reconnect import _MockStreamCM, _response_from_iter, _collect


@pytest.mark.asyncio
async def test_nonstrict_reset_removes_upstream_length_and_range(monkeypatch):
    manager = StreamManager(enable_pooling=False)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS", False)
    monkeypatch.setattr(settings, "LIVE_SILENT_RECONNECT_MIN_CHUNKS", 10)

    async def body():
        yield b"A"

    response = _response_from_iter(body())
    response.headers = {
        "content-type": "video/mp2t",
        "content-length": "999999",
        "content-range": "bytes 0-999998/999999",
    }
    monkeypatch.setattr(
        manager.live_stream_client, "stream", lambda *a, **kw: _MockStreamCM(response)
    )
    sid = await manager.get_or_create_stream(
        "http://source.example/live/a.ts", end_session_on_reconnect=True
    )
    try:
        result = await manager.stream_continuous_direct(sid, "viewer")
        assert result.status_code == 200
        assert (
            "content-length" not in result.headers
            and "content-range" not in result.headers
        )
        assert await _collect(result) == [b"A"]
    finally:
        await manager.http_client.aclose()
        await manager.live_stream_client.aclose()
