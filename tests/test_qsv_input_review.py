"""Request-scoped cancellation and shared QSV startup failover limits."""

import asyncio

import httpx
import pytest

from config import settings
from tests.test_live_recovery import manager as manager, sources, A, B, C
from tests.test_qsv_input_recovery import setup_input, broken, good
from ts_canonicalizer import TSCanonicalizationError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disconnect", ["cleanup", "cancel", "replace", "generation", "ownership"]
)
@pytest.mark.parametrize("stage", ["resolver", "detach"])
async def test_adapter_resolver_does_not_revive_disconnected_request(
    manager, monkeypatch, disconnect, stage
):
    sid = await setup_input(manager)
    opened, closed = sources(monkeypatch, manager, {A: broken, B: good})
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(stream_id, reason):
        if stage == "resolver":
            entered.set()
            await release.wait()
        return B

    detach = manager._detach_direct_generation

    async def blocked_detach(*args, **kwargs):
        result = await detach(*args, **kwargs)
        if stage == "detach":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(manager, "_detach_direct_generation", blocked_detach)
    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    connection = manager.clients["encoder"].active_connection_id
    read = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(entered.wait(), 1)
    if disconnect == "cleanup":
        await manager.cleanup_client("encoder")
    elif disconnect == "cancel":
        manager.connection_cancel_events[connection].set()
    elif disconnect == "replace":
        manager.clients["encoder"].active_connection_id = "replacement"
    elif disconnect == "generation":
        manager.streams[sid].direct_generation += 1
    else:
        parent = manager.streams[sid].normalization_parent_id
        manager.streams[parent].normalization_input_id = None
    release.set()
    try:
        with pytest.raises((TSCanonicalizationError, StopAsyncIteration)):
            await asyncio.wait_for(read, 1)
        assert opened == [A]
        assert closed == [True]
        if disconnect == "cleanup":
            assert "encoder" not in manager.clients
        elif disconnect == "replace":
            assert manager.clients["encoder"].active_connection_id == "replacement"
    finally:
        await response.body_iterator.aclose()
        await manager.cleanup_client("encoder")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 0])
@pytest.mark.parametrize("first", ["transport", "adapter"])
async def test_transport_and_adapter_share_one_response_budget(
    manager, monkeypatch, limit, first
):
    monkeypatch.setattr(settings, "MAX_FAILOVER_ATTEMPTS", limit)
    sid = await setup_input(manager)

    async def transport_error():
        raise httpx.ReadError("synthetic read failure")
        yield  # pragma: no cover

    factories = {A: transport_error, B: broken, C: good}
    if first == "adapter":
        factories = {A: broken, B: transport_error, C: good}
    opened, closed = sources(monkeypatch, manager, factories)
    calls = []

    async def resolve(stream_id, reason):
        calls.append(reason)
        return B if len(calls) == 1 else C

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    try:
        if limit == 1:
            try:
                data = await asyncio.wait_for(anext(response.body_iterator), 1)
            except (TSCanonicalizationError, StopAsyncIteration):
                data = None
            assert not data
            assert len(calls) == 1
            assert opened == [A, B]
        else:
            assert await asyncio.wait_for(anext(response.body_iterator), 1)
            assert len(calls) == 2
            assert opened == [A, B, C]
    finally:
        await response.body_iterator.aclose()
    assert len(closed) == len(opened)
    assert not manager.connection_cancel_events


@pytest.mark.asyncio
async def test_disconnect_during_replacement_prefetch_never_selects_again(
    manager, monkeypatch
):
    sid = await setup_input(manager)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_broken():
        entered.set()
        await release.wait()
        async for chunk in broken():
            yield chunk

    opened, closed = sources(
        monkeypatch, manager, {A: broken, B: blocked_broken, C: good}
    )
    calls = []

    async def resolve(stream_id, reason):
        calls.append(reason)
        return B if len(calls) == 1 else C

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    response = await manager.stream_continuous_direct(sid, "encoder")
    read = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(entered.wait(), 1)
    await manager.cleanup_client("encoder")
    release.set()
    try:
        with pytest.raises((TSCanonicalizationError, StopAsyncIteration)):
            await asyncio.wait_for(read, 1)
        assert opened == [A, B]
        assert len(calls) == 1
        assert "encoder" not in manager.clients
    finally:
        await response.body_iterator.aclose()
    assert len(closed) == 2


@pytest.mark.asyncio
async def test_failover_budget_is_fresh_for_a_new_response_on_same_stream(
    manager, monkeypatch
):
    monkeypatch.setattr(settings, "MAX_FAILOVER_ATTEMPTS", 1)
    sid = await setup_input(manager)
    factories = {A: broken, B: good, C: good}
    opened, closed = sources(monkeypatch, manager, factories)
    calls = []

    async def resolve(stream_id, reason):
        calls.append(reason)
        return B if len(calls) == 1 else C

    monkeypatch.setattr(manager, "_resolve_next_failover_url", resolve)
    for _ in range(2):
        response = await manager.stream_continuous_direct(sid, "encoder")
        try:
            assert await asyncio.wait_for(anext(response.body_iterator), 1)
        finally:
            await response.body_iterator.aclose()
        factories[B] = broken
    assert len(calls) == 2
    assert opened == [A, B, B, C]
    assert len(closed) == 4
    assert not manager.connection_cancel_events
