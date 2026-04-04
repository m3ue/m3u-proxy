import sys
import os
import tempfile

# Add src to path so tests can import the application
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient
import api


def test_transcode_hls_segment_serving(monkeypatch):
    """End-to-end test: create a transcoded HLS stream and fetch a local .ts segment via the segment proxy.

    The test monkeypatches `api.stream_manager` to use a `StreamManager` whose
    pooled_manager returns a fake shared process with an `hls_dir` that contains
    `index.m3u8` and `segment1.ts`. The segment endpoint should read the file
    from disk and return its bytes.
    """

    from stream_manager import StreamManager

    sm = StreamManager()

    # Create temp hls dir and write a simple playlist + a small .ts file
    tmpdir = tempfile.mkdtemp(prefix="test_m3u_hls_")
    playlist_text = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:1.0,
segment1.ts
"""
    with open(os.path.join(tmpdir, "index.m3u8"), "w", encoding="utf-8") as fh:
        fh.write(playlist_text)

    # Write a small binary blob to act as the TS segment
    seg_path = os.path.join(tmpdir, "segment1.ts")
    seg_bytes = b"\x00\x01\x02\x03TSSEGMENT"
    with open(seg_path, "wb") as fh:
        fh.write(seg_bytes)

    class FakeShared:
        def __init__(self, hls_dir, playlist_text):
            self.mode = "hls"
            self.hls_dir = hls_dir
            self._playlist = playlist_text

        async def read_playlist(self):
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
            **kwargs,
        ):
            return ("fake-stream-key", fake_shared)

        async def force_stop_stream(self, stream_key):
            return None

    sm.pooled_manager = FakePooled()

    # Patch the api module's stream_manager instance so the app uses our StreamManager
    monkeypatch.setattr(api, "stream_manager", sm)

    # Create the TestClient after monkeypatching
    client = TestClient(api.app)

    # Use a custom profile template (starts with '-' -> custom template)
    payload = {
        "url": "http://example.com/source.m3u8",
        "profile": "-i {input_url} -c:v libx264 -preset veryfast -hls_time 1 -hls_list_size 0 -f hls index.m3u8",
    }

    # Create the transcoded stream
    resp = client.post("/transcode", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    stream_id = data["stream_id"]

    # Build file:// URL for the segment and request it via the segment proxy
    file_url = f"file://{seg_path}"
    # Note: api.get_hls_segment expects query params: client_id and url
    seg_resp = client.get(
        f"/hls/{stream_id}/segment?client_id=test_client&url={file_url}"
    )
    assert seg_resp.status_code == 200, seg_resp.text
    # Response content should match the bytes we wrote
    assert seg_resp.content == seg_bytes
