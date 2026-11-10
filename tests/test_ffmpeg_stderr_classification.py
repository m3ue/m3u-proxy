"""Input vs write error classification in the pooled ffmpeg stderr reader (#103)."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pooled_stream_manager import SharedTranscodingProcess


async def monitor(*chunks, status="running"):
    pooled = SharedTranscodingProcess(
        "classify",
        "http://fixture.invalid/live.ts",
        "default",
        [],
    )
    pooled.status = status
    pooled.process = SimpleNamespace(
        stderr=SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b""])),
        returncode=None,
    )
    await pooled._log_stderr()
    return pooled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        # Exact line from issue #103 (mid-stream upstream loss).
        b"[in#0/mpegts @ 0xffffb5085520] Error during demuxing: I/O error\n",
        b"[in#0/hls @ 0x1] Error during demuxing: Input/output error\n",
        b"[in#1/mpegts @ 0x1] Error retrieving a packet from demuxer: I/O error\n",
    ],
)
async def test_input_context_io_errors_trip_failover(line, caplog):
    with caplog.at_level(logging.ERROR, logger="pooled_stream_manager"):
        pooled = await monitor(b"frame=100 fps=25 bitrate=1024kbits/s speed=1x\r", line)

    assert pooled.status == "input_failed"
    assert "input error detected" in caplog.text
    assert "write error detected" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        b"[out#0/mpegts @ 0x1] Error writing trailer: I/O error\n",
        b"av_interleaved_write_frame(): I/O error\n",
        b"Error writing output: No space left on device\n",
        b"[hls @ 0x1] /tmp/hls/seg_001.ts: Permission denied\n",
    ],
)
async def test_output_and_unprefixed_write_errors_stay_failed(line):
    pooled = await monitor(line)
    assert pooled.status == "failed"


@pytest.mark.asyncio
async def test_open_time_input_errors_unchanged():
    pooled = await monitor(
        b"[in#0 @ 0x1] Error opening input: Server returned 404 Not Found\n"
    )
    assert pooled.status == "input_failed"
