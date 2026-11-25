"""A client that stops reading must not stall a shared direct stream (#85).

The first client of a shared live stream (the "primary") reads upstream at the
pace it is consumed, so pausing it froze every subscriber, DVR recordings
included, until they gave up on it and opened another provider connection.
"""

import asyncio
import os
import sys
from contextlib import aclosing
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import stream_manager as stream_manager_module  # noqa: E402
from stream_manager import PrimaryOutput, StreamInfo, StreamManager  # noqa: E402

STREAM_ID = "stream-1"


@pytest.fixture(autouse=True)
def short_stall(monkeypatch):
    monkeypatch.setattr(stream_manager_module.settings, "PRIMARY_STALL_SECONDS", 0.05)


class Upstream:
    """A primary reader: broadcasts each chunk, then hands it to its own client."""

    def __init__(self, manager, total=1000, interval=0.005):
        self.manager = manager
        self.total = total
        self.interval = interval
        self.read = 0
        self.closed = False

    async def chunks(self):
        try:
            while self.read < self.total:
                await asyncio.sleep(self.interval)
                chunk = b"%d" % self.read
                self.read += 1
                self.manager._broadcast_chunk_to_subscribers(STREAM_ID, chunk)
                yield chunk
        finally:
            self.closed = True


def make_manager(subscribers=("sub",)):
    manager = StreamManager(redis_url=None, enable_pooling=False)
    now = datetime.now(timezone.utc)
    manager.streams[STREAM_ID] = StreamInfo(
        stream_id=STREAM_ID,
        original_url="http://provider.example/live/user/pass/1.ts",
        created_at=now,
        last_access=now,
        is_live_continuous=True,
    )
    manager._direct_broadcast_primary[STREAM_ID] = "primary"
    queues = {conn: asyncio.Queue(maxsize=512) for conn in subscribers}
    manager._direct_broadcast_queues[STREAM_ID] = dict(queues)
    return manager, queues


async def wait_until(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_subscribers_keep_receiving_while_the_primary_client_is_paused():
    manager, queues = make_manager()
    upstream = Upstream(manager)
    manager._watch_primary_stalls(STREAM_ID)

    async with aclosing(manager._relay_primary("primary", upstream.chunks())) as output:
        assert await anext(output) == b"0"
        await wait_until(lambda: queues["sub"].qsize() >= 20)
        received = [queues["sub"].get_nowait() for _ in range(20)]

    assert received == [b"%d" % i for i in range(20)]


@pytest.mark.asyncio
async def test_resumed_primary_continues_in_order_without_a_gap():
    manager, queues = make_manager()
    upstream = Upstream(manager)
    manager._watch_primary_stalls(STREAM_ID)

    async with aclosing(manager._relay_primary("primary", upstream.chunks())) as output:
        received = [await anext(output)]
        await wait_until(lambda: upstream.read >= 20)
        while len(received) < 60:
            received.append(await anext(output))

    assert received == [b"%d" % i for i in range(60)]
    assert upstream.closed


@pytest.mark.asyncio
async def test_a_long_pause_keeps_only_the_newest_chunks():
    manager, _ = make_manager()
    upstream = Upstream(manager, total=50, interval=0)
    output = PrimaryOutput(upstream.chunks(), max_chunks=10)

    async with aclosing(output.relay()) as relay:
        assert await anext(relay) == b"0"
        output.start_drain(lambda: True)
        await wait_until(lambda: upstream.closed)
        rest = [chunk async for chunk in relay]

    assert rest == [b"%d" % i for i in range(40, 50)]


@pytest.mark.asyncio
async def test_without_subscribers_a_paused_client_still_holds_back_the_upstream():
    manager, _ = make_manager(subscribers=())
    upstream = Upstream(manager)
    manager._watch_primary_stalls(STREAM_ID)

    async with aclosing(manager._relay_primary("primary", upstream.chunks())) as output:
        await anext(output)
        await asyncio.sleep(0.2)
        assert upstream.read == 1


@pytest.mark.asyncio
async def test_reading_ahead_stops_when_the_last_subscriber_leaves():
    manager, queues = make_manager()
    upstream = Upstream(manager)
    manager._watch_primary_stalls(STREAM_ID)

    async with aclosing(manager._relay_primary("primary", upstream.chunks())) as output:
        await anext(output)
        await wait_until(lambda: upstream.read > 5)
        manager._direct_broadcast_queues[STREAM_ID].clear()
        await asyncio.sleep(0.1)
        stopped_at = upstream.read
        await asyncio.sleep(0.1)
        assert upstream.read == stopped_at

    await wait_until(lambda: STREAM_ID not in manager._primary_stall_watchers)


@pytest.mark.asyncio
async def test_a_client_leaving_mid_stall_closes_the_reader():
    manager, _ = make_manager()
    upstream = Upstream(manager)
    manager._watch_primary_stalls(STREAM_ID)

    output = manager._relay_primary("primary", upstream.chunks())
    await anext(output)
    await wait_until(lambda: upstream.read > 5)
    await output.aclose()

    assert upstream.closed
    assert manager._primary_outputs == {}


@pytest.mark.asyncio
async def test_a_first_chunk_read_before_the_response_started_comes_first():
    manager, _ = make_manager()
    upstream = Upstream(manager, total=3)
    source = upstream.chunks()
    first = await anext(source)

    relayed = [c async for c in manager._relay_primary("primary", source, first)]

    assert relayed == [b"0", b"1", b"2"]
