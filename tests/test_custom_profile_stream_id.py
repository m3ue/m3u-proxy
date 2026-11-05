"""Two custom (template) profiles for the same source must not share an ID.

Every user template resolves to the profile name "custom". Before the
fingerprint, a copy/remux HLS template and a GPU-transcode HLS template on the
same URL produced the same stream ID and pooling key, so the second request was
handed the first stream's process.
"""

import hashlib

import pytest
import pytest_asyncio

from config import settings
from stream_manager import StreamManager

URL = "http://provider.example/live/u/p/123.ts"

PASSTHROUGH = ["-i", URL, "-c:v", "copy", "-c:a", "copy", "-f", "hls", "pipe:1"]
REMOTE_720P = [
    "-hwaccel",
    "cuda",
    "-hwaccel_output_format",
    "cuda",
    "-i",
    URL,
    "-vf",
    "scale_cuda=-2:720",
    "-c:v",
    "h264_nvenc",
    "-b:v",
    "3500k",
    "-c:a",
    "aac",
    "-f",
    "hls",
    "pipe:1",
]


def test_fingerprint_only_applies_to_custom_profiles():
    assert StreamManager._custom_profile_fingerprint("720p", REMOTE_720P) == ""
    assert StreamManager._custom_profile_fingerprint(None, REMOTE_720P) == ""
    assert StreamManager._custom_profile_fingerprint("custom", None) == ""
    assert StreamManager._custom_profile_fingerprint("custom", REMOTE_720P) != ""


def test_fingerprint_is_stable_and_distinguishes_templates():
    a = StreamManager._custom_profile_fingerprint("custom", PASSTHROUGH)
    assert a == StreamManager._custom_profile_fingerprint("custom", list(PASSTHROUGH))
    assert a != StreamManager._custom_profile_fingerprint("custom", REMOTE_720P)


def test_fingerprint_not_fooled_by_argument_boundaries():
    # ["-a", "b c"] and ["-a b", "c"] join to the same text with spaces.
    one = StreamManager._custom_profile_fingerprint("custom", ["-a", "b c"])
    two = StreamManager._custom_profile_fingerprint("custom", ["-a b", "c"])
    assert one != two


@pytest_asyncio.fixture
async def manager(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "WORKER_ID", None)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    m = StreamManager(enable_pooling=False)
    yield m
    await m.stop()


@pytest.mark.asyncio
async def test_two_custom_hls_profiles_get_distinct_stream_ids(manager):
    passthrough_id = await manager.get_or_create_stream(
        URL,
        is_transcoded=True,
        transcode_profile="custom",
        transcode_ffmpeg_args=PASSTHROUGH,
    )
    remote_id = await manager.get_or_create_stream(
        URL,
        is_transcoded=True,
        transcode_profile="custom",
        transcode_ffmpeg_args=REMOTE_720P,
    )
    assert passthrough_id != remote_id
    assert manager.streams[remote_id].transcode_ffmpeg_args == REMOTE_720P

    # The same template still pools onto the same stream.
    again = await manager.get_or_create_stream(
        URL,
        is_transcoded=True,
        transcode_profile="custom",
        transcode_ffmpeg_args=list(PASSTHROUGH),
    )
    assert again == passthrough_id


def test_pooling_keys_differ_for_two_custom_hls_profiles():
    from pooled_stream_manager import PooledStreamManager

    pm = PooledStreamManager.__new__(PooledStreamManager)
    k1 = pm._generate_stream_key(URL, "custom", PASSTHROUGH)
    k2 = pm._generate_stream_key(URL, "custom", REMOTE_720P)
    assert k1 != k2
    assert k1 == pm._generate_stream_key(URL, "custom", list(PASSTHROUGH))
    # Predefined profiles keep their historical keys.
    expected = hashlib.sha256(f"{URL}|720p|hls".encode()).hexdigest()[:16]
    assert pm._generate_stream_key(URL, "720p", REMOTE_720P) == expected


@pytest.mark.asyncio
async def test_predefined_profile_keeps_historical_stream_id(manager):
    stream_id = await manager.get_or_create_stream(
        URL,
        is_transcoded=True,
        transcode_profile="720p",
        transcode_ffmpeg_args=REMOTE_720P,
    )
    assert stream_id == hashlib.md5(f"{URL}|transcoded|720p|hls".encode()).hexdigest()
