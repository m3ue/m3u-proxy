"""Encoder contract and fail-closed admission, without provider/device I/O."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import api
import live_normalization as normalization
from config import settings
from tests import test_live_normalization as fixtures
from tests.test_live_normalization import payload, scope

manager = fixtures.manager


@pytest.mark.asyncio
async def test_encoder_isolates_both_identities_and_preserves_cpu_custom(manager, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(normalization, "validate_vaapi_encoder", probe)
    cpu = await api.create_stream(payload(normalize_live_output=True), scope())
    gpu = await api.create_stream(payload(normalize_live_output=True, normalization_encoder="vaapi"), scope())
    c, g = (manager.streams[item["stream_id"]] for item in (cpu, gpu))
    assert c.stream_id != g.stream_id
    assert c.normalization_input_id != g.normalization_input_id
    assert "libx264" in c.transcode_ffmpeg_args
    assert "h264_vaapi" in g.transcode_ffmpeg_args
    assert "bwdif=mode=send_field:parity=auto:deint=interlaced,format=nv12,hwupload" in g.transcode_ffmpeg_args
    assert not any(flag in g.transcode_ffmpeg_args for flag in ("-hwaccel", "-preset", "-tune", "libx264"))
    assert "-merge_pmt_versions 1" in " ".join(g.transcode_ffmpeg_args)
    assert "aresample=async=1:first_pts=0" in g.transcode_ffmpeg_args
    assert g.metadata["normalization_encoder"] == "vaapi"
    reused = await api.create_stream(payload(normalize_live_output=True, normalization_encoder="vaapi"), scope())
    assert reused["stream_id"] == g.stream_id
    monkeypatch.setattr(settings, "LIVE_NORMALIZE_PROFILE", "-i {input_url} -vf null -c:v libx264 -crf 22 -metadata title=custom -f mpegts {output_args}")
    custom = await api.create_stream(payload(normalize_live_output=True), scope())
    assert custom["stream_id"] != c.stream_id
    custom_args = manager.streams[custom["stream_id"]].transcode_ffmpeg_args
    assert "title=custom" in custom_args
    assert custom_args[custom_args.index("-vf") + 1] == "null"
    assert custom_args[custom_args.index("-crf") + 1] == "22"
    assert custom_args.count("-vf") == custom_args.count("-crf") == 1
    assert "-fps_mode" not in custom_args
    assert (await api.create_stream(payload(normalize_live_output=True, normalization_encoder="vaapi"), scope()))["stream_id"] == g.stream_id


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder", ["cpu", "vaapi", "qsv"])
async def test_outer_encoder_metadata_unspoofable(manager, monkeypatch, encoder):
    monkeypatch.setattr(normalization, "validate_vaapi_encoder", AsyncMock())
    monkeypatch.setattr(normalization, "validate_qsv_encoder", AsyncMock())
    request = payload(normalize_live_output=True, normalization_encoder=encoder)
    request.metadata.update(normalization_encoder="forged", normalize_live_output="false")
    result = await api.create_stream(request, scope())
    assert result["metadata"]["normalization_encoder"] == encoder
    assert result["metadata"]["normalize_live_output"] == "true"


@pytest.mark.asyncio
async def test_default_and_explicit_cpu_reuse_and_off_ignores_encoder(manager, monkeypatch):
    probe = AsyncMock(side_effect=AssertionError("CPU/off must not probe GPU"))
    monkeypatch.setattr(normalization, "validate_vaapi_encoder", probe)
    default = await api.create_stream(payload(normalize_live_output=True), scope())
    explicit = await api.create_stream(payload(normalize_live_output=True, normalization_encoder="cpu"), scope())
    assert default["stream_id"] == explicit["stream_id"]
    # Older metadata has no encoder marker: no accidental CPU -> GPU inference.
    manager.streams[default["stream_id"]].metadata.pop("normalization_encoder")
    assert (await api.create_stream(payload(normalize_live_output=True), scope()))["stream_id"] == default["stream_id"]
    raw = await api.create_stream(payload(normalize_live_output=False), scope())
    off = await api.create_stream(payload(normalize_live_output=False, normalization_encoder="vaapi"), scope())
    assert raw["stream_id"] == off["stream_id"]
    probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder", ["nvenc", "QSV", "VAAPI", "", None, "/dev/dri/renderD129"])
async def test_invalid_encoder_http_422_before_allocation(manager, monkeypatch, encoder):
    monkeypatch.setattr(settings, "API_TOKEN", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture:12345") as client:
        result = await client.post("/streams", json={"url": "http://fixture/a.ts", "normalize_live_output": True, "normalization_encoder": encoder})
    assert result.status_code == 422
    assert not manager.streams


@pytest.mark.asyncio
async def test_missing_gpu_fails_before_resources(manager, monkeypatch):
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: False)
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(HTTPException) as error:
        await api.create_stream(payload(normalize_live_output=True, normalization_encoder="vaapi"), scope())
    assert error.value.status_code == 503
    assert not manager.streams and not manager.stream_clients
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "missing", "timeout", "cancel"])
async def test_unusable_gpu_probe_fails_closed_and_reaps(manager, monkeypatch, failure):
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: True)
    process = SimpleNamespace(returncode=1 if failure == "exit" else None)
    process.kill = Mock()
    process.wait = AsyncMock(return_value=1)
    if failure == "timeout":
        process.wait.side_effect = [asyncio.TimeoutError(), 1]
    elif failure == "cancel":
        process.wait.side_effect = [asyncio.CancelledError(), 1]
    spawn = AsyncMock(return_value=process)
    if failure == "missing":
        spawn.side_effect = FileNotFoundError()
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else HTTPException) as error:
        await api.create_stream(payload(normalize_live_output=True, normalization_encoder="vaapi"), scope())
    if failure != "cancel":
        assert error.value.status_code == 503
    assert not manager.streams and not manager.stream_clients
    assert process.kill.call_count == (1 if failure in ("timeout", "cancel") else 0)


@pytest.mark.asyncio
async def test_successful_gpu_probe_fixed_local_command(monkeypatch):
    monkeypatch.setattr(normalization, "_intel_device_available", lambda: True)
    process = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0), kill=Mock())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(normalization.asyncio, "create_subprocess_exec", spawn)
    await normalization.validate_vaapi_encoder()
    args = spawn.await_args.args
    assert args[0] == "ffmpeg"
    assert args[args.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert args[args.index("-c:v") + 1] == "h264_vaapi"
    assert args[args.index("-i") + 1] == "testsrc2=size=320x180:rate=25"
    process.kill.assert_not_called()


@pytest.mark.parametrize("mode,vendor,expected", [(0o020660, "0x8086\n", True), (0o100644, "0x8086", False), (0o020660, "0x10de", False)])
def test_device_must_be_intel_character_device(monkeypatch, mode, vendor, expected):
    monkeypatch.setattr(normalization.Path, "stat", lambda self: SimpleNamespace(st_mode=mode))
    monkeypatch.setattr(normalization.Path, "read_text", lambda self: vendor)
    assert normalization._intel_device_available() is expected


def test_device_missing_or_inaccessible(monkeypatch):
    def missing(self):
        raise PermissionError()
    monkeypatch.setattr(normalization.Path, "stat", missing)
    assert normalization._intel_device_available() is False
