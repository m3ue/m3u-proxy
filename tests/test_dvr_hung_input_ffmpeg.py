"""
End-to-end regression test with real FFmpeg: a DVR capture whose input hangs
(no data, connection left open, as when packets are silently dropped) must keep
the segments recorded before the hang.

Before the fix the stall watchdog sent SIGTERM twice. FFmpeg ignores the first
while blocked in the read; the second aborts its final playlist write and renames
the empty temp file over live.m3u8, so the restart began a new playlist and the
finished recording only contained video from after the outage.
"""

import asyncio
import os
import shutil
import subprocess
import time
from unittest.mock import patch

import pytest

from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required"),
]

MEDIA_SECONDS = 120


@pytest.fixture(scope="module")
def source_ts(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "source.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(MEDIA_SECONDS),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "25",
            "-b:v",
            "300k",
            "-maxrate",
            "300k",
            "-bufsize",
            "150k",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            # Constant mux rate, so pacing by bytes is pacing by time.
            "-muxrate",
            "600k",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
    )
    return path


class LiveSource:
    """Serves a TS file as a live stream paced at real time; can be told to hang."""

    def __init__(self, path):
        self.data = path.read_bytes()
        self.rate = len(self.data) / MEDIA_SECONDS
        self.t0 = time.monotonic()
        self.hanging = False

    def _offset(self):
        pos = int((time.monotonic() - self.t0) * self.rate) % len(self.data)
        return pos - pos % 188

    async def handle(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            while self.hanging:  # accepted, never answered
                await asyncio.sleep(0.1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n\r\n")
            pos, last = self._offset(), time.monotonic()
            while True:
                await asyncio.sleep(0.1)
                now = time.monotonic()
                if self.hanging:  # data that airs during the hang is never sent
                    pos, last = self._offset(), now
                    continue
                n = int((now - last) * self.rate)
                n -= n % 188
                if n <= 0:
                    continue
                last = now
                end = pos + n
                size = len(self.data)
                chunk = (
                    self.data[pos:end]
                    if end <= size
                    else self.data[pos:] + self.data[: end - size]
                )
                pos = end % size
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.transport.abort()


def _listed(playlist):
    try:
        with open(playlist) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


@pytest.mark.asyncio
async def test_hung_input_keeps_segments_recorded_before_the_outage(
    tmp_path, source_ts
):
    source = LiveSource(source_ts)
    server = await asyncio.start_server(source.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    config = BroadcastConfig(
        network_id="hung-input",
        stream_url=f"http://127.0.0.1:{port}/stream.ts",
        duration_seconds=90,
        segment_duration=2,
        dvr_mode=True,
        hls_list_size=0,
    )
    proc = NetworkBroadcastProcess(config, str(tmp_path))
    events = []

    async def record_callback(event, data):
        events.append(event)

    proc._send_callback = record_callback
    playlist = os.path.join(proc.hls_dir, "live.m3u8")

    with (
        patch("src.broadcast_manager.settings.DVR_STALL_TIMEOUT_SECONDS", 10.0),
        patch("src.broadcast_manager.settings.DVR_RESTART_WINDOW_SECONDS", 120.0),
        patch.object(
            NetworkBroadcastProcess, "_DVR_STALL_KILL_GRACE_SECONDS", 2.0, create=True
        ),
    ):
        assert await proc.start()
        for _ in range(60):
            await asyncio.sleep(0.5)
            if len(_listed(playlist)) >= 4:
                break
        before_outage = _listed(playlist)
        assert len(before_outage) >= 4, "capture never got going"

        source.hanging = (
            True  # watchdog: SIGTERM at 10s; the old code sent another at 20s
        )
        await asyncio.sleep(30)
        source.hanging = False

        await asyncio.wait_for(proc._monitor_task, timeout=90)

    server.close()
    final = _listed(playlist)

    assert events == ["programme_ended"]
    missing = [s for s in before_outage if s not in final]
    assert not missing, f"segments recorded before the outage were dropped: {missing}"
    assert len(final) > len(before_outage), "nothing was recorded after the outage"
