from stream_manager import (
    StreamManager,
    ClientInfo,
    StreamInfo,
    ProxyStats,
    M3U8Processor
)
import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch
import httpx

# Add src to path so we can import our modules
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestClientInfo:
    """Test ClientInfo dataclass"""

    def test_client_info_creation(self):
        client = ClientInfo(
            client_id="test_client",
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc)
        )
        assert client.client_id == "test_client"
        assert client.bytes_served == 0
        assert client.segments_served == 0
        assert client.user_agent is None


class TestStreamInfo:
    """Test StreamInfo dataclass"""

    def test_stream_info_creation(self):
        stream = StreamInfo(
            stream_id="test_stream",
            original_url="http://example.com/stream.m3u8",
            created_at=datetime.now(timezone.utc),
            last_access=datetime.now(timezone.utc)
        )
        assert stream.stream_id == "test_stream"
        assert stream.original_url == "http://example.com/stream.m3u8"
        assert stream.client_count == 0
        assert stream.is_active is True
        assert len(stream.failover_urls) == 0
        assert "Mozilla" in stream.user_agent  # Default user agent


class TestM3U8Processor:
    """Test M3U8 URL processing"""

    def test_rewrite_playlist_urls(self):
        processor = M3U8Processor("http://original.com/", "stream123")

        playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
segment1.ts
#EXTINF:10.0,
segment2.ts
#EXT-X-ENDLIST"""

        result = processor.process_playlist(playlist, "http://proxy.com")

        assert "segment" in result  # URLs should be processed
        assert "#EXTM3U" in result
        assert "#EXT-X-VERSION:3" in result

    def test_rewrite_absolute_urls(self):
        from urllib.parse import quote
        processor = M3U8Processor("http://original.com/", "stream123")

        playlist = """#EXTM3U
#EXTINF:10.0,
http://original.com/segment1.ts
#EXTINF:10.0,
http://original.com/segment2.ts"""

        proxy_base_url = "http://proxy.com/hls/stream123"
        result = processor.process_playlist(
            playlist, proxy_base_url, "http://original.com/")

        # Verify that the segment URLs are correctly rewritten and encoded, .ts added back
        encoded_segment1 = quote("http://original.com/segment1.ts", safe='')
        expected_url1 = f"{proxy_base_url}/segment.ts?url={encoded_segment1}&client_id=stream123"
        assert expected_url1 in result

        encoded_segment2 = quote("http://original.com/segment2.ts", safe='')
        expected_url2 = f"{proxy_base_url}/segment.ts?url={encoded_segment2}&client_id=stream123"
        assert expected_url2 in result


class TestStreamManager:
    """Test StreamManager functionality"""

    @pytest.fixture
    def stream_manager(self):
        return StreamManager()

    @pytest.mark.asyncio
    async def test_stream_id_generation(self, stream_manager):
        """Test that stream IDs are generated consistently"""
        url = "http://example.com/test.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)
        assert stream_id is not None
        assert len(stream_id) == 32  # MD5 hash length
        assert isinstance(stream_id, str)

        # Same URL should generate same ID
        stream_id2 = await stream_manager.get_or_create_stream(url)
        assert stream_id == stream_id2

    def test_get_stream_info_nonexistent(self, stream_manager):
        # Current API doesn't have get_stream_info method
        # Instead, check that stream doesn't exist in streams dict
        result = stream_manager.streams.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_stream(self, stream_manager):
        url = "http://example.com/test.m3u8"
        failover_urls = ["http://backup1.com/test.m3u8",
                         "http://backup2.com/test.m3u8"]
        user_agent = "TestAgent/1.0"

        stream_id = await stream_manager.get_or_create_stream(
            url,
            failover_urls=failover_urls,
            user_agent=user_agent
        )

        assert stream_id is not None
        assert len(stream_id) == 32  # MD5 hash length, not 8

        # Check stream was created properly
        stream_info = stream_manager.streams.get(stream_id)
        assert stream_info is not None
        assert stream_info.original_url == url
        assert stream_info.failover_urls == failover_urls
        assert stream_info.user_agent == user_agent

    @pytest.mark.asyncio
    async def test_get_or_create_stream_existing(self, stream_manager):
        url = "http://example.com/test.m3u8"

        # Create stream first time
        stream_id1 = await stream_manager.get_or_create_stream(url)

        # Should return same stream ID for same URL
        stream_id2 = await stream_manager.get_or_create_stream(url)

        assert stream_id1 == stream_id2

    def test_get_all_streams(self, stream_manager):
        stats = stream_manager.get_stats()
        proxy_stats = stats["proxy_stats"]
        assert proxy_stats["total_streams"] == 0
        assert proxy_stats["active_streams"] == 0
        assert proxy_stats["total_clients"] == 0

    @pytest.mark.asyncio
    async def test_register_client(self, stream_manager):
        # First create a stream
        url = "http://example.com/test.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)

        # Now register a client for that stream
        client_id = "test_client_123"
        client_info = await stream_manager.register_client(
            client_id=client_id,
            stream_id=stream_id,
            user_agent="TestClient/1.0",
            ip_address="127.0.0.1"
        )

        assert client_info is not None
        assert client_info.client_id == client_id

        # Check client was registered
        assert client_id in stream_manager.clients
        registered_client = stream_manager.clients[client_id]
        assert registered_client.user_agent == "TestClient/1.0"
        assert registered_client.ip_address == "127.0.0.1"
        assert registered_client.stream_id == stream_id

    @pytest.mark.asyncio
    async def test_proxy_hls_segment_success(self, stream_manager):
        # Create a stream first
        url = "http://example.com/test.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)

        # Register a client
        client_id = "test_client"
        await stream_manager.register_client(
            client_id=client_id,
            stream_id=stream_id,
            user_agent="TestClient/1.0",
            ip_address="127.0.0.1"
        )

        # Mock httpx client for segment request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "video/mp2t"}

        async def mock_aiter_bytes(chunk_size=1024):
            yield b"test_segment_data"

        mock_response.aiter_bytes = mock_aiter_bytes

        with patch.object(stream_manager, 'http_client') as mock_client:
            mock_client.stream.return_value.__aenter__.return_value = mock_response

            # Test the actual API method
            response = await stream_manager.proxy_hls_segment(
                stream_id,
                client_id,
                "http://example.com/segment1.ts"
            )

            # Response should be a StreamingResponse
            from fastapi.responses import StreamingResponse
            assert isinstance(response, StreamingResponse)

    @pytest.mark.asyncio
    async def test_get_playlist_content_success(self, stream_manager):
        url = "http://example.com/playlist.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)

        # Register a client
        client_id = "test_client"
        await stream_manager.register_client(
            client_id=client_id,
            stream_id=stream_id,
            user_agent="TestClient/1.0",
            ip_address="127.0.0.1"
        )

        playlist_content = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:10.0,
segment1.ts"""

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = playlist_content
        mock_response.headers = {
            "content-type": "application/vnd.apple.mpegurl"}

        with patch.object(stream_manager, 'http_client') as mock_client:
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await stream_manager.get_playlist_content(
                stream_id,
                client_id,
                "http://proxy.com"
            )

            assert result is not None
            assert "#EXTM3U" in result
            assert "segment" in result  # URLs should be processed

    @pytest.mark.asyncio
    async def test_failover_mechanism(self, stream_manager):
        url = "http://example.com/test.m3u8"
        failover_urls = ["http://backup.com/test.m3u8"]

        stream_id = await stream_manager.get_or_create_stream(
            url,
            failover_urls=failover_urls
        )

        # Check failover URLs were set
        stream_info = stream_manager.streams.get(stream_id)
        assert stream_info is not None
        assert stream_info.failover_urls == failover_urls
        assert stream_info.error_count == 0

        # We can't easily test actual failover without complex mocking
        # so just verify the structure is correct (-1 means no failover used yet)
        assert stream_info.current_failover_index == -1
        assert stream_info.current_url == url


@pytest.mark.asyncio
async def test_concurrent_access():
    """Test concurrent access to stream manager"""
    stream_manager = StreamManager()
    url = "http://example.com/concurrent.m3u8"

    async def create_stream():
        return await stream_manager.get_or_create_stream(url)

    # Create multiple tasks that try to create the same stream
    tasks = [create_stream() for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All should return the same stream ID
    assert all(result == results[0] for result in results)

    # Should only have created one stream
    stats = stream_manager.get_stats()
    proxy_stats = stats["proxy_stats"]
    assert proxy_stats["total_streams"] == 1


if __name__ == "__main__":
    pytest.main([__file__])
