"""
Tests for the silent reconnect feature.

Covers providers that periodically close the connection — either cleanly
(StopAsyncIteration / HTTP EOF) or abruptly (httpx.ReadError / TCP RST).
In both cases the proxy must keep the downstream client connected and
seamlessly reopen the upstream connection.
"""

import pytest
import httpx

from stream_manager import StreamManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _MockStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_chunks_then_stop(chunk_data: bytes, count: int):
    """Return an async iterator that yields `count` chunks then stops."""

    class _Iter:
        def __init__(self):
            self._sent = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent >= count:
                raise StopAsyncIteration
            self._sent += 1
            return chunk_data

    return _Iter()


def _make_chunks_then_read_error(chunk_data: bytes, count: int):
    """Return an async iterator that yields `count` chunks then raises ReadError."""

    class _Iter:
        def __init__(self):
            self._sent = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent >= count:
                raise httpx.ReadError("Connection reset by peer")
            self._sent += 1
            return chunk_data

    return _Iter()


def _response_from_iter(iterator):
    """Wrap an async iterator in a minimal mock response."""

    class _Resp:
        status_code = 200
        headers = {"content-type": "video/mp2t"}

        def raise_for_status(self):
            pass

        def aiter_bytes(self, chunk_size=32768):
            return iterator

    return _Resp()


def _make_manager():
    return StreamManager()


async def _collect(response) -> list[bytes]:
    """Drain a StreamingResponse body_iterator into a list."""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Scenario 1: provider closes cleanly (StopAsyncIteration) after N chunks,
#             then reconnects and delivers more — client stays connected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_reconnect_on_clean_provider_close(monkeypatch):
    """
    Provider closes the connection cleanly (StopAsyncIteration) after a burst
    of chunks, as some providers do every 10-15 seconds.  The proxy must
    silently reconnect and keep the client's HTTP response open.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/channel.ts"
    stream_id = await manager.get_or_create_stream(url)

    chunk_data = b"X" * 32768
    # Two separate connections: first yields 10 chunks, second yields 5 chunks.
    responses = [
        _response_from_iter(_make_chunks_then_stop(chunk_data, 10)),
        _response_from_iter(_make_chunks_then_stop(chunk_data, 5)),
    ]
    call_count = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return _MockStreamCM(resp)

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    # Should receive chunks from both connections (10 + 5 = 15 total)
    assert len(chunks) == 15, (
        f"Expected 15 chunks across two connections, got {len(chunks)}"
    )
    assert call_count >= 2, "Expected at least two upstream connections (one reconnect)"
    assert all(c == chunk_data for c in chunks)


@pytest.mark.asyncio
async def test_silent_reconnect_clean_close_below_min_chunks_ends_stream(monkeypatch):
    """
    Provider closes after fewer chunks than LIVE_SILENT_RECONNECT_MIN_CHUNKS.
    This should be treated as natural stream completion, not a reconnect.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 10)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/channel.ts"
    stream_id = await manager.get_or_create_stream(url)

    chunk_data = b"Y" * 32768
    call_count = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal call_count
        call_count += 1
        # Only 3 chunks — below the min_chunks threshold
        return _MockStreamCM(_response_from_iter(_make_chunks_then_stop(chunk_data, 3)))

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    assert len(chunks) == 3, (
        f"Expected 3 chunks (natural completion), got {len(chunks)}"
    )
    assert call_count == 1, "Expected no reconnect (below min_chunks threshold)"


# ---------------------------------------------------------------------------
# Scenario 2: provider drops connection with httpx.ReadError (TCP RST) after
#             N chunks — same reconnect behaviour as the clean-close case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_reconnect_on_upstream_read_error(monkeypatch):
    """
    Provider drops the TCP connection mid-stream (httpx.ReadError) after
    delivering enough chunks.  The proxy must silently reconnect rather than
    treating it as a client disconnect and ending the recording.

    Regression test for the bug where the ReadError handler broke out of the
    outer failover loop, bypassing the silent-reconnect check entirely and
    causing Channels DVR recordings to stop after ~14-15 seconds.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/channel.ts"
    stream_id = await manager.get_or_create_stream(url)

    chunk_data = b"Z" * 32768
    # First connection: 10 chunks then ReadError (TCP RST).
    # Second connection: 5 more chunks then clean close.
    responses = [
        _response_from_iter(_make_chunks_then_read_error(chunk_data, 10)),
        _response_from_iter(_make_chunks_then_stop(chunk_data, 5)),
    ]
    call_count = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return _MockStreamCM(resp)

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    events = []

    async def fake_emit(event_type, stream_id_arg, data):
        events.append(event_type)

    manager._emit_event = fake_emit

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    # Should receive all 15 chunks (10 before error + 5 on reconnect)
    assert len(chunks) == 15, (
        f"Expected 15 chunks (silent reconnect on ReadError), got {len(chunks)}. "
        f"Events: {events}"
    )
    assert call_count >= 2, "Expected at least two upstream connections (one reconnect)"
    assert all(c == chunk_data for c in chunks)

    # CLIENT_DISCONNECTED must not appear BEFORE stream completion.
    # (It IS legitimately emitted by cleanup_client at end-of-stream, so we
    # only care that it didn't fire mid-stream due to the upstream ReadError
    # being misidentified as a client disconnect.)
    first_stream_stopped = next(
        (i for i, e in enumerate(events) if e == "STREAM_STOPPED"), len(events)
    )
    early_disconnects = [
        e for e in events[:first_stream_stopped] if e == "CLIENT_DISCONNECTED"
    ]
    assert early_disconnects == [], (
        f"Proxy incorrectly emitted CLIENT_DISCONNECTED before stream stopped. "
        f"Events: {events}"
    )


@pytest.mark.asyncio
async def test_silent_reconnect_read_error_below_min_chunks_ends_stream(monkeypatch):
    """
    Provider drops the TCP connection after too few chunks (below
    LIVE_SILENT_RECONNECT_MIN_CHUNKS).  The proxy should treat this as a
    genuine stream failure rather than attempting an infinite reconnect loop.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 10)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    url = "http://provider.example.com/live/channel.ts"
    stream_id = await manager.get_or_create_stream(url)

    chunk_data = b"A" * 32768
    call_count = 0

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        nonlocal call_count
        call_count += 1
        # Only 3 chunks before ReadError — below min_chunks threshold
        return _MockStreamCM(
            _response_from_iter(_make_chunks_then_read_error(chunk_data, 3))
        )

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    assert len(chunks) == 3, (
        f"Expected 3 chunks then stream end (below min_chunks), got {len(chunks)}"
    )
    assert call_count == 1, "Expected no reconnect attempt (below min_chunks threshold)"


# ---------------------------------------------------------------------------
# Scenario 3: failovers configured — a healthy session that the provider
#             closes cleanly is retried on the same URL before failing over.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_close_with_failover_retries_same_url_first(monkeypatch):
    """
    The provider drops a healthy session but the channel is fine. The proxy
    must reconnect to the same URL instead of switching to the failover feed,
    which would splice a different stream into the client's connection.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    primary_url = "http://provider.example.com/live/primary.ts"
    failover_url = "http://provider.example.com/live/failover.ts"
    stream_id = await manager.get_or_create_stream(
        primary_url, failover_urls=[failover_url]
    )

    chunk_data = b"P" * 32768
    requested_urls = []
    responses = [
        _response_from_iter(_make_chunks_then_stop(chunk_data, 10)),
        _response_from_iter(_make_chunks_then_stop(chunk_data, 3)),
    ]

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        requested_urls.append(url)
        if len(requested_urls) > len(responses):
            # Anything after the second connection is the failover; end it.
            return _MockStreamCM(
                _response_from_iter(_make_chunks_then_stop(chunk_data, 0))
            )
        return _MockStreamCM(responses[len(requested_urls) - 1])

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    assert requested_urls[:2] == [primary_url, primary_url], (
        f"Expected a same-URL retry before failover, got {requested_urls}"
    )
    assert len(chunks) >= 13


@pytest.mark.asyncio
async def test_clean_close_with_failover_fails_over_when_retry_dies_quickly(
    monkeypatch,
):
    """
    The same-URL retry delivers almost nothing, so the channel really is down:
    the proxy must move on to the failover URL rather than retrying again.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 1)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    primary_url = "http://provider.example.com/live/primary.ts"
    failover_url = "http://provider.example.com/live/failover.ts"
    stream_id = await manager.get_or_create_stream(
        primary_url, failover_urls=[failover_url]
    )

    chunk_data = b"Q" * 32768
    requested_urls = []

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        requested_urls.append(url)
        if url == failover_url:
            return _MockStreamCM(
                _response_from_iter(_make_chunks_then_stop(chunk_data, 4))
            )
        count = 10 if len(requested_urls) == 1 else 0
        return _MockStreamCM(
            _response_from_iter(_make_chunks_then_stop(chunk_data, count))
        )

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    chunks = await _collect(response)

    assert requested_urls[:3] == [primary_url, primary_url, failover_url], (
        f"Expected primary, one same-URL retry, then failover; got {requested_urls}"
    )
    assert len(chunks) == 14


@pytest.mark.asyncio
async def test_clean_close_with_failover_and_retry_disabled_fails_over_immediately(
    monkeypatch,
):
    """LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS = 0 keeps the old behaviour."""
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 1)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    primary_url = "http://provider.example.com/live/primary.ts"
    failover_url = "http://provider.example.com/live/failover.ts"
    stream_id = await manager.get_or_create_stream(
        primary_url, failover_urls=[failover_url]
    )

    chunk_data = b"R" * 32768
    requested_urls = []

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        requested_urls.append(url)
        count = 10 if url == primary_url else 4
        return _MockStreamCM(
            _response_from_iter(_make_chunks_then_stop(chunk_data, count))
        )

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    await _collect(response)

    assert requested_urls[:2] == [primary_url, failover_url], (
        f"Expected immediate failover with the retry disabled, got {requested_urls}"
    )


@pytest.mark.asyncio
async def test_per_stream_retry_window_overrides_env_default(monkeypatch):
    """A stream created with silent_close_retry_window=0 fails over immediately."""
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 1)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    primary_url = "http://provider.example.com/live/primary.ts"
    failover_url = "http://provider.example.com/live/failover.ts"
    stream_id = await manager.get_or_create_stream(
        primary_url, failover_urls=[failover_url], silent_close_retry_window=0
    )
    assert manager.streams[stream_id].silent_close_retry_window == 0

    chunk_data = b"S" * 32768
    requested_urls = []

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        requested_urls.append(url)
        count = 10 if url == primary_url else 4
        return _MockStreamCM(
            _response_from_iter(_make_chunks_then_stop(chunk_data, count))
        )

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    await _collect(response)

    assert requested_urls[:2] == [primary_url, failover_url], (
        f"Expected the per-stream 0 to disable the same-URL retry, got {requested_urls}"
    )


def test_same_url_retry_is_off_by_default():
    """The same-URL retry is opt-in: without an override, silent closes fail over as before."""
    from config import Settings

    assert Settings.model_fields["LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS"].default == 0


@pytest.mark.asyncio
async def test_failover_url_gets_its_own_same_url_retry(monkeypatch):
    """
    The retry window is per URL: once the primary's retry dies and the proxy fails
    over, a healthy failover feed that is then dropped is retried on its own URL
    instead of skipping straight to the next failover.
    """
    manager = _make_manager()

    monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr("config.settings.LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr("config.settings.LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS", 60.0)
    monkeypatch.setattr("config.settings.MAX_FAILOVER_ATTEMPTS", 10)
    monkeypatch.setattr("config.settings.LIVE_CHUNK_TIMEOUT_SECONDS", 1.0)

    primary_url = "http://provider.example.com/live/primary.ts"
    failover_url = "http://provider.example.com/live/failover.ts"
    second_failover_url = "http://provider.example.com/live/failover2.ts"
    stream_id = await manager.get_or_create_stream(
        primary_url, failover_urls=[failover_url, second_failover_url]
    )

    chunk_data = b"T" * 32768
    requested_urls = []
    # Primary plays then drops, its retry dies, the failover plays then drops.
    plan = [10, 0, 10]

    async def fake_stream(method, url, headers=None, follow_redirects=True):
        requested_urls.append(url)
        index = len(requested_urls) - 1
        count = plan[index] if index < len(plan) else 0
        return _MockStreamCM(
            _response_from_iter(_make_chunks_then_stop(chunk_data, count))
        )

    monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

    response = await manager.stream_continuous_direct(stream_id, "test_client")
    await _collect(response)

    assert requested_urls[:4] == [
        primary_url,
        primary_url,
        failover_url,
        failover_url,
    ], f"Expected the failover feed to get its own same-URL retry, got {requested_urls}"
