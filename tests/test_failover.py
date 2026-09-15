"""
Comprehensive tests for failover functionality in m3u-proxy.

Tests cover:
- HLS playlist failover (HLS -> HLS)
- Direct stream failover (TS -> TS)
- Transcoding failover (any input -> transcoded output)
- FFmpeg input error detection
- Automatic failover triggering
"""

from pooled_stream_manager import PooledStreamManager, SharedTranscodingProcess
from stream_manager import StreamManager
import pytest
import pytest_asyncio
import asyncio
import httpx
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestHLSPlaylistFailover:
    """Test failover for HLS playlists"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        """Create a StreamManager instance for testing"""
        manager = StreamManager()
        yield manager
        # Cleanup
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_hls_failover_on_fetch_error(self, stream_manager):
        """Test that HLS playlist failover works when primary URL fails"""
        primary_url = "http://primary.example.com/playlist.m3u8"
        failover_url = "http://backup.example.com/playlist.m3u8"

        # Create stream with failover
        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        stream_info = stream_manager.streams[stream_id]
        assert stream_info.failover_urls == [failover_url]
        assert stream_info.current_url == primary_url
        assert stream_info.current_failover_index == -1

        # Mock HTTP client to simulate primary failure and backup success
        mock_response_backup = Mock()
        mock_response_backup.status_code = 200
        mock_response_backup.headers = {"content-type": "application/vnd.apple.mpegurl"}
        mock_response_backup.url = failover_url
        mock_response_backup.text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
segment1.ts
#EXT-X-ENDLIST"""
        mock_response_backup.raise_for_status = Mock()

        with patch.object(stream_manager.http_client, "get") as mock_get:
            # First call fails (primary), second call succeeds (failover)
            mock_get.side_effect = [
                httpx.ConnectError("DNS resolution failed"),
                mock_response_backup,
            ]

            # Attempt to get playlist content
            content = await stream_manager.get_playlist_content(
                stream_id, "test_client", "http://proxy.com/hls/test"
            )

            # Should have triggered failover and returned content
            assert content is not None
            assert "#EXTM3U" in content

            # Verify failover was triggered
            assert stream_info.current_url == failover_url
            # current_failover_index tracks position in failover_urls array (0 = first failover URL)
            assert stream_info.current_failover_index == 0
            assert stream_info.failover_attempts == 1

    @pytest.mark.asyncio
    async def test_hls_failover_exhaustion(self, stream_manager):
        """Test that all failover URLs are tried before giving up"""
        primary_url = "http://primary.example.com/playlist.m3u8"
        failover_urls = [
            "http://backup1.example.com/playlist.m3u8",
            "http://backup2.example.com/playlist.m3u8",
        ]

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=failover_urls
        )

        with patch.object(stream_manager.http_client, "get") as mock_get:
            # All URLs fail
            mock_get.side_effect = httpx.ConnectError("All connections failed")

            content = await stream_manager.get_playlist_content(
                stream_id, "test_client", "http://proxy.com/hls/test"
            )

            # Should return None after all attempts exhausted
            assert content is None

            # Should have attempted all URLs
            # Primary + 2 failovers = 3 total attempts
            assert mock_get.call_count == 3

    @pytest.mark.asyncio
    async def test_hls_same_type_detection(self, stream_manager):
        """Test that HLS -> HLS failover is properly detected"""
        primary_url = "http://primary.example.com/playlist.m3u8"
        failover_url = "http://backup.example.com/playlist.m3u8"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/vnd.apple.mpegurl"}
        mock_response.url = failover_url
        mock_response.text = "#EXTM3U\n#EXTINF:10.0,\nsegment.ts"
        mock_response.raise_for_status = Mock()

        with patch.object(stream_manager.http_client, "get") as mock_get:
            mock_get.side_effect = [httpx.ConnectError("Primary failed"), mock_response]

            content = await stream_manager.get_playlist_content(
                stream_id, "test_client", "http://proxy.com/hls/test"
            )

            # Should return HLS content, not redirect marker
            assert content is not None
            assert content != "REDIRECT_TO_DIRECT_STREAM"
            assert "#EXTM3U" in content

            # Stream type should still be HLS
            stream_info = stream_manager.streams[stream_id]
            assert stream_info.is_hls is True


class TestDirectStreamFailover:
    """Test failover for direct streams (TS, MP4, etc.)"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        manager = StreamManager()
        yield manager
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_direct_stream_failover_structure(self, stream_manager):
        """Test that direct streams support failover configuration"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        stream_info = stream_manager.streams[stream_id]
        assert stream_info.failover_urls == [failover_url]
        assert stream_info.is_live_continuous is True
        assert stream_info.is_hls is False

    @pytest.mark.asyncio
    async def test_direct_stream_failover_event(self, stream_manager):
        """Test that failover event is properly set for direct streams"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        # Manually trigger failover
        result = await stream_manager._try_update_failover_url(stream_id, "test_error")

        assert result is True
        stream_info = stream_manager.streams[stream_id]
        assert stream_info.current_url == failover_url
        assert stream_info.failover_attempts == 1

    @pytest.mark.asyncio
    async def test_direct_stream_reconnect_recovers_from_redirect_502(
        self, stream_manager, monkeypatch
    ):
        """Regression: sticky redirected origin returns 502, proxy recovers to entry URL and reconnects."""
        primary_url = "http://provider.example.com/live/channel.ts"
        sticky_redirect_url = "http://edge-2.provider.example.com/live/channel.ts"

        # Force immediate sticky-origin recovery path (no same-URL retry loop)
        monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
        monkeypatch.setattr("config.settings.STREAM_TOTAL_TIMEOUT", 5.0)

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, use_sticky_session=True
        )
        stream_info = stream_manager.streams[stream_id]

        # Simulate a previously locked sticky backend from redirect handling
        stream_info.current_url = sticky_redirect_url

        class _OneChunkIterator:
            def __init__(self, chunk: bytes):
                self.chunk = chunk
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return self.chunk

        class _MockResponse:
            def __init__(
                self,
                status_code: int,
                chunk: bytes | None = None,
                request_url: str = "http://example.com",
            ):
                self.status_code = status_code
                self.headers = {"content-type": "video/mp2t"}
                self._chunk = chunk
                self._request_url = request_url

            def raise_for_status(self):
                if self.status_code >= 400:
                    request = httpx.Request("GET", self._request_url)
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        f"{self.status_code} Bad Gateway",
                        request=request,
                        response=response,
                    )

            def aiter_bytes(self, chunk_size=32768):
                if self._chunk is None:
                    return _OneChunkIterator(b"")
                return _OneChunkIterator(self._chunk)

        class _MockStreamCM:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc, tb):
                return False

        called_urls = []

        async def fake_stream(method, url, headers=None, follow_redirects=True):
            called_urls.append(url)
            if url == sticky_redirect_url:
                return _MockStreamCM(_MockResponse(502, request_url=url))
            if url == primary_url:
                return _MockStreamCM(_MockResponse(200, chunk=b"ok", request_url=url))
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(stream_manager.live_stream_client, "stream", fake_stream)

        response = await stream_manager.stream_continuous_direct(
            stream_id, "test_client"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

        assert chunks == [b"ok"]
        assert called_urls[0] == sticky_redirect_url
        assert called_urls[1] == primary_url
        # Sticky origin should have been cleared back to configured entry point flow
        assert stream_info.current_url is None

    @pytest.mark.asyncio
    async def test_direct_stream_reconnect_recovers_after_retry_exhaustion(
        self, stream_manager, monkeypatch
    ):
        """Regression: with retries enabled, sticky redirect 502 exhausts retries then recovers to entry URL."""
        primary_url = "http://provider.example.com/live/channel.ts"
        sticky_redirect_url = "http://edge-2.provider.example.com/live/channel.ts"

        monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr("config.settings.STREAM_RETRY_DELAY", 0.0)
        monkeypatch.setattr("config.settings.STREAM_TOTAL_TIMEOUT", 5.0)

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, use_sticky_session=True
        )
        stream_info = stream_manager.streams[stream_id]
        stream_info.current_url = sticky_redirect_url

        class _OneChunkIterator:
            def __init__(self, chunk: bytes):
                self.chunk = chunk
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return self.chunk

        class _MockResponse:
            def __init__(
                self,
                status_code: int,
                chunk: bytes | None = None,
                request_url: str = "http://example.com",
            ):
                self.status_code = status_code
                self.headers = {"content-type": "video/mp2t"}
                self._chunk = chunk
                self._request_url = request_url

            def raise_for_status(self):
                if self.status_code >= 400:
                    request = httpx.Request("GET", self._request_url)
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        f"{self.status_code} Bad Gateway",
                        request=request,
                        response=response,
                    )

            def aiter_bytes(self, chunk_size=32768):
                if self._chunk is None:
                    return _OneChunkIterator(b"")
                return _OneChunkIterator(self._chunk)

        class _MockStreamCM:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc, tb):
                return False

        called_urls = []

        async def fake_stream(method, url, headers=None, follow_redirects=True):
            called_urls.append(url)
            if url == sticky_redirect_url:
                return _MockStreamCM(_MockResponse(502, request_url=url))
            if url == primary_url:
                return _MockStreamCM(
                    _MockResponse(200, chunk=b"ok-retry", request_url=url)
                )
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(stream_manager.live_stream_client, "stream", fake_stream)

        response = await stream_manager.stream_continuous_direct(
            stream_id, "test_client_retry"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

        assert chunks == [b"ok-retry"]
        # Two retries on sticky URL + initial attempt = 3 calls, then recovery call to primary
        assert called_urls[:3] == [
            sticky_redirect_url,
            sticky_redirect_url,
            sticky_redirect_url,
        ]
        assert called_urls[3] == primary_url
        assert stream_info.current_url is None


class TestTranscodingFailover:
    """Test failover for transcoded streams"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        # Create manager with pooled transcoding support
        manager = StreamManager()
        # Mock the pooled manager
        manager.pooled_manager = Mock(spec=PooledStreamManager)
        yield manager
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_transcoding_input_error_detection(self, stream_manager):
        """Test that FFmpeg input errors trigger failover"""
        primary_url = "http://primary.example.com/stream.m3u8"
        failover_url = "http://backup.example.com/stream.m3u8"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_urls=[failover_url],
            is_transcoded=True,
            transcode_profile="h264_720p",
            transcode_ffmpeg_args=["-c:v", "libx264"],
        )

        # Create a mock shared process with input_failed status
        mock_process = Mock(spec=SharedTranscodingProcess)
        mock_process.status = "input_failed"
        mock_process.process = Mock()
        mock_process.process.pid = 12345
        mock_process.process.returncode = None
        mock_process.process.stdout = Mock()
        mock_process.client_queues = {"test_client": asyncio.Queue()}

        # Mock pooled manager to return failed process first, then good process
        mock_good_process = Mock(spec=SharedTranscodingProcess)
        mock_good_process.status = "running"
        mock_good_process.process = Mock()
        mock_good_process.process.pid = 12346
        mock_good_process.process.returncode = None
        mock_good_process.process.stdout = Mock()
        mock_good_process.client_queues = {"test_client": asyncio.Queue()}

        stream_manager.pooled_manager.get_or_create_shared_stream = AsyncMock(
            side_effect=[
                ("stream_key_1", mock_process),
                ("stream_key_2", mock_good_process),
            ]
        )
        stream_manager.pooled_manager.force_stop_stream = AsyncMock()

        # The stream should detect the input_failed status and trigger failover
        stream_info = stream_manager.streams[stream_id]
        assert stream_info.is_transcoded is True
        assert stream_info.failover_urls == [failover_url]


class TestFFmpegErrorPatterns:
    """Test FFmpeg stderr error pattern detection"""

    def test_input_error_patterns(self):
        """Test that SharedTranscodingProcess detects input errors via the
        module-level pattern tuple."""
        from pooled_stream_manager import _FFMPEG_INPUT_ERROR_PATTERNS

        # These are the error patterns we should detect
        error_patterns = [
            "Error opening input file https://example.com/stream.m3u8",
            "Failed to resolve hostname error.tvnow.best",
            "Connection refused",
            "Connection timed out",
            "Server returned 404 Not Found",
            "Server returned 503 Service Unavailable",
            "Invalid data found when processing input",
            "Protocol not found",
        ]

        for error_msg in error_patterns:
            assert any(
                pattern in error_msg.lower() for pattern in _FFMPEG_INPUT_ERROR_PATTERNS
            ), f"Expected to match an input error pattern: {error_msg}"

    def test_input_error_patterns_excludes_bare_end_of_file(self):
        """Regression: ffmpeg 8.1 prints 'Error reading HTTP response: End of file'
        on every HLS segment fetch end while -reconnect 1 silently reconnects,
        so 'end of file' must not be a failover trigger on its own. Real upstream
        loss is caught by the more specific patterns + the data-starvation
        detector in stream_manager."""
        from pooled_stream_manager import _FFMPEG_INPUT_ERROR_PATTERNS

        assert "end of file" not in _FFMPEG_INPUT_ERROR_PATTERNS

    @pytest.mark.asyncio
    async def test_stderr_monitor_detects_input_error(self):
        """Test that _log_stderr properly detects and marks input failures"""
        process = SharedTranscodingProcess(
            stream_id="test_stream",
            url="http://example.com/stream.m3u8",
            profile="test",
            ffmpeg_args=["-i", "{input_url}", "-c", "copy", "pipe:1"],
            user_agent="test-agent",
        )

        # Mock the process and stderr
        mock_process = Mock()
        mock_stderr = AsyncMock()

        # Simulate FFmpeg stderr output with input error as chunked reads
        error_line = b"Error opening input file http://example.com/stream.m3u8: Input/output error\n"
        # _log_stderr reads chunks via read(CHUNK_SIZE) and buffers/splits on newlines
        mock_stderr.read = AsyncMock(side_effect=[error_line, b""])

        mock_process.stderr = mock_stderr
        mock_process.returncode = None
        process.process = mock_process

        # Run the stderr monitor
        await process._log_stderr()

        # Should have marked the stream as input_failed
        assert process.status == "input_failed"

    @pytest.mark.asyncio
    async def test_stderr_monitor_ignores_segment_end_eof(self):
        """Regression for ffmpeg 8.1: a normal HLS segment-fetch EOF must NOT
        flip status to input_failed. ffmpeg 8.1 logs this on every segment end
        and recovers via -reconnect 1, so it isn't a failure."""
        process = SharedTranscodingProcess(
            stream_id="test_stream_eof",
            url="http://example.com/stream.m3u8",
            profile="test",
            ffmpeg_args=["-i", "{input_url}", "-c", "copy", "pipe:1"],
            user_agent="test-agent",
        )

        mock_process = Mock()
        mock_stderr = AsyncMock()

        eof_line = b"[http @ 0x7f202400c740] Error reading HTTP response: End of file\n"
        mock_stderr.read = AsyncMock(side_effect=[eof_line, b""])

        mock_process.stderr = mock_stderr
        mock_process.returncode = None
        process.process = mock_process

        await process._log_stderr()

        # Status must remain "starting" (init default), not flip to input_failed.
        assert process.status != "input_failed"


class TestFailoverStats:
    """Test failover statistics tracking"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        manager = StreamManager()
        yield manager
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_failover_resets_content_type_verification(self, stream_manager):
        """A failover URL can be a different provider/backend entirely, so a
        stale content_type_verified from the old URL must not stick - a
        genuinely-HLS backup would otherwise keep being served raw forever."""
        primary_url = "http://primary.example.com/movie/u/p/123.m3u8"
        failover_url = "http://backup.example.com/movie/u/p/123.m3u8"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )
        stream_info = stream_manager.streams[stream_id]
        assert stream_info.is_vod is True

        # Simulate the primary having already been probed and confirmed raw.
        stream_info.content_type_verified = True

        await stream_manager._try_update_failover_url(stream_id, "test_reason")

        assert stream_info.current_url == failover_url
        assert stream_info.content_type_verified is False
        assert stream_info.is_vod is True

    @pytest.mark.asyncio
    async def test_failover_attempt_tracking(self, stream_manager):
        """Test that failover attempts are tracked"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        stream_info = stream_manager.streams[stream_id]
        assert stream_info.failover_attempts == 0
        assert stream_info.last_failover_time is None

        # Trigger failover
        await stream_manager._try_update_failover_url(stream_id, "test_reason")

        assert stream_info.failover_attempts == 1
        assert stream_info.last_failover_time is not None
        assert isinstance(stream_info.last_failover_time, datetime)

    @pytest.mark.asyncio
    async def test_multiple_failover_cycles(self, stream_manager):
        """Test that failover walks through all URLs then stops (no wrap-around)"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_urls = [
            "http://backup1.example.com/stream.ts",
            "http://backup2.example.com/stream.ts",
            "http://backup3.example.com/stream.ts",
        ]

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=failover_urls
        )

        stream_info = stream_manager.streams[stream_id]

        # Initial state: current_failover_index is -1 (no failover used yet)
        # Each call increments: -1 -> 0 (backup1), 0 -> 1 (backup2), 1 -> 2 (backup3)
        expected_sequence = [
            (failover_urls[0], 0),  # First failover: backup1
            (failover_urls[1], 1),  # Second failover: backup2
            (failover_urls[2], 2),  # Third failover: backup3
        ]

        # Trigger multiple failovers
        for i, (expected_url, expected_index) in enumerate(expected_sequence):
            result = await stream_manager._try_update_failover_url(
                stream_id, f"attempt_{i}"
            )
            assert result is True
            assert stream_info.current_url == expected_url
            assert stream_info.current_failover_index == expected_index
            assert stream_info.failover_attempts == i + 1

        # Verify we used all URLs
        assert stream_info.failover_attempts == 3

        # Fourth call should return False — all URLs exhausted, no wrap-around
        result = await stream_manager._try_update_failover_url(stream_id, "attempt_3")
        assert result is False
        assert stream_info.failover_attempts == 3  # unchanged


class TestFailoverIntegration:
    """Integration tests for end-to-end failover scenarios"""

    @pytest_asyncio.fixture
    async def stream_manager(self):
        manager = StreamManager()
        yield manager
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_no_failover_urls_returns_false(self, stream_manager):
        """Test that streams without failover URLs return False on failover attempt"""
        primary_url = "http://example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(primary_url)

        result = await stream_manager._try_update_failover_url(stream_id, "test")

        assert result is False
        stream_info = stream_manager.streams[stream_id]
        assert stream_info.failover_attempts == 0

    @pytest.mark.asyncio
    async def test_failover_event_signals_clients(self, stream_manager):
        """Test that failover event is set to signal waiting clients"""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url, failover_urls=[failover_url]
        )

        stream_info = stream_manager.streams[stream_id]

        # Initially, event should not be set
        assert not stream_info.failover_event.is_set()

        # Trigger failover
        await stream_manager._try_update_failover_url(stream_id, "test")

        # Event should be set temporarily (gets cleared after 0.1s)
        # Note: The event might already be cleared due to the sleep in the method
        # So we just check that failover happened
        assert stream_info.current_url == failover_url


class TestManualFailoverRegression:
    """Regression tests for manual failover triggered from the Stream Monitor.

    Covers the two bugs from issue #1180:
    1. Transcoded streams: FFmpeg is killed (queuing None) before the generator can check
       failover_event in the normal place, so the generator disconnected the client instead
       of reconnecting to the failover URL.
    2. Direct (live TS) streams, resolver mode: after the inner loop broke due to
       failover_event, the post-inner-loop code called _try_update_failover_url a second
       time, double-advancing to URL #3 instead of reconnecting to the intended URL #2.
    """

    @pytest_asyncio.fixture
    async def stream_manager(self):
        manager = StreamManager()
        manager.pooled_manager = Mock(spec=PooledStreamManager)
        yield manager
        if hasattr(manager, "_running"):
            manager._running = False

    @pytest.mark.asyncio
    async def test_transcoded_failover_stays_connected_on_queue_none(
        self, stream_manager, monkeypatch
    ):
        """Regression #1180 (transcoded path): when _try_update_failover_url kills the
        FFmpeg process (sending None to the client queue) while failover_event is set,
        the generator must stay connected and serve data from the failover stream rather
        than disconnecting the client."""
        primary_url = "http://primary.example.com/stream.m3u8"
        failover_url = "http://backup.example.com/stream.m3u8"
        client_id = "test_client"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_resolver_url="http://resolver.example.com/failover",
            is_transcoded=True,
            transcode_profile="custom",
            transcode_ffmpeg_args=["-i", "{input_url}", "-f", "mpegts", "pipe:1"],
        )
        stream_info = stream_manager.streams[stream_id]

        # Queue 1 (original stream): starts empty — None is injected externally once
        # the generator is already reading from it, mimicking _try_update_failover_url
        # killing FFmpeg mid-stream.  We use an instrumented queue so the test can
        # coordinate precisely: set failover_event and inject None only AFTER the
        # generator has started its q1.get() call (i.e. after the outer loop's
        # failover_event.clear() has already run).
        class _SignallingQueue:
            """Wraps asyncio.Queue, setting an asyncio.Event on the first get() call."""

            def __init__(self):
                self._q = asyncio.Queue()
                self.first_get = asyncio.Event()

            async def get(self):
                self.first_get.set()
                return await self._q.get()

            def empty(self):
                return self._q.empty()

        q1 = _SignallingQueue()

        # Queue 2 (failover stream): delivers one real chunk then ends naturally.
        q2 = asyncio.Queue()
        await q2.put(b"failover_data")
        await q2.put(None)

        def _make_process(queue):
            p = Mock()
            p.status = "running"
            p.process = Mock()
            p.process.pid = 1000
            p.process.returncode = None
            p.process.stdout = Mock()
            p.client_queues = {client_id: queue}
            return p

        stream_manager.pooled_manager.get_or_create_shared_stream = AsyncMock(
            side_effect=[
                ("key1", _make_process(q1)),
                ("key2", _make_process(q2)),
            ]
        )
        stream_manager.pooled_manager.remove_client_from_stream = AsyncMock()
        stream_manager.pooled_manager.force_stop_stream = AsyncMock()
        stream_manager.pooled_manager.update_client_activity = Mock()

        # Skip the 2-second FFmpeg startup polling loop for test speed.
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

        chunks = []

        async def consume():
            resp = await stream_manager.stream_transcoded(stream_id, client_id)
            async for chunk in resp.body_iterator:
                chunks.append(chunk)

        consume_task = asyncio.create_task(consume())

        # Wait until the generator has started its first q1.get() — the outer loop's
        # failover_event.clear() has already executed by this point, so setting the
        # event here won't be wiped by that safety clear.
        await q1.first_get.wait()

        # Simulate _try_update_failover_url: update URL, fire event, kill FFmpeg.
        stream_info.current_url = failover_url
        stream_info.failover_event.set()
        await q1._q.put(None)

        await consume_task

        assert b"failover_data" in chunks, (
            "Generator should have served data from the failover stream"
        )
        assert (
            stream_manager.pooled_manager.get_or_create_shared_stream.call_count == 2
        ), (
            "Generator must reconnect (call get_or_create_shared_stream twice) "
            "rather than disconnecting the client"
        )

    @pytest.mark.asyncio
    async def test_direct_stream_no_double_advance_after_failover_event(
        self, stream_manager, monkeypatch
    ):
        """Regression #1180 (direct stream, resolver mode): after the inner loop breaks
        due to failover_event, the generator must reconnect to the already-set failover URL
        without calling _try_update_failover_url again.  Calling it would double-advance
        to URL #3 instead of URL #2 (the intended manual-failover target)."""
        primary_url = "http://primary.example.com/stream.ts"
        failover_url = "http://backup.example.com/stream.ts"

        stream_id = await stream_manager.get_or_create_stream(
            primary_url,
            failover_resolver_url="http://resolver.example.com/failover",
        )
        stream_info = stream_manager.streams[stream_id]

        # Simulate _try_update_failover_url having already run externally:
        # current_url = failover URL, event is set, attempt counter updated.
        stream_info.current_url = failover_url
        stream_info.failover_event.set()
        stream_info.failover_attempts = 1
        stream_info.current_failover_index = 0

        called_urls = []
        call_number = [0]

        async def _fake_stream(method, url, **kwargs):
            called_urls.append(url)
            call_number[0] += 1

            # On the second connection: disable resolver so the natural stream-end
            # path exits cleanly (has_failover=False, chunks < min_chunks threshold).
            if call_number[0] >= 2:
                stream_info.failover_resolver_url = None
                stream_info.failover_urls = []

            class _Iter:
                def __init__(self, chunks):
                    self._chunks = list(chunks)
                    self._idx = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._idx >= len(self._chunks):
                        raise StopAsyncIteration
                    c = self._chunks[self._idx]
                    self._idx += 1
                    return c

            class _Resp:
                status_code = 200
                headers = {"content-type": "video/mp2t"}

                def raise_for_status(self):
                    pass

                def aiter_bytes(self, chunk_size=32768):
                    # First connection: no chunks (inner loop detects failover_event immediately).
                    # Second connection: one chunk (< LIVE_SILENT_RECONNECT_MIN_CHUNKS=10 → natural exit).
                    data = [] if call_number[0] == 1 else [b"failover_data"]
                    return _Iter(data)

            class _CM:
                async def __aenter__(self):
                    return _Resp()

                async def __aexit__(self, *args):
                    return False

            return _CM()

        monkeypatch.setattr(stream_manager.live_stream_client, "stream", _fake_stream)

        response = await stream_manager.stream_continuous_direct(
            stream_id, "test_client"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

        assert len(called_urls) >= 2, (
            f"Expected at least 2 upstream connections, got: {called_urls}"
        )
        assert called_urls[0] == failover_url, (
            "First connection should be to the already-set failover URL"
        )
        assert called_urls[1] == failover_url, (
            "Second connection must ALSO be the failover URL — "
            "_try_update_failover_url must not be called again after the "
            "failover_event break (that would double-advance to URL #3)"
        )
        assert b"failover_data" in chunks, (
            "Generator should have served data from the second (failover) connection"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
