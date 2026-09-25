import sys
import os
import pytest

# Add src to path so we can import our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pooled_stream_manager
from hls_input import hls_extension_args, is_hls_url
from pooled_stream_manager import SharedTranscodingProcess


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/live/index.m3u8", True),
        ("https://example.com/live/INDEX.M3U8?token=abc", True),
        ("http://192.168.11.22:8096/Videos/682/stream.ts?static=true", False),
        ("https://example.com/live/stream?type=m3u8", False),
        (None, False),
    ],
)
def test_is_hls_url_checks_path_only(url, expected):
    assert is_hls_url(url) is expected


def test_hls_extension_args_only_for_hls():
    args = hls_extension_args("https://example.com/index.m3u8")
    assert args[args.index("-allowed_segment_extensions") + 1] == "ALL"
    assert args[args.index("-extension_picky") + 1] == "0"
    assert hls_extension_args("https://example.com/stream.ts") == []


async def _captured_transcode_cmd(monkeypatch, url):
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        raise RuntimeError("stop before launching ffmpeg")

    monkeypatch.setattr(
        pooled_stream_manager.asyncio, "create_subprocess_exec", fake_exec
    )
    proc = SharedTranscodingProcess(
        stream_id="hlsext",
        url=url,
        profile="default",
        ffmpeg_args=["-i", "{input}", "-c:v", "libx264", "-c:a", "aac"],
    )
    await proc.start_process()
    return captured["cmd"]


@pytest.mark.asyncio
async def test_transcode_hls_input_relaxes_segment_extension_checks(monkeypatch):
    url = "https://example.com/stream/index.m3u8?token=abc"
    cmd = await _captured_transcode_cmd(monkeypatch, url)

    before_input = cmd[: cmd.index("-i")]
    assert "-allowed_extensions" in before_input
    assert "-allowed_segment_extensions" in before_input
    assert before_input[before_input.index("-extension_picky") + 1] == "0"
    assert cmd[cmd.index("-i") + 1] == url


@pytest.mark.asyncio
async def test_transcode_non_hls_input_has_no_hls_options(monkeypatch):
    cmd = await _captured_transcode_cmd(
        monkeypatch, "http://192.168.11.22:8096/Videos/682/stream.ts?static=true"
    )

    for opt in (
        "-allowed_extensions",
        "-allowed_segment_extensions",
        "-extension_picky",
    ):
        assert opt not in cmd
