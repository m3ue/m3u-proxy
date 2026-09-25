"""Regression test for the ASGI disconnect monitor's client cleanup.

Previously, the disconnect monitor only set the connection's cancel_event and
signaled broadcast subscribers on client disconnect — it never called
cleanup_client() itself. Removal of the stale client record was left to the
streaming generator's own post-loop code (which often can't run, since
Starlette stops iterating a generator once the client is gone) or the
periodic sweep, which only evicts a client after CLIENT_TIMEOUT seconds of
staleness and only runs every 30s. In practice this meant a client could
stay "connected" for tens of seconds after actually disconnecting — most
visible on VOD streams, where the read loop goes long enough between chunks
that it rarely gets a chance to notice cancel_event itself.

The monitor now calls cleanup_client() directly as soon as it observes the
ASGI http.disconnect message, so the client record — and thus active
client/stream counts — update immediately.
"""

import asyncio
import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from api import _start_disconnect_monitor  # noqa: E402


@pytest.mark.asyncio
async def test_disconnect_monitor_calls_cleanup_client_immediately():
    sm = MagicMock()
    sm.cleanup_client = AsyncMock()
    sm._signal_subscribers_end = MagicMock()
    sm._direct_broadcast_primary = {}

    client_info = MagicMock()
    client_info.active_connection_id = "conn-1"
    sm.clients = {"client-1": client_info}

    cancel_event = asyncio.Event()
    sm.connection_cancel_events = {"conn-1": cancel_event}

    request = MagicMock()
    request.receive = AsyncMock(return_value={"type": "http.disconnect"})

    created_tasks = []
    real_create_task = asyncio.create_task

    def capture_task(coro):
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    with patch("api.asyncio.create_task", side_effect=capture_task):
        _start_disconnect_monitor(request, "client-1", sm)
        await created_tasks[0]

    assert cancel_event.is_set()
    sm.cleanup_client.assert_awaited_once_with("client-1", "conn-1")


@pytest.mark.asyncio
async def test_disconnect_monitor_signals_broadcast_subscribers_before_cleanup():
    sm = MagicMock()
    sm.cleanup_client = AsyncMock()
    sm._signal_subscribers_end = MagicMock()
    sm._direct_broadcast_primary = {"stream-1": "conn-1"}

    client_info = MagicMock()
    client_info.active_connection_id = "conn-1"
    client_info.stream_id = "stream-1"
    sm.clients = {"client-1": client_info}

    cancel_event = asyncio.Event()
    sm.connection_cancel_events = {"conn-1": cancel_event}

    request = MagicMock()
    request.receive = AsyncMock(return_value={"type": "http.disconnect"})

    created_tasks = []
    real_create_task = asyncio.create_task

    def capture_task(coro):
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    with patch("api.asyncio.create_task", side_effect=capture_task):
        _start_disconnect_monitor(request, "client-1", sm)
        await created_tasks[0]

    sm._signal_subscribers_end.assert_called_once_with("stream-1", "conn-1")
    sm.cleanup_client.assert_awaited_once_with("client-1", "conn-1")


@pytest.mark.asyncio
async def test_disconnect_monitor_noop_when_disabled(monkeypatch):
    import api

    monkeypatch.setattr(api.settings, "DISABLE_ASGI_DISCONNECT_MONITOR", True)

    sm = MagicMock()
    sm.cleanup_client = AsyncMock()

    request = MagicMock()
    request.receive = AsyncMock(return_value={"type": "http.disconnect"})

    with patch("api.asyncio.create_task") as mock_create_task:
        _start_disconnect_monitor(request, "client-1", sm)
        mock_create_task.assert_not_called()
