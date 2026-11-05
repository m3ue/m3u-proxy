"""Different FFmpeg args for the same source must not share a stream or process.

Every user template resolves to the profile name "custom", and a predefined
profile renders differently per profile_variables. Before the fingerprint, two
such encodes of one URL produced the same stream ID and pooling key, so the
second request was handed the first stream's process.
"""

import hashlib

import pytest
import pytest_asyncio

from config import settings
from stream_manager import StreamManager
from transcoding import get_profile_manager

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


def test_fingerprint_is_empty_without_args():
    assert StreamManager._ffmpeg_args_fingerprint(None) == ""
    assert StreamManager._ffmpeg_args_fingerprint([]) == ""
    assert StreamManager._ffmpeg_args_fingerprint(REMOTE_720P) != ""


def test_fingerprint_is_stable_and_distinguishes_templates():
    a = StreamManager._ffmpeg_args_fingerprint(PASSTHROUGH)
    assert a == StreamManager._ffmpeg_args_fingerprint(list(PASSTHROUGH))
    assert a != StreamManager._ffmpeg_args_fingerprint(REMOTE_720P)


def test_fingerprint_not_fooled_by_argument_boundaries():
    # ["-a", "b c"] and ["-a b", "c"] join to the same text with spaces.
    one = StreamManager._ffmpeg_args_fingerprint(["-a", "b c"])
    two = StreamManager._ffmpeg_args_fingerprint(["-a b", "c"])
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


def _render_720p(**variables):
    base = {"input_url": URL, "output_args": "pipe:1", "format": "mpegts"}
    return get_profile_manager().get_profile("720p").render({**base, **variables})


@pytest.mark.asyncio
async def test_predefined_profile_variables_get_distinct_stream_ids(manager):
    high = _render_720p(video_bitrate="2500k")
    low = _render_720p(video_bitrate="800k")

    async def stream_id(args):
        return await manager.get_or_create_stream(
            URL,
            is_transcoded=True,
            transcode_profile="720p",
            transcode_ffmpeg_args=args,
        )

    high_id = await stream_id(high)
    low_id = await stream_id(low)
    assert high_id != low_id
    assert manager.streams[low_id].transcode_ffmpeg_args == low

    # The same variables still pool onto the same stream.
    assert await stream_id(_render_720p(video_bitrate="2500k")) == high_id


def test_pooling_keys_differ_for_predefined_profile_variables():
    from pooled_stream_manager import PooledStreamManager

    pm = PooledStreamManager.__new__(PooledStreamManager)
    high = _render_720p(video_bitrate="2500k")
    low = _render_720p(video_bitrate="800k")
    assert pm._generate_stream_key(URL, "720p", high) != pm._generate_stream_key(
        URL, "720p", low
    )
    assert pm._generate_stream_key(URL, "720p", high) == pm._generate_stream_key(
        URL, "720p", _render_720p(video_bitrate="2500k")
    )


def test_resolver_keys_ignore_ffmpeg_args():
    from pooled_stream_manager import PooledStreamManager

    pm = PooledStreamManager.__new__(PooledStreamManager)
    expected = hashlib.sha256(f"{URL}|resolver|ytdlp|best".encode()).hexdigest()[:16]
    assert (
        pm._generate_stream_key(
            URL, "ytdlp", ffmpeg_args=[], resolver_type="ytdlp", resolver_args="best"
        )
        == expected
    )
