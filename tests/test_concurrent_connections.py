"""
Test for concurrent connection race condition fix.

This test verifies that when a client (e.g., Kodi) makes concurrent connections
with the same client_id (e.g., during seeking), the connections don't interfere
with each other.
"""
import pytest
import asyncio
from stream_manager import StreamManager, ClientInfo


@pytest.mark.asyncio
async def test_concurrent_connections_no_race_condition():
    """
    Test that concurrent connections from the same client don't interfere.

    Simulates the scenario where:
    1. Request 1 starts streaming
    2. Request 2 starts (before Request 1 finishes) - e.g., Kodi seeking
    3. Request 1 finishes and cleans up
    4. Request 2 should still be active and not affected by Request 1's cleanup
    """
    # Create stream manager
    manager = StreamManager(redis_url=None, enable_pooling=False)
    await manager.start()

    try:
        # Create a test stream
        stream_url = "http://example.com/stream.ts"
        stream_id = await manager.get_or_create_stream(stream_url)

        # Register client (simulates first connection)
        client_id = "test_client_123"
        await manager.register_client(client_id, stream_id)

        # Simulate Connection 1: Generate unique connection_id
        import uuid
        connection_id_1 = str(uuid.uuid4())
        cancel_event_1 = asyncio.Event()
        manager.connection_cancel_events[connection_id_1] = cancel_event_1
        manager.clients[client_id].active_connection_id = connection_id_1

        # Verify Connection 1 is set up
        assert client_id in manager.clients
        assert manager.clients[client_id].active_connection_id == connection_id_1
        assert connection_id_1 in manager.connection_cancel_events
        assert not cancel_event_1.is_set()

        # Simulate Connection 2 (concurrent - e.g., Kodi seeking)
        connection_id_2 = str(uuid.uuid4())
        cancel_event_2 = asyncio.Event()
        manager.connection_cancel_events[connection_id_2] = cancel_event_2
        manager.clients[client_id].active_connection_id = connection_id_2

        # Verify Connection 2 is now active
        assert manager.clients[client_id].active_connection_id == connection_id_2
        assert connection_id_2 in manager.connection_cancel_events
        assert not cancel_event_2.is_set()

        # Both cancel events should exist
        assert connection_id_1 in manager.connection_cancel_events
        assert connection_id_2 in manager.connection_cancel_events

        # Connection 1 finishes and calls cleanup with its connection_id
        # This should NOT affect Connection 2
        await manager.cleanup_client(client_id, connection_id_1)

        # Verify: Connection 1's cancel event is cleaned up
        assert connection_id_1 not in manager.connection_cancel_events

        # CRITICAL: Connection 2 should still be active and untouched
        assert client_id in manager.clients, "Client should still exist (Connection 2 is active)"
        assert manager.clients[client_id].active_connection_id == connection_id_2, "Active connection should still be Connection 2"
        assert connection_id_2 in manager.connection_cancel_events, "Connection 2's cancel event should still exist"
        assert not cancel_event_2.is_set(), "Connection 2's cancel event should NOT be triggered"

        # Now cleanup Connection 2 (normal completion)
        await manager.cleanup_client(client_id, connection_id_2)

        # Verify: Everything is cleaned up
        assert client_id not in manager.clients
        assert connection_id_2 not in manager.connection_cancel_events

        print("✅ Race condition test PASSED - concurrent connections properly isolated")

    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cleanup_without_connection_id_still_works():
    """
    Test that cleanup_client still works when called without connection_id.

    This is important for backward compatibility with periodic cleanup tasks
    and manual disconnection endpoints.
    """
    manager = StreamManager(redis_url=None, enable_pooling=False)
    await manager.start()

    try:
        stream_url = "http://example.com/stream.ts"
        stream_id = await manager.get_or_create_stream(stream_url)

        client_id = "test_client_456"
        await manager.register_client(client_id, stream_id)

        # Cleanup without connection_id should still work
        await manager.cleanup_client(client_id)

        # Verify client is cleaned up
        assert client_id not in manager.clients

        print("✅ Backward compatibility test PASSED - cleanup without connection_id works")

    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_old_connection_cleanup_doesnt_affect_new_connection():
    """
    Test the exact scenario from the bug report:
    - Connection 1 starts
    - Connection 2 starts (overwrites active_connection_id)
    - Connection 1 finishes
    - Connection 2 should NOT be affected
    """
    manager = StreamManager(redis_url=None, enable_pooling=False)
    await manager.start()

    try:
        stream_url = "http://example.com/stream.ts"
        stream_id = await manager.get_or_create_stream(stream_url)

        client_id = "kodi_client_seeking"
        await manager.register_client(client_id, stream_id)

        import uuid

        # Connection 1: Start streaming
        conn1_id = str(uuid.uuid4())
        event1 = asyncio.Event()
        manager.connection_cancel_events[conn1_id] = event1
        manager.clients[client_id].active_connection_id = conn1_id

        # Connection 2: Kodi seeks, opens new connection
        conn2_id = str(uuid.uuid4())
        event2 = asyncio.Event()
        manager.connection_cancel_events[conn2_id] = event2
        manager.clients[client_id].active_connection_id = conn2_id

        # Connection 1 finishes (timeout, completed, etc.)
        await manager.cleanup_client(client_id, conn1_id)

        # BUG CHECK: Connection 2 should NOT be canceled
        assert not event2.is_set(), "❌ BUG: Connection 2 was incorrectly canceled!"
        assert client_id in manager.clients, "❌ BUG: Client was incorrectly removed!"
        assert conn2_id in manager.connection_cancel_events, "❌ BUG: Connection 2's event was incorrectly deleted!"

        print("✅ Bug fix verified - Connection 2 unaffected by Connection 1's cleanup")

    finally:
        await manager.stop()


if __name__ == "__main__":
    # Run tests
    asyncio.run(test_concurrent_connections_no_race_condition())
    asyncio.run(test_cleanup_without_connection_id_still_works())
    asyncio.run(test_old_connection_cleanup_doesnt_affect_new_connection())
