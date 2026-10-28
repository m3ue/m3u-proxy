"""
Configuration for Redis pooling and multi-worker coordination.
"""

import multiprocessing
import os
from config import settings


def get_redis_config() -> dict:
    """Get Redis configuration"""
    # Build Redis URL from components or use explicit URL if provided
    # Format: redis://[:password@]host:port/db
    # Use REDIS_PASSWORD if set, otherwise fall back to API_TOKEN (same as M3U_PROXY_TOKEN)
    # This ensures both m3u-editor and m3u-proxy use the same password
    redis_password = settings.REDIS_PASSWORD or settings.API_TOKEN

    if redis_password:
        redis_url = os.getenv(
            "REDIS_URL",
            f"redis://:{redis_password}@{settings.REDIS_HOST}:{settings.REDIS_SERVER_PORT}/{settings.REDIS_DB}",
        )
    else:
        redis_url = os.getenv(
            "REDIS_URL",
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_SERVER_PORT}/{settings.REDIS_DB}",
        )

    return {
        "host": settings.REDIS_HOST,
        "port": settings.REDIS_SERVER_PORT,
        "password": redis_password,
        "db": settings.REDIS_DB,
        "redis_url": redis_url,
        "enabled": settings.REDIS_ENABLED,
        "pooling_enabled": settings.ENABLE_TRANSCODING_POOLING,
        "max_clients_per_stream": settings.MAX_CLIENTS_PER_SHARED_STREAM,
        "stream_timeout": settings.SHARED_STREAM_TIMEOUT,
        "worker_id": settings.WORKER_ID,
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "cleanup_interval": settings.CLEANUP_INTERVAL,
        "sharing_strategy": settings.STREAM_SHARING_STRATEGY,
    }


def shares_listener_with_workers() -> bool:
    """Whether sibling worker processes accept connections on this process's port.

    Uvicorn --workers (spawned multiprocessing children) and declared worker counts.
    A request to 127.0.0.1 can then reach a process that does not hold the stream.
    Separate instances (one process each, distinguished by WORKER_ID) do not count.
    """
    return multiprocessing.parent_process() is not None or any(
        os.getenv(name, "1") != "1" for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS")
    )


def should_use_pooling() -> bool:
    """Enable Redis pooling or explicitly opted-in, single-process transcoding."""
    if not settings.ENABLE_TRANSCODING_POOLING:
        return False
    if settings.REDIS_ENABLED:
        return True
    if not settings.ENABLE_SINGLE_PROCESS_TRANSCODING:
        return False
    # Reject declared multi-worker topologies before allocating any stream state.
    # External launchers must also be configured for exactly one process.
    if settings.WORKER_ID or shares_listener_with_workers():
        raise ValueError(
            "Single-process transcoding requires a single process without WORKER_ID; "
            "WEB_CONCURRENCY and UVICORN_WORKERS must be unset or 1"
        )
    return True
