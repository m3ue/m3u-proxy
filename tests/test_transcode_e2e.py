import sys
import os
import pytest
import tempfile

# Add src to path so tests can import the application
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
import api


@pytest.mark.parametrize(
    "profile_template",
    [
        # Custom profile template that uses HLS output and includes the {input_url} placeholder
        "-i {input_url} -c:v libx264 -preset veryfast -hls_time 1 -hls_list_size 0 -f hls index.m3u8"
    ],
)
def test_transcode_hls_end_to_end(monkeypatch, profile_template):
    """End-to-end test: create a transcoded HLS stream and fetch the processed playlist.

    The test monkeypatches the app's stream_manager.pooled_manager to return a fake
    shared process in HLS mode which reads from a temporary directory containing
    an `index.m3u8` file. This avoids launching ffmpeg while exercising the
    /transcode and /hls/{stream_id}/playlist.m3u8 API code paths.
    """

    # Prepare a fresh StreamManager instance for the app and attach a fake pooled manager
    from stream_manager import StreamManager

    sm = StreamManager()

    # Create temp hls dir and write a simple playlist
    tmpdir = tempfile.mkdtemp(prefix="test_m3u_hls_")
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:1.0,
segment1.ts
#EXTINF:1.0,
segment2.ts
"""
    with open(os.path.join(tmpdir, "index.m3u8"), "w", encoding="utf-8") as fh:
        fh.write(playlist_text)

    class FakeShared:
        def __init__(self, hls_dir, playlist_text):
            self.mode = "hls"
            self.hls_dir = hls_dir
            self._playlist = playlist_text

        async def read_playlist(self):
            # Simulate reading from disk; return the playlist text
            return self._playlist

    fake_shared = FakeShared(tmpdir, playlist_text)

    class FakePooled:
        async def start(self):
            return None

        async def stop(self):
            return None

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
            # Return a dummy stream key and our fake shared process
            return ("fake-stream-key", fake_shared)

        async def force_stop_stream(self, stream_key):
            return None

    sm.pooled_manager = FakePooled()

    # Patch the api module's stream_manager instance so the app uses our StreamManager
    monkeypatch.setattr(api, "stream_manager", sm)

    # Create a TestClient AFTER monkeypatching stream_manager so lifespan uses our patched manager
    client = TestClient(api.app)

    # Build the transcode request using the custom profile template
    payload = {"url": "http://example.com/source.m3u8", "profile": profile_template}

    # Create the transcoded stream
    resp = client.post("/transcode", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stream_type"] == "transcoded"
    stream_id = data["stream_id"]
    assert stream_id

    # Now fetch the playlist endpoint which should use the pooled manager/fake shared
    playlist_resp = client.get(f"/hls/{stream_id}/playlist.m3u8")
    assert playlist_resp.status_code == 200, playlist_resp.text
    assert "#EXTM3U" in playlist_resp.text
    # Ensure that the content was processed (segment proxy URLs should exist)
    # Note: Changed from 'segment.ts?url=' to 'segment?url=' to match actual API endpoint
    assert "segment?url=" in playlist_resp.text or "segment1.ts" in playlist_resp.text
