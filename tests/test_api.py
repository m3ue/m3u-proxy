# Add src to path first
from stream_manager import StreamManager
from api import app, get_content_type, is_direct_stream
from config import settings
import httpx
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from unittest.mock import Mock, AsyncMock, patch
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestHelperFunctions:
    """Test utility functions"""

    def test_get_content_type(self):
        assert get_content_type("test.ts") == "video/mp2t"
        assert get_content_type("test?profile=pass") == "video/mp2t"
        assert get_content_type("playlist.m3u8") == "application/vnd.apple.mpegurl"
        assert get_content_type("video.mp4") == "video/mp4"
        assert get_content_type("video.mkv") == "video/x-matroska"
        assert get_content_type("video.webm") == "video/webm"
        assert get_content_type("video.avi") == "video/x-msvideo"
        assert get_content_type("unknown.xyz") == "application/octet-stream"

    def test_is_direct_stream(self):
        assert is_direct_stream("stream.ts") is True
        assert is_direct_stream("video.mp4") is True
        assert is_direct_stream("video.mkv") is True
        assert is_direct_stream("video.webm") is True
        assert is_direct_stream("video.avi") is True
        assert is_direct_stream("playlist.m3u8") is False
        assert is_direct_stream("unknown.xyz") is False

    def test_is_direct_stream_vod_path_m3u8(self):
        """Provider movie/series URLs ending in .m3u8 aren't reliably real
        HLS - route them like other VOD content and let the runtime probe
        (resolve_vod_content_type) redirect to /hls/ if it turns out to be."""
        assert is_direct_stream("http://p.example.com/movie/u/p/123.m3u8") is True
        assert is_direct_stream("http://p.example.com/series/u/p/123.m3u8") is True
        assert is_direct_stream("http://p.example.com/timeshift/u/p/1/2/x.m3u8") is True
        # Live .m3u8 URLs are unaffected - still routed to the HLS endpoint.
        assert is_direct_stream("http://p.example.com/live/u/p/123.m3u8") is False
        assert is_direct_stream("http://p.example.com/hls/stream.m3u8") is False
        # A live URL that genuinely contains the /movie/ or /series/ path
        # segment (e.g. an EPG category) must agree with
        # StreamManager._detect_stream_type()'s /live/ precedence, or the two
        # classifiers route the same URL inconsistently across entry points.
        assert is_direct_stream("http://p.example.com/live/movie/u/p/1.m3u8") is False
        assert is_direct_stream("http://p.example.com/live/series/u/p/1.m3u8") is False

    def test_is_direct_stream_vod_path_extensionless(self):
        """The movie/series/timeshift carve-out must apply regardless of
        extension, matching StreamManager._detect_stream_type() exactly - an
        extensionless /movie/12345 URL is flagged VOD internally regardless
        of extension, so is_direct_stream() must agree here too."""
        assert is_direct_stream("http://p.example.com/movie/u/p/12345") is True
        assert is_direct_stream("http://p.example.com/series/u/p/12345") is True
        assert (
            is_direct_stream("http://p.example.com/timeshift/u/p/1/2/12345") is True
        )


class TestAPI:
    """Test FastAPI endpoints"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def mock_stream_manager(self):
        with patch("api.stream_manager") as mock:
            # Mock the streams dict to include test_stream_123
            mock.streams = {
                "test_stream_123": Mock(
                    is_vod=False, is_hls=False, content_type_verified=True
                )
            }

            mock.get_or_create_stream = AsyncMock(return_value="test_stream_123")
            mock.resolve_vod_content_type = AsyncMock(return_value=None)
            mock.get_stream_info = Mock(
                return_value=Mock(
                    stream_id="test_stream_123",
                    original_url="http://example.com/test.m3u8",
                    is_active=True,
                    client_count=1,
                    error_count=0,
                )
            )
            mock.get_stats = Mock(
                return_value={
                    "proxy_stats": {
                        "total_streams": 1,
                        "active_streams": 1,
                        "total_clients": 0,
                        "active_clients": 0,
                        "total_bytes_served": 0,
                        "total_segments_served": 0,
                        "uptime_seconds": 3600,
                    },
                    "streams": [
                        {
                            "stream_id": "test_stream_123",
                            "original_url": "http://example.com/test.m3u8",
                            "is_active": True,
                            "client_count": 1,
                            "error_count": 0,
                            "uptime": 3600,
                        }
                    ],
                    "clients": [],
                }
            )
            mock.get_all_streams = Mock(return_value=[])
            mock.get_all_clients = Mock(return_value=[])
            yield mock

    def test_root_endpoint(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "version" in data
        assert "uptime" in data

    def test_health_endpoint(self, client, mock_stream_manager):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "stats" in data

    def test_create_stream_post_valid(self, client, mock_stream_manager):
        payload = {
            "url": "http://example.com/stream.m3u8",
            "failover_urls": ["http://backup.com/stream.m3u8"],
            "user_agent": "TestApp/1.0",
        }

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["stream_id"] == "test_stream_123"
        assert "playlist_url" in data
        assert "direct_url" in data

    def test_create_stream_post_minimal(self, client, mock_stream_manager):
        payload = {"url": "http://example.com/stream.m3u8"}

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["stream_id"] == "test_stream_123"

    def test_create_stream_with_custom_headers(self, client, mock_stream_manager):
        payload = {
            "url": "http://example.com/stream.m3u8",
            "headers": {
                "X-Custom-Header": "TestValue",
                "Authorization": "Bearer token",
            },
        }

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        # Verify that the stream manager's get_or_create_stream was called with the headers
        mock_stream_manager.get_or_create_stream.assert_called_once()
        call_args = mock_stream_manager.get_or_create_stream.call_args
        assert call_args.kwargs["headers"] == payload["headers"]

    def test_create_stream_post_invalid_url(self, client):
        payload = {"url": "not_a_valid_url"}

        response = client.post("/streams", json=payload)
        assert response.status_code == 422  # Validation error

    def test_get_streams(self, client, mock_stream_manager):
        response = client.get("/streams")
        assert response.status_code == 200
        data = response.json()
        assert "streams" in data
        assert isinstance(data["streams"], list)

    def test_get_stream_info_exists(self, client, mock_stream_manager):
        response = client.get("/streams/test_stream_123")
        assert response.status_code == 200

        data = response.json()
        assert "stream" in data
        assert "clients" in data
        assert "client_count" in data
        assert data["stream"]["stream_id"] == "test_stream_123"
        assert data["stream"]["original_url"] == "http://example.com/test.m3u8"

    def test_get_stream_info_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/streams/nonexistent")
        assert response.status_code == 404

        data = response.json()
        assert "not found" in data["detail"].lower()

    def test_delete_stream_exists(self, client, mock_stream_manager):
        mock_stream_manager.cleanup_client = AsyncMock()
        mock_stream_manager._emit_event = AsyncMock()
        mock_stream_manager.stream_clients = {"test_stream_123": {"client1"}}
        mock_stream_manager.streams = {"test_stream_123": Mock(is_transcoded=False)}

        response = client.delete("/streams/test_stream_123")
        assert response.status_code == 200

        data = response.json()
        assert "deleted" in data["message"].lower()

    def test_delete_stream_not_found(self, client, mock_stream_manager):
        mock_stream_manager.streams = {}
        response = client.delete("/streams/nonexistent")
        assert response.status_code == 404

    def test_get_clients(self, client, mock_stream_manager):
        response = client.get("/clients")
        assert response.status_code == 200

        data = response.json()
        assert "clients" in data
        assert isinstance(data["clients"], list)

    def test_playlist_endpoint(self, client, mock_stream_manager):
        # Mock the get_playlist_content method used by the endpoint
        mock_stream_manager.get_playlist_content = AsyncMock(
            return_value="#EXTM3U\nsegment1.ts"
        )
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())
        mock_stream_manager.clients = {}

        response = client.get("/hls/test_stream_123/playlist.m3u8")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert "#EXTM3U" in response.text

    def test_playlist_endpoint_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/playlist/nonexistent")
        assert response.status_code == 404

    def test_proxy_endpoint_segment(self, client, mock_stream_manager):
        # Mock the proxy_hls_segment method used by the endpoint
        from starlette.responses import StreamingResponse

        async def mock_response_generator():
            yield b"segment_data_chunk_1"
            yield b"segment_data_chunk_2"

        mock_response = StreamingResponse(
            mock_response_generator(), media_type="video/mp2t"
        )
        mock_stream_manager.proxy_hls_segment = AsyncMock(return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())

        response = client.get(
            "/hls/test_stream_123/segment?client_id=test_client&url=http://example.com/segment1.ts"
        )
        assert response.status_code == 200
        # Note: In tests, the media_type might not be set exactly as expected

    def test_proxy_endpoint_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/proxy/nonexistent/segment.ts")
        assert response.status_code == 404

    def test_direct_stream_endpoint(self, client, mock_stream_manager):
        from starlette.responses import StreamingResponse

        async def mock_stream_generator():
            yield b"stream_data_chunk_1"
            yield b"stream_data_chunk_2"

        # Mock the stream_continuous_direct method used by the endpoint
        mock_response = StreamingResponse(
            mock_stream_generator(), media_type="video/mp4"
        )

        # Create proper async mocks that accept any arguments
        mock_stream_manager.stream_continuous_direct = AsyncMock(
            return_value=mock_response
        )
        mock_stream_manager.stream_transcoded = AsyncMock(return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=None)
        mock_stream_manager.unregister_client = AsyncMock(return_value=None)
        mock_stream_manager.get_stream_info = Mock(return_value=None)
        mock_stream_manager.clients = {}

        response = client.get("/stream/test_stream_123")
        assert response.status_code == 200

    def test_direct_stream_endpoint_redirects_genuine_hls_vod(self, monkeypatch):
        """A VOD URL that looks raw but whose response is genuinely HLS should
        redirect to the HLS endpoint instead of streaming the master playlist
        as raw bytes (the private-backend-URL leak this probe exists to fix)."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"

        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]
        assert stream_info.is_vod is True

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"#EXTM3U\n#EXT-X-STREAM-INF\nvariant.m3u8"

            async def aclose(self):
                pass

        async def fake_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}", follow_redirects=False)

            assert response.status_code == 302
            assert response.headers["location"].endswith(
                f"/hls/{stream_id}/playlist.m3u8"
            )
            assert stream_info.is_hls is True
            assert stream_info.is_vod is False
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_direct_stream_endpoint_keeps_raw_vod_unredirected(self, monkeypatch):
        """A VOD URL that is genuinely raw media must not be redirected, and
        should fall through to the normal direct-stream path unchanged."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"

        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"\x00\x00\x00\x18ftypmp42"

            async def aclose(self):
                pass

        async def fake_probe_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_probe_send)

        from fastapi.responses import StreamingResponse

        async def mock_stream_generator():
            yield b"raw-video-bytes"

        manager.stream_continuous_direct = AsyncMock(
            return_value=StreamingResponse(
                mock_stream_generator(), media_type="video/mp4"
            )
        )

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}")

            assert response.status_code == 200
            assert response.content == b"raw-video-bytes"
            assert stream_info.is_hls is False
            assert stream_info.is_vod is True
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_transcoded_vod_stream_is_never_probed_or_redirected(self, monkeypatch):
        """A transcoded VOD stream must not be probed or redirected to the HLS
        endpoint - the served content is FFmpeg's output, not the source URL,
        so the source's real content type is irrelevant here."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"

        stream_id = asyncio.run(
            manager.get_or_create_stream(vod_url, is_transcoded=True)
        )
        stream_info = manager.streams[stream_id]
        assert stream_info.is_vod is True

        probe_called = False

        async def fake_probe_send(request, stream=True, **kwargs):
            nonlocal probe_called
            probe_called = True
            raise AssertionError("should not probe a transcoded stream's source")

        monkeypatch.setattr(manager.http_client, "send", fake_probe_send)

        from fastapi.responses import StreamingResponse

        async def mock_transcoded_generator():
            yield b"transcoded-bytes"

        manager.stream_transcoded = AsyncMock(
            return_value=StreamingResponse(
                mock_transcoded_generator(), media_type="video/mp2t"
            )
        )

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}")

            assert response.status_code == 200
            assert response.content == b"transcoded-bytes"
            assert probe_called is False
            assert stream_info.content_type_verified is False
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_resolve_vod_content_type_probes_only_once_under_concurrency(
        self, monkeypatch
    ):
        """Concurrent callers racing to probe a never-before-verified VOD
        stream must only trigger one upstream connection, not one each -
        providers commonly cap concurrent connections per account."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]

        probe_count = 0

        async def fake_send(request, stream=True, **kwargs):
            nonlocal probe_count
            probe_count += 1
            await asyncio.sleep(0.05)  # widen the race window

            class _FakeResponse:
                status_code = 200
                headers = {}

                def raise_for_status(self):
                    pass

                async def aiter_bytes(self):
                    yield b"#EXTM3U\nrest"

                async def aclose(self):
                    pass

            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:

            async def run_concurrent():
                await asyncio.gather(
                    manager.resolve_vod_content_type(stream_id),
                    manager.resolve_vod_content_type(stream_id),
                    manager.resolve_vod_content_type(stream_id),
                )

            asyncio.run(run_concurrent())

            assert probe_count == 1
            assert stream_info.is_hls is True
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_probe_skipped_for_unambiguous_raw_video_extension(self, monkeypatch):
        """A VOD URL with a definite raw-video extension is unambiguous by
        construction - probing it wastes an upstream connection and up to
        VOD_PROBE_TIMEOUT of latency for no benefit."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.mp4"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]
        assert stream_info.is_vod is True

        probe_called = False

        async def fake_send(request, stream=True, **kwargs):
            nonlocal probe_called
            probe_called = True
            raise AssertionError("should not probe an unambiguous .mp4 URL")

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            asyncio.run(manager.resolve_vod_content_type(stream_id))

            assert probe_called is False
            assert stream_info.content_type_verified is True
            assert stream_info.is_vod is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_probe_tolerates_leading_whitespace_before_extm3u(self, monkeypatch):
        """A non-conformant but genuine HLS server can emit a leading blank
        line or BOM before #EXTM3U - too tight a byte window or a strict
        startswith() would misclassify it as raw and lock that in forever."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"\n\n#EXTM3U\n#EXT-X-VERSION:3\nrest-of-playlist"

            async def aclose(self):
                pass

        async def fake_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            asyncio.run(manager.resolve_vod_content_type(stream_id))

            assert stream_info.is_hls is True
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_failover_never_flips_live_stream_to_vod_category(self, monkeypatch):
        """A failover URL's shape must never change a live/continuous
        stream's fundamental category - doing so would orphan its existing
        broadcast-sharing subscribers (gated on is_vod) and pick the wrong
        httpx client/timeout profile (chosen from is_live_continuous)."""
        manager = StreamManager()
        # Primary looks live; failover URL happens to look VOD-shaped.
        primary_url = "http://primary.example.com/live/u/p/1.ts"
        failover_url = "http://backup.example.com/movie/u/p/1.mp4"

        stream_id = asyncio.run(
            manager.get_or_create_stream(primary_url, failover_urls=[failover_url])
        )
        stream_info = manager.streams[stream_id]
        assert stream_info.is_live_continuous is True
        assert stream_info.is_vod is False

        try:
            asyncio.run(
                manager._try_update_failover_url(stream_id, "test_reason")
            )

            assert stream_info.current_url == failover_url
            # Category is locked - still live, not reclassified as VOD.
            assert stream_info.is_live_continuous is True
            assert stream_info.is_vod is False
            # Never eligible for probing in the first place, so untouched.
            assert stream_info.content_type_verified is False
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_recycled_stream_keeps_its_probe_lock(self):
        """Recycling an orphaned stream_id (0 clients) must not pop its
        probe lock - a probe for the just-replaced StreamInfo could still be
        in flight, and popping would hand the fresh session a brand-new Lock,
        letting two probes run concurrently against the same provider."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"

        try:
            stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
            lock_before = manager._vod_probe_locks.setdefault(
                stream_id, asyncio.Lock()
            )

            # Recycle: same stream_id, 0 clients, requested again.
            asyncio.run(manager.get_or_create_stream(vod_url))

            assert manager._vod_probe_locks.get(stream_id) is lock_before
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_probe_discards_stale_result_after_concurrent_failover(self, monkeypatch):
        """If a failover swaps in a new URL (and resets classification) while
        a probe for the old URL is still in flight, the in-flight probe's
        result must not overwrite the fresh state when it finally resolves -
        otherwise a genuinely-HLS failover URL can be permanently marked
        "verified, not HLS" from stale data about a different URL entirely."""
        manager = StreamManager()
        old_url = "http://old.example.com/movie/1234.m3u8"
        new_url = "http://new.example.com/movie/1234.m3u8"

        stream_id = asyncio.run(
            manager.get_or_create_stream(old_url, failover_urls=[new_url])
        )
        stream_info = manager.streams[stream_id]

        probe_started = asyncio.Event()
        release_probe = asyncio.Event()

        async def fake_send(request, stream=True, **kwargs):
            # Only the (first, old-URL) probe should ever reach here - once
            # discarded, resolve_vod_content_type() for the new URL runs
            # again after the lock frees up, but the test asserts on state
            # before that second call, so a single fake response suffices.
            probe_started.set()
            await release_probe.wait()

            class _FakeResponse:
                status_code = 200
                headers = {}

                def raise_for_status(self):
                    pass

                async def aiter_bytes(self):
                    yield b"#EXTM3U\nrest"

                async def aclose(self):
                    pass

            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:

            async def scenario():
                probe_task = asyncio.create_task(
                    manager.resolve_vod_content_type(stream_id)
                )
                await probe_started.wait()

                # Failover swaps the URL and resets classification while the
                # above probe (for old_url) is still awaiting its response.
                await manager._try_update_failover_url(stream_id, "test")
                assert stream_info.current_url == new_url
                assert stream_info.content_type_verified is False

                # Now let the stale (old_url) probe finish.
                release_probe.set()
                await probe_task

            asyncio.run(scenario())

            # The stale probe's "genuine HLS" result about old_url must not
            # have been committed - the fresh, post-failover state stands.
            assert stream_info.current_url == new_url
            assert stream_info.content_type_verified is False
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_probe_failure_does_not_permanently_lock_in_classification(
        self, monkeypatch
    ):
        """A network error during the probe must not mark content_type_verified
        - otherwise one transient blip on a genuinely-HLS stream would lock it
        into being served raw forever, the exact bug this probe exists to fix."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]

        async def fake_send_error(request, stream=True, **kwargs):
            raise httpx.ConnectTimeout("simulated network blip")

        monkeypatch.setattr(manager.http_client, "send", fake_send_error)

        try:
            asyncio.run(manager.resolve_vod_content_type(stream_id))

            assert stream_info.content_type_verified is False
            assert stream_info.content_type_probe_failed_at is not None
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_probe_skips_retry_within_cooldown_then_retries_after(self, monkeypatch):
        """After a failed probe, requests within the cooldown window must not
        re-probe (avoids hammering a flaky upstream), but a request after the
        cooldown expires must get a real, fresh answer."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]

        probe_count = 0

        async def fake_send_success(request, stream=True, **kwargs):
            nonlocal probe_count
            probe_count += 1

            class _FakeResponse:
                status_code = 200
                headers = {}

                def raise_for_status(self):
                    pass

                async def aiter_bytes(self):
                    yield b"#EXTM3U\nrest"

                async def aclose(self):
                    pass

            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send_success)

        try:
            # Simulate a failure that just happened - well within the cooldown.
            stream_info.content_type_probe_failed_at = datetime.now(timezone.utc)
            asyncio.run(manager.resolve_vod_content_type(stream_id))
            assert probe_count == 0
            assert stream_info.content_type_verified is False

            # Simulate the cooldown having fully elapsed.
            stream_info.content_type_probe_failed_at = datetime.now(
                timezone.utc
            ) - timedelta(seconds=settings.VOD_PROBE_RETRY_COOLDOWN + 1)
            asyncio.run(manager.resolve_vod_content_type(stream_id))
            assert probe_count == 1
            assert stream_info.is_hls is True
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_head_direct_stream_probes_and_redirects_on_first_request(
        self, monkeypatch
    ):
        """A player's HEAD before any GET must not skip content-type probing -
        otherwise HEAD hits the raw backend URL directly for what turns out
        to be a genuine HLS master playlist."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))
        stream_info = manager.streams[stream_id]
        assert stream_info.content_type_verified is False

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"#EXTM3U\nrest"

            async def aclose(self):
                pass

        async def fake_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.head(f"/stream/{stream_id}", follow_redirects=False)

            assert response.status_code == 302
            assert response.headers["location"].endswith(
                f"/hls/{stream_id}/playlist.m3u8"
            )
            assert stream_info.content_type_verified is True
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_hls_redirect_forwards_explicit_client_id_and_username(self, monkeypatch):
        """An explicit client_id/username passed to /stream/ must survive the
        redirect to /hls/, or session/client-record continuity breaks."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"
        stream_id = asyncio.run(manager.get_or_create_stream(vod_url))

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"#EXTM3U\nrest"

            async def aclose(self):
                pass

        async def fake_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(
                    f"/stream/{stream_id}",
                    params={"client_id": "my-fixed-id", "username": "alice"},
                    follow_redirects=False,
                )

            assert response.status_code == 302
            location = response.headers["location"]
            assert "client_id=my-fixed-id" in location
            assert "username=alice" in location
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_hls_redirect_forwards_username_shorthand_aliases(self, monkeypatch):
        """get_client_info() accepts username under 'user' or 'u' as well as
        'username' - the redirect must not silently drop those aliases."""
        manager = StreamManager()
        vod_url = "http://provider.example.com/movie/1234.m3u8"

        class _FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            async def aiter_bytes(self):
                yield b"#EXTM3U\nrest"

            async def aclose(self):
                pass

        async def fake_send(request, stream=True, **kwargs):
            return _FakeResponse()

        monkeypatch.setattr(manager.http_client, "send", fake_send)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                for i, alias in enumerate(("user", "u")):
                    stream_id = asyncio.run(
                        manager.get_or_create_stream(f"{vod_url}?variant={i}")
                    )
                    response = client.get(
                        f"/stream/{stream_id}",
                        params={alias: "bob"},
                        follow_redirects=False,
                    )
                    assert response.status_code == 302
                    assert "username=bob" in response.headers["location"]
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_direct_stream_endpoint_recovers_from_redirect_502(self, monkeypatch):
        """API regression: /stream recovers when sticky redirected upstream returns 502 on reconnect."""
        manager = StreamManager()

        primary_url = "http://provider.example.com/live/channel.ts"
        sticky_redirect_url = "http://edge-2.provider.example.com/live/channel.ts"

        monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 0)
        monkeypatch.setattr("config.settings.STREAM_TOTAL_TIMEOUT", 5.0)

        stream_id = asyncio.run(
            manager.get_or_create_stream(primary_url, use_sticky_session=True)
        )
        stream_info = manager.streams[stream_id]
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
                    _MockResponse(200, chunk=b"ok-api", request_url=url)
                )
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}")

            assert response.status_code == 200
            assert response.content == b"ok-api"
            assert called_urls == [sticky_redirect_url, primary_url]
            assert stream_info.current_url is None
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_direct_stream_endpoint_recovers_after_retry_exhaustion(self, monkeypatch):
        """API regression: /stream recovers to entry URL after sticky redirect 502 retries are exhausted."""
        manager = StreamManager()

        primary_url = "http://provider.example.com/live/channel.ts"
        sticky_redirect_url = "http://edge-2.provider.example.com/live/channel.ts"

        monkeypatch.setattr("config.settings.STREAM_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr("config.settings.STREAM_RETRY_DELAY", 0.0)
        monkeypatch.setattr("config.settings.STREAM_TOTAL_TIMEOUT", 5.0)

        stream_id = asyncio.run(
            manager.get_or_create_stream(primary_url, use_sticky_session=True)
        )
        stream_info = manager.streams[stream_id]
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
                    _MockResponse(200, chunk=b"ok-api-retry", request_url=url)
                )
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(manager.live_stream_client, "stream", fake_stream)

        try:
            with patch("api.stream_manager", manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}")

            assert response.status_code == 200
            assert response.content == b"ok-api-retry"
            assert called_urls[:3] == [
                sticky_redirect_url,
                sticky_redirect_url,
                sticky_redirect_url,
            ]
            assert called_urls[3] == primary_url
            assert stream_info.current_url is None
        finally:
            asyncio.run(manager.http_client.aclose())
            asyncio.run(manager.live_stream_client.aclose())

    def test_stats_endpoint(self, client, mock_stream_manager):
        response = client.get("/stats")
        assert response.status_code == 200

        data = response.json()
        assert "total_streams" in data
        assert "active_streams" in data
        assert "total_clients" in data

    @patch("api.stream_manager")
    def test_error_handling_stream_creation_failure(self, mock_sm, client):
        mock_sm.get_or_create_stream = AsyncMock(
            side_effect=Exception("Stream creation failed")
        )

        payload = {"url": "http://example.com/stream.m3u8"}
        response = client.post("/streams", json=payload)
        assert response.status_code == 500

        data = response.json()
        assert "failed" in data["detail"].lower()

    def test_cors_headers(self, client):
        response = client.get("/", headers={"Origin": "http://localhost:3000"})
        # FastAPI might add CORS headers if configured
        assert response.status_code == 200


class TestStreamValidation:
    """Test stream URL validation"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_valid_urls(self, client):
        valid_urls = [
            "http://example.com/stream.m3u8",
            "https://secure.example.com/playlist.m3u8",
            "http://192.168.1.100:8085/live/stream.ts",
            "https://cdn.example.com/video.mp4",
        ]

        with patch("api.stream_manager") as mock_sm:
            mock_sm.get_or_create_stream = AsyncMock(return_value="test_123")

            for url in valid_urls:
                payload = {"url": url}
                response = client.post("/streams", json=payload)
                assert response.status_code == 200, f"Failed for URL: {url}"

    def test_invalid_urls(self, client):
        invalid_urls = [
            "not_a_url",
            "ftp://example.com/file.m3u8",  # Wrong protocol
            "http://",  # Incomplete URL
            "",  # Empty string
            "javascript:alert('xss')",  # XSS attempt
        ]

        for url in invalid_urls:
            payload = {"url": url}
            response = client.post("/streams", json=payload)
            # Should either be 422 (validation) or 500 (processing error)
            assert response.status_code in [422, 500], f"Should reject URL: {url}"


class TestDeleteStreamsByMetadata:
    """Tests for DELETE /streams/by-metadata with force and client_id parameters."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _make_stream(self, is_transcoded=False, stream_stopped_emitted=False):
        stream = Mock()
        stream.is_transcoded = is_transcoded
        stream.stream_stopped_emitted = stream_stopped_emitted
        stream.metadata = {"playlist_uuid": "test-uuid-123"}
        stream.connected_clients = set()
        return stream

    def test_force_true_stops_stream_with_remaining_clients(self, client):
        """force=True (default) always stops the stream regardless of other clients."""
        stream_id = "stream-abc"
        stream = self._make_stream()
        stream.connected_clients = {"other-client"}

        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {stream_id: stream}
            mock_sm.stream_clients = {stream_id: {"other-client"}}
            mock_sm.pooled_manager = None
            mock_sm.cleanup_client = AsyncMock()
            mock_sm._emit_event = AsyncMock()

            response = client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "force": "true",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 1
        assert len(data["skipped_streams"]) == 0

    def test_force_false_skips_stream_when_other_clients_remain(self, client):
        """force=False skips the stream when the requesting client is not the last viewer."""
        stream_id = "stream-abc"
        stream = self._make_stream()
        stream.connected_clients = {"requesting-client", "other-client"}

        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {stream_id: stream}
            mock_sm.stream_clients = {stream_id: {"requesting-client", "other-client"}}
            mock_sm.pooled_manager = None
            mock_sm.cleanup_client = AsyncMock()
            mock_sm._emit_event = AsyncMock()

            response = client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "force": "false",
                    "client_id": "requesting-client",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        assert len(data["skipped_streams"]) == 1
        assert data["skipped_streams"][0]["reason"] == "has_active_clients"
        assert data["skipped_streams"][0]["active_clients"] == 1

    def test_force_false_stops_stream_when_requesting_client_is_last(self, client):
        """force=False stops the stream when the requesting client is the last viewer."""
        stream_id = "stream-abc"
        stream = self._make_stream()
        stream.connected_clients = {"sole-client"}

        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {stream_id: stream}
            mock_sm.stream_clients = {stream_id: {"sole-client"}}
            mock_sm.pooled_manager = None
            mock_sm.cleanup_client = AsyncMock()
            mock_sm._emit_event = AsyncMock()

            response = client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "force": "false",
                    "client_id": "sole-client",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 1
        assert len(data["skipped_streams"]) == 0

    def test_force_false_without_client_id_skips_stream_with_active_clients(
        self, client
    ):
        """force=False with no client_id still checks remaining clients before stopping."""
        stream_id = "stream-abc"
        stream = self._make_stream()
        stream.connected_clients = {"some-client"}

        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {stream_id: stream}
            mock_sm.stream_clients = {stream_id: {"some-client"}}
            mock_sm.pooled_manager = None
            mock_sm.cleanup_client = AsyncMock()
            mock_sm._emit_event = AsyncMock()

            response = client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "force": "false",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        assert len(data["skipped_streams"]) == 1

    def test_client_id_max_length_validation(self, client):
        """client_id longer than 128 characters is rejected with 422."""
        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {}
            mock_sm.stream_clients = {}

            response = client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "client_id": "x" * 129,
                },
            )

        assert response.status_code == 422

    def test_discard_updates_both_data_structures(self, client):
        """Verifies that connected_clients and stream_clients are both updated on discard."""
        stream_id = "stream-abc"
        stream = self._make_stream()
        stream.connected_clients = {"sole-client"}

        with patch("api.stream_manager") as mock_sm:
            mock_sm.streams = {stream_id: stream}
            mock_sm.stream_clients = {stream_id: {"sole-client"}}
            mock_sm.pooled_manager = None
            mock_sm.cleanup_client = AsyncMock()
            mock_sm._emit_event = AsyncMock()

            client.delete(
                "/streams/by-metadata",
                params={
                    "field": "playlist_uuid",
                    "value": "test-uuid-123",
                    "force": "false",
                    "client_id": "sole-client",
                },
            )

        # Both data structures must have had the client removed before the stop decision.
        assert "sole-client" not in stream.connected_clients


if __name__ == "__main__":
    pytest.main([__file__])
