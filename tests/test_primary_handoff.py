"""Regression tests for broadcast primary handoff on direct live streams.

When several clients share one direct live stream, the first client (the
"primary") reads upstream and broadcasts chunks to the others. When the primary's
client disconnected, every subscriber used to be told at once, and each promoted
itself and opened its own new upstream connection. The departing primary only
parked its connection for reuse *afterwards* (from its generator's finally
block), so the handoff always went unclaimed. The new connections made the
provider replay its buffer (viewers saw the last 10-20s again) and put extra,
untracked connections on the provider account.

Now exactly one subscriber is promoted, the rest stay attached, only the current
primary can trigger a promotion, and the promoted subscriber waits briefly for
the departing primary's upstream connection instead of racing it.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stream_manager import StreamInfo, StreamManager  # noqa: E402

STREAM_ID = "stream-1"


class FakeUpstream:
    """Async context manager standing in for an httpx streaming response."""

    def __init__(self, chunks, fail_first_read=False):
        self.chunks = list(chunks)
        self.fail_first_read = fail_first_read
        self.closed = False
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size=32768):
        if self.fail_first_read:
            raise ConnectionError("inherited connection is broken")
        for chunk in self.chunks:
            yield chunk


def make_manager(is_vod: bool = False) -> StreamManager:
    manager = StreamManager(redis_url=None, enable_pooling=False)
    now = datetime.now(timezone.utc)
    manager.streams[STREAM_ID] = StreamInfo(
        stream_id=STREAM_ID,
        original_url="http://provider.example/live/user/pass/1.ts",
        created_at=now,
        last_access=now,
        is_vod=is_vod,
        is_live_continuous=True,
    )
    return manager


def attach(manager: StreamManager, primary: str, subscribers: list[str]) -> dict:
    """Register a primary connection and subscriber queues for STREAM_ID."""
    manager._direct_broadcast_primary[STREAM_ID] = primary
    queues = {conn: asyncio.Queue(maxsize=512) for conn in subscribers}
    manager._direct_broadcast_queues[STREAM_ID] = dict(queues)
    for conn in [primary, *subscribers]:
        manager.connection_cancel_events[conn] = asyncio.Event()
    manager.stream_clients[STREAM_ID] = {f"client-{c}" for c in [primary, *subscribers]}
    return queues


def test_signal_promotes_exactly_one_subscriber_and_keeps_the_rest_attached():
    manager = make_manager()
    queues = attach(manager, "primary", ["sub-a", "sub-b"])

    manager._signal_subscribers_end(STREAM_ID, "primary")

    assert queues["sub-a"].get_nowait() is None
    assert queues["sub-b"].empty()
    assert manager._direct_broadcast_primary[STREAM_ID] == "sub-a"
    assert list(manager._direct_broadcast_queues[STREAM_ID]) == ["sub-b"]


def test_repeat_signals_from_the_departing_primary_are_noops():
    manager = make_manager()
    queues = attach(manager, "primary", ["sub-a", "sub-b"])

    manager._signal_subscribers_end(STREAM_ID, "primary")
    # The departing primary's post-loop and outer-finally safety nets fire again.
    manager._signal_subscribers_end(STREAM_ID, "primary")
    manager._signal_subscribers_end(STREAM_ID, "primary")

    assert queues["sub-b"].empty()
    assert manager._direct_broadcast_primary[STREAM_ID] == "sub-a"
    assert "sub-b" in manager._direct_broadcast_queues[STREAM_ID]


def test_signal_without_subscribers_clears_broadcast_state():
    manager = make_manager()
    attach(manager, "primary", [])

    manager._signal_subscribers_end(STREAM_ID, "primary")

    assert STREAM_ID not in manager._direct_broadcast_primary
    assert STREAM_ID not in manager._direct_broadcast_queues


def test_handoff_is_only_expected_when_the_primary_client_disconnected():
    manager = make_manager()
    attach(manager, "primary", ["sub-a"])

    # Upstream ended on its own (cancel event not set): no handoff to wait for.
    manager._signal_subscribers_end(STREAM_ID, "primary")

    assert STREAM_ID not in manager._handoff_settled


@pytest.mark.asyncio
async def test_promoted_subscriber_claims_a_handoff_stored_after_the_signal():
    manager = make_manager()
    attach(manager, "primary", ["sub-a"])
    manager.connection_cancel_events["primary"].set()

    manager._signal_subscribers_end(STREAM_ID, "primary")

    upstream = FakeUpstream([b"x"])
    entry = (upstream, upstream, upstream.aiter_bytes())

    async def departing_primary_finally():
        await asyncio.sleep(0.05)
        manager._offer_upstream_handoff(STREAM_ID, "client-primary", entry)
        manager._settle_handoff(STREAM_ID)

    asyncio.create_task(departing_primary_finally())
    claimed = await asyncio.wait_for(manager._claim_handoff(STREAM_ID), timeout=1)

    assert claimed is entry
    assert STREAM_ID not in manager._handoff_upstream


@pytest.mark.asyncio
async def test_claim_stops_waiting_when_the_primary_settles_without_a_handoff(
    monkeypatch,
):
    import stream_manager

    monkeypatch.setattr(stream_manager.settings, "PRIMARY_HANDOFF_WAIT_SECONDS", 5.0)
    manager = make_manager()
    attach(manager, "primary", ["sub-a"])
    manager.connection_cancel_events["primary"].set()
    manager._signal_subscribers_end(STREAM_ID, "primary")

    asyncio.get_running_loop().call_later(0.05, manager._settle_handoff, STREAM_ID)
    claimed = await asyncio.wait_for(manager._claim_handoff(STREAM_ID), timeout=1)

    assert claimed is None


def promote(manager: StreamManager, connection_id: str, cancel_event=None):
    return manager._promoted_primary_generate(
        STREAM_ID,
        f"client-{connection_id}",
        connection_id,
        cancel_event or asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_promoted_primary_reuses_inherited_upstream_and_feeds_other_subscribers():
    manager = make_manager()
    queues = attach(manager, "primary", ["sub-a", "sub-b"])
    manager.live_stream_client = MagicMock()
    manager.connection_cancel_events["primary"].set()
    manager._signal_subscribers_end(STREAM_ID, "primary")

    inherited = FakeUpstream([b"one", b"two"])
    manager._handoff_upstream[STREAM_ID] = (
        inherited,
        inherited,
        inherited.aiter_bytes(),
    )

    received = [chunk async for chunk in promote(manager, "sub-a")]

    assert received == [b"one", b"two"]
    manager.live_stream_client.stream.assert_not_called()
    assert queues["sub-b"].get_nowait() == b"one"
    assert queues["sub-b"].get_nowait() == b"two"


@pytest.mark.asyncio
async def test_promoted_primary_falls_back_to_a_fresh_upstream_when_inherit_is_broken():
    manager = make_manager()
    attach(manager, "primary", ["sub-a"])
    fresh = FakeUpstream([b"fresh"])
    manager.live_stream_client = MagicMock()
    manager.live_stream_client.stream.return_value = fresh

    broken = FakeUpstream([], fail_first_read=True)
    manager._handoff_upstream[STREAM_ID] = (broken, broken, broken.aiter_bytes())

    received = [chunk async for chunk in promote(manager, "sub-a")]

    assert received == [b"fresh"]
    assert broken.closed
    manager.live_stream_client.stream.assert_called_once()


@pytest.mark.asyncio
async def test_promoted_primary_hands_off_again_when_its_own_client_leaves():
    manager = make_manager()
    queues = attach(manager, "primary", ["sub-a", "sub-b"])
    manager.live_stream_client = MagicMock()
    manager._signal_subscribers_end(STREAM_ID, "primary")

    inherited = FakeUpstream([b"one", b"two", b"three"])
    manager._handoff_upstream[STREAM_ID] = (
        inherited,
        inherited,
        inherited.aiter_bytes(),
    )

    cancel_event = manager.connection_cancel_events["sub-a"]
    generator = promote(manager, "sub-a", cancel_event)
    assert await generator.__anext__() == b"one"

    # sub-a's client disconnects while sub-b is still watching.
    manager.stream_clients[STREAM_ID].discard("client-sub-a")
    cancel_event.set()
    await generator.aclose()

    assert not inherited.closed
    assert STREAM_ID in manager._handoff_upstream
    assert manager._direct_broadcast_primary[STREAM_ID] == "sub-b"
    assert queues["sub-b"].get_nowait() == b"one"
    assert queues["sub-b"].get_nowait() is None


@pytest.mark.asyncio
async def test_unclaimed_handoff_cleanup_leaves_a_newer_handoff_alone(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        await real_sleep(0)

    manager = make_manager()
    older = FakeUpstream([])
    newer = FakeUpstream([])

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    manager._offer_upstream_handoff(STREAM_ID, "client-a", (older, older, None))
    newer_entry = (newer, newer, None)
    manager._handoff_upstream[STREAM_ID] = newer_entry
    for _ in range(5):
        await real_sleep(0)

    assert not newer.closed
    assert manager._handoff_upstream[STREAM_ID] is newer_entry


@pytest.mark.asyncio
async def test_shielded_reader_keeps_a_cancelled_read_for_the_next_consumer():
    from stream_manager import ShieldedUpstreamReader

    release = asyncio.Event()

    async def upstream():
        yield b"first"
        await release.wait()
        yield b"second"
        yield b"third"

    reader = ShieldedUpstreamReader(upstream())
    assert await reader.__anext__() == b"first"

    # The departing primary's task is cancelled mid-read (client disconnected).
    read = asyncio.create_task(reader.__anext__())
    await asyncio.sleep(0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    # The promoted subscriber inherits the reader and gets the in-flight chunk.
    release.set()
    assert await reader.__anext__() == b"second"
    assert await reader.__anext__() == b"third"
    with pytest.raises(StopAsyncIteration):
        await reader.__anext__()


@pytest.mark.asyncio
async def test_shielded_reader_survives_a_chunk_timeout():
    from stream_manager import ShieldedUpstreamReader

    release = asyncio.Event()

    async def upstream():
        await release.wait()
        yield b"late"

    reader = ShieldedUpstreamReader(upstream())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(reader.__anext__(), timeout=0.01)

    release.set()
    assert await reader.__anext__() == b"late"


def test_signal_skips_subscribers_whose_client_already_disconnected():
    manager = make_manager()
    queues = attach(manager, "primary", ["sub-gone", "sub-live"])
    manager.connection_cancel_events["sub-gone"].set()

    manager._signal_subscribers_end(STREAM_ID, "primary")

    assert queues["sub-gone"].empty()
    assert queues["sub-live"].get_nowait() is None
    assert manager._direct_broadcast_primary[STREAM_ID] == "sub-live"
    assert (
        STREAM_ID not in manager._direct_broadcast_queues
        or not (manager._direct_broadcast_queues[STREAM_ID])
    )


@pytest.mark.asyncio
async def test_last_viewer_leaving_closes_the_upstream_instead_of_parking_it():
    manager = make_manager()
    attach(manager, "primary", ["sub-a"])
    manager.live_stream_client = MagicMock()
    manager._signal_subscribers_end(STREAM_ID, "primary")

    inherited = FakeUpstream([b"one", b"two"])
    manager._handoff_upstream[STREAM_ID] = (
        inherited,
        inherited,
        inherited.aiter_bytes(),
    )

    cancel_event = manager.connection_cancel_events["sub-a"]
    generator = promote(manager, "sub-a", cancel_event)
    assert await generator.__anext__() == b"one"

    # sub-a was the only viewer left.
    manager.stream_clients[STREAM_ID] = set()
    cancel_event.set()
    await generator.aclose()

    assert inherited.closed
    assert STREAM_ID not in manager._handoff_upstream
    assert STREAM_ID not in manager._direct_broadcast_primary


def test_upstream_is_not_handed_off_for_vod_or_a_live_upstream_failure():
    manager = make_manager(is_vod=True)
    manager.stream_clients[STREAM_ID] = {"client-a", "client-b"}
    disconnected = asyncio.Event()
    disconnected.set()
    assert not manager._can_hand_off_upstream(STREAM_ID, "client-a", disconnected)

    manager = make_manager()
    manager.stream_clients[STREAM_ID] = {"client-a", "client-b"}
    assert manager._can_hand_off_upstream(STREAM_ID, "client-a", disconnected)
    # Upstream ended/failed rather than the client disconnecting.
    assert not manager._can_hand_off_upstream(STREAM_ID, "client-a", asyncio.Event())
