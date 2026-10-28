"""Scoped startup classification through the production stderr reader."""

import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pooled_stream_manager import SharedTranscodingProcess

QSV_PROFILE = "live-normalize:qsv:" + "a" * 64
PACKET_WARNING = (
    b"[vist#0:0/h264 @ 0x1] [dec:h264 @ 0x2] "
    b"Error submitting packet to decoder: Invalid data found when processing input\n"
)
DECODE_WARNING = (
    b"[vist#0:0/h264 @ 0x1] [dec:h264 @ 0x2] "
    b"Decoding error: Invalid data found when processing input\n"
)


async def monitor(*chunks, profile=QSV_PROFILE, status="running", resolver_type=None):
    pooled = SharedTranscodingProcess(
        "startup",
        "http://fixture.invalid/live.ts",
        profile,
        [],
        resolver_type=resolver_type,
    )
    pooled.status = status
    pooled.process = SimpleNamespace(
        stderr=SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b""])),
        returncode=None,
    )
    await pooled._log_stderr()
    return pooled


@pytest.fixture(scope="module")
def recovered_join_100():
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
            "testsrc2=size=320x180:rate=25:duration=6",
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
    joined = generated[: 188 * 3] + generated[188 * 60 :]
    decoded = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "warning",
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
            "-threads:v",
            "1",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-frames:v",
            "100",
            "-f",
            "framehash",
            "pipe:1",
        ],
        input=joined,
        capture_output=True,
        timeout=20,
    )
    assert decoded.returncode == 0, decoded.stderr.decode()
    assert (
        b"Error submitting packet to decoder: Invalid data found when processing input"
        in decoded.stderr
    )
    frames = [
        line
        for line in decoded.stdout.splitlines()
        if line and not line.startswith(b"#")
    ]
    assert len(frames) == 100
    return decoded.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["starting", "running"])
async def test_qsv_real_join_with_100_decoded_frames_is_not_input_failure(
    recovered_join_100, status
):
    pooled = await monitor(recovered_join_100, status=status)
    assert pooled.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "warning",
    [
        PACKET_WARNING,
        DECODE_WARNING,
        b"[dec:h264 @ 0xabc] Decoding error: Invalid data found when processing input\n",
    ],
)
async def test_exact_h264_decoder_diagnostics_are_scoped_nonfatal(warning):
    # Also exercise arbitrary stderr chunk boundaries and CR-delimited progress.
    pooled = await monitor(
        warning[:31],
        warning[31:],
        b"frame= 100 fps=25 q=18.0 size=512kB time=00:00:04.00 bitrate=1024kbits/s speed=1x\r",
    )
    assert pooled.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [
        "test",
        "qsv",
        "live-normalize:cpu:" + "a" * 64,
        "live-normalize:vaapi:" + "a" * 64,
        "live-normalize:qsv:",
        "live-normalize:qsv:not-a-generated-key",
        QSV_PROFILE + ":suffix",
    ],
)
@pytest.mark.parametrize("warning", [PACKET_WARNING, DECODE_WARNING])
async def test_other_profiles_keep_existing_fatal_classification(profile, warning):
    pooled = await monitor(warning, profile=profile)
    assert pooled.status == "input_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_type", ["streamlink", "ytdlp"])
async def test_resolver_stderr_cannot_use_qsv_decoder_exception(resolver_type):
    pooled = await monitor(PACKET_WARNING, resolver_type=resolver_type)
    assert pooled.status == "input_failed"


HARD_ERRORS = [
    b"Invalid data found when processing input\n",
    b"Error opening input file fixture.ts: Invalid data found when processing input\n",
    b"[mpegts @ 0x1] Invalid data found when processing input\n",
    b"[http @ 0x1] Invalid data found when processing input\n",
    b"[dec:hevc @ 0x1] Decoding error: Invalid data found when processing input\n",
    b"Decoding error: Invalid data found when processing input\n",
    b"[h264 @ 0x1] Invalid data found when processing input\n",
    b"Connection refused\n",
    b"Connection timed out\n",
    b"Server returned 404 Not Found\n",
    b"Server returned 503 Service Unavailable\n",
    b"Protocol not found\n",
    b"Failed to resolve hostname\n",
    PACKET_WARNING.rstrip() + b"; Error opening input fixture.ts\n",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_error", HARD_ERRORS)
@pytest.mark.parametrize("hard_first", [False, True])
async def test_hard_errors_survive_mixed_decoder_warnings_and_progress(
    hard_error, hard_first
):
    before, after = (
        (hard_error, PACKET_WARNING) if hard_first else (DECODE_WARNING, hard_error)
    )
    pooled = await monitor(
        before,
        after,
        PACKET_WARNING,
        b"frame=100 fps=25 bitrate=1024kbits/s speed=1x\r",
    )
    assert pooled.status == "input_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_first", [False, True])
async def test_write_errors_survive_decoder_warnings_and_progress(hard_first):
    hard_error = b"Error writing output: No space left on device\n"
    chunks = (
        (hard_error, PACKET_WARNING) if hard_first else (PACKET_WARNING, hard_error)
    )
    pooled = await monitor(
        *chunks, DECODE_WARNING, b"frame=100 fps=25 bitrate=1024kbits/s\r"
    )
    assert pooled.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "input_failed"])
async def test_preexisting_failure_is_never_reset_by_decoder_warning(status):
    pooled = await monitor(PACKET_WARNING, DECODE_WARNING, status=status)
    assert pooled.status == status


@pytest.mark.asyncio
async def test_qsv_decoder_warning_does_not_disable_no_output_failure_or_cleanup(
    monkeypatch,
):
    pooled = await monitor(PACKET_WARNING)
    assert pooled.output_timeout == 30
    monkeypatch.setattr(
        "pooled_stream_manager.time.time", lambda: pooled.last_chunk_time + 31
    )
    assert pooled.health_check() is False
    assert pooled.status == "failed"
    pooled.process.terminate = lambda: None
    pooled.process.wait = AsyncMock(return_value=0)
    await pooled.cleanup()
    pooled.process.wait.assert_awaited_once()
    assert pooled.status == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
async def test_qsv_decoder_warning_does_not_disable_exit_failure(returncode):
    pooled = await monitor(PACKET_WARNING)
    pooled.process.returncode = returncode
    assert pooled.health_check() is False
    assert pooled.status == "failed"
