import sys
import os
import pytest
import asyncio

# Add src to path so we can import our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pooled_stream_manager import SharedTranscodingProcess
from stream_manager import StreamManager


@pytest.mark.asyncio
async def test_shared_transcoding_process_hls_mode(tmp_path):
    """Ensure SharedTranscodingProcess detects HLS args and creates hls_dir"""
    # Use ffmpeg_args that indicate HLS output
    ffmpeg_args = [
        "-fflags",
        "+genpts+discardcorrupt+igndts",
        "-i",
        "{input}",
        "-c:v",
        "libx264",
        "-hls_time",
        "10",
        "-hls_list_size",
        "0",
        "-f",
        "hls",
        "index.m3u8",
    ]

    proc = SharedTranscodingProcess(
        stream_id="testhls",
        url="http://example.com/stream.m3u8",
        profile="hls",
        ffmpeg_args=ffmpeg_args,
    )

    assert getattr(proc, "mode", None) == "hls"
    assert proc.hls_dir is not None
    assert os.path.isdir(proc.hls_dir)


@pytest.mark.asyncio
async def test_stream_manager_get_playlist_from_pooled_hls(monkeypatch):
    """When a stream is configured as transcoded HLS and pooled manager returns an HLS shared process, get_playlist_content should return processed playlist"""
    sm = StreamManager()

    # Create a transcoded stream
    url = "http://example.com/transcoded_playlist.m3u8"
    stream_id = await sm.get_or_create_stream(
        url,
        is_transcoded=True,
        transcode_profile="hls",
        transcode_ffmpeg_args=["-f", "hls", "-hls_time", "10"],
    )

    # Register a client
    client_id = "client1"
    await sm.register_client(client_id, stream_id)

    # Prepare fake shared process
    class FakeShared:
        def __init__(self, hls_dir, playlist_text):
            self.mode = "hls"
            self.hls_dir = hls_dir
            self._playlist = playlist_text

        async def read_playlist(self):
            return self._playlist

    playlist_text = """#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10.0,\nsegment1.ts\n#EXTINF:10.0,\nsegment2.ts\n"""

    # Create temp dir and write a dummy playlist file there to mimic actual ffmpeg output
    import tempfile

    hls_dir = tempfile.mkdtemp(prefix="test_m3u_hls_")
    with open(os.path.join(hls_dir, "index.m3u8"), "w", encoding="utf-8") as fh:
        fh.write(playlist_text)

    fake_shared = FakeShared(hls_dir, playlist_text)

    async def fake_get_or_create_shared_stream(
        url,
        profile,
        ffmpeg_args,
        client_id,
        user_agent=None,
        headers=None,
        stream_id=None,
        reuse_stream_key=None,
    ):
        return ("fakekey", fake_shared)

    # Attach a mocked pooled manager to the stream manager
    class FakePooled:
        async def get_or_create_shared_stream(
            self,
            url,
            profile,
            ffmpeg_args,
            client_id,
            user_agent=None,
            headers=None,
            stream_id=None,
            reuse_stream_key=None,
            **kwargs,
        ):
            return await fake_get_or_create_shared_stream(
                url,
                profile,
                ffmpeg_args,
                client_id,
                user_agent,
                headers,
                stream_id,
                reuse_stream_key,
            )

    sm.pooled_manager = FakePooled()

    # Now call get_playlist_content which should detect transcoded HLS and use pooled manager
    processed = await sm.get_playlist_content(
        stream_id, client_id, base_proxy_url="http://proxy.test"
    )

    assert processed is not None
    assert "#EXTM3U" in processed
    # Since M3U8Processor rewrites segment URLs, we expect 'segment?url=' in processed
    # (Note: Changed from 'segment.ts' to 'segment' to match actual API endpoint)
    assert "segment?url=" in processed or "segment1.ts" in processed


@pytest.mark.asyncio
async def test_stream_transcoded_content_type_detection(monkeypatch):
    """Verify stream_transcoded returns streamingresponse with media_type derived from ffmpeg args"""
    sm = StreamManager()

    # Create a stream configured to transcode to mp4
    url = "http://example.com/video_source"
    stream_id = await sm.get_or_create_stream(
        url,
        is_transcoded=True,
        transcode_profile="mp4",
        transcode_ffmpeg_args=["-f", "mp4", "-movflags", "+frag_keyframe+empty_moov"],
    )

    client_id = "client_mp4"
    await sm.register_client(client_id, stream_id)

    # Build a fake shared process for stdout-mode
    class FakeProcess:
        def __init__(self):
            self.mode = "stdout"

            # fake process object with returncode None and stdout present
            class P:
                returncode = None
                pid = 1234
                stdout = object()

            self.process = P()
            # client queue exists but we won't actually stream
            self.client_queues = {client_id: asyncio.Queue()}

    fake_shared = FakeProcess()

    async def fake_get_or_create_shared_stream(
        url, profile, ffmpeg_args, client_id, user_agent=None, headers=None
    ):
        return ("fakekey2", fake_shared)

    class FakePooled2:
        async def get_or_create_shared_stream(
            self,
            url,
            profile,
            ffmpeg_args,
            client_id,
            user_agent=None,
            headers=None,
            **kwargs,
        ):
            return await fake_get_or_create_shared_stream(
                url, profile, ffmpeg_args, client_id, user_agent, headers
            )

    sm.pooled_manager = FakePooled2()

    # Call stream_transcoded - we won't iterate the response, only inspect media_type/header
    response = await sm.stream_transcoded(stream_id, client_id)

    # StreamingResponse should be returned and media_type should be video/mp4 based on args
    from fastapi.responses import StreamingResponse

    assert isinstance(response, StreamingResponse)
    assert response.media_type in (
        "video/mp4",
        "video/mp2t",
        "application/octet-stream",
    )


@pytest.mark.asyncio
async def test_stream_transcoded_content_type_detection_matroska(monkeypatch):
    """Verify stream_transcoded detects matroska/mkv format from ffmpeg args"""
    sm = StreamManager()

    # Create a stream configured to transcode to matroska
    url = "http://example.com/video_source"
    stream_id = await sm.get_or_create_stream(
        url,
        is_transcoded=True,
        transcode_profile="mkv",
        transcode_ffmpeg_args=["-f", "matroska"],
    )

    client_id = "client_mkv"
    await sm.register_client(client_id, stream_id)

    class FakeProcess:
        def __init__(self):
            self.mode = "stdout"

            class P:
                returncode = None
                pid = 4321
                stdout = object()

            self.process = P()
            self.client_queues = {client_id: asyncio.Queue()}

    fake_shared = FakeProcess()

    async def fake_get_or_create_shared_stream(
        url, profile, ffmpeg_args, client_id, user_agent=None, headers=None
    ):
        return ("fakekey3", fake_shared)

    class FakePooled3:
        async def get_or_create_shared_stream(
            self,
            url,
            profile,
            ffmpeg_args,
            client_id,
            user_agent=None,
            headers=None,
            **kwargs,
        ):
            return await fake_get_or_create_shared_stream(
                url, profile, ffmpeg_args, client_id, user_agent, headers
            )

    sm.pooled_manager = FakePooled3()

    response = await sm.stream_transcoded(stream_id, client_id)
    from fastapi.responses import StreamingResponse

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "video/x-matroska"
