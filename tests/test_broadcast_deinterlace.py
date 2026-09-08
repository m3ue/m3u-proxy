"""Tests for the `deinterlace` (yadif) option in broadcast FFmpeg commands.

Interlaced MPEG-2 sources (ATSC OTA / HDHomeRun) need deinterlacing when the
proxy transcodes them to H.264. `deinterlace` adds `yadif` to the video filter
chain, and only takes effect when `transcode=True` (a stream copy cannot filter).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def _build_cmd(**overrides) -> list:
    defaults = dict(
        network_id="test-net",
        stream_url="http://example.com/stream.ts",
    )
    defaults.update(overrides)
    config = BroadcastConfig(**defaults)
    proc = NetworkBroadcastProcess.__new__(NetworkBroadcastProcess)
    proc.config = config
    proc.hls_dir = "/tmp/hls"
    return proc._build_ffmpeg_command()


def _vf(cmd):
    """Return the -vf filter chain from an FFmpeg argv, or None."""
    for i, v in enumerate(cmd):
        if v == "-vf" and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def test_no_deinterlace_by_default():
    cmd = _build_cmd(transcode=True)
    assert _vf(cmd) is None


def test_deinterlace_adds_yadif_when_transcoding():
    cmd = _build_cmd(transcode=True, deinterlace=True)
    assert _vf(cmd) == "yadif"


def test_deinterlace_combined_with_scale_in_single_vf():
    """FFmpeg only honours the last -vf, so yadif + scale must share one chain."""
    cmd = _build_cmd(transcode=True, deinterlace=True, video_resolution="1280:720")
    assert _vf(cmd) == "yadif,scale=1280:720"
    # Exactly one -vf flag.
    assert cmd.count("-vf") == 1


def test_deinterlace_ignored_without_transcode():
    """Stream-copy mode cannot filter; deinterlace is a no-op and -c:v copy stays."""
    cmd = _build_cmd(transcode=False, deinterlace=True)
    assert _vf(cmd) is None
    assert "copy" in cmd
