"""Owned normalization composition; no provider I/O in unit tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import api
from config import settings
from stream_manager import StreamManager


@pytest_asyncio.fixture
async def manager(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "WORKER_ID", None)
    monkeypatch.setattr(settings, "ENABLE_SINGLE_PROCESS_TRANSCODING", True)
    monkeypatch.setattr(settings, "ENABLE_TRANSCODING_POOLING", True)
    monkeypatch.setattr(settings, "LIVE_NORMALIZE_OUTPUT", False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    sm = StreamManager(enable_pooling=True)
    monkeypatch.setattr(api, "stream_manager", sm)
    yield sm
    await sm.stop()


@pytest.fixture(params=["cpu", "vaapi", "qsv"])
def encoder_policy(request, monkeypatch):
    import live_normalization

    monkeypatch.setattr(live_normalization, "validate_vaapi_encoder", AsyncMock())
    monkeypatch.setattr(live_normalization, "validate_qsv_encoder", AsyncMock())
    return request.param


def scope():
    return Request(
        {
            "type": "http",
            "server": ("0.0.0.0", 12345),
            "headers": [(b"host", b"attacker.invalid:99")],
        }
    )


def payload(**kwargs):
    return api.StreamCreateRequest(
        url="http://fixture.invalid/live/a.ts",
        failover_urls=["http://fixture.invalid/live/b.ts"],
        metadata={"id": "7", "playlist_uuid": "provider", "original_channel_id": "8"},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_owned_identity_metadata_reuse_and_selected_backup(manager):
    raw = await api.create_stream(payload(), scope())
    result = await api.create_stream(payload(normalize_live_output=True), scope())
    outer = manager.streams[result["stream_id"]]
    inner = manager.normalization_source(outer)
    assert len({raw["stream_id"], outer.stream_id, inner.stream_id}) == 3
    assert outer.is_transcoded and not inner.is_transcoded
    assert inner.end_session_on_reconnect is False
    assert (
        inner.single_recovery_per_exit
        and inner.strict_live_ts
        and inner.use_sticky_session
    )
    assert inner.metadata["original_channel_id"] == "8"
    assert outer.metadata["normalize_live_output"] == "true"
    assert outer.metadata["normalization_encoder"] == "cpu"
    assert "transcoding" not in outer.metadata
    assert outer.original_url.startswith("http://127.0.0.1:12345/stream/")
    inner.current_url = inner.failover_urls[0]
    inner.failover_attempts = 1
    again = await api.create_stream(payload(normalize_live_output=True), scope())
    assert again["stream_id"] == outer.stream_id
    assert manager.streams[outer.stream_id] is outer
    assert manager.normalization_source(outer) is inner
    assert inner.current_url.endswith("/b.ts")
    stats = manager.get_stats()
    assert stats["proxy_stats"]["total_streams"] == 2
    shown = next(s for s in stats["streams"] if s["stream_id"] == outer.stream_id)
    assert shown["current_url"] == inner.current_url
    assert shown["failover_attempts"] == 1
    matches = await api.get_streams_by_metadata("normalize_live_output", "true", False)
    assert matches["total_matching"] == 1
    counts = await api.get_streams_counts_by_metadata(
        "playlist_uuid", "provider", False
    )
    assert counts["counts"] == {"provider": 2}
    await api.delete_stream(outer.stream_id)
    assert set(manager.streams) == {raw["stream_id"]}


@pytest.mark.asyncio
async def test_false_overrides_global_and_legacy_id(manager, monkeypatch):
    raw = await api.create_stream(payload(), scope())
    monkeypatch.setattr(settings, "LIVE_NORMALIZE_OUTPUT", True)
    disabled = await api.create_stream(payload(normalize_live_output=False), scope())
    assert raw["stream_id"] == disabled["stream_id"]
    enabled = await api.create_stream(payload(), scope())
    assert enabled["stream_id"] != raw["stream_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://fixture/a.mpd",
        "http://fixture/a.m3u8",
        "http://fixture/movie/a.ts",
        "http://fixture/a.mp4",
    ],
)
async def test_reject_non_live_before_allocation(manager, url):
    with pytest.raises(HTTPException) as error:
        await api.create_stream(
            api.StreamCreateRequest(url=url, normalize_live_output=True), scope()
        )
    assert error.value.status_code == 400
    assert not manager.streams


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,value",
    [
        ("REDIS_ENABLED", True),
        ("ENABLE_SINGLE_PROCESS_TRANSCODING", False),
        ("ENABLE_TRANSCODING_POOLING", False),
        ("WORKER_ID", "worker"),
    ],
)
async def test_preflight(manager, monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    with pytest.raises(HTTPException):
        await api.create_stream(payload(normalize_live_output=True), scope())
    assert not manager.streams


@pytest.mark.asyncio
async def test_outer_control_routes_to_inner_without_encoder_restart(
    manager, monkeypatch
):
    result = await api.create_stream(payload(normalize_live_output=True), scope())
    outer = manager.streams[result["stream_id"]]
    inner = manager.normalization_source(outer)
    update = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_try_update_failover_url", update)
    await api.trigger_failover(outer.stream_id)
    update.assert_awaited_once_with(inner.stream_id, "manual")
    assert outer.current_url == outer.original_url


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("after_allocation", [False, True])
async def test_partial_failure_rolls_back(
    manager, monkeypatch, cancel, after_allocation, encoder_policy
):
    import asyncio

    original = manager.get_or_create_stream

    async def create(*args, **kwargs):
        if kwargs.get("is_transcoded"):
            if after_allocation:
                await original(*args, **kwargs)
            raise (
                asyncio.CancelledError() if cancel else RuntimeError("fixture failure")
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "get_or_create_stream", create)
    with pytest.raises(asyncio.CancelledError if cancel else HTTPException):
        await api.create_stream(payload(normalize_live_output=True, normalization_encoder=encoder_policy), scope())
    assert not manager.streams


@pytest.mark.asyncio
async def test_idle_cascade_and_metadata_cannot_forge_ownership(manager, encoder_policy):
    from datetime import datetime, timezone, timedelta

    result = await api.create_stream(payload(normalize_live_output=True, normalization_encoder=encoder_policy), scope())
    outer = manager.streams[result["stream_id"]]
    inner = manager.normalization_source(outer)
    foreign = await manager.get_or_create_stream(
        "http://fixture.invalid/foreign.ts",
        metadata={"normalization_parent_id": outer.stream_id},
    )
    outer.last_access = datetime.now(timezone.utc) - timedelta(days=1)
    inner.last_access = outer.last_access
    await manager._cleanup_inactive_streams()
    assert set(manager.streams) == {foreign}


@pytest.mark.asyncio
async def test_active_input_retained_and_concurrent_posts_share_encoder(
    manager, monkeypatch
):
    import asyncio
    from datetime import datetime, timezone, timedelta

    first, second = await asyncio.gather(
        api.create_stream(payload(normalize_live_output=True), scope()),
        api.create_stream(payload(normalize_live_output=True), scope()),
    )
    assert first["stream_id"] == second["stream_id"]
    outer = manager.streams[first["stream_id"]]
    inner = manager.normalization_source(outer)
    await manager.register_client("viewer", outer.stream_id, "test", "127.0.0.1")
    await manager.register_client("encoder", inner.stream_id, "ffmpeg", "127.0.0.1")
    inner.last_access = datetime.now(timezone.utc) - timedelta(days=1)
    key = "owned-encoder-key"
    outer.transcode_stream_key = key
    stop = AsyncMock()
    monkeypatch.setattr(manager.pooled_manager, "force_stop_stream", stop)
    await manager._cleanup_inactive_streams()
    assert manager.normalization_source(outer) is inner
    events = AsyncMock()
    manager.set_event_manager(events)
    response = await api.trigger_failover(outer.stream_id)
    assert response["new_url"] == inner.failover_urls[0]
    assert response["failover_attempts"] == 1
    assert inner.failover_attempts == 1
    assert outer.transcode_stream_key == key
    stop.assert_not_awaited()
    assert len(events.emit_event.await_args_list) == 1
    assert events.emit_event.await_args.args[0].stream_id == outer.stream_id
    # Internal provider/client lifecycle events must not double-count webhooks.
    await manager._emit_event("CLIENT_CONNECTED", inner.stream_id, {})
    assert events.emit_event.await_count == 1
    reused = await api.create_stream(payload(normalize_live_output=True), scope())
    assert reused["stream_id"] == outer.stream_id
    assert inner.current_url.endswith("/b.ts")
    assert outer.transcode_stream_key == key
    stats = manager.get_stats()
    assert stats["proxy_stats"]["total_streams"] == 1
    assert stats["proxy_stats"]["active_clients"] == 1
    assert len(stats["clients"]) == 1
    matches = await api.get_streams_by_metadata("playlist_uuid", "provider", True)
    assert matches["total_matching"] == 1
    assert matches["matching_streams"][0]["url"].endswith("/b.ts")
    assert matches["matching_streams"][0]["failover_attempts"] == 1
    await api.delete_stream(outer.stream_id)
    stop.assert_awaited_once_with(key)
    assert not manager.streams and not manager.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("oldest", [False, True])
async def test_metadata_delete_counts_one_and_cascades(manager, oldest, encoder_policy):
    result = await api.create_stream(payload(normalize_live_output=True, normalization_encoder=encoder_policy), scope())
    outer = manager.streams[result["stream_id"]]
    inner = manager.normalization_source(outer)
    if oldest:
        deleted = await api.delete_oldest_stream_by_metadata(
            "playlist_uuid", "provider", None
        )
        assert deleted["deleted_stream"] == outer.stream_id
    else:
        deleted = await api.delete_streams_by_metadata(
            "playlist_uuid", "provider", None, None, True
        )
        assert deleted["deleted_count"] == 1
    assert outer.stream_id not in manager.streams
    assert inner.stream_id not in manager.streams


@pytest.mark.asyncio
async def test_missing_socket_port_and_profile_failure_do_not_allocate(
    manager, monkeypatch
):
    with pytest.raises(HTTPException) as error:
        await api.create_stream(payload(normalize_live_output=True))
    assert error.value.status_code == 400
    assert not manager.streams
    monkeypatch.setattr(
        settings, "LIVE_NORMALIZE_PROFILE", "-i {missing} {output_args}"
    )
    with pytest.raises(HTTPException):
        await api.create_stream(payload(normalize_live_output=True), scope())
    assert not manager.streams


@pytest.mark.asyncio
async def test_private_profile_policy_id_and_ownership_not_metadata(
    manager, monkeypatch
):
    a = await api.create_stream(payload(normalize_live_output=True), scope())
    first = manager.streams[a["stream_id"]]
    monkeypatch.setattr(
        settings,
        "LIVE_NORMALIZE_PROFILE",
        settings.LIVE_NORMALIZE_PROFILE + " -metadata title=alternate",
    )
    b = await api.create_stream(payload(normalize_live_output=True), scope())
    assert b["stream_id"] != first.stream_id
    assert (
        manager.normalization_source(first).stream_id
        != manager.normalization_source(manager.streams[b["stream_id"]]).stream_id
    )
    foreign_id = await manager.get_or_create_stream(
        "http://fixture.invalid/foreign.ts",
        metadata={
            "normalization_parent_id": first.stream_id,
            "normalization_input_id": first.normalization_input_id,
        },
    )
    foreign = manager.streams[foreign_id]
    assert (
        foreign.normalization_input_id is None
        and foreign.normalization_parent_id is None
    )
    assert len(manager.get_stats()["streams"]) == 3
    await manager.cleanup_normalization_input(foreign)
    assert len(manager.streams) == 5


@pytest.mark.asyncio
async def test_owned_input_delete_rejected_without_disrupting_outer(manager, monkeypatch, encoder_policy):
    import httpx

    monkeypatch.setattr(settings, "API_TOKEN", "offline-fixture-only")
    stop = AsyncMock()
    monkeypatch.setattr(manager.pooled_manager, "force_stop_stream", stop)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid:12345",
        headers={"X-API-Token": "offline-fixture-only"},
    ) as client:
        raw = await api.create_stream(payload(), scope())
        unrelated = manager.streams[raw["stream_id"]]
        await manager.register_client("unrelated", unrelated.stream_id, "test", "127.0.0.1")
        created = await client.post(
            "/streams", json=payload(normalize_live_output=True, normalization_encoder=encoder_policy).model_dump()
        )
        assert created.status_code == 200
        outer = manager.streams[created.json()["stream_id"]]
        inner = manager.normalization_source(outer)
        outer.transcode_stream_key = "owned-encoder-key"
        await manager.register_client("viewer", outer.stream_id, "test", "127.0.0.1")
        await manager.register_client("encoder", inner.stream_id, "ffmpeg", "127.0.0.1")
        streams_before = dict(manager.streams)
        clients_before = dict(manager.clients)
        stats_before = manager._stats.active_streams

        rejected = await client.delete(f"/streams/{inner.stream_id}")
        assert rejected.status_code == 409
        assert manager.streams == streams_before
        assert manager.clients == clients_before
        assert manager._stats.active_streams == stats_before
        assert manager.normalization_source(outer) is inner
        assert outer.transcode_stream_key == "owned-encoder-key"
        assert not inner.stream_stopped_emitted
        assert not outer.stream_stopped_emitted
        stop.assert_not_awaited()

        switched = await client.post(f"/streams/{outer.stream_id}/failover")
        assert switched.status_code == 200
        assert switched.json()["new_url"].endswith("/b.ts")
        assert inner.current_url.endswith("/b.ts")
        assert inner.failover_attempts == 1
        assert manager.streams[unrelated.stream_id] is unrelated
        assert unrelated.current_url == unrelated.original_url
        assert outer.transcode_stream_key == "owned-encoder-key"
        stop.assert_not_awaited()

        deleted = await client.delete(f"/streams/{outer.stream_id}")
        assert deleted.status_code == 200
        stop.assert_awaited_once_with("owned-encoder-key")
        assert manager.streams == {unrelated.stream_id: unrelated}
        assert set(manager.clients) == {"unrelated"}
        assert manager.stream_clients[unrelated.stream_id] == {"unrelated"}


@pytest.mark.asyncio
async def test_native_http_stream_control_contract(manager, monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "API_TOKEN", "offline-fixture-only")
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://fixture.invalid:12345",
        headers={
            "X-API-Token": "offline-fixture-only",
            "Host": "untrusted.invalid:9999",
        },
    ) as client:
        created = await client.post(
            "/streams", json=payload(normalize_live_output=True).model_dump()
        )
        assert created.status_code == 200
        outer_id = created.json()["stream_id"]
        assert created.json()["stream_type"] == "direct"
        assert created.json()["metadata"]["normalize_live_output"] == "true"
        outer = manager.streams[outer_id]
        assert outer.original_url.startswith("http://127.0.0.1:12345/")
        await manager.register_client("native-viewer", outer_id, "test", "127.0.0.1")
        switched = await client.post(f"/streams/{outer_id}/failover")
        assert switched.status_code == 200
        assert switched.json()["stream_type"] == "Live Continuous"
        assert switched.json()["new_url"].endswith("/b.ts")
        assert switched.json()["failover_attempts"] == 1
        assert switched.json()["active_clients"] == 1
        detail = await client.get(f"/streams/{outer_id}")
        assert detail.status_code == 200
        assert detail.json()["stream"]["current_url"].endswith("/b.ts")
        assert detail.json()["stream"]["failover_attempts"] == 1
        exhausted = await client.post(f"/streams/{outer_id}/failover")
        assert exhausted.status_code == 500  # existing exhaustion contract
        deleted = await client.delete(f"/streams/{outer_id}")
        assert deleted.status_code == 200
        assert not manager.streams
