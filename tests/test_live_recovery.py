"""Opt-in policies: real generators, byte boundaries and nullable API overrides."""

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import Settings, settings
from stream_manager import StreamManager
from tests.test_silent_reconnect import _MockStreamCM, _response_from_iter, _collect

A = "http://source.example/live/a.ts"
B = "http://source.example/live/b.ts"
C = "http://source.example/live/c.ts"


async def chunks(data=b"A", count=2, hang=False):
    for _ in range(count):
        yield data
    if hang:
        await asyncio.Event().wait()


@pytest_asyncio.fixture
async def manager(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    monkeypatch.setattr(settings, "WORKER_ID", None)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.setattr(settings, "STREAM_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "LIVE_SILENT_RECONNECT_MIN_CHUNKS", 5)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS_PREBUFFER_SIZE", 0)
    monkeypatch.setattr(settings, "LIVE_CHUNK_TIMEOUT_SECONDS", 0.03)
    m = StreamManager(enable_pooling=False)
    yield m
    await m.http_client.aclose()
    await m.live_stream_client.aclose()


def sources(monkeypatch, manager, factories):
    opened, closed = [], []

    class CM(_MockStreamCM):
        async def __aexit__(self, *args):
            closed.append(True)
            return False

    def stream(method, url, **kwargs):
        opened.append(url)
        return CM(_response_from_iter(factories[url]()))

    monkeypatch.setattr(manager.live_stream_client, "stream", stream)
    return opened, closed


def test_defaults_and_nullable_schema():
    from api import StreamCreateRequest

    assert not Settings(_env_file=None).LIVE_END_SESSION_ON_RECONNECT
    assert not Settings(_env_file=None).LIVE_SINGLE_RECOVERY_PER_EXIT
    request = StreamCreateRequest(url=A)
    assert request.end_session_on_reconnect is None
    assert request.single_recovery_per_exit is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,env",
    [
        ("end_session_on_reconnect", "LIVE_END_SESSION_ON_RECONNECT"),
        ("single_recovery_per_exit", "LIVE_SINGLE_RECOVERY_PER_EXIT"),
    ],
)
async def test_policy_global_override_and_identity(manager, monkeypatch, name, env):
    legacy = await manager.get_or_create_stream(A)
    assert legacy == hashlib.md5(A.encode()).hexdigest()
    monkeypatch.setattr(settings, env, True)
    enabled = await manager.get_or_create_stream(A)
    disabled = await manager.get_or_create_stream(A, **{name: False})
    assert enabled != disabled == legacy
    assert getattr(manager.streams[enabled], name) is True
    assert getattr(manager.streams[disabled], name) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
@pytest.mark.parametrize(
    "policy", ["end_session_on_reconnect", "single_recovery_per_exit"]
)
async def test_topology_rejected_before_state(manager, monkeypatch, env, policy):
    from fastapi import HTTPException

    monkeypatch.setenv(env, "2")
    with pytest.raises(HTTPException) as error:
        await manager.get_or_create_stream(A, **{policy: True})
    assert error.value.status_code == 400
    assert not manager.streams and not manager.clients


@pytest.mark.asyncio
async def test_spawned_worker_rejected_before_state(manager):
    from fastapi import HTTPException

    with patch("redis_config.multiprocessing.parent_process", return_value=object()):
        with pytest.raises(HTTPException) as error:
            await manager.get_or_create_stream(A, single_recovery_per_exit=True)
    assert error.value.status_code == 400
    assert not manager.streams and not manager.clients


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,value", [("REDIS_ENABLED", True), ("WORKER_ID", "worker")]
)
@pytest.mark.parametrize(
    "policy", ["end_session_on_reconnect", "single_recovery_per_exit"]
)
async def test_redis_and_separate_instances_supported(
    manager, monkeypatch, setting, value, policy
):
    # Direct streams and their recovery state stay in this process; Redis only
    # coordinates transcoders, and each WORKER_ID instance has its own listener.
    monkeypatch.setattr(settings, setting, value)
    manager._redis_requested = setting == "REDIS_ENABLED"
    stream_id = await manager.get_or_create_stream(A, **{policy: True})
    assert getattr(manager.streams[stream_id], policy) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,extra",
    [
        ("http://source.example/movie.mp4", {}),
        ("http://source.example/live/a.m3u8", {}),
        ("http://source.example/live/a.mpd", {}),
        (A, {"is_transcoded": True}),
    ],
)
async def test_vod_and_transcode_ignore_policies(manager, url, extra):
    sid = await manager.get_or_create_stream(
        url, end_session_on_reconnect=True, single_recovery_per_exit=True, **extra
    )
    assert not manager.streams[sid].end_session_on_reconnect
    assert not manager.streams[sid].single_recovery_per_exit


@pytest.mark.asyncio
async def test_eof_only_a_then_same_endpoint_b_and_repost_retains_source(
    manager, monkeypatch
):
    opened, closed = sources(
        monkeypatch, manager, {A: lambda: chunks(b"A"), B: lambda: chunks(b"B")}
    )
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    response = await manager.stream_continuous_direct(sid, "one")
    assert await _collect(response) == [b"A", b"A"]
    assert opened == [A] and closed == [True]
    assert manager.streams[sid].current_url == B
    assert sid == await manager.get_or_create_stream(A, end_session_on_reconnect=True)
    response = await manager.stream_continuous_direct(sid, "two")
    assert await _collect(response) == [b"B", b"B"]
    assert opened == [A, B]
    assert (
        not manager._direct_broadcast_primary and not manager.connection_cancel_events
    )


@pytest.mark.asyncio
async def test_reset_waits_for_inflight_selection_and_deduplicates_waiters(
    manager, monkeypatch
):
    sid = await manager.get_or_create_stream(
        A,
        failover_resolver_url="http://resolver.example/next",
        end_session_on_reconnect=True,
    )
    manager._direct_broadcast_primary[sid] = "primary"
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def resolve(stream_id, reason):
        calls.append(reason)
        started.set()
        await release.wait()
        return B

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    selection = asyncio.create_task(manager._try_update_failover_url(sid, "timeout"))
    await started.wait()
    duplicate = asyncio.create_task(
        manager._try_update_failover_url(sid, "other_waiter")
    )
    reset = asyncio.create_task(manager._end_direct_generation(sid, "primary"))
    await asyncio.sleep(0)
    assert not reset.done() and manager.streams[sid].direct_generation == 0
    release.set()
    assert await selection and await duplicate
    await reset
    assert calls == ["timeout"]
    assert manager.streams[sid].direct_generation == 1
    assert manager.streams[sid].current_url == B


@pytest.mark.asyncio
async def test_same_url_reconnect_also_ends_body(manager, monkeypatch):
    opened, closed = sources(monkeypatch, manager, {A: lambda: chunks(b"A", 6)})
    sid = await manager.get_or_create_stream(A, end_session_on_reconnect=True)
    assert (
        await _collect(await manager.stream_continuous_direct(sid, "one")) == [b"A"] * 6
    )
    assert opened == [A] and closed == [True]
    assert manager.streams[sid].current_url == A


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("resolver", [False, True])
async def test_timeout_selected_once_not_cb_or_eof(
    manager, monkeypatch, enabled, resolver
):
    # Keep setup/packet delivery outside the simulated no-data interval. Slow
    # runners must not trip the 1 ms breaker before the timeout under test.
    from datetime import datetime as RealDateTime, timedelta
    import stream_manager as stream_module

    clock = [RealDateTime.now(stream_module.timezone.utc)]

    class Clock(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(stream_module, "datetime", Clock)
    monkeypatch.setattr(settings, "STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT", 0.001)
    opened, _ = sources(
        monkeypatch,
        manager,
        {
            A: lambda: chunks(hang=True),
            B: lambda: chunks(b"B"),
            C: lambda: chunks(b"C"),
        },
    )
    sid = await manager.get_or_create_stream(
        A,
        failover_urls=[] if resolver else [B, C],
        failover_resolver_url="http://resolver.example/next" if resolver else None,
        strict_live_ts=True,
        single_recovery_per_exit=enabled,
        end_session_on_reconnect=True,
    )
    reasons = []

    async def select(stream_id, reason):
        reasons.append(reason)
        clock[0] += timedelta(seconds=1)
        manager.streams[sid].current_url = B if len(reasons) == 1 else C
        return True

    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    assert await _collect(await manager.stream_continuous_direct(sid, "one")) == [
        b"A",
        b"A",
    ]
    assert reasons == (
        ["chunk_timeout_after_retries"]
        if enabled
        else ["chunk_timeout_after_retries", "circuit_breaker_timeout"]
    )
    assert opened == [A]
    assert manager.streams[sid].current_url == (B if enabled else C)


@pytest.mark.asyncio
async def test_retry_not_reclassified_as_eof(manager, monkeypatch):
    monkeypatch.setattr(settings, "STREAM_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "STREAM_RETRY_DELAY", 0.001)
    opened, _ = sources(monkeypatch, manager, {A: lambda: chunks(hang=True)})
    sid = await manager.get_or_create_stream(
        A,
        failover_urls=[B],
        single_recovery_per_exit=True,
        end_session_on_reconnect=True,
    )
    select = AsyncMock()
    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    assert await _collect(await manager.stream_continuous_direct(sid, "one")) == [
        b"A",
        b"A",
    ]
    select.assert_not_called()
    assert opened == [A]


@pytest.mark.asyncio
async def test_reset_discards_all_subscriber_queues_and_closes_handoff(
    manager, monkeypatch
):
    opened, _ = sources(
        monkeypatch, manager, {A: lambda: chunks(), B: lambda: chunks(b"B")}
    )
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    primary = await manager.stream_continuous_direct(sid, "primary")
    subs = [await manager.stream_continuous_direct(sid, f"sub{i}") for i in range(3)]
    for queue in manager._direct_broadcast_queues[sid].values():
        for _ in range(512):
            queue.put_nowait(b"stale")
    handoff = AsyncMock()
    manager._handoff_upstream[sid] = (handoff, None, None)
    assert await _collect(primary) == [b"A", b"A"]
    assert await asyncio.gather(*(_collect(s) for s in subs)) == [[], [], []]
    handoff.__aexit__.assert_awaited_once()
    assert not manager._handoff_upstream and not manager._handoff_settled
    assert not manager._direct_broadcast_queues and not manager.connection_cancel_events
    assert opened == [A]


@pytest.mark.asyncio
async def test_promoted_inherited_upstream_finishes_without_generation_mix(
    manager, monkeypatch
):
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    await manager.register_client("promoted", sid)
    cancel = asyncio.Event()
    manager.connection_cancel_events["conn"] = cancel
    ctx = _MockStreamCM(None)
    manager._handoff_upstream[sid] = (ctx, None, chunks(b"A"))
    opened, _ = sources(monkeypatch, manager, {B: lambda: chunks(b"B")})
    body = [
        b
        async for b in manager._promoted_primary_generate(
            sid, "promoted", "conn", cancel
        )
    ]
    assert body == [b"A", b"A"] and opened == []
    assert manager.streams[sid].current_url == B
    assert not manager._direct_broadcast_primary


async def failed_first_read(failure, started=None):
    if started is not None:
        started.set()
    if failure == "timeout":
        await asyncio.Event().wait()
    if failure == "error":
        raise httpx.ReadError("inherited upstream failed")
    return
    yield  # Keep EOF an async iterator without emitting any bytes.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["eof", "timeout", "error"])
@pytest.mark.parametrize("failover", [False, True])
async def test_promoted_inherited_first_read_failure_retains_selected_source(
    manager, monkeypatch, failure, failover
):
    import api

    monkeypatch.setattr(api, "stream_manager", manager)
    opened, _ = sources(
        monkeypatch, manager, {A: lambda: chunks(b"A"), B: lambda: chunks(b"B")}
    )
    payload = {
        "url": A,
        "failover_urls": [B] if failover else [],
        "end_session_on_reconnect": True,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://test"
    ) as client:
        created = await client.post("/streams", json=payload)
        assert created.status_code == 200
        sid = created.json()["stream_id"]
        info = manager.streams[sid]
        ctx = AsyncMock()
        manager._handoff_upstream[sid] = (ctx, None, failed_first_read(failure))
        body = [
            b
            async for b in manager._promoted_primary_generate(
                sid, "p", "conn", asyncio.Event()
            )
        ]
        assert body == [] and opened == []  # No fresh connection or splice.
        ctx.__aexit__.assert_awaited_once()
        selected = B if failover else A
        assert info.current_url == selected
        assert info.failover_attempts == int(failover)
        assert info.direct_generation == 1
        assert not manager._direct_broadcast_primary
        following = await client.get(f"/stream/{sid}")
        assert following.status_code == 200
        assert following.content == (b"BB" if failover else b"AA")
        repost = await client.post("/streams", json=payload)
        assert repost.status_code == 200 and repost.json()["stream_id"] == sid
        assert info.current_url == selected
        following = await client.get(f"/stream/{sid}")
        assert following.status_code == 200
        assert following.content == (b"BB" if failover else b"AA")
        assert opened == [selected, selected]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["eof", "timeout", "error"])
async def test_promoted_first_read_failure_after_reset_cannot_select_again(
    manager, monkeypatch, failure
):
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B, C], end_session_on_reconnect=True
    )
    closing, release = asyncio.Event(), asyncio.Event()
    ctx = AsyncMock()

    async def close(*args):
        closing.set()
        await release.wait()

    ctx.__aexit__.side_effect = close
    manager._handoff_upstream[sid] = (ctx, None, failed_first_read(failure))
    select = AsyncMock(wraps=manager._try_update_failover_url)
    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    opened, _ = sources(monkeypatch, manager, {B: lambda: chunks(b"B")})

    async def consume():
        return [
            b
            async for b in manager._promoted_primary_generate(
                sid, "old", "old-conn", asyncio.Event()
            )
        ]

    pending = asyncio.create_task(consume())
    await asyncio.wait_for(closing.wait(), 1)
    assert await manager._try_update_failover_url(sid, "concurrent_selection")
    await manager._end_direct_generation(sid, "old-conn")
    fresh = await manager.stream_continuous_direct(sid, "fresh")
    fresh_owner = manager._direct_broadcast_primary[sid]
    release.set()
    assert await pending == []
    assert manager._direct_broadcast_primary[sid] == fresh_owner
    assert manager.streams[sid].current_url == B
    select.assert_awaited_once_with(sid, "concurrent_selection")
    assert await _collect(fresh) == [b"B", b"B"]
    assert opened == [B]
    ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["eof", "timeout", "error"])
async def test_promoted_first_read_failure_with_cancel_event_does_not_reset(
    manager, monkeypatch, failure
):
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    ctx, cancel = AsyncMock(), asyncio.Event()
    # A disconnect arriving while the unusable inherited connection is closing
    # must not be reclassified as an upstream-driven generation reset.
    ctx.__aexit__.side_effect = lambda *args: cancel.set()
    manager._handoff_upstream[sid] = (ctx, None, failed_first_read(failure))
    select = AsyncMock(wraps=manager._try_update_failover_url)
    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    opened, _ = sources(monkeypatch, manager, {B: lambda: chunks(b"B")})
    assert [
        b async for b in manager._promoted_primary_generate(sid, "p", "conn", cancel)
    ] == []
    assert cancel.is_set() and opened == []
    select.assert_not_awaited()
    assert manager.streams[sid].current_url == A
    assert manager.streams[sid].direct_generation == 0
    ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_promoted_first_read_task_cancellation_does_not_select_failover(
    manager, monkeypatch
):
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    started = asyncio.Event()
    ctx, cancel = AsyncMock(), asyncio.Event()
    manager._handoff_upstream[sid] = (
        ctx,
        None,
        failed_first_read("timeout", started=started),
    )
    select = AsyncMock(wraps=manager._try_update_failover_url)
    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    pending = asyncio.create_task(
        anext(manager._promoted_primary_generate(sid, "p", "conn", cancel))
    )
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancel.is_set()
    select.assert_not_awaited()
    assert manager.streams[sid].current_url == A
    assert manager.streams[sid].direct_generation == 0
    ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_promoted_without_handoff_ends_instead_of_opening_upstream(
    manager, monkeypatch
):
    sid = await manager.get_or_create_stream(A, end_session_on_reconnect=True)
    opened, _ = sources(monkeypatch, manager, {A: lambda: chunks()})
    body = [
        b
        async for b in manager._promoted_primary_generate(
            sid, "p", "conn", asyncio.Event()
        )
    ]
    assert body == [] and opened == []


@pytest.mark.asyncio
async def test_api_nullable_forwarding_and_unsupported_topology(manager, monkeypatch):
    import api

    monkeypatch.setattr(api, "stream_manager", manager)
    monkeypatch.setattr(settings, "LIVE_END_SESSION_ON_RECONNECT", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://test"
    ) as client:
        enabled = await client.post("/streams", json={"url": A})
        inherited = await client.post(
            "/streams", json={"url": A, "end_session_on_reconnect": None}
        )
        disabled = await client.post(
            "/streams", json={"url": A, "end_session_on_reconnect": False}
        )
        assert enabled.status_code == disabled.status_code == 200
        assert inherited.json()["stream_id"] == enabled.json()["stream_id"]
        assert enabled.json()["stream_id"] != disabled.json()["stream_id"]
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        result = await client.post("/streams", json={"url": B})
        assert (
            result.status_code == 400
            and "one server process" in result.json()["detail"]
        )


@pytest.mark.asyncio
async def test_cancel_primary_then_subscriber_inherits_a_and_resets_to_b(
    manager, monkeypatch
):
    opened, closed = sources(
        monkeypatch, manager, {A: lambda: chunks(b"A", 4), B: lambda: chunks(b"B")}
    )
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], end_session_on_reconnect=True
    )
    primary = await manager.stream_continuous_direct(sid, "primary")
    sub = await manager.stream_continuous_direct(sid, "subscriber")
    assert await anext(primary.body_iterator) == b"A"
    # Mimic ASGI disconnect cleanup. The wrapper is suspended at its first yield,
    # so resume it after setting cancellation to exercise real handoff/finally.
    conn = manager.clients["primary"].active_connection_id
    await manager.cleanup_client("primary", conn)
    assert await _collect(primary) == []
    assert sid in manager._handoff_upstream
    # Legacy primary cancellation can discard its in-flight chunk; the
    # remaining inherited connection must not open/splice any B bytes.
    assert await _collect(sub) == [b"A"] * 2
    assert opened == [A] and closed == [True]
    assert manager.streams[sid].current_url == B
    assert not manager._handoff_upstream and not manager.connection_cancel_events
    assert await _collect(await manager.stream_continuous_direct(sid, "next")) == [
        b"B",
        b"B",
    ]
    assert opened == [A, B]


@pytest.mark.asyncio
async def test_stale_primary_cannot_broadcast_into_new_generation(manager, monkeypatch):
    release = asyncio.Event()

    async def delayed():
        yield b"A"
        await release.wait()
        yield b"stale-A"

    opened, closed = sources(
        monkeypatch, manager, {A: delayed, B: lambda: chunks(b"B")}
    )
    sid = await manager.get_or_create_stream(A, end_session_on_reconnect=True)
    primary = await manager.stream_continuous_direct(sid, "old")
    assert await anext(primary.body_iterator) == b"A"
    old_conn = manager._direct_broadcast_primary[sid]
    pending = asyncio.create_task(_collect(primary))
    await asyncio.sleep(0)
    await manager._end_direct_generation(sid, old_conn)
    manager.streams[sid].current_url = B
    fresh = await manager.stream_continuous_direct(sid, "new")
    release.set()
    assert await pending == []
    assert await _collect(fresh) == [b"B", b"B"]
    assert opened == [A, B] and len(closed) == 2
    assert not manager.connection_cancel_events


@pytest.mark.asyncio
async def test_single_recovery_without_session_end_keeps_selected_b(
    manager, monkeypatch
):
    monkeypatch.setattr(settings, "STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT", 0.001)
    opened, _ = sources(
        monkeypatch, manager, {A: lambda: chunks(hang=True), B: lambda: chunks(b"B", 8)}
    )
    sid = await manager.get_or_create_stream(
        A, failover_urls=[B], strict_live_ts=True, single_recovery_per_exit=True
    )
    original = manager._try_update_failover_url
    select = AsyncMock(side_effect=original)
    monkeypatch.setattr(manager, "_try_update_failover_url", select)
    response = await manager.stream_continuous_direct(sid, "client")
    body = response.body_iterator
    assert await anext(body) == b"A"
    assert await anext(body) == b"A"
    assert await anext(body) == b"B"
    assert (
        select.await_count == 1
        and select.call_args.args[1] == "chunk_timeout_after_retries"
    )
    await manager.cleanup_client(
        "client", manager.clients["client"].active_connection_id
    )
    assert await _collect(response) == []
    assert opened == [A, B]
