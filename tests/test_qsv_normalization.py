"""Explicit QSV isolation and admission; no providers and no real GPU access."""

import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from tests import test_live_normalization as fixtures
from tests.test_live_normalization import payload, scope
import api
import live_normalization as normalization
from config import settings

manager = fixtures.manager


@pytest.mark.asyncio
async def test_qsv_private_ids_pool_reuse_and_server_owned_profile(
    manager, monkeypatch
):
    monkeypatch.setattr(normalization, "validate_vaapi_encoder", AsyncMock())
    probe = AsyncMock()
    monkeypatch.setattr(normalization, "validate_qsv_encoder", probe)
    streams = []
    for encoder in ("cpu", "vaapi", "qsv"):
        result = await api.create_stream(
            payload(normalize_live_output=True, normalization_encoder=encoder), scope()
        )
        streams.append(manager.streams[result["stream_id"]])
    assert len({s.stream_id for s in streams}) == 3
    assert len({s.normalization_input_id for s in streams}) == 3
    gpu = streams[-1]
    assert gpu.metadata["normalization_encoder"] == "qsv"
    args = gpu.transcode_ffmpeg_args
    assert args[args.index("-hwaccel") + 1] == "none"
    assert args[args.index("-err_detect:v") + 1] == "explode"
    assert args[args.index("-ec:v") + 1] == "0"
    assert args.index("-ec:v") < args.index("-i")
    assert "hwdownload" not in normalization.QSV_FILTER
    assert "-hwaccel_output_format" not in args
    assert args[args.index("-c:v") + 1] == "h264_qsv"
    assert args[args.index("-vf") + 1] == normalization.QSV_FILTER
    assert args[args.index("-global_quality") + 1] == "18"
    assert args[args.index("-bf") + 1] == "0"
    assert args[args.index("-enc_time_base") + 1] == "1/90000"
    assert "-r" not in args and "-b:v" not in args and "-look_ahead" not in args
    monkeypatch.setattr(
        settings, "LIVE_NORMALIZE_PROFILE", "invalid CPU custom template"
    )
    reused = await api.create_stream(
        payload(normalize_live_output=True, normalization_encoder="qsv"), scope()
    )
    assert reused["stream_id"] == gpu.stream_id
    assert probe.await_count == 2
    # Effective QSV profile changes isolate both the player and private input.
    monkeypatch.setattr(
        normalization,
        "QSV_PROFILE",
        normalization.QSV_PROFILE.replace("-global_quality 18", "-global_quality 19"),
    )
    changed = await api.create_stream(
        payload(normalize_live_output=True, normalization_encoder="qsv"), scope()
    )
    other = manager.streams[changed["stream_id"]]
    assert other.stream_id != gpu.stream_id
    assert other.normalization_input_id != gpu.normalization_input_id


@pytest.mark.asyncio
async def test_qsv_serializes_only_software_input_decoder(manager, monkeypatch):
    """Strict H.264 join recovery must not enter FFmpeg's frame-thread loop."""
    monkeypatch.setattr(normalization, "validate_vaapi_encoder", AsyncMock())
    monkeypatch.setattr(normalization, "validate_qsv_encoder", AsyncMock())
    for encoder in ("cpu", "vaapi", "qsv"):
        result = await api.create_stream(
            payload(normalize_live_output=True, normalization_encoder=encoder), scope()
        )
        args = manager.streams[result["stream_id"]].transcode_ffmpeg_args
        input_at = args.index("-i")
        if encoder == "qsv":
            assert args[:input_at].count("-threads:v") == 1
            assert args[args.index("-threads:v") + 1] == "1"
            assert "-threads:v" not in args[input_at:]
            assert args[args.index("-hwaccel") + 1] == "none"
            assert args[args.index("-err_detect:v") + 1] == "explode"
            assert args[args.index("-ec:v") + 1] == "0"
        else:
            assert "-threads:v" not in args


@pytest.mark.asyncio
async def test_qsv_off_does_not_probe_or_change_direct_id(manager, monkeypatch):
    probe = AsyncMock(side_effect=AssertionError("off must not probe GPU"))
    monkeypatch.setattr(normalization, "validate_qsv_encoder", probe)
    a = await api.create_stream(payload(normalize_live_output=False), scope())
    b = await api.create_stream(
        payload(normalize_live_output=False, normalization_encoder="qsv"), scope()
    )
    assert a["stream_id"] == b["stream_id"]
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_qsv_missing_device_http_503_no_allocations(manager, monkeypatch):
    monkeypatch.setattr(settings, "API_TOKEN", "")
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: False)
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://fixture:12345"
    ) as client:
        result = await client.post(
            "/streams",
            json={
                "url": "http://fixture/a.ts",
                "normalize_live_output": True,
                "normalization_encoder": "qsv",
            },
        )
    assert result.status_code == 503
    assert not manager.streams and not manager.stream_clients
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,failure",
    [
        (stage, failure)
        for stage in (0, 1)
        for failure in ("exit", "missing", "timeout", "cancel", "empty")
        if not (stage == 1 and failure == "empty")
    ],
)
async def test_qsv_probe_failures_reap_and_allocate_nothing(
    manager, monkeypatch, stage, failure
):
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: True)
    good = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(b"local-fixture", None))
    )
    process = SimpleNamespace(
        returncode=1 if failure == "exit" else None,
        communicate=AsyncMock(return_value=(b"", None)),
        kill=Mock(),
        wait=AsyncMock(return_value=1),
    )
    if failure in ("timeout", "cancel"):
        process.communicate.side_effect = (
            asyncio.TimeoutError() if failure == "timeout" else asyncio.CancelledError()
        )
    if failure == "empty":
        process.returncode = 0
    sequence = [good] * stage + [
        FileNotFoundError() if failure == "missing" else process
    ]
    spawn = AsyncMock(side_effect=sequence)
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else HTTPException
    ) as error:
        await api.create_stream(
            payload(normalize_live_output=True, normalization_encoder="qsv"), scope()
        )
    if failure != "cancel":
        assert error.value.status_code == 503
    assert not manager.streams and not manager.stream_clients
    assert process.kill.call_count == (1 if failure in ("timeout", "cancel") else 0)
    if failure in ("timeout", "cancel"):
        process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_qsv_admission_exercises_exact_static_software_decode_qsv_encode_pipeline(
    monkeypatch,
):
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: True)
    source = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"fixture", None)),
        kill=Mock(),
    )
    encoder = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(None, None)), kill=Mock()
    )
    spawn = AsyncMock(side_effect=[source, encoder])
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    await normalization.validate_qsv_encoder()
    generator, hardware = (c.args for c in spawn.await_args_list)
    assert generator[generator.index("-c:v") + 1] == "libx264"
    assert "tff=1" in generator and "18" in generator
    template = shlex.split(normalization.QSV_PROFILE)
    for option in (
        "-hwaccel",
        "-err_detect:v",
        "-ec:v",
        "-threads:v",
        "-fflags",
        "-vf",
        "-c:v",
        "-global_quality",
        "-enc_time_base",
        "-bf",
    ):
        assert (
            hardware[hardware.index(option) + 1] == template[template.index(option) + 1]
        )
    assert hardware[hardware.index("-hwaccel") + 1] == "none"
    assert "-hwaccel_output_format" not in hardware
    assert hardware.index("-ec:v") < hardware.index("-i")
    assert hardware[hardware.index("-i") + 1] == "pipe:0"
    encoder.communicate.assert_awaited_once_with(input=b"fixture")
    source.kill.assert_not_called()
    encoder.kill.assert_not_called()
