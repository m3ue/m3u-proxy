"""Default-off access to the existing local FFmpeg process manager."""

import logging
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import Settings, settings
from pooled_stream_manager import PooledStreamManager
from redis_config import should_use_pooling


@pytest.fixture(autouse=True)
def topology(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_ID", None)
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING_POOLING", True)
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)


def test_defaults_unchanged():
    defaults = Settings.model_fields
    assert defaults["REDIS_ENABLED"].default is False
    assert defaults["ENABLE_TRANSCODING_POOLING"].default is True
    assert defaults["ENABLE_SINGLE_PROCESS_TRANSCODING"].default is False


@pytest.mark.parametrize("redis", [False, True])
@pytest.mark.parametrize("pooling", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_pooling_truth_table(monkeypatch, redis, pooling, local):
    monkeypatch.setattr(settings, "REDIS_ENABLED", redis)
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING_POOLING", pooling)
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", local)
    assert should_use_pooling() is (pooling and (redis or local))


@pytest.mark.parametrize("worker", ["WORKER_ID", "WEB_CONCURRENCY", "UVICORN_WORKERS"])
@pytest.mark.parametrize("value", ["2", "invalid", "0"])
def test_unsupported_local_workers_rejected(monkeypatch, worker, value):
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    if worker == "WORKER_ID":
        monkeypatch.setattr(settings, worker, value)
    else:
        monkeypatch.setenv(worker, value)
    with pytest.raises(ValueError, match="single process"):
        should_use_pooling()
    # The opt-in guard must not change the existing disabled/Redis modes.
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", False)
    assert should_use_pooling() is False
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING_POOLING", False)
    assert should_use_pooling() is False
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING_POOLING", True)
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    assert should_use_pooling() is True


def test_explicit_single_worker_supported(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    assert should_use_pooling() is True


def test_spawned_worker_rejected(monkeypatch):
    # Uvicorn --workers uses multiprocessing spawn, even without worker ENV.
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    with patch("redis_config.multiprocessing.parent_process", return_value=object()):
        with pytest.raises(ValueError, match="single process"):
            should_use_pooling()
        monkeypatch.setattr(settings, "REDIS_ENABLED", True)
        assert should_use_pooling() is True


def test_main_local_mode_does_not_probe_redis(monkeypatch, tmp_path):
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "LOG_ANONYMIZE", False)
    root_logger = logging.getLogger()
    previous_handlers = set(root_logger.handlers)
    try:
        with patch("uvicorn.run") as server, patch("redis.asyncio.from_url") as redis:
            entry = runpy.run_path(str(Path(__file__).resolve().parents[1] / "main.py"))
            entry["main"]()
            redis.assert_not_called()
            server.assert_called_once()
    finally:
        for handler in set(root_logger.handlers) - previous_handlers:
            root_logger.removeHandler(handler)
            handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redis,local", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_api_native_manager_and_info(monkeypatch, redis, local):
    monkeypatch.setattr(settings, "REDIS_ENABLED", redis)
    monkeypatch.setitem(settings.__dict__, "ENABLE_SINGLE_PROCESS_TRANSCODING", local)
    monkeypatch.setenv("REDIS_URL", "redis://unused.invalid:6379/4")
    # Execute real API initialization without replacing the native manager or
    # reloading the shared api module used by other suites. No lifespan/network.
    with patch(
        "redis.asyncio.from_url", side_effect=AssertionError("Unexpected Redis I/O")
    ):
        api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/api.py"))
        manager = api["stream_manager"]
        try:
            assert api["redis_url"] == (
                "redis://unused.invalid:6379/4" if redis else None
            )
            assert manager._redis_requested is redis
            assert manager.enable_pooling is (redis or local)
            pooled = manager.pooled_manager
            if redis or local:
                assert isinstance(pooled, PooledStreamManager)
                assert pooled.enable_sharing is redis
                assert pooled.redis_client is None
            else:
                assert pooled is None
            info = await api["get_info"]()
            assert info["redis"]["pooling_enabled"] is redis
            assert info["redis"]["sharing_strategy"] == (
                settings.STREAM_SHARING_STRATEGY if redis else None
            )
            if local and not redis:
                # The inner stream may use the existing generation policy guard.
                stream_id = await manager.get_or_create_stream(
                    "http://fixture.invalid/live.ts", end_session_on_reconnect=True
                )
                assert manager.streams[stream_id].end_session_on_reconnect
                await pooled.start()
                assert pooled.redis_client is None
                await pooled.stop()
        finally:
            await manager.http_client.aclose()
            await manager.live_stream_client.aclose()
