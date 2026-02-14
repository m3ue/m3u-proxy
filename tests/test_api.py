# Add src to path first
from stream_manager import StreamManager
from api import app, get_content_type, is_direct_stream
import httpx
import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, AsyncMock, patch
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestHelperFunctions:
    """Test utility functions"""

    def test_get_content_type(self):
        assert get_content_type("test.ts") == "video/mp2t"
        assert get_content_type("test?profile=pass") == "video/mp2t"
        assert get_content_type(
            "playlist.m3u8") == "application/vnd.apple.mpegurl"
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


class TestAPI:
    """Test FastAPI endpoints"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def mock_stream_manager(self):
        with patch('api.stream_manager') as mock:
            # Mock the streams dict to include test_stream_123
            mock.streams = {"test_stream_123": Mock()}

            mock.get_or_create_stream = AsyncMock(
                return_value="test_stream_123")
            mock.get_stream_info = Mock(return_value=Mock(
                stream_id="test_stream_123",
                original_url="http://example.com/test.m3u8",
                is_active=True,
                client_count=1,
                error_count=0
            ))
            mock.get_stats = Mock(return_value={
                "proxy_stats": {
                    "total_streams": 1,
                    "active_streams": 1,
                    "total_clients": 0,
                    "active_clients": 0,
                    "total_bytes_served": 0,
                    "total_segments_served": 0,
                    "uptime_seconds": 3600
                },
                "streams": [{
                    "stream_id": "test_stream_123",
                    "original_url": "http://example.com/test.m3u8",
                    "is_active": True,
                    "client_count": 1,
                    "error_count": 0,
                    "uptime": 3600
                }],
                "clients": []
            })
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
            "user_agent": "TestApp/1.0"
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
                "Authorization": "Bearer token"
            }
        }

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        # Verify that the stream manager's get_or_create_stream was called with the headers
        mock_stream_manager.get_or_create_stream.assert_called_once()
        call_args = mock_stream_manager.get_or_create_stream.call_args
        assert call_args.kwargs['headers'] == payload['headers']

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
        mock_stream_manager.stream_clients = {'test_stream_123': {'client1'}}
        mock_stream_manager.streams = {
            'test_stream_123': Mock(is_transcoded=False)
        }

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
            return_value="#EXTM3U\nsegment1.ts")
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
            mock_response_generator(), media_type="video/mp2t")
        mock_stream_manager.proxy_hls_segment = AsyncMock(
            return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())

        response = client.get(
            "/hls/test_stream_123/segment?client_id=test_client&url=http://example.com/segment1.ts")
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
            mock_stream_generator(), media_type="video/mp4")

        # Create proper async mocks that accept any arguments
        mock_stream_manager.stream_continuous_direct = AsyncMock(
            return_value=mock_response)
        mock_stream_manager.stream_transcoded = AsyncMock(
            return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=None)
        mock_stream_manager.unregister_client = AsyncMock(return_value=None)
        mock_stream_manager.get_stream_info = Mock(return_value=None)
        mock_stream_manager.clients = {}

        response = client.get("/stream/test_stream_123")
        assert response.status_code == 200

    def test_direct_stream_endpoint_recovers_from_redirect_502(self, monkeypatch):
        """API regression: /stream recovers when sticky redirected upstream returns 502 on reconnect."""
        manager = StreamManager()

        primary_url = "http://provider.example.com/live/channel.ts"
        sticky_redirect_url = "http://edge-2.provider.example.com/live/channel.ts"

        monkeypatch.setattr('config.settings.STREAM_RETRY_ATTEMPTS', 0)
        monkeypatch.setattr('config.settings.STREAM_TOTAL_TIMEOUT', 5.0)

        stream_id = asyncio.run(manager.get_or_create_stream(
            primary_url,
            use_sticky_session=True
        ))
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
            def __init__(self, status_code: int, chunk: bytes | None = None, request_url: str = "http://example.com"):
                self.status_code = status_code
                self.headers = {"content-type": "video/mp2t"}
                self._chunk = chunk
                self._request_url = request_url

            def raise_for_status(self):
                if self.status_code >= 400:
                    request = httpx.Request("GET", self._request_url)
                    response = httpx.Response(
                        self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        f"{self.status_code} Bad Gateway",
                        request=request,
                        response=response
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
                return _MockStreamCM(_MockResponse(200, chunk=b"ok-api", request_url=url))
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(manager.live_stream_client, 'stream', fake_stream)

        try:
            with patch('api.stream_manager', manager):
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

        monkeypatch.setattr('config.settings.STREAM_RETRY_ATTEMPTS', 2)
        monkeypatch.setattr('config.settings.STREAM_RETRY_DELAY', 0.0)
        monkeypatch.setattr('config.settings.STREAM_TOTAL_TIMEOUT', 5.0)

        stream_id = asyncio.run(manager.get_or_create_stream(
            primary_url,
            use_sticky_session=True
        ))
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
            def __init__(self, status_code: int, chunk: bytes | None = None, request_url: str = "http://example.com"):
                self.status_code = status_code
                self.headers = {"content-type": "video/mp2t"}
                self._chunk = chunk
                self._request_url = request_url

            def raise_for_status(self):
                if self.status_code >= 400:
                    request = httpx.Request("GET", self._request_url)
                    response = httpx.Response(
                        self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        f"{self.status_code} Bad Gateway",
                        request=request,
                        response=response
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
                return _MockStreamCM(_MockResponse(200, chunk=b"ok-api-retry", request_url=url))
            return _MockStreamCM(_MockResponse(500, request_url=url))

        monkeypatch.setattr(manager.live_stream_client, 'stream', fake_stream)

        try:
            with patch('api.stream_manager', manager):
                client = TestClient(app)
                response = client.get(f"/stream/{stream_id}")

            assert response.status_code == 200
            assert response.content == b"ok-api-retry"
            assert called_urls[:3] == [sticky_redirect_url,
                                       sticky_redirect_url, sticky_redirect_url]
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

    @patch('api.stream_manager')
    def test_error_handling_stream_creation_failure(self, mock_sm, client):
        mock_sm.get_or_create_stream = AsyncMock(
            side_effect=Exception("Stream creation failed"))

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
            "https://cdn.example.com/video.mp4"
        ]

        with patch('api.stream_manager') as mock_sm:
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
            "javascript:alert('xss')"  # XSS attempt
        ]

        for url in invalid_urls:
            payload = {"url": url}
            response = client.post("/streams", json=payload)
            # Should either be 422 (validation) or 500 (processing error)
            assert response.status_code in [
                422, 500], f"Should reject URL: {url}"


if __name__ == "__main__":
    pytest.main([__file__])
