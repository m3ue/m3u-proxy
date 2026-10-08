"""
Tests for DVR capture resilience in NetworkBroadcastProcess / BroadcastManager.

A DVR recording used to fail outright on any upstream drop: the stderr input-error
check reported broadcast_failed immediately, and the editor's retry restarted
FFmpeg as a programme "transition" that rewrote live.m3u8 without the earlier
segments (which broadcast GC then deleted as orphans). DVR captures now:

- write an EVENT playlist (seekable from the start while still recording),
- append to the existing playlist on every (re)start via append_list,
- restart FFmpeg in place for what's left of the duration, inside a continuous
  outage window that resets whenever a new segment lands,
- terminate a capture that stalls without exiting so it gets restarted too.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broadcast_manager import (
    BroadcastConfig,
    BroadcastManager,
    NetworkBroadcastProcess,
)


def _make_process(tmp_path, **config_overrides) -> NetworkBroadcastProcess:
    defaults = dict(
        network_id="rec-1",
        stream_url="http://example.com/stream.ts",
        dvr_mode=True,
        hls_list_size=0,
        duration_seconds=1800,
    )
    defaults.update(config_overrides)
    proc = NetworkBroadcastProcess(BroadcastConfig(**defaults), str(tmp_path))
    proc.hls_dir = str(tmp_path)
    return proc


def _fake_process(returncode=None):
    process = MagicMock()
    process.returncode = returncode
    process.pid = 1234
    process.wait = AsyncMock(return_value=returncode)
    process.stderr = None
    return process


def _write_playlist(tmp_path, segments):
    lines = ["#EXTM3U", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    for name in segments:
        lines += ["#EXTINF:6.0,", name]
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "live.m3u8").write_text("\n".join(lines) + "\n")


def test_dvr_command_writes_event_playlist_and_appends(tmp_path):
    cmd = _make_process(tmp_path)._build_ffmpeg_command()

    assert cmd[cmd.index("-hls_playlist_type") + 1] == "event"
    flags = cmd[cmd.index("-hls_flags") + 1].split("+")
    assert "append_list" in flags
    assert "delete_segments" not in flags


def test_live_broadcast_command_is_unchanged(tmp_path):
    proc = _make_process(tmp_path, dvr_mode=False, hls_list_size=20)
    cmd = proc._build_ffmpeg_command()

    assert "-hls_playlist_type" not in cmd
    flags = cmd[cmd.index("-hls_flags") + 1].split("+")
    assert "append_list" not in flags
    assert "delete_segments" in flags


def test_start_number_is_zero_when_playlist_lists_segments(tmp_path):
    # append_list advances past listed segments itself; a non-zero start would skip ahead.
    _write_playlist(tmp_path, ["live000000.ts", "live000001.ts"])
    cmd = _make_process(tmp_path)._build_ffmpeg_command()

    assert cmd[cmd.index("-start_number") + 1] == "0"


def test_start_number_continues_after_loose_segments_without_playlist(tmp_path):
    for name in ["live000000.ts", "live000007.ts"]:
        (tmp_path / name).write_bytes(b"x")

    assert _make_process(tmp_path)._hls_start_number() == 8


def test_start_number_is_zero_for_a_new_recording(tmp_path):
    assert _make_process(tmp_path)._hls_start_number() == 0


@pytest.mark.asyncio
async def test_dvr_start_keeps_existing_segments(tmp_path):
    _write_playlist(tmp_path, ["live000000.ts", "live000001.ts"])
    proc = _make_process(tmp_path)

    with (
        patch(
            "src.broadcast_manager.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_fake_process()),
        ),
        patch.object(proc, "_log_stderr", AsyncMock()),
        patch.object(proc, "_monitor_process", AsyncMock()),
        patch.object(proc, "_poll_bytes", AsyncMock()),
    ):
        assert await proc.start()

    assert (tmp_path / "live000000.ts").exists()
    assert (tmp_path / "live.m3u8").exists()
    assert proc._deadline is not None


@pytest.mark.asyncio
async def test_dvr_input_error_does_not_fail_the_recording(tmp_path):
    proc = _make_process(tmp_path)
    process = MagicMock()
    process.returncode = None
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(
        side_effect=[b"Server returned 503 Service Unavailable\n", b""]
    )
    proc._send_callback = AsyncMock()

    await proc._log_stderr(process)

    proc._send_callback.assert_not_called()
    assert proc.status != "failed"
    assert "503" in proc.error_message


@pytest.mark.asyncio
async def test_restart_records_only_the_remaining_duration(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process(returncode=1)
    proc._deadline = time.monotonic() + 600
    new_process = _fake_process()

    with (
        patch("src.broadcast_manager.asyncio.sleep", AsyncMock()),
        patch(
            "src.broadcast_manager.asyncio.create_subprocess_exec",
            AsyncMock(return_value=new_process),
        ) as spawn,
        patch.object(proc, "_log_stderr", AsyncMock()),
    ):
        assert await proc._restart_dvr_capture()

    cmd = list(spawn.call_args.args)
    assert 590 <= int(cmd[cmd.index("-t") + 1]) <= 601
    assert proc.process is new_process
    assert proc._restart_count == 1


@pytest.mark.asyncio
async def test_restart_gives_up_after_the_outage_window(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process(returncode=1)
    proc._deadline = time.monotonic() + 600
    proc._outage_started_at = time.monotonic() - 61

    with patch(
        "src.broadcast_manager.asyncio.create_subprocess_exec", AsyncMock()
    ) as spawn:
        assert not await proc._restart_dvr_capture()

    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_exit_near_the_deadline_reports_programme_ended(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process(returncode=1)
    proc._deadline = time.monotonic() + 3
    proc._send_callback = AsyncMock()

    await proc._monitor_process()

    assert proc._send_callback.call_args.args[0] == "programme_ended"
    assert proc.status == "stopped"


@pytest.mark.asyncio
async def test_exhausted_outage_reports_broadcast_failed(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process(returncode=1)
    proc._deadline = time.monotonic() + 600
    proc._outage_started_at = time.monotonic() - 61
    proc._send_callback = AsyncMock()

    await proc._monitor_process()

    assert proc._send_callback.call_args.args[0] == "broadcast_failed"


@pytest.mark.asyncio
async def test_new_segment_resets_the_outage_window(tmp_path):
    proc = _make_process(tmp_path)
    proc._outage_started_at = time.monotonic() - 30
    proc._restart_count = 3
    (tmp_path / "live000004.ts").write_bytes(b"x")

    async def stop_after_one_poll(_interval):
        proc._stopping = True

    with patch("src.broadcast_manager.asyncio.sleep", stop_after_one_poll):
        await proc._poll_bytes()

    assert proc._outage_started_at is None
    assert proc._restart_count == 0


def test_stalled_capture_is_terminated(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process()
    proc._last_segment_at = time.monotonic() - 31

    proc._terminate_if_stalled()

    proc.process.terminate.assert_called_once()
    assert proc._outage_started_at is not None


def test_healthy_capture_is_left_alone(tmp_path):
    proc = _make_process(tmp_path)
    proc.process = _fake_process()
    proc._last_segment_at = time.monotonic() - 5

    proc._terminate_if_stalled()

    proc.process.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_manager_restart_of_dvr_recording_resumes_instead_of_transitioning(
    tmp_path,
):
    manager = BroadcastManager(hls_base_dir=str(tmp_path / "hls"))
    manager.START_FAILURE_GRACE = 0
    existing = MagicMock()
    existing.stop = AsyncMock(return_value=12)
    manager.broadcasts["rec-1"] = existing

    config = BroadcastConfig(
        network_id="rec-1",
        stream_url="http://example.com/stream.ts",
        dvr_mode=True,
    )
    with (
        patch.object(NetworkBroadcastProcess, "start", AsyncMock(return_value=True)),
        patch(
            "src.broadcast_manager.settings.DVR_RECORDING_DIR", str(tmp_path / "dvr")
        ),
    ):
        await manager.start_broadcast(config)

    assert config.segment_start_number == 0
    assert config.add_discontinuity is False
    existing.cleanup_orphaned_segments.assert_not_called()
