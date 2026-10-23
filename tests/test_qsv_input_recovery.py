"""Owned QSV adapter recovery with synthetic sources only."""
import asyncio
from datetime import datetime, timezone

import pytest

from tests.test_live_recovery import manager as manager, sources, A, B, C
from config import settings
from stream_manager import StreamInfo
from ts_canonicalizer import TSCanonicalizationError
from tests.test_ts_canonicalizer import fixture


async def setup_input(manager):
    sid = await manager.get_or_create_stream(
        A, failover_resolver_url="http://resolver.invalid/next",
        end_session_on_reconnect=False, single_recovery_per_exit=True,
    )
    now = datetime.now(timezone.utc)
    outer = StreamInfo("outer", "http://127.0.0.1/inner", now, now)
    outer.normalization_input_id = sid
    outer.metadata = {"normalization_encoder": "qsv"}
    manager.streams["outer"] = outer
    manager.streams[sid].normalization_parent_id = "outer"
    return sid


async def broken():
    yield b"!" * 188
    await asyncio.Event().wait()


async def good():
    yield fixture()
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_adapter_start_error_uses_inner_resolver_without_ending_response(manager, monkeypatch):
    sid = await setup_input(manager)
    opened, closed = sources(monkeypatch, manager, {A: broken, B: good})
    calls = []

    async def resolve(stream_id, reason):
        calls.append((stream_id, reason))
        return B

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    try:
        data = await asyncio.wait_for(anext(response.body_iterator), 1)
        assert data and len(data) % 188 == 0
        assert opened == [A, B]
        assert len(closed) == 1
        assert calls == [(sid, "normalization_input_error")]
        # One epoch per successful upstream open, plus rejected-source fencing.
        assert manager.streams[sid].direct_generation == 3
        assert manager.streams[sid].current_url == B
        assert manager.streams[sid].failover_attempts == 1
    finally:
        await response.body_iterator.aclose()
    assert len(closed) == 2
    assert not manager._direct_broadcast_primary
    assert not manager.connection_cancel_events


@pytest.mark.asyncio
async def test_adapter_failure_after_output_ends_input_instead_of_resetting_abi(manager, monkeypatch):
    sid = await setup_input(manager)
    calls = []

    async def late_error():
        yield fixture()
        yield b"!" * 188
        await asyncio.Event().wait()

    async def resolve(stream_id, reason):
        calls.append(reason)
        return B

    opened, closed = sources(monkeypatch, manager, {A: late_error, B: good})
    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    assert await anext(response.body_iterator)
    with pytest.raises(TSCanonicalizationError):
        await asyncio.wait_for(anext(response.body_iterator), 1)
    assert not calls
    assert opened == [A] and closed == [True]
    assert not manager.connection_cancel_events


@pytest.mark.asyncio
async def test_stale_adapter_resolver_cannot_overwrite_new_generation(manager, monkeypatch):
    sid = await setup_input(manager)
    opened, closed = sources(monkeypatch, manager, {A: broken, B: good, C: good})
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(stream_id, reason):
        entered.set()
        await release.wait()
        return B

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    read = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(entered.wait(), 1)
    info = manager.streams[sid]
    info.direct_generation += 1
    info.current_url = C
    release.set()
    with pytest.raises(TSCanonicalizationError):
        await asyncio.wait_for(read, 1)
    assert opened == [A]
    assert closed == [True]
    assert info.current_url == C
    assert info.direct_generation == 2
    assert info.failover_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["limit", "exhausted", "static", "unknown_private"])
async def test_rejected_candidates_are_bounded_and_never_forwarded(manager, monkeypatch, mode):
    from tests.test_qsv_ts_integration import dvb_fixture

    monkeypatch.setattr(settings, "MAX_FAILOVER_ATTEMPTS", 2)
    sid = await setup_input(manager)
    info = manager.streams[sid]
    calls = []

    async def unknown():
        yield dvb_fixture(b"")
        await asyncio.Event().wait()

    factories = {A: broken, B: broken, C: broken}
    if mode == "unknown_private":
        factories[B] = unknown
        factories[C] = good
    if mode == "static":
        info.failover_resolver_url = None
        info.failover_urls = [B, C]
    else:
        async def resolve(stream_id, reason):
            calls.append((stream_id, reason))
            if mode == "exhausted":
                return None
            return (B if len(calls) == 1 else C) if mode == "unknown_private" else B
        monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)

    opened, closed = sources(monkeypatch, manager, factories)
    response = await manager.stream_continuous_direct(sid, "encoder")
    try:
        if mode == "unknown_private":
            assert await asyncio.wait_for(anext(response.body_iterator), 1)
            assert opened == [A, B, C]
        else:
            with pytest.raises(TSCanonicalizationError):
                await asyncio.wait_for(anext(response.body_iterator), 1)
            assert opened == ([A] if mode == "exhausted" else [A, B, C] if mode == "static" else [A, B, B])
    finally:
        await response.body_iterator.aclose()
    assert len(closed) == len(opened)
    assert len(calls) == (0 if mode == "static" else 1 if mode == "exhausted" else 2)
    assert info.failover_attempts == (0 if mode == "exhausted" else 2)
    assert not manager._direct_broadcast_primary
    assert not manager.connection_cancel_events


@pytest.mark.asyncio
async def test_finish_failure_selects_next_candidate(manager, monkeypatch):
    # The raw generator handles transient EOF itself; isolate terminal EOF to
    # exercise the adapter.finish boundary without a second EOF recovery.
    from fastapi.responses import StreamingResponse
    sid = await setup_input(manager)
    opened, closed = [], []

    async def source(url):
        try:
            yield b"partial" if url == A else fixture()
        finally:
            closed.append(url)

    async def original(stream_id, client_id, range_header, **kwargs):
        url = manager.streams[sid].current_url or A
        opened.append(url)
        return StreamingResponse(source(url))

    async def resolve(stream_id, reason):
        return B

    monkeypatch.setattr(manager, "_stream_continuous_direct_original", original)
    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    assert [chunk async for chunk in response.body_iterator]
    assert opened == closed == [A, B]
    assert manager.streams[sid].direct_generation == 1


@pytest.mark.asyncio
async def test_rejected_primary_ends_old_subscriber_without_second_selection(manager, monkeypatch):
    sid = await setup_input(manager)
    opened, closed = sources(monkeypatch, manager, {A: broken, B: good})
    calls = []

    async def resolve(stream_id, reason):
        calls.append(reason)
        return B

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    primary = await manager.stream_continuous_direct(sid, "encoder")
    subscriber = await manager.stream_continuous_direct(sid, "observer")
    try:
        assert await asyncio.wait_for(anext(primary.body_iterator), 1)
        # A delayed subscriber must not restart a generation after its replacement
        # primary has already gone away.
        await primary.body_iterator.aclose()
        with pytest.raises(TSCanonicalizationError):
            await asyncio.wait_for(anext(subscriber.body_iterator), 1)
        assert calls == ["normalization_input_error"]
        assert opened == [A, B]
        assert not manager._handoff_upstream
    finally:
        await primary.body_iterator.aclose()
        await subscriber.body_iterator.aclose()
    assert len(closed) == 2
    assert not manager.connection_cancel_events

