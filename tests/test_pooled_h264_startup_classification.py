"""A recoverable H.264 join error is not proof that the input has failed."""

import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pooled_stream_manager import SharedTranscodingProcess


@pytest.fixture(scope="module")
def recovered_join():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg required for real H.264 startup reproduction")
    generated = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-g",
            "25",
            "-bf",
            "0",
            "-an",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    # Retain PAT/PMT, join during the first GOP, before the next complete IDR.
    joined = generated[: 188 * 3] + generated[188 * 60 :]
    decoded = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "warning",
            # A small probe makes the join warning reach the decoder, rather than
            # being consumed entirely during probing. No production options change.
            "-analyzeduration",
            "1",
            "-probesize",
            "2048",
            "-hwaccel",
            "none",
            "-err_detect:v",
            "explode",
            "-ec:v",
            "0",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-f",
            "framehash",
            "pipe:1",
        ],
        input=joined,
        capture_output=True,
        timeout=20,
    )
    assert decoded.returncode == 0, decoded.stderr.decode()
    assert b"Decoding error: Invalid data found when processing input" in decoded.stderr
    frames = [
        line
        for line in decoded.stdout.splitlines()
        if line and not line.startswith(b"#")
    ]
    assert len(frames) > 0
    return decoded.stderr, len(frames)


async def monitor(diagnostic, status="running"):
    pooled = SharedTranscodingProcess(
        "startup", "http://fixture.invalid/live.ts", "test", []
    )
    pooled.status = status
    # Keep the process alive while draining stderr, as during a real join.
    pooled.process = SimpleNamespace(
        stderr=SimpleNamespace(read=AsyncMock(side_effect=[diagnostic, b""])),
        returncode=None,
    )
    await pooled._log_stderr()
    return pooled


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["starting", "running"])
async def test_real_join_can_decode_frames_but_baseline_still_marks_input_failed(
    recovered_join, status
):
    diagnostic, frames = recovered_join
    pooled = await monitor(diagnostic, status)
    assert frames > 0
    # Characterization, NOT approval of this classification. The same text can
    # also accompany a non-recovering decoder; do not whitelist it globally.
    assert pooled.status == "input_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        b"Invalid data found when processing input\n",
        b"Error opening input file fixture.ts: Invalid data found when processing input\n",
        b"Connection refused\n",
        b"Connection timed out\n",
        b"Server returned 404 Not Found\n",
        b"Server returned 503 Service Unavailable\n",
        b"Protocol not found\n",
    ],
)
async def test_genuine_or_unscoped_input_errors_remain_fatal(diagnostic):
    pooled = await monitor(diagnostic)
    assert pooled.status == "input_failed"


@pytest.mark.asyncio
async def test_progress_does_not_erase_an_input_failure():
    pooled = await monitor(
        b"[vist#0:0/h264 @ 0x1] [dec:h264 @ 0x2] "
        b"Decoding error: Invalid data found when processing input\n"
        b"frame=   50 fps=25 q=18.0 size=512kB time=00:00:02.00 bitrate=2048kbits/s speed=1x\r"
    )
    assert pooled.status == "input_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        b"[h264 @ 0x1] non-existing PPS 0 referenced\n",
        b"[h264 @ 0x1] decode_slice_header error\n",
        b"[http @ 0x1] Error reading HTTP response: End of file\n",
    ],
)
async def test_join_warnings_without_invalid_data_are_not_input_failures(diagnostic):
    pooled = await monitor(diagnostic)
    assert pooled.status == "running"
