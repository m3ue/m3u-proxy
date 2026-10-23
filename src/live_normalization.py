"""Compose the existing direct proxy and pooled FFmpeg, never rewrite handoff.

The inner HTTP body stays open while sources switch. Only the outer stream is
publicly counted; ownership is server state, not client-supplied metadata.
"""

import asyncio
import hashlib
from pathlib import Path
import stat
from urllib.parse import urlparse
from string import Formatter

from fastapi import HTTPException

from config import settings
from redis_config import should_use_pooling
from transcoding import get_profile_manager


# Fixed, server-owned Intel VAAPI policy. Decode remains software; only video
# encoding is uploaded. Never accept a client-supplied device or command.
VAAPI_DEVICE = "/dev/dri/renderD128"
VAAPI_PROFILE = (
    "-vaapi_device /dev/dri/renderD128 "
    "-analyzeduration 1000000 -probesize 1000000 "
    "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 2 -reconnect_max_retries 5 "
    "-fflags +genpts+discardcorrupt -merge_pmt_versions 1 "
    "-dts_delta_threshold 1 -i {input_url} "
    "-map 0:v:0 -map 0:a:0 "
    "-vf bwdif=mode=send_field:parity=auto:deint=interlaced,format=nv12,hwupload "
    "-fps_mode passthrough "
    "-af aresample=async=1:first_pts=0 "
    "-c:v h264_vaapi -rc_mode CQP -qp 18 -g 25 -bf 0 -c:a aac -b:a 128k "
    "-f mpegts {output_args}"
)


# Decode and field-aware bwdif stay on the CPU; only encoding uses QSV.
# Hardware decoding conceals truncated unbounded H.264 PES despite explode/ec=0.
# This is a static correctness-first preset, not a runtime software fallback.
QSV_DEVICE_ARGS = (
    "-init_hw_device", "vaapi=va:/dev/dri/renderD128",
    "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs",
)
# Strict H.264 recovery can loop on an initial broken access unit with frame
# threading. Serialize only this software input decoder, not the QSV encoder.
QSV_DECODING_ARGS = (
    "-hwaccel", "none", "-err_detect:v", "explode", "-ec:v", "0",
    "-threads:v", "1",
)
QSV_FILTER = (
    "bwdif=mode=send_field:parity=auto:deint=interlaced,"
    "format=nv12,hwupload=extra_hw_frames=64"
)
QSV_ENCODING_ARGS = (
    "-fps_mode", "passthrough", "-enc_time_base", "1/90000",
    "-c:v", "h264_qsv", "-global_quality", "18", "-g", "25", "-bf", "0",
)
QSV_PROFILE = (
    " ".join(QSV_DEVICE_ARGS) + " "
    "-analyzeduration 1000000 -probesize 1000000 "
    "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 2 -reconnect_max_retries 5 "
    "-fflags +genpts+discardcorrupt -merge_pmt_versions 1 "
    "-dts_delta_threshold 1 " + " ".join(QSV_DECODING_ARGS) + " -i {input_url} "
    "-map 0:v:0 -map 0:a:0 -vf " + QSV_FILTER + " "
    + " ".join(QSV_ENCODING_ARGS) + " "
    "-af aresample=async=1:first_pts=0 -c:a aac -b:a 128k "
    "-f mpegts {output_args}"
)


async def validate_qsv_encoder():
    """Exercise software decode/bwdif/QSV upload/encode before stream allocation.

    Generate a tiny compatible H.264 TFF fixture in RAM, never a provider URL or
    lossless H.264. Both child processes are bounded/reaped.
    No cached admission and no automatic software/VAAPI fallback.
    """
    if not _intel_device_available():
        raise HTTPException(503, "Intel QSV renderD128 is unavailable")
    fixture = None
    commands = (
        (
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=50:duration=0.16",
            "-vf", "tinterlace=mode=interleave_top,setfield=tff",
            "-frames:v", "4", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-flags", "+ilme+ildct",
            "-x264-params", "tff=1", "-an", "-f", "mpegts", "pipe:1",
        ),
        (*QSV_DEVICE_ARGS, *QSV_DECODING_ARGS, "-fflags", "+genpts+discardcorrupt",
         "-i", "pipe:0", "-vf", QSV_FILTER,
         *QSV_ENCODING_ARGS, "-an", "-f", "null", "-"),
    )
    for index, command in enumerate(commands):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", *command,
                stdin=asyncio.subprocess.PIPE if index else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL if index else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(
                process.communicate(input=fixture), timeout=10
            )
            if process.returncode != 0 or (index == 0 and not output):
                raise HTTPException(503, "Intel QSV H.264 pipeline probe failed")
            if index == 0:
                fixture = output
        except (OSError, asyncio.TimeoutError) as exc:
            raise HTTPException(503, "Intel QSV H.264 pipeline probe unavailable") from exc
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()


def _intel_device_available():
    try:
        return (
            stat.S_ISCHR(Path(VAAPI_DEVICE).stat().st_mode)
            and Path("/sys/class/drm/renderD128/device/vendor").read_text().strip().lower()
            == "0x8086"
        )
    except OSError:
        return False


async def validate_vaapi_encoder():
    """Fail closed before allocating streams; test actual upload + encoding.

    No provider traffic, no CPU fallback, no cached readiness after device loss.
    Reap the bounded local probe even when the requesting task is cancelled.
    """
    if not _intel_device_available():
        raise HTTPException(503, "Intel VAAPI renderD128 is unavailable")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-v", "error", "-nostdin",
            "-vaapi_device", VAAPI_DEVICE,
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-frames:v", "1", "-vf",
            "bwdif=mode=send_field:parity=auto:deint=interlaced,format=nv12,hwupload",
            "-fps_mode", "passthrough",
            "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "18",
            "-bf", "0", "-g", "25",
            "-an", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=10)
        if process.returncode != 0:
            raise HTTPException(503, "Intel VAAPI H.264 encoder probe failed")
    except (OSError, asyncio.TimeoutError) as exc:
        raise HTTPException(503, "Intel VAAPI H.264 encoder probe unavailable") from exc
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def create_normalized_stream(manager, payload, request):
    urls = [payload.url, *(payload.failover_urls or [])]
    if any(
        not manager._detect_stream_type(url)[2]
        or urlparse(url).path.lower().endswith((".mpd", ".m3u8", ".m3u"))
        for url in urls
    ):
        raise HTTPException(400, "Live normalization supports direct live MPEG-TS only")
    try:
        available = (
            not settings.REDIS_ENABLED
            and not manager._redis_requested
            and settings.ENABLE_SINGLE_PROCESS_TRANSCODING
            and should_use_pooling()
            and manager.pooled_manager is not None
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not available:
        raise HTTPException(
            400,
            "Live normalization requires single-process transcoding pooling without Redis",
        )

    # Do not trust Host/forwarded headers. The socket's server port is authoritative.
    server = request.scope.get("server") if request is not None else None
    if not server or not isinstance(server[1], int) or not 0 < server[1] < 65536:
        raise HTTPException(400, "Live normalization requires the ASGI listening port")
    encoder = payload.normalization_encoder
    parameters = settings.LIVE_NORMALIZE_PROFILE
    if encoder == "vaapi":
        await validate_vaapi_encoder()
        parameters = VAAPI_PROFILE
    elif encoder == "qsv":
        await validate_qsv_encoder()
        parameters = QSV_PROFILE
    try:
        fields = {
            name
            for _, name, _, _ in Formatter().parse(parameters)
            if name is not None
        }
        if not {"input_url", "output_args"}.issubset(fields) or fields - {
            "input_url",
            "output_args",
            "format",
        }:
            raise ValueError(
                "expected input_url/output_args and optional format placeholders"
            )
    except ValueError as exc:
        raise HTTPException(400, f"Invalid LIVE_NORMALIZE_PROFILE: {exc}") from exc
    profile = get_profile_manager().create_profile_from_template(
        name="live-normalize",
        parameters=parameters,
        description="Persistent direct-live output normalization",
    )
    # Include the effective normalizer profile in both private identities.
    salt = (
        f"live-normalize:{encoder}:"
        + hashlib.sha256(parameters.encode()).hexdigest()
    )
    options = payload.model_dump(exclude={"url", "normalize_live_output", "normalization_encoder"})
    options.update(
        strict_live_ts=True,
        use_sticky_session=True,
        end_session_on_reconnect=False,
        single_recovery_per_exit=True,
    )
    async with manager._normalization_lock:
        before = set(manager.streams)
        try:
            inner_id = await manager.get_or_create_stream(
                payload.url, **options, identity_salt=salt
            )
            inner = manager.streams[inner_id]
            # No recycling/restarting an existing owned encoder or selected B.
            if inner.normalization_parent_id in manager.streams:
                outer = manager.streams[inner.normalization_parent_id]
                outer.last_access = inner.last_access
                return outer.stream_id
            loopback = f"http://127.0.0.1:{server[1]}/stream/{inner_id}"
            args = profile.render(
                {"input_url": loopback, "output_args": "pipe:1", "format": "mpegts"}
            )
            outer_id = await manager.get_or_create_stream(
                loopback,
                metadata={
                    **(payload.metadata or {}),
                    "normalize_live_output": "true",
                    "normalization_encoder": encoder,
                },
                is_transcoded=True,
                transcode_profile=salt,
                transcode_ffmpeg_args=args,
                identity_salt=salt,
            )
            outer = manager.streams[outer_id]
            outer.normalization_input_id = inner_id
            inner.normalization_parent_id = outer_id
            return outer_id
        except BaseException:
            # Also roll back cancellation, including failure after an allocation.
            # No provider connections/encoder can exist before the initial GET.
            for stream_id in set(manager.streams) - before:
                manager.streams.pop(stream_id, None)
                manager.stream_clients.pop(stream_id, None)
                manager._stats.active_streams = max(
                    0, manager._stats.active_streams - 1
                )
            raise
