"""
Tests for overlap trimming on live MPEG-TS silent reconnects (issue #63).

Providers that close live connections periodically usually restart from their
rolling buffer, a few seconds behind what the client already received. With
Strict Live TS Mode, the proxy should drop that replayed overlap so the client
receives one continuous byte stream with no jump-back.
"""

import os

import pytest

from stream_manager import StreamManager
from ts_overlap import (
    OverlapTrimmer,
    TRIM_MATCHED,
    TRIM_NO_MATCH,
    TRIM_PENDING,
)


CHUNK_SIZE = 32768


def _payload(size: int) -> bytes:
    """Random bytes with no 0x47 sync bytes, so discontinuity injection is a no-op."""
    return os.urandom(size).replace(b"\x47", b"\x00")


class _MockStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _response_from_iter(iterator):
    class _Resp:
        status_code = 200
        headers = {"content-type": "video/mp2t"}

        def raise_for_status(self):
            pass

        def aiter_bytes(self, chunk_size=CHUNK_SIZE):
            return iterator

    return _Resp()


async def _collect(response) -> list[bytes]:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return chunks


def _iter_bytes_then_stop(data: bytes, chunk_size: int = CHUNK_SIZE):
    """Async iterator yielding `data` in fixed-size chunks, then a clean close."""

    class _Iter:
        def __init__(self):
            self._offset = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._offset >= len(data):
                raise StopAsyncIteration
            chunk = data[self._offset : self._offset + chunk_size]
            self._offset += chunk_size
            return chunk

    return _Iter()


# ---------------------------------------------------------------------------
# OverlapTrimmer unit tests
# ---------------------------------------------------------------------------


def _trimmer(max_search=1 << 20, max_wait=5.0):
    return OverlapTrimmer(
        signature_bytes=4096, max_search_bytes=max_search, max_wait_seconds=max_wait
    )


def test_trimmer_drops_overlap_and_returns_exact_continuation():
    stream = _payload(100_000)
    trimmer = _trimmer()
    trimmer.record(stream[:60_000])
    assert trimmer.arm() is True

    # New connection restarts 20 KB behind the last delivered byte
    out, result = trimmer.feed(stream[40_000:], now=0.0)

    assert result == TRIM_MATCHED
    assert out == stream[60_000:]
    assert trimmer.last_trimmed_bytes == 20_000
    assert trimmer.pending is False


def test_trimmer_holds_until_signature_spans_multiple_chunks():
    stream = _payload(100_000)
    trimmer = _trimmer()
    trimmer.record(stream[:60_000])
    trimmer.arm()

    # Signature (last 4096 bytes delivered) straddles the two chunks
    out, result = trimmer.feed(stream[50_000:58_000], now=0.0)
    assert (out, result) == (b"", TRIM_PENDING)

    out, result = trimmer.feed(stream[58_000:70_000], now=0.1)
    assert result == TRIM_MATCHED
    assert out == stream[60_000:70_000]


def test_trimmer_releases_held_bytes_unchanged_when_search_window_exhausted():
    trimmer = _trimmer(max_search=10_000)
    trimmer.record(_payload(20_000))
    trimmer.arm()

    unrelated = _payload(12_000)
    out, result = trimmer.feed(unrelated[:6_000], now=0.0)
    assert result == TRIM_PENDING
    out, result = trimmer.feed(unrelated[6_000:], now=0.1)

    assert result == TRIM_NO_MATCH
    assert out == unrelated
    assert trimmer.pending is False


def test_trimmer_releases_held_bytes_after_max_wait():
    trimmer = _trimmer(max_wait=1.0)
    trimmer.record(_payload(20_000))
    trimmer.arm()

    first, second = _payload(1_000), _payload(1_000)
    assert trimmer.feed(first, now=10.0) == (b"", TRIM_PENDING)
    out, result = trimmer.feed(second, now=11.5)

    assert result == TRIM_NO_MATCH
    assert out == first + second


def test_trimmer_does_not_arm_without_enough_delivered_data():
    trimmer = _trimmer()
    trimmer.record(b"\x47" * 100)
    assert trimmer.arm() is False
    assert trimmer.pending is False


# ---------------------------------------------------------------------------
# End-to-end: silent reconnect through stream_continuous_direct
# ---------------------------------------------------------------------------


async def _run_reconnect(monkeypatch, first: bytes, second: bytes, strict: bool):
    manager = StreamManager()

    monkeypatch.setattr("config.settings.STRICT_LIVE_TS", strict)
    monkeypatch.setattr("config.settings.STRICT_LIVE_TS_PREBUFFER_SIZE", 0)
    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    stream_id = await manager.get_or_create_stream(
        "http://provider.example.com/live/channel.ts"
    )

    responses = [
        _response_from_iter(_iter_bytes_then_stop(first)),
        _response_from_iter(_iter_bytes_then_stop(second)),
        # Third connection ends immediately (below min chunks -> stream end)
        _response_from_iter(_iter_bytes_then_stop(b"")),
    ]
    call_count = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return _MockStreamCM(resp)

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    return b"".join(await _collect(response))


@pytest.mark.asyncio
async def test_silent_reconnect_trims_replayed_overlap_in_strict_mode(monkeypatch):
    stream = _payload(CHUNK_SIZE * 20)
    cut = CHUNK_SIZE * 10
    # Provider restarts ~3.5 chunks behind the last delivered byte
    first, second = stream[:cut], stream[cut - 115_000 :]

    received = await _run_reconnect(monkeypatch, first, second, strict=True)

    assert received == stream


@pytest.mark.asyncio
async def test_silent_reconnect_forwards_unrelated_data_when_no_overlap(monkeypatch):
    first = _payload(CHUNK_SIZE * 10)
    second = _payload(CHUNK_SIZE * 10)
    monkeypatch.setattr("config.settings.STRICT_LIVE_TS_OVERLAP_MAX_SEARCH_SIZE", 65536)

    received = await _run_reconnect(monkeypatch, first, second, strict=True)

    assert received == first + second


@pytest.mark.asyncio
async def test_silent_reconnect_does_not_trim_outside_strict_mode(monkeypatch):
    stream = _payload(CHUNK_SIZE * 20)
    cut = CHUNK_SIZE * 10
    first, second = stream[:cut], stream[cut - 115_000 :]

    received = await _run_reconnect(monkeypatch, first, second, strict=False)

    assert received == first + second


@pytest.mark.asyncio
async def test_silent_reconnect_does_not_trim_when_disabled(monkeypatch):
    stream = _payload(CHUNK_SIZE * 20)
    cut = CHUNK_SIZE * 10
    first, second = stream[:cut], stream[cut - 115_000 :]
    monkeypatch.setattr("config.settings.STRICT_LIVE_TS_OVERLAP_TRIM", False)

    received = await _run_reconnect(monkeypatch, first, second, strict=True)

    assert received == first + second
