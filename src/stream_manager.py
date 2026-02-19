"""
Stream Manager with Separate Proxy Paths
This version implements efficient per-client proxying for continuous streams
while maintaining the shared buffer approach for HLS segments.
"""

import m3u8
import asyncio
import httpx
import logging
import subprocess
import signal
import os
import time
import re
import uuid
from typing import Dict, Optional, AsyncIterator, List, Set, Any
from urllib.parse import urljoin, urlparse, quote, unquote
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from asyncio import Queue
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ClientInfo:
    client_id: str
    created_at: datetime
    last_access: datetime
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    # Username for tracking auth (from m3u-editor)
    username: Optional[str] = None
    stream_id: Optional[str] = None
    bytes_served: int = 0
    segments_served: int = 0
    is_connected: bool = True
    # Connection idle tracking - for monitoring long-held connections
    last_data_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    # Alert flag to prevent duplicate warnings
    idle_warning_logged: bool = False
    idle_error_logged: bool = False
    # Active connection ID - tracks the currently active streaming connection
    # Used to prevent race conditions when multiple concurrent connections share the same client_id
    active_connection_id: Optional[str] = None


@dataclass
class StreamInfo:
    stream_id: str
    original_url: str
    created_at: datetime
    last_access: datetime
    client_count: int = 0
    total_bytes_served: int = 0
    total_segments_served: int = 0
    error_count: int = 0
    is_active: bool = True
    failover_urls: List[str] = field(default_factory=list)
    failover_resolver_url: Optional[str] = None
    current_failover_index: int = 0
    current_url: Optional[str] = None
    final_playlist_url: Optional[str] = None
    user_agent: str = settings.DEFAULT_USER_AGENT
    # Track connected clients for stats
    connected_clients: Set[str] = field(default_factory=set)
    # Failover management
    failover_attempts: int = 0
    last_failover_time: Optional[datetime] = None
    connection_timeout: float = settings.DEFAULT_CONNECTION_TIMEOUT
    read_timeout: float = settings.DEFAULT_READ_TIMEOUT
    max_retries: int = settings.DEFAULT_MAX_RETRIES
    backoff_factor: float = settings.DEFAULT_BACKOFF_FACTOR
    health_check_interval: float = settings.DEFAULT_HEALTH_CHECK_INTERVAL
    last_health_check: Optional[datetime] = None
    # Failover event - signals all clients to reconnect
    failover_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Stream type detection
    is_hls: bool = False
    is_vod: bool = False
    is_live_continuous: bool = False
    # HLS variant tracking - for variant playlists that are part of a master playlist
    parent_stream_id: Optional[str] = None
    is_variant_stream: bool = False
    # Custom metadata - arbitrary key/value pairs for external identification
    metadata: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    # Transcoding configuration
    is_transcoded: bool = False
    transcode_profile: Optional[str] = None
    transcode_ffmpeg_args: List[str] = field(default_factory=list)
    transcode_process: Optional[asyncio.subprocess.Process] = None
    # Key used by the pooled manager to identify shared transcoding processes
    transcode_stream_key: Optional[str] = None
    # Strict Live TS Mode - improved handling for live MPEG-TS streams
    strict_live_ts: bool = False
    # Circuit breaker - track bad upstream endpoints temporarily
    upstream_marked_bad_until: Optional[datetime] = None
    # Sticky Session Handler - lock to specific backend origin after redirects
    # Prevents playback loops from load balancers bouncing between origins
    use_sticky_session: bool = False
    # Bitrate monitoring - track bytes and detect degraded streams
    bitrate_check_start_time: Optional[float] = None
    bitrate_bytes_window: int = 0
    low_bitrate_count: int = 0
    bitrate_monitoring_started: bool = False


@dataclass
class ProxyStats:
    total_streams: int = 0
    active_streams: int = 0
    total_clients: int = 0
    active_clients: int = 0
    total_bytes_served: int = 0
    total_segments_served: int = 0
    uptime_start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    connection_pool_stats: Dict = field(default_factory=dict)
    failover_stats: Dict = field(default_factory=dict)


class M3U8Processor:
    def __init__(self, base_url: str, client_id: str, user_agent: Optional[str] = None, original_url: Optional[str] = None, parent_stream_id: Optional[str] = None):
        self.base_url = base_url
        self.client_id = client_id
        self.user_agent = user_agent or settings.DEFAULT_USER_AGENT
        self.original_url = original_url or base_url
        self.parent_stream_id = parent_stream_id

    def process_playlist(self, content: str, base_proxy_url: str, original_base_url: Optional[str] = None) -> str:
        """Process M3U8 content and rewrite segment URLs using the m3u8 library."""
        try:
            playlist = m3u8.loads(
                content, uri=original_base_url or self.original_url)

            # Handle both variant playlists (master) and media playlists
            if playlist.is_variant:
                for variant in playlist.playlists:
                    variant.uri = self._rewrite_url(
                        variant.absolute_uri, base_proxy_url)
                for media in playlist.media:
                    if media.uri:
                        media.uri = self._rewrite_url(
                            media.absolute_uri, base_proxy_url)
            else:
                for segment in playlist.segments:
                    segment.uri = self._rewrite_url(
                        segment.absolute_uri, base_proxy_url)
                # Handle initialization section if present
                for seg_map in (playlist.segment_map if isinstance(playlist.segment_map, list) else []):
                    if hasattr(seg_map, 'uri') and seg_map.uri:
                        seg_map.uri = self._rewrite_url(
                            seg_map.absolute_uri, base_proxy_url)

            return playlist.dumps()
        except Exception as e:
            logger.error(f"Error processing M3U8 playlist: {e}")
            return content

    def _rewrite_url(self, original_url: str, base_proxy_url: str) -> str:
        """Rewrites a URL to point to the proxy, encoding the original URL."""
        encoded_url = quote(original_url, safe='')
        if original_url.endswith('.m3u8'):
            # For variant playlists, include parent stream ID
            parent_param = f"&parent={self.parent_stream_id}" if self.parent_stream_id else ""
            return f"{base_proxy_url}/playlist.m3u8?url={encoded_url}&client_id={self.client_id}{parent_param}"
        else:
            return f"{base_proxy_url}/segment.ts?url={encoded_url}&client_id={self.client_id}"


class StreamManager:
    def __init__(self, redis_url: Optional[str] = None, enable_pooling: bool = True):
        self.streams: Dict[str, StreamInfo] = {}
        self.clients: Dict[str, ClientInfo] = {}
        self.stream_clients: Dict[str, Set[str]] = {}
        self.client_timeout = settings.CLIENT_TIMEOUT
        self.stream_timeout = settings.STREAM_TIMEOUT

        # Track cancellation flags for active streaming generators
        # Key: connection_id (unique per streaming request), Value: asyncio.Event that gets set when stream should stop
        # Changed from client_id to connection_id to fix race condition when clients make concurrent connections
        self.connection_cancel_events: Dict[str, asyncio.Event] = {}

        # Pooling configuration
        self.enable_pooling = enable_pooling
        # Will be PooledStreamManager if available
        self.pooled_manager: Optional[Any] = None

        # Redis configuration
        if redis_url and enable_pooling:
            try:
                from pooled_stream_manager import PooledStreamManager
                self.pooled_manager = PooledStreamManager(redis_url=redis_url)
                # Set parent stream manager reference for event coordination
                self.pooled_manager.set_parent_stream_manager(self)
                logger.info("Redis pooling enabled")
            except ImportError:
                logger.warning(
                    "Redis pooling requested but pooled_stream_manager not available")
            except Exception as e:
                logger.warning(f"Failed to initialize Redis pooling: {e}")
        elif enable_pooling:
            logger.info("Pooling enabled in single-worker mode (no Redis)")
            try:
                from pooled_stream_manager import PooledStreamManager
                self.pooled_manager = PooledStreamManager(enable_sharing=False)
            except ImportError:
                logger.warning("pooled_stream_manager not available")
        else:
            logger.info("Connection pooling disabled")

        # Optimized HTTP clients with connection pooling
        # VOD Client: Handles Video On Demand streams with extended timeouts
        # - read: Tolerates upstream CDN stalls and re-buffering up to 5 minutes
        # - write: Allows clients to pause content for up to 1 hour without losing session
        # - pool: Standard pool timeout to prevent connection exhaustion
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.DEFAULT_CONNECTION_TIMEOUT,  # Fail fast if upstream is down
                read=settings.VOD_READ_TIMEOUT,  # Allow upstream stalls/CDN delays
                write=settings.VOD_WRITE_TIMEOUT,  # Allow extended client pause periods
                pool=10.0  # Standard pool timeout
            ),
            follow_redirects=True,
            max_redirects=10,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0
            )
        )

        # Live TV Client: Handles live continuous streams with client backpressure tolerance
        # - connect: Default (fail fast on upstream unavailability)
        # - read: Default (upstream should be live, data constantly flowing)
        # - write: Extended timeout to handle client buffer fills without dropping connection
        #          Clients may pause reading when their buffer is full; we wait up to 30 minutes
        #          for them to drain, balancing against resource exhaustion (vs infinite timeout)
        # - pool: Standard pool timeout to prevent connection exhaustion
        self.live_stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.DEFAULT_CONNECTION_TIMEOUT,  # Fail fast if upstream is down
                read=settings.DEFAULT_READ_TIMEOUT,  # Live data should flow continuously
                write=settings.LIVE_TV_WRITE_TIMEOUT,  # Support client backpressure/buffering
                pool=10.0  # Standard pool timeout
            ),
            follow_redirects=True,
            max_redirects=10,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=50,
                keepalive_expiry=30.0
            )
        )

        self._stats = ProxyStats()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._running = False
        self.event_manager = None

    def set_event_manager(self, event_manager):
        """Set the event manager for emitting events"""
        self.event_manager = event_manager
        # Also set it on the pooled manager if available
        if self.pooled_manager:
            self.pooled_manager.set_event_manager(event_manager)

    async def _emit_event(self, event_type: str, stream_id: str, data: dict):
        """Helper method to emit events if event manager is available"""
        if self.event_manager:
            try:
                from models import StreamEvent, EventType
                event = StreamEvent(
                    event_type=getattr(EventType, event_type),
                    stream_id=stream_id,
                    data=data
                )
                await self.event_manager.emit_event(event)
            except Exception as e:
                logger.error(f"Error emitting event: {e}")

    async def start(self):
        """Start the stream manager"""
        self._running = True

        # Start pooled manager if available
        if self.pooled_manager:
            await self.pooled_manager.start()

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        # Disable health checks for now until we can come up with a better approach
        # Using get/head requests can interfere with live streams, or 502 errors
        # Need to instead check the health during actual streaming requests
        # self._health_check_task = asyncio.create_task(
        #     self._periodic_health_check())

        mode = "with Redis pooling" if (
            self.pooled_manager and self.pooled_manager.enable_sharing) else "single-worker"
        logger.info(
            f"Stream manager started {mode} and optimized connection pooling")

    async def stop(self):
        """Stop the stream manager"""
        self._running = False

        # Stop pooled manager
        if self.pooled_manager:
            await self.pooled_manager.stop()

        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._health_check_task:
            self._health_check_task.cancel()
        await self.http_client.aclose()
        await self.live_stream_client.aclose()
        logger.info("Stream manager stopped")

    def _detect_stream_type(self, url: str) -> tuple[bool, bool, bool]:
        """Detect stream type: (is_hls, is_vod, is_live_continuous)"""
        url_lower = url.lower()

        # HLS detection
        if url_lower.endswith('.m3u8'):
            return (True, False, False)

        # VOD/Timeshift detection - these should NOT use strict mode
        if url_lower.endswith(('.mp4', '.mkv', '.webm', '.avi')) or '/timeshift/' in url_lower or '/movie/' in url_lower or '/series/' in url_lower:
            return (False, True, False)

        # Live continuous stream (.ts or live path)
        if url_lower.endswith('.ts') or '/live/' in url_lower:
            return (False, False, True)

        # Default: treat as live continuous
        return (False, False, True)

    async def get_or_create_stream(
        self,
        stream_url: str,
        failover_urls: Optional[List[str]] = None,
        failover_resolver_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        parent_stream_id: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        is_transcoded: bool = False,
        transcode_profile: Optional[str] = None,
        transcode_ffmpeg_args: Optional[List[str]] = None,
        strict_live_ts: Optional[bool] = None,
        use_sticky_session: Optional[bool] = None
    ) -> str:
        """Get or create a stream and return its ID

        Args:
            stream_url: The URL of the stream
            failover_urls: Optional list of failover URLs (legacy)
            failover_resolver_url: Optional callback URL for smart failover (preferred)
            user_agent: Optional user agent string
            parent_stream_id: Optional parent stream ID for variant playlists
            metadata: Optional custom key/value pairs for external identification
            headers: Optional dictionary of custom headers
            is_transcoded: Whether this stream should be transcoded
            transcode_profile: Name of the transcoding profile to use
            transcode_ffmpeg_args: FFmpeg arguments for transcoding
            strict_live_ts: Enable Strict Live TS Mode for this stream
            use_sticky_session: Enable Sticky Session Handler for this stream (defaults to config setting)
        """
        import hashlib
        stream_id = hashlib.md5(stream_url.encode()).hexdigest()

        if stream_id not in self.streams:
            now = datetime.now(timezone.utc)
            if user_agent is None:
                user_agent = settings.DEFAULT_USER_AGENT

            # Detect stream type
            is_hls, is_vod, is_live_continuous = self._detect_stream_type(
                stream_url)

            # If this is a variant stream, inherit user agent from parent
            is_variant = parent_stream_id is not None
            if is_variant and parent_stream_id in self.streams:
                user_agent = self.streams[parent_stream_id].user_agent

            # Determine use_sticky_session: use parameter if provided, otherwise use global config
            effective_use_sticky_session = use_sticky_session if use_sticky_session is not None else settings.USE_STICKY_SESSION

            self.streams[stream_id] = StreamInfo(
                stream_id=stream_id,
                original_url=stream_url,
                current_url=stream_url,
                created_at=now,
                last_access=now,
                failover_urls=failover_urls or [],
                failover_resolver_url=failover_resolver_url,
                user_agent=user_agent,
                is_hls=is_hls,
                is_vod=is_vod,
                is_live_continuous=is_live_continuous,
                parent_stream_id=parent_stream_id,
                is_variant_stream=is_variant,
                metadata=metadata or {},
                headers=headers or {},
                is_transcoded=is_transcoded,
                transcode_profile=transcode_profile,
                transcode_ffmpeg_args=transcode_ffmpeg_args or [],
                strict_live_ts=strict_live_ts or False,
                use_sticky_session=effective_use_sticky_session
            )
            self.stream_clients[stream_id] = set()

            # Only count non-variant streams in stats
            if not is_variant:
                self._stats.total_streams += 1
                self._stats.active_streams += 1

            stream_type = "Transcoding" if is_transcoded else (
                "HLS" if is_hls else ("VOD" if is_vod else "Live Continuous"))
            variant_info = f" (variant of {parent_stream_id})" if is_variant else ""
            logger.info(
                f"Created new stream: {stream_id} ({stream_type}){variant_info} with user agent: {user_agent}")

        self.streams[stream_id].last_access = datetime.now(timezone.utc)
        return stream_id

    async def register_client(
        self,
        client_id: str,
        stream_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
        username: Optional[str] = None
    ) -> ClientInfo:
        """Register a client for a stream

        If the stream is a variant, the client is registered with the parent stream instead.
        """
        now = datetime.now(timezone.utc)

        # If this is a variant stream, register client with the parent instead
        effective_stream_id = stream_id
        if stream_id in self.streams:
            stream = self.streams[stream_id]
            if stream.is_variant_stream and stream.parent_stream_id:
                effective_stream_id = stream.parent_stream_id
                logger.debug(
                    f"Redirecting client registration from variant {stream_id} to parent {effective_stream_id}")

        if client_id not in self.clients:
            self.clients[client_id] = ClientInfo(
                client_id=client_id,
                created_at=now,
                last_access=now,
                user_agent=user_agent,
                ip_address=ip_address,
                username=username,
                stream_id=effective_stream_id
            )
            self._stats.total_clients += 1
            self._stats.active_clients += 1
            logger.info(
                f"Registered new client: {client_id}" + (f" (username: {username})" if username else ""))

        if effective_stream_id in self.stream_clients:
            self.stream_clients[effective_stream_id].add(client_id)
            self.streams[effective_stream_id].client_count = len(
                self.stream_clients[effective_stream_id])
            self.streams[effective_stream_id].connected_clients.add(client_id)

        client_info = self.clients[client_id]
        client_info.last_access = now
        client_info.stream_id = effective_stream_id
        client_info.is_connected = True
        # Update username if provided (may be set on subsequent requests)
        if username:
            client_info.username = username

        await self._emit_event("CLIENT_CONNECTED", effective_stream_id, {
            "client_id": client_id,
            "user_agent": user_agent,
            "ip_address": ip_address,
            "username": username,
            "stream_client_count": len(self.stream_clients[effective_stream_id]) if effective_stream_id in self.stream_clients else 0,
            "metadata": self.streams[effective_stream_id].metadata if effective_stream_id in self.streams else {}
        })

        return client_info

    async def cleanup_client(self, client_id: str, connection_id: Optional[str] = None):
        """Clean up a client and signal its streaming generator to stop

        Args:
            client_id: The client identifier
            connection_id: Optional connection identifier. If provided, only cleanup if it matches
                          the active connection. This prevents race conditions when clients make
                          concurrent connections.
        """
        if client_id in self.clients:
            client_info = self.clients[client_id]
            stream_id = client_info.stream_id

            # Check if this cleanup request is for the current active connection
            # If connection_id is provided but doesn't match, skip cleanup to prevent
            # race condition where an old connection cleans up a new one
            if connection_id and client_info.active_connection_id != connection_id:
                logger.debug(
                    f"Skipping cleanup for client {client_id}: connection {connection_id} is not active "
                    f"(active: {client_info.active_connection_id})")
                # Still clean up the cancel event for this specific connection
                if connection_id in self.connection_cancel_events:
                    del self.connection_cancel_events[connection_id]
                return

            # Signal the streaming generator to stop
            if connection_id and connection_id in self.connection_cancel_events:
                self.connection_cancel_events[connection_id].set()
                logger.info(
                    f"Signaled streaming generator to stop for client: {client_id}, connection: {connection_id}")
            elif client_info.active_connection_id and client_info.active_connection_id in self.connection_cancel_events:
                # Fallback: if no connection_id provided, use the active one from client_info
                self.connection_cancel_events[client_info.active_connection_id].set(
                )
                logger.info(
                    f"Signaled streaming generator to stop for client: {client_id}, active connection: {client_info.active_connection_id}")

            if stream_id and stream_id in self.stream_clients:
                self.stream_clients[stream_id].discard(client_id)
                if stream_id in self.streams:
                    self.streams[stream_id].client_count = len(
                        self.stream_clients[stream_id])
                    self.streams[stream_id].connected_clients.discard(
                        client_id)

            await self._emit_event("CLIENT_DISCONNECTED", stream_id or "unknown", {
                "client_id": client_id,
                "connection_id": connection_id,
                "bytes_served": client_info.bytes_served,
                "segments_served": client_info.segments_served,
                "metadata": self.streams[stream_id].metadata if stream_id and stream_id in self.streams else {}
            })
            # Notify pooled manager (if any) that this client is gone so shared
            # transcoding processes can be cleaned up when no clients remain.
            if self.pooled_manager:
                try:
                    await self.pooled_manager.remove_client_from_stream(client_id)
                except Exception as e:
                    logger.error(
                        f"Error notifying pooled manager about client {client_id} removal: {e}")

            del self.clients[client_id]
            self._stats.active_clients -= 1

            # Clean up cancel event for this connection
            if connection_id and connection_id in self.connection_cancel_events:
                del self.connection_cancel_events[connection_id]
            elif client_info.active_connection_id and client_info.active_connection_id in self.connection_cancel_events:
                del self.connection_cancel_events[client_info.active_connection_id]

            logger.info(f"Cleaned up client: {client_id}")

    # ============================================================================
    # DIRECT PROXY FOR CONTINUOUS STREAMS (New Architecture)
    # ============================================================================

    async def stream_continuous_direct(
        self,
        stream_id: str,
        client_id: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """
        Direct byte-for-byte proxy for continuous streams (.ts, .mp4, .mkv).
        Each client gets their own provider connection - NO shared buffer.
        Provider connection is truly ephemeral and only open while streaming.

        When Strict Live TS Mode is enabled (globally via STRICT_LIVE_TS=true or per-stream):
        - Strips Range headers completely for live TS streams
        - Pre-buffers 256-512 KB before sending to client for smoother playback
        - Implements circuit breaker to detect and avoid stalled upstream endpoints
        - Returns HTTP 200 (never 206) with no Content-Length for live streams
        """
        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        # Register this client
        if client_id not in self.clients:
            await self.register_client(client_id, stream_id)

        # Generate a unique connection ID for this streaming request
        # This prevents race conditions when the same client makes concurrent connections (e.g., Kodi seeking)
        connection_id = str(uuid.uuid4())

        # Create cancellation event for this specific connection
        cancel_event = asyncio.Event()
        self.connection_cancel_events[connection_id] = cancel_event

        # Update the client's active connection ID
        if client_id in self.clients:
            self.clients[client_id].active_connection_id = connection_id
            logger.debug(
                f"Client {client_id} now has active connection: {connection_id}")

        # Determine if strict mode is enabled (global or per-stream)
        # NEVER apply strict mode to VOD/timeshift content - it needs more time to start
        strict_mode_enabled = (
            settings.STRICT_LIVE_TS or stream_info.strict_live_ts) and not stream_info.is_vod

        # Check circuit breaker - if upstream marked as bad, try failover immediately
        # Skip for VOD/timeshift since it's provider-specific
        if strict_mode_enabled and stream_info.upstream_marked_bad_until and not stream_info.is_vod:
            if datetime.now(timezone.utc) < stream_info.upstream_marked_bad_until:
                logger.warning(
                    f"Stream {stream_id} upstream marked as bad until {stream_info.upstream_marked_bad_until}, attempting immediate failover")
                has_failover = bool(
                    stream_info.failover_resolver_url or stream_info.failover_urls)
                if has_failover:
                    await self._try_update_failover_url(stream_id, "circuit_breaker_bad_upstream")
                    current_url = stream_info.current_url or stream_info.original_url
                    # Clear the bad marker since we switched to a new upstream
                    stream_info.upstream_marked_bad_until = None
            else:
                # Cooldown expired, clear the marker
                logger.info(
                    f"Stream {stream_id} circuit breaker cooldown expired, clearing bad upstream marker")
                stream_info.upstream_marked_bad_until = None

        if strict_mode_enabled and stream_info.is_live_continuous:
            logger.info(
                f"Starting direct proxy with STRICT LIVE TS MODE for client {client_id}, stream {stream_id}")
        else:
            logger.info(
                f"Starting direct proxy for client {client_id}, stream {stream_id}")

        # Variables to capture from the generator for response headers
        provider_status_code = None
        provider_content_range = None
        provider_content_length = None

        # Parse Range start byte (needed for VOD upstream reconnection)
        range_start = 0
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
            if m:
                range_start = int(m.group(1))

        async def generate():
            """Generator that directly proxies bytes from provider to client with failover support"""
            nonlocal provider_status_code, provider_content_range, provider_content_length

            bytes_served = 0
            resume_from_byte = None
            chunk_count = 0
            response = None
            stream_context = None
            last_stats_update = 0  # Track bytes at last stats update
            vod_reconnects = 0
            failover_count = 0
            max_failovers = 3
            max_vod_reconnects = 5

            # Retry tracking variables for handling temporary connection issues
            retry_count = 0
            max_retries = settings.STREAM_RETRY_ATTEMPTS
            retry_delay = settings.STREAM_RETRY_DELAY
            total_timeout_start = asyncio.get_event_loop().time()
            total_timeout = settings.STREAM_TOTAL_TIMEOUT

            def _recover_sticky_origin_if_needed() -> bool:
                """If sticky session is pinned to a redirected backend that failed, reset to entry URL."""
                if not stream_info.use_sticky_session:
                    return False

                known_urls = [stream_info.original_url] + \
                    (stream_info.failover_urls or [])
                if stream_info.current_url and stream_info.current_url not in known_urls:
                    logger.warning(
                        f"Sticky origin {stream_info.current_url} failed during direct stream. "
                        f"Reverting to configured entry point."
                    )
                    stream_info.current_url = None
                    return True

                return False

            # Main streaming loop with automatic reconnection on failover
            while failover_count <= max_failovers:
                try:
                    # Get current URL (may have changed due to failover)
                    active_url = stream_info.current_url or stream_info.original_url

                    # Emit stream started event (or resumed after failover)
                    if failover_count == 0:
                        await self._emit_event("STREAM_STARTED", stream_id, {
                            "url": active_url,
                            "client_id": client_id,
                            "mode": "direct_proxy",
                            "metadata": stream_info.metadata
                        })
                    else:
                        logger.info(
                            f"Reconnecting client {client_id} to failover URL: {active_url}")

                    # Prepare headers
                    headers = {
                        'User-Agent': stream_info.user_agent,
                        'Referer': f"{urlparse(active_url).scheme}://{urlparse(active_url).netloc}/",
                        'Origin': f"{urlparse(active_url).scheme}://{urlparse(active_url).netloc}",
                        'Accept': '*/*',
                        'Connection': 'keep-alive'
                    }
                    headers.update(stream_info.headers)

                    # IMPORTANT: Do NOT send Range headers for live continuous streams
                    # Live IPTV streams (.ts) are infinite and don't support range requests
                    # Range requests can cause providers to immediately close the connection
                    # In strict mode, we are even more aggressive about stripping Range headers
                    if range_header:
                        if stream_info.is_live_continuous:
                            # Never send Range for live streams
                            if strict_mode_enabled:
                                logger.info(
                                    f"STRICT MODE: Completely stripping Range header for live TS stream: {range_header}")
                            else:
                                logger.info(
                                    f"Ignoring Range header for live stream (not supported)")
                        elif not strict_mode_enabled:
                            # VOD stream, not in strict mode - honor range request
                            if resume_from_byte is not None:
                                headers['Range'] = f"bytes={resume_from_byte}-"
                                logger.info(
                                    f"Resuming VOD upstream from byte {resume_from_byte}")
                            else:
                                headers['Range'] = range_header
                                logger.info(
                                    f"Including Range header for VOD stream: {range_header}")

                    # Select appropriate HTTP client
                    client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client

                    # OPEN provider connection - happens ONLY when client starts consuming
                    logger.info(
                        f"Opening provider connection for {stream_id} to {active_url}")

                    # Get the stream context manager
                    stream_context = client_to_use.stream(
                        'GET', active_url, headers=headers, follow_redirects=True)
                    # If the client returned a coroutine (test stub), await it to get the context manager
                    if asyncio.iscoroutine(stream_context):
                        stream_context = await stream_context
                    # Enter the context to get the response object
                    response = await stream_context.__aenter__()

                    # Now we can call methods on the actual response object
                    response.raise_for_status()

                    # Capture provider response details for proper HTTP 206 handling
                    provider_status_code = response.status_code
                    provider_content_range = response.headers.get(
                        'content-range')
                    provider_content_length = response.headers.get(
                        'content-length')

                    logger.info(
                        f"Provider connected: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                    if provider_content_range:
                        logger.info(
                            f"Provider Content-Range: {provider_content_range}")

                    # Reset retry counter on successful connection
                    # This gives us fresh retries for any issues during streaming
                    if retry_count > 0:
                        logger.info(
                            f"Connection successful after {retry_count} retries, resetting retry counter")
                        retry_count = 0

                    # Create a single iterator for the response stream
                    # IMPORTANT: Async iterators can only be consumed once! We must use the same
                    # iterator for both pre-buffering and main streaming. The pre-buffer phase yields
                    # chunks directly (no storage), then breaks to let the main loop continue seamlessly.
                    stream_iterator = response.aiter_bytes(chunk_size=32768)

                    # Initialize last_chunk_time for circuit breaker tracking
                    last_chunk_time = asyncio.get_event_loop().time()

                    # Pre-buffering for Strict Live TS Mode
                    target_prebuffer = settings.STRICT_LIVE_TS_PREBUFFER_SIZE if strict_mode_enabled and stream_info.is_live_continuous else 0

                    if target_prebuffer > 0:
                        logger.info(
                            f"STRICT MODE: Pre-buffering {target_prebuffer} bytes (~0.5-1s) before streaming to client {client_id}")
                        prebuffer_start = asyncio.get_event_loop().time()
                        prebuffer_timeout = settings.STRICT_LIVE_TS_PREBUFFER_TIMEOUT
                        prebuffer_size = 0
                        prebuffer_chunks = 0
                        # Use per-chunk timeout to avoid blocking indefinitely when upstream stalls
                        chunk_timeout = settings.LIVE_CHUNK_TIMEOUT_SECONDS if hasattr(
                            settings, 'LIVE_CHUNK_TIMEOUT_SECONDS') else 5.0

                        # Pre-buffer by reading from the iterator until we reach target
                        while True:
                            try:
                                if chunk_timeout and chunk_timeout > 0:
                                    chunk = await asyncio.wait_for(stream_iterator.__anext__(), timeout=chunk_timeout)
                                else:
                                    chunk = await stream_iterator.__anext__()

                                # Yield immediately - no separate storage needed
                                yield chunk
                                bytes_served += len(chunk)
                                chunk_count += 1
                                prebuffer_size += len(chunk)
                                prebuffer_chunks += 1

                                # Update last chunk time for circuit breaker
                                last_chunk_time = asyncio.get_event_loop().time()

                                # Check timeout for prebuffer overall
                                if asyncio.get_event_loop().time() - prebuffer_start > prebuffer_timeout:
                                    logger.warning(
                                        f"STRICT MODE: Pre-buffer timeout after {prebuffer_timeout}s, proceeding with {prebuffer_size} bytes")
                                    break

                                # Reached target - break and continue with normal streaming
                                if prebuffer_size >= target_prebuffer:
                                    logger.info(
                                        f"STRICT MODE: Pre-buffer complete: {prebuffer_size} bytes in {prebuffer_chunks} chunks")
                                    break

                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"STRICT MODE: Pre-buffer chunk timeout ({chunk_timeout}s), proceeding with {prebuffer_size} bytes")
                                break
                            except StopAsyncIteration:
                                # Upstream closed during pre-buffer; exit prebuffer loop
                                logger.info(
                                    "STRICT MODE: Upstream closed during pre-buffer")
                                break

                        logger.info(
                            f"STRICT MODE: Emitted pre-buffer, now streaming live for client {client_id}")

                    # Direct byte-for-byte proxy - Continue with the SAME iterator
                    # Track time of last received chunk for circuit breaker
                    circuit_breaker_timeout = settings.STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT if strict_mode_enabled else 0

                    # Continue streaming from where pre-buffer left off (or from start if no pre-buffer)
                    while True:
                        try:
                            # Read next chunk with a per-chunk timeout to detect silent stalls
                            chunk_timeout = settings.LIVE_CHUNK_TIMEOUT_SECONDS if hasattr(
                                settings, 'LIVE_CHUNK_TIMEOUT_SECONDS') else 5.0
                            if chunk_timeout and chunk_timeout > 0:
                                try:
                                    chunk = await asyncio.wait_for(stream_iterator.__anext__(), timeout=chunk_timeout)
                                except asyncio.TimeoutError:
                                    # No data received within chunk timeout
                                    logger.warning(
                                        f"No data received for {chunk_timeout}s from upstream for stream {stream_id}, client {client_id}")

                                    # Check if we've exceeded total timeout across all retries
                                    elapsed_total = asyncio.get_event_loop().time() - total_timeout_start
                                    total_timeout_exceeded = total_timeout > 0 and elapsed_total > total_timeout

                                    # Try retrying the current URL first before failover
                                    allow_initial_retry = (
                                        not stream_info.is_vod) or bytes_served == 0
                                    if retry_count < max_retries and not total_timeout_exceeded and allow_initial_retry:
                                        retry_count += 1
                                        current_delay = retry_delay * \
                                            (1.5 ** (retry_count - 1)
                                             ) if settings.STREAM_RETRY_EXPONENTIAL_BACKOFF else retry_delay
                                        logger.info(
                                            f"Retrying connection for stream {stream_id}, client {client_id} "
                                            f"(attempt {retry_count}/{max_retries}, delay: {current_delay}s)"
                                        )

                                        # Clean up current connection
                                        if stream_context is not None:
                                            try:
                                                await stream_context.__aexit__(None, None, None)
                                            except Exception:
                                                pass
                                        stream_context = None
                                        response = None

                                        # Wait before retrying
                                        await asyncio.sleep(current_delay)

                                        # Break to reconnect with same URL
                                        break

                                    # Retries exhausted or total timeout exceeded, try failover
                                    has_failover = bool(
                                        stream_info.failover_resolver_url or stream_info.failover_urls)
                                    if has_failover and failover_count < max_failovers and not stream_info.is_vod:
                                        logger.info(
                                            f"Retries exhausted, attempting failover due to chunk timeout for client {client_id} "
                                            f"(failover attempt {failover_count + 1}/{max_failovers})")
                                        await self._try_update_failover_url(stream_id, "chunk_timeout_after_retries")
                                        # Reset retry counter for new URL
                                        retry_count = 0
                                        failover_count += 1
                                        if stream_context is not None:
                                            try:
                                                await stream_context.__aexit__(None, None, None)
                                            except Exception:
                                                pass
                                        stream_context = None
                                        response = None
                                        break  # Reconnect with new URL
                                    else:
                                        # Sticky-session recovery path: if we were pinned to a redirected backend,
                                        # reset to the configured entry URL and reconnect once before failing.
                                        if _recover_sticky_origin_if_needed():
                                            logger.info(
                                                f"Retrying stream {stream_id} after sticky origin recovery")
                                            retry_count = 0
                                            if stream_context is not None:
                                                try:
                                                    await stream_context.__aexit__(None, None, None)
                                                except Exception:
                                                    pass
                                            stream_context = None
                                            response = None
                                            continue

                                        # For VOD, upstream can stall; reconnect instead of terminating client stream
                                        if stream_info.is_vod and bytes_served > 0 and vod_reconnects < max_vod_reconnects:
                                            vod_reconnects += 1
                                            resume_from_byte = range_start + bytes_served
                                            logger.warning(
                                                f"VOD chunk timeout ({chunk_timeout}s); reconnecting from byte {resume_from_byte} "
                                                f"(attempt {vod_reconnects}/{max_vod_reconnects})"
                                            )
                                            if stream_context is not None:
                                                try:
                                                    await stream_context.__aexit__(None, None, None)
                                                except Exception:
                                                    pass
                                            stream_context = None
                                            response = None
                                            break  # break inner loop -> reconnect outer loop
                                        else:
                                            reason = "total_timeout_exceeded" if total_timeout_exceeded else "no_failover_available"
                                            await self._emit_event("STREAM_FAILED", stream_id, {
                                                "client_id": client_id,
                                                "error": f"No data received for {chunk_timeout}s",
                                                "error_type": "chunk_timeout",
                                                "reason": reason,
                                                "retry_count": retry_count,
                                                "no_failover": not has_failover
                                            })
                                            # Terminate streaming for this client
                                            return

                            else:
                                chunk = await stream_iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        # Update last chunk time
                        last_chunk_time = asyncio.get_event_loop().time()

                        # Check if streaming should be cancelled
                        if cancel_event.is_set():
                            logger.info(
                                f"Streaming cancelled for client {client_id} by external request")
                            return

                        # Check for failover event
                        if stream_info.failover_event.is_set():
                            stream_info.failover_event.clear()  # Clear immediately to prevent infinite loop
                            logger.info(
                                f"Failover detected for stream {stream_id}, reconnecting client {client_id}")
                            # Close current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None
                            failover_count += 1
                            # Break inner loop to reconnect with new URL
                            break

                        yield chunk
                        bytes_served += len(chunk)
                        chunk_count += 1

                        # Bitrate monitoring - detect degraded streams
                        if settings.ENABLE_BITRATE_MONITORING and stream_info:
                            current_time = asyncio.get_event_loop().time()

                            # Grace period - don't monitor during initial buffering
                            if not stream_info.bitrate_monitoring_started:
                                if stream_info.bitrate_check_start_time is None:
                                    stream_info.bitrate_check_start_time = current_time
                                elif current_time - stream_info.bitrate_check_start_time >= settings.BITRATE_MONITORING_GRACE_PERIOD:
                                    # Grace period over, start real monitoring
                                    stream_info.bitrate_monitoring_started = True
                                    stream_info.bitrate_check_start_time = current_time
                                    stream_info.bitrate_bytes_window = 0
                            else:
                                # Add bytes to current window
                                stream_info.bitrate_bytes_window += len(chunk)

                                # Check if interval elapsed
                                elapsed = current_time - \
                                    (stream_info.bitrate_check_start_time or current_time)
                                if elapsed >= settings.BITRATE_CHECK_INTERVAL:
                                    # Calculate bitrate in bytes/second
                                    bitrate = stream_info.bitrate_bytes_window / elapsed if elapsed > 0 else 0

                                    if bitrate < settings.MIN_BITRATE_THRESHOLD:
                                        stream_info.low_bitrate_count += 1
                                        logger.warning(
                                            f"Low bitrate detected for stream {stream_id}: "
                                            f"{bitrate:.0f} B/s ({bitrate * 8 / 1000:.0f} Kbps), "
                                            f"threshold: {settings.MIN_BITRATE_THRESHOLD * 8 / 1000:.0f} Kbps, "
                                            f"count: {stream_info.low_bitrate_count}/{settings.BITRATE_FAILOVER_THRESHOLD}"
                                        )

                                        # Trigger failover if threshold exceeded
                                        # Skip failover for VOD/timeshift since it's provider-specific
                                        if stream_info.low_bitrate_count >= settings.BITRATE_FAILOVER_THRESHOLD:
                                            has_failover = bool(
                                                stream_info.failover_resolver_url or stream_info.failover_urls)
                                            if has_failover and failover_count < max_failovers and not stream_info.is_vod:
                                                logger.error(
                                                    f"Bitrate consistently below threshold for stream {stream_id}, "
                                                    f"triggering failover (attempt {failover_count + 1}/{max_failovers})"
                                                )
                                                await self._try_update_failover_url(stream_id, "low_bitrate")
                                                # Reset bitrate monitoring for new connection
                                                stream_info.low_bitrate_count = 0
                                                stream_info.bitrate_monitoring_started = False
                                                stream_info.bitrate_check_start_time = None
                                                stream_info.bitrate_bytes_window = 0
                                                failover_count += 1
                                                # Close current connection
                                                if stream_context is not None:
                                                    try:
                                                        await stream_context.__aexit__(None, None, None)
                                                    except Exception:
                                                        pass
                                                stream_context = None
                                                response = None
                                                break  # Reconnect with new URL
                                    else:
                                        # Good bitrate - reset counter
                                        if stream_info.low_bitrate_count > 0:
                                            logger.info(
                                                f"Bitrate recovered for stream {stream_id}: "
                                                f"{bitrate * 8 / 1000:.0f} Kbps, resetting low bitrate counter"
                                            )
                                        stream_info.low_bitrate_count = 0

                                    # Reset window for next interval
                                    stream_info.bitrate_check_start_time = current_time
                                    stream_info.bitrate_bytes_window = 0

                        # Update idle tracking every chunk (important for idle detection accuracy)
                        # This ensures we accurately detect when data is flowing vs stuck
                        if client_id in self.clients:
                            self.clients[client_id].last_data_time = datetime.now(
                                timezone.utc)

                        # Update stats periodically (every 10 chunks = ~320KB)
                        if chunk_count % 10 == 0:
                            # Calculate delta since last update
                            bytes_delta = bytes_served - last_stats_update

                            if client_id in self.clients:
                                self.clients[client_id].last_access = datetime.now(
                                    timezone.utc)
                                self.clients[client_id].bytes_served += bytes_delta
                            if stream_id in self.streams:
                                self.streams[stream_id].last_access = datetime.now(
                                    timezone.utc)
                                self.streams[stream_id].total_bytes_served += bytes_delta
                            self._stats.total_bytes_served += bytes_delta

                            # Update last stats checkpoint
                            last_stats_update = bytes_served

                        # Update stats (lightweight) - log more frequently to debug
                        if chunk_count == 1:
                            logger.info(
                                f"First chunk delivered to client {client_id}: {len(chunk)} bytes")
                        elif chunk_count <= 10:
                            logger.info(
                                f"Chunk {chunk_count} delivered to client {client_id}: {len(chunk)} bytes")
                        elif chunk_count % 100 == 0:
                            logger.info(
                                f"Client {client_id}: {chunk_count} chunks, {bytes_served:,} bytes served")

                    # Check circuit breaker after loop exits (if strict mode enabled)
                    # Skip for VOD/timeshift since failover doesn't make sense for provider-specific content
                    if strict_mode_enabled and circuit_breaker_timeout > 0 and not stream_info.is_vod:
                        time_since_last_chunk = asyncio.get_event_loop().time() - last_chunk_time
                        if time_since_last_chunk > circuit_breaker_timeout:
                            logger.error(
                                f"STRICT MODE: Circuit breaker triggered - no data for {time_since_last_chunk:.1f}s (threshold: {circuit_breaker_timeout}s)")
                            # Mark upstream as bad
                            cooldown_seconds = settings.STRICT_LIVE_TS_CIRCUIT_BREAKER_COOLDOWN
                            stream_info.upstream_marked_bad_until = datetime.now(
                                timezone.utc) + timedelta(seconds=cooldown_seconds)
                            logger.warning(
                                f"STRICT MODE: Marking upstream as bad for {cooldown_seconds}s until {stream_info.upstream_marked_bad_until}")

                            # Try failover if available (check both resolver URL and static list)
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover and failover_count < max_failovers:
                                logger.info(
                                    f"STRICT MODE: Attempting failover due to circuit breaker")
                                await self._try_update_failover_url(stream_id, "circuit_breaker_timeout")
                                failover_count += 1
                                # Close current connection
                                if stream_context is not None:
                                    try:
                                        await stream_context.__aexit__(None, None, None)
                                    except Exception:
                                        pass
                                stream_context = None
                                response = None
                                continue  # Retry with failover URL

                    # If we reach here, the upstream response iterator ended without error.
                    # For live continuous streams, this is unexpected — it likely means the
                    # provider silently closed the connection (e.g., connection limit reached).
                    # Treat this as a failure and attempt failover if available.
                    if stream_info.is_live_continuous and not stream_info.failover_event.is_set():
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if has_failover and failover_count < max_failovers:
                            logger.warning(
                                f"Live stream ended unexpectedly for client {client_id} "
                                f"({chunk_count} chunks, {bytes_served} bytes) — "
                                f"provider may have closed connection. "
                                f"Attempting failover (attempt {failover_count + 1}/{max_failovers})")
                            await self._try_update_failover_url(stream_id, "live_stream_silent_close")

                            # Reset circuit breaker state for the old upstream
                            stream_info.upstream_marked_bad_until = None

                            # Clean up current connection before failover
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None
                            failover_count += 1
                            continue  # Retry with failover URL

                    # Stream completed normally (VOD, or live with no failover available)
                    if not stream_info.failover_event.is_set():
                        logger.info(
                            f"Stream completed for client {client_id}: {chunk_count} chunks, {bytes_served} bytes")
                        await self._emit_event("STREAM_STOPPED", stream_id, {
                            "client_id": client_id,
                            "bytes_served": bytes_served,
                            "chunks_served": chunk_count,
                            "metadata": stream_info.metadata
                        })
                        break  # Exit the failover loop
                    # else: failover event was set, continue to next iteration

                except httpx.ReadError as e:
                    # ReadError often means client disconnected or provider closed connection
                    # This is especially common with Range requests on live streams
                    # MUST be caught before NetworkError since ReadError is a subclass
                    error_str = str(e) if str(e) else "<empty ReadError>"
                    logger.info(
                        f"ReadError for client {client_id}: {error_str} (bytes_served: {bytes_served}, chunk_count: {chunk_count})")

                    if bytes_served == 0:
                        # No data was sent - check if we can retry before failover
                        elapsed_total = asyncio.get_event_loop().time() - total_timeout_start
                        total_timeout_exceeded = total_timeout > 0 and elapsed_total > total_timeout

                        # Try retrying the current URL first
                        allow_initial_retry = (
                            not stream_info.is_vod) or bytes_served == 0
                        if retry_count < max_retries and not total_timeout_exceeded and allow_initial_retry:
                            retry_count += 1
                            current_delay = retry_delay * \
                                (1.5 ** (retry_count - 1)
                                 ) if settings.STREAM_RETRY_EXPONENTIAL_BACKOFF else retry_delay
                            logger.info(
                                f"Retrying connection after ReadError for stream {stream_id}, client {client_id} "
                                f"(attempt {retry_count}/{max_retries}, delay: {current_delay}s)"
                            )

                            # Clean up current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None

                            # Wait before retrying
                            await asyncio.sleep(current_delay)

                            continue  # Retry with same URL

                        # Retries exhausted, try failover if available
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if _recover_sticky_origin_if_needed():
                            logger.info(
                                f"Retrying stream {stream_id} after sticky origin recovery")
                            retry_count = 0
                            continue

                        if has_failover and failover_count < max_failovers and not stream_info.is_vod:
                            logger.info(
                                f"Retries exhausted, attempting automatic failover for client {client_id} (ReadError, no data)")
                            await self._try_update_failover_url(stream_id, "connection_error_after_retries")
                            # Reset retry counter for new URL
                            retry_count = 0
                            failover_count += 1
                            # Clean up current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None
                            continue  # Retry with failover URL
                        else:
                            # No retries or failover available, emit failure event
                            reason = "total_timeout_exceeded" if total_timeout_exceeded else "no_failover_available"
                            await self._emit_event("STREAM_FAILED", stream_id, {
                                "client_id": client_id,
                                "error": error_str,
                                "error_type": "ReadError",
                                "reason": reason,
                                "retry_count": retry_count,
                                "no_failover": not has_failover
                            })
                            break
                    else:
                        # Some data was sent.
                        # For VOD, upstream can drop mid-stream; reconnect using Range instead of treating as client disconnect.
                        if stream_info.is_vod and vod_reconnects < max_vod_reconnects:
                            vod_reconnects += 1
                            resume_from_byte = range_start + bytes_served
                            logger.warning(
                                f"VOD upstream ReadError mid-stream; reconnecting from byte {resume_from_byte} "
                                f"(attempt {vod_reconnects}/{max_vod_reconnects})"
                            )

                            # Clean up current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None

                            continue  # Reconnect outer loop with Range bytes=resume_from_byte-
                        else:
                            # Non-VOD or retries exhausted -> treat as client disconnect
                            logger.info(
                                f"Client {client_id} likely disconnected during streaming")
                            await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                                "client_id": client_id,
                                "bytes_served": bytes_served,
                                "chunks_served": chunk_count,
                                "reason": "read_error_during_stream"
                            })
                            break

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as e:
                    logger.warning(
                        f"Stream error for client {client_id}: {type(e).__name__}: {e}")

                    # Check if we can retry before failover
                    elapsed_total = asyncio.get_event_loop().time() - total_timeout_start
                    total_timeout_exceeded = total_timeout > 0 and elapsed_total > total_timeout

                    # Try retrying the current URL first
                    allow_initial_retry = (
                        not stream_info.is_vod) or bytes_served == 0
                    if retry_count < max_retries and not total_timeout_exceeded and allow_initial_retry:
                        retry_count += 1
                        current_delay = retry_delay * \
                            (1.5 ** (retry_count - 1)
                             ) if settings.STREAM_RETRY_EXPONENTIAL_BACKOFF else retry_delay
                        logger.info(
                            f"Retrying connection after {type(e).__name__} for stream {stream_id}, client {client_id} "
                            f"(attempt {retry_count}/{max_retries}, delay: {current_delay}s)"
                        )

                        # Clean up current connection
                        if stream_context is not None:
                            try:
                                await stream_context.__aexit__(None, None, None)
                            except Exception:
                                pass
                        stream_context = None
                        response = None

                        # Wait before retrying
                        await asyncio.sleep(current_delay)

                        continue  # Retry with same URL

                    # Retries exhausted, try automatic failover
                    has_failover = bool(
                        stream_info.failover_resolver_url or stream_info.failover_urls)
                    if _recover_sticky_origin_if_needed():
                        logger.info(
                            f"Retrying stream {stream_id} after sticky origin recovery")
                        retry_count = 0
                        continue

                    if has_failover and failover_count < max_failovers and not stream_info.is_vod:
                        logger.info(
                            f"Retries exhausted, attempting automatic failover for client {client_id} "
                            f"(failover attempt {failover_count + 1}/{max_failovers})")
                        await self._try_update_failover_url(stream_id, f"stream_error_{type(e).__name__}_after_retries")
                        # Reset retry counter for new URL
                        retry_count = 0
                        failover_count += 1
                        # Clean up current connection
                        if stream_context is not None:
                            try:
                                await stream_context.__aexit__(None, None, None)
                            except Exception:
                                pass
                        stream_context = None
                        response = None
                        continue  # Retry with failover URL
                    else:
                        # For VOD, reconnect mid-stream instead of failing the client
                        if stream_info.is_vod and bytes_served > 0 and vod_reconnects < max_vod_reconnects:
                            vod_reconnects += 1
                            resume_from_byte = range_start + bytes_served
                            logger.warning(
                                f"VOD upstream error {type(e).__name__} mid-stream; reconnecting from byte {resume_from_byte} "
                                f"(attempt {vod_reconnects}/{max_vod_reconnects})"
                            )
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None
                            continue

                        reason = "total_timeout_exceeded" if total_timeout_exceeded else "no_failover_available"
                        await self._emit_event("STREAM_FAILED", stream_id, {
                            "client_id": client_id,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "reason": reason,
                            "retry_count": retry_count,
                            "no_failover": not has_failover
                        })
                        break

                except (ConnectionResetError, ConnectionError, BrokenPipeError) as e:
                    # Client disconnected - this is normal, not an error
                    logger.info(
                        f"Client {client_id} disconnected: {type(e).__name__}")
                    await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                        "client_id": client_id,
                        "bytes_served": bytes_served,
                        "chunks_served": chunk_count,
                        "reason": "client_disconnected"
                    })
                    break

                except Exception as e:
                    # Log with more detail for debugging
                    error_str = str(e) if str(
                        e) else f"<empty {type(e).__name__}>"
                    logger.warning(
                        f"Stream error for client {client_id}: {type(e).__name__}: {error_str}")
                    logger.warning(
                        f"Exception details - bytes_served: {bytes_served}, chunks: {chunk_count}")

                    # Check if this looks like a client disconnection (empty error message often indicates this)
                    if not str(e) and bytes_served > 0:
                        logger.info(
                            f"Likely client disconnection for {client_id} (empty error message, data was streaming)")
                        await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                            "client_id": client_id,
                            "bytes_served": bytes_served,
                            "chunks_served": chunk_count,
                            "reason": "possible_client_disconnection"
                        })
                        break
                    else:
                        # Check if we can retry before failover
                        elapsed_total = asyncio.get_event_loop().time() - total_timeout_start
                        total_timeout_exceeded = total_timeout > 0 and elapsed_total > total_timeout

                        # Try retrying the current URL first
                        allow_initial_retry = (
                            not stream_info.is_vod) or bytes_served == 0
                        if retry_count < max_retries and not total_timeout_exceeded and allow_initial_retry:
                            retry_count += 1
                            current_delay = retry_delay * \
                                (1.5 ** (retry_count - 1)
                                 ) if settings.STREAM_RETRY_EXPONENTIAL_BACKOFF else retry_delay
                            logger.info(
                                f"Retrying connection after {type(e).__name__} for stream {stream_id}, client {client_id} "
                                f"(attempt {retry_count}/{max_retries}, delay: {current_delay}s)"
                            )

                            # Clean up current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None

                            # Wait before retrying
                            await asyncio.sleep(current_delay)

                            continue  # Retry with same URL

                        # Try failover for unknown errors too
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if _recover_sticky_origin_if_needed():
                            logger.info(
                                f"Retrying stream {stream_id} after sticky origin recovery")
                            retry_count = 0
                            continue

                        if has_failover and failover_count < max_failovers and not stream_info.is_vod:
                            logger.info(
                                f"Retries exhausted, attempting automatic failover for client {client_id} (unknown error)")
                            await self._try_update_failover_url(stream_id, f"unknown_error_{type(e).__name__}_after_retries")
                            # Reset retry counter for new URL
                            retry_count = 0
                            failover_count += 1
                            # Clean up current connection
                            if stream_context is not None:
                                try:
                                    await stream_context.__aexit__(None, None, None)
                                except Exception:
                                    pass
                            stream_context = None
                            response = None
                            continue  # Retry with failover URL
                        else:
                            reason = "total_timeout_exceeded" if total_timeout_exceeded else "no_failover_available"
                            await self._emit_event("STREAM_FAILED", stream_id, {
                                "client_id": client_id,
                                "error": error_str,
                                "error_type": type(e).__name__,
                                "reason": reason,
                                "retry_count": retry_count,
                                "bytes_served": bytes_served
                            })
                            break

                finally:
                    # Manually exit the context manager
                    if stream_context is not None:
                        try:
                            await stream_context.__aexit__(None, None, None)
                            logger.info(
                                f"Provider connection closed for client {client_id}")
                        except Exception as close_error:
                            logger.warning(
                                f"Error closing response: {close_error}")

            # Update final stats (add any remaining bytes not yet counted)
            bytes_remaining = bytes_served - last_stats_update
            if bytes_remaining > 0:
                if client_id in self.clients:
                    self.clients[client_id].bytes_served += bytes_remaining
                    self.clients[client_id].last_access = datetime.now(
                        timezone.utc)

                if stream_id in self.streams:
                    self.streams[stream_id].total_bytes_served += bytes_remaining
                    self.streams[stream_id].last_access = datetime.now(
                        timezone.utc)

                self._stats.total_bytes_served += bytes_remaining

            # Cleanup client - pass connection_id to prevent race conditions
            await self.cleanup_client(client_id, connection_id)

        # Determine content type
        # Add `or current_url.endswith('?profile=pass')` to handle TVHeadend passthrough URLs
        if current_url.endswith(('.ts', '?profile=pass')) or '/live/' in current_url:
            content_type = "video/mp2t"
        elif current_url.endswith('.mp4'):
            content_type = "video/mp4"
        elif current_url.endswith('.mkv'):
            content_type = "video/x-matroska"
        elif current_url.endswith('.webm'):
            content_type = "video/webm"
        else:
            content_type = "application/octet-stream"

        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*"
        }

        # For live streams, explicitly state we don't support range requests
        # In strict mode, be even more explicit
        if stream_info.is_live_continuous:
            headers["Accept-Ranges"] = "none"
            if strict_mode_enabled:
                # Remove any connection-related headers that might interfere
                headers["Connection"] = "keep-alive"
                logger.debug(
                    "STRICT MODE: Setting Accept-Ranges: none and Connection: keep-alive")
        else:
            headers["Accept-Ranges"] = "bytes"

        # Create generator to start streaming
        gen = generate()

        # Consume first iteration to capture provider response headers
        # This is necessary to determine if we should return 206 or 200
        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            # Empty stream
            return StreamingResponse(iter([]), media_type=content_type, headers=headers)

        # Now we have provider_status_code, provider_content_range, provider_content_length
        # Determine proper response status and headers
        status_code = 200

        # In strict mode for live TS, NEVER return 206 or Content-Length
        if strict_mode_enabled and stream_info.is_live_continuous:
            status_code = 200
            # Do NOT include Content-Length or Content-Range for live streams in strict mode
            logger.info(
                "STRICT MODE: Returning 200 OK without Content-Length for live TS stream")
        elif range_header and provider_status_code == 206 and provider_content_range:
            # Provider returned 206, we should also return 206 (only for non-strict or VOD)
            status_code = 206
            headers["Content-Range"] = provider_content_range
            logger.info(
                f"Returning 206 Partial Content with range: {provider_content_range}")

        headers.pop("Content-Length", None)

        # Create new generator that yields the first chunk then continues with the rest
        async def generate_with_first_chunk():
            yield first_chunk
            async for chunk in gen:
                yield chunk

        return StreamingResponse(
            generate_with_first_chunk(),
            status_code=status_code,
            media_type=content_type,
            headers=headers
        )

    async def stream_transcoded(
        self,
        stream_id: str,
        client_id: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """
        Stream transcoded content using the PooledStreamManager.
        """
        if not self.pooled_manager:
            raise HTTPException(
                status_code=501, detail="Transcoding pooling is not enabled")

        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        if not stream_info.is_transcoded:
            raise HTTPException(
                status_code=400, detail="Stream is not configured for transcoding")

        # Register client
        if client_id not in self.clients:
            await self.register_client(client_id, stream_id)

        # Generate a unique connection ID for this streaming request
        # This prevents race conditions when the same client makes concurrent connections (e.g., Kodi seeking)
        connection_id = str(uuid.uuid4())

        # Create cancellation event for this specific connection
        cancel_event = asyncio.Event()
        self.connection_cancel_events[connection_id] = cancel_event

        # Update the client's active connection ID
        if client_id in self.clients:
            self.clients[client_id].active_connection_id = connection_id
            logger.debug(
                f"Client {client_id} now has active connection: {connection_id}")

        logger.info(
            f"Requesting pooled transcoded stream for client {client_id}, stream {stream_id}")

        async def generate():
            shared_process = None
            stream_key = None
            bytes_served = 0
            failover_count = 0
            max_failovers = 3
            is_failover = False  # Track if we broke due to failover

            # Main loop with automatic reconnection on failover
            while failover_count <= max_failovers:
                try:
                    # Get current URL (may have changed due to failover)
                    active_url = stream_info.current_url or stream_info.original_url

                    if failover_count > 0:
                        logger.info(
                            f"Starting failover attempt {failover_count}/{max_failovers} for client {client_id}, " +
                            f"new URL: {active_url}")

                    # Get or create a shared transcoding process
                    stream_key, shared_process = await self.pooled_manager.get_or_create_shared_stream(
                        url=active_url,
                        profile=stream_info.transcode_profile,
                        ffmpeg_args=stream_info.transcode_ffmpeg_args,
                        client_id=client_id,
                        user_agent=stream_info.user_agent,
                        headers=stream_info.headers,
                        stream_id=stream_id,
                        # Reuse existing key if available
                        reuse_stream_key=stream_info.transcode_stream_key,
                    )

                    # Update the tracked stream key so future failovers can stop the correct process
                    stream_info.transcode_stream_key = stream_key

                    if not shared_process or not shared_process.process or not shared_process.process.stdout:
                        # Try failover if available
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if has_failover and failover_count < max_failovers:
                            logger.warning(
                                f"Failed to create transcoding process, attempting failover")
                            await self._try_update_failover_url(stream_id, "transcode_start_error")
                            failover_count += 1
                            continue
                        else:
                            raise HTTPException(
                                status_code=500, detail="Failed to get a valid transcoding process")

                    # Wait for FFmpeg to start and stderr monitor to detect input errors
                    # Poll the status repeatedly to catch errors that occur during startup
                    # This is especially important for connection errors (DNS failures, 404s, etc.)
                    max_wait_time = 2.0  # Wait up to 2 seconds
                    check_interval = 0.1  # Check every 100ms
                    elapsed = 0.0

                    while elapsed < max_wait_time:
                        await asyncio.sleep(check_interval)
                        elapsed += check_interval

                        # Check if the process failed due to input errors (detected by stderr monitor)
                        if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover and failover_count < max_failovers:
                                logger.warning(
                                    f"Transcoding process failed due to input error before streaming (detected after {elapsed:.1f}s), attempting failover")
                                # Clean up failed process
                                if self.pooled_manager:
                                    try:
                                        await self.pooled_manager.force_stop_stream(stream_key)
                                    except Exception:
                                        pass
                                stream_key = None
                                await self._try_update_failover_url(stream_id, "transcode_input_error")
                                failover_count += 1
                                break  # Break out of wait loop to continue outer loop
                            else:
                                # No failover available - log error and return empty stream
                                logger.error(
                                    f"Transcoding process failed due to input error and no failover available for stream {stream_id}")
                                return

                        # Check if process has exited early
                        if shared_process.process.returncode is not None:
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover and failover_count < max_failovers:
                                logger.warning(
                                    f"Transcoding process exited before streaming (detected after {elapsed:.1f}s), attempting failover")
                                # Clean up failed process
                                if self.pooled_manager and stream_key:
                                    try:
                                        await self.pooled_manager.force_stop_stream(stream_key)
                                    except Exception:
                                        pass
                                stream_key = None
                                await self._try_update_failover_url(stream_id, "transcode_process_exited")
                                failover_count += 1
                                break  # Break out of wait loop to continue outer loop
                            else:
                                logger.error(
                                    f"Transcoding process exited with code {shared_process.process.returncode} and no failover available for stream {stream_id}")
                                return

                        # If we have data in the queue, FFmpeg is producing output - safe to start streaming
                        client_queue = shared_process.client_queues.get(
                            client_id)
                        if client_queue and not client_queue.empty():
                            logger.info(
                                f"FFmpeg process producing data after {elapsed:.1f}s, starting stream")
                            break

                    # Check one final time after the wait loop before proceeding
                    # (in case we timed out without detecting the issue)
                    if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if has_failover and failover_count < max_failovers:
                            logger.warning(
                                f"Transcoding process failed due to input error (final check), attempting failover")
                            if self.pooled_manager:
                                try:
                                    await self.pooled_manager.force_stop_stream(stream_key)
                                except Exception:
                                    pass
                            stream_key = None
                            await self._try_update_failover_url(stream_id, "transcode_input_error")
                            failover_count += 1
                            continue
                        else:
                            logger.error(
                                f"Transcoding process failed due to input error and no failover available for stream {stream_id}")
                            return

                    # Verify the process is actually running (final check)
                    if shared_process.process.returncode is not None:
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if has_failover and failover_count < max_failovers:
                            logger.warning(
                                f"Transcoding process exited before streaming, attempting failover")
                            # Clean up failed process
                            if self.pooled_manager and stream_key:
                                try:
                                    await self.pooled_manager.force_stop_stream(stream_key)
                                except Exception:
                                    pass
                            stream_key = None
                            await self._try_update_failover_url(stream_id, "transcode_process_exited")
                            failover_count += 1
                            continue
                        else:
                            # No failover available - log error and return empty stream
                            # Cannot raise HTTPException here as response may have already started
                            logger.error(
                                f"Transcoding process exited with code {shared_process.process.returncode} and no failover available for stream {stream_id}")
                            return

                    logger.info(
                        f"Streaming from FFmpeg process PID {shared_process.process.pid} for client {client_id}")

                    # Get the client's queue - the broadcaster will feed chunks into it
                    client_queue = shared_process.client_queues.get(client_id)
                    if not client_queue:
                        raise HTTPException(
                            status_code=500, detail="Client queue not found")

                    # Stream data from the client's queue (fed by broadcaster)
                    while True:
                        # Check if streaming should be cancelled
                        if cancel_event.is_set():
                            logger.info(
                                f"Transcoded streaming cancelled for client {client_id} by external request")
                            return

                        # Check if the transcoding process failed due to input error during streaming
                        if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover and failover_count < max_failovers:
                                logger.warning(
                                    f"Transcoding process encountered input error during streaming, triggering failover")
                                # Clean up current connection
                                if client_id and stream_key and self.pooled_manager:
                                    try:
                                        await self.pooled_manager.remove_client_from_stream(client_id)
                                        await self.pooled_manager.force_stop_stream(stream_key)
                                    except Exception as e:
                                        logger.warning(
                                            f"Error cleaning up failed stream: {e}")
                                stream_key = None
                                await self._try_update_failover_url(stream_id, "transcode_runtime_input_error")
                                failover_count += 1
                                break
                            else:
                                logger.error(
                                    f"Transcoding failed due to input error and no failover available")
                                return

                        # Check for failover event
                        if stream_info.failover_event.is_set():
                            logger.info(
                                f"Failover detected for transcoded stream {stream_id}, will reconnect client {client_id} to new URL: {stream_info.current_url}")

                            # IMPORTANT: Clear the event immediately so other checks don't trigger
                            # This prevents infinite loop where event keeps getting detected
                            stream_info.failover_event.clear()

                            # Clean up current connection
                            if client_id and stream_key and self.pooled_manager:
                                try:
                                    await self.pooled_manager.remove_client_from_stream(client_id)
                                    logger.info(
                                        f"Removed client {client_id} from old stream {stream_key}")
                                except Exception as e:
                                    logger.warning(
                                        f"Error removing client from old stream: {e}")
                            # Clear the stream_key so we don't try to clean it up again
                            stream_key = None
                            is_failover = True  # Mark that we're doing a failover
                            failover_count += 1
                            # Break inner loop to reconnect with new URL
                            break

                        # Get chunk from queue (broadcaster puts chunks here) with timeout
                        # to allow checking cancellation event periodically
                        try:
                            chunk = await asyncio.wait_for(client_queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            # No chunk available, loop back to check cancellation/failover
                            continue

                        if chunk is None:  # None signals end of stream
                            logger.info(
                                f"Transcoded streaming ended for client {client_id}")
                            return

                        yield chunk
                        bytes_served += len(chunk)

                        # Update client activity and idle tracking
                        if self.pooled_manager:
                            self.pooled_manager.update_client_activity(
                                client_id)
                        if client_id in self.clients:
                            now = datetime.now(timezone.utc)
                            self.clients[client_id].last_access = now
                            # Track transcoded stream data flow
                            self.clients[client_id].last_data_time = now
                            self.clients[client_id].bytes_served += len(chunk)

                        # Update stream-level stats (for bandwidth tracking)
                        if stream_id in self.streams:
                            self.streams[stream_id].total_bytes_served += len(
                                chunk)
                            self.streams[stream_id].last_access = datetime.now(
                                timezone.utc)

                        # Update global stats
                        self._stats.total_bytes_served += len(chunk)

                    # If we broke due to failover, continue to next iteration to reconnect
                    if is_failover:
                        is_failover = False  # Reset flag
                        continue
                    else:
                        # Normal completion - break outer loop
                        break

                except (HTTPException, ConnectionError, BrokenPipeError) as e:
                    logger.error(
                        f"Error during pooled transcoding for client {client_id}: {e}")

                    # Try automatic failover (check both resolver URL and static list)
                    has_failover = bool(
                        stream_info.failover_resolver_url or stream_info.failover_urls)
                    if has_failover and failover_count < max_failovers:
                        logger.info(
                            f"Attempting automatic failover for transcoded stream (attempt {failover_count + 1}/{max_failovers})")
                        await self._try_update_failover_url(stream_id, f"transcode_error_{type(e).__name__}")
                        # Clean up current connection
                        if client_id and stream_key and self.pooled_manager:
                            await self.pooled_manager.remove_client_from_stream(client_id)
                        failover_count += 1
                        continue
                    else:
                        raise

                except Exception as e:
                    logger.error(
                        f"Unexpected error during pooled transcoding for client {client_id}: {e}")
                    # Don't retry on unexpected exceptions
                    break

            # Final cleanup after all retries exhausted or normal completion
            # Clean up: remove client from the shared stream
            if client_id and stream_key and self.pooled_manager:
                try:
                    await self.pooled_manager.remove_client_from_stream(client_id)
                except Exception:
                    pass

            # Final client cleanup - pass connection_id to prevent race conditions
            await self.cleanup_client(client_id, connection_id)
            logger.info(
                f"Finished pooled stream for client {client_id}, served {bytes_served} bytes")

        headers = {
            "Content-Type": None,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
        }
        # Determine content type from transcode args

        def _detect_content_type_from_ffmpeg_args(ffmpeg_args: List[str]) -> str:
            # Map common ffmpeg format names/extensions to Content-Type
            fmt_map = {
                'mp4': 'video/mp4',
                'mov': 'video/mp4',
                'matroska': 'video/x-matroska',
                'mkv': 'video/x-matroska',
                'webm': 'video/webm',
                'mpegts': 'video/mp2t',
                'mpeg': 'video/mpeg',
                'hls': 'application/vnd.apple.mpegurl',
                'hls_native': 'application/vnd.apple.mpegurl',
                'flv': 'video/x-flv',
                'ogg': 'video/ogg',
                'mp3': 'audio/mpeg'
            }

            try:
                args = ffmpeg_args or []
                # Look for explicit -f <format>
                fmt = None
                for i, a in enumerate(args):
                    if a == '-f' and i + 1 < len(args):
                        fmt = args[i + 1].lower()
                        break
                    # handle combined -fmatroska (rare)
                    if a.startswith('-f') and len(a) > 2:
                        fmt = a[2:].lower()
                        break

                # If we found a format token, map it
                if fmt:
                    if fmt in fmt_map:
                        return fmt_map[fmt]
                    # Some formats may include codec lists like "mov,mp4,m4a"
                    if ',' in fmt:
                        for part in fmt.split(','):
                            if part in fmt_map:
                                return fmt_map[part]

                # Fallback: inspect any output filenames in args for known extensions
                for a in args:
                    if isinstance(a, str):
                        la = a.lower()
                        if la.endswith('.mp4'):
                            return 'video/mp4'
                        if la.endswith('.mkv') or la.endswith('.mk3d'):
                            return 'video/x-matroska'
                        if la.endswith('.webm'):
                            return 'video/webm'
                        if la.endswith('.ts'):
                            return 'video/mp2t'
                        if la.endswith('.m3u8'):
                            return 'application/vnd.apple.mpegurl'

                # If output is a pipe (pipe:1) and no explicit fmt, assume streaming MPEG-TS
                joined = ' '.join(args).lower()
                if 'pipe:1' in joined or 'pipe:' in joined:
                    return 'video/mp2t'

            except Exception:
                pass

            return 'application/octet-stream'

        content_type = _detect_content_type_from_ffmpeg_args(
            stream_info.transcode_ffmpeg_args)

        # Set header content-type
        headers['Content-Type'] = content_type

        # Transcoded streams are live/progressive streams; disallow range requests to
        # avoid client players issuing range-based reconnects which can cause
        # duplicate client registrations and premature cleanup.
        headers['Accept-Ranges'] = 'none'

        return StreamingResponse(generate(), media_type=content_type, headers=headers)

    async def _seamless_failover(self, stream_id: str, error: Exception) -> Optional[httpx.Response]:
        """
        Attempt seamless failover to next URL.
        Returns new response object if successful, None if all failovers exhausted.
        """
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]

        # Check if any failover mechanism is available
        has_failover = bool(
            stream_info.failover_resolver_url or stream_info.failover_urls)
        if not has_failover:
            logger.warning(
                f"No failover mechanism available for stream {stream_id}")
            return None

        # Resolve next failover URL using the preferred method
        next_url = await self._resolve_next_failover_url(stream_id)

        if not next_url:
            logger.warning(
                f"No more failover URLs available for stream {stream_id}")
            return None

        logger.info(
            f"Attempting failover for stream {stream_id} to: {next_url}")

        try:
            headers = {
                'User-Agent': stream_info.user_agent,
                'Referer': f"{urlparse(next_url).scheme}://{urlparse(next_url).netloc}/",
                'Origin': f"{urlparse(next_url).scheme}://{urlparse(next_url).netloc}",
                'Accept': '*/*'
            }

            client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client

            # Open new connection to failover URL
            new_response = await client_to_use.stream('GET', next_url, headers=headers, follow_redirects=True).__aenter__()
            new_response.raise_for_status()

            # Update stream info
            old_url = stream_info.current_url
            stream_info.current_url = next_url
            # Note: current_failover_index is already incremented by _resolve_next_failover_url
            stream_info.failover_attempts += 1
            stream_info.last_failover_time = datetime.now(timezone.utc)

            logger.info(f"Seamless failover successful for stream {stream_id}")

            await self._emit_event("FAILOVER_TRIGGERED", stream_id, {
                "old_url": old_url,
                "new_url": next_url,
                "failover_index": stream_info.current_failover_index,
                "attempt_number": stream_info.failover_attempts,
                "reason": str(error),
                "seamless": True
            })

            return new_response

        except Exception as e:
            logger.error(
                f"Failover attempt failed for stream {stream_id}: {e}")
            stream_info.failover_attempts += 1

            # Try next failover URL recursively if available
            # Calculate max attempts based on available failover mechanism
            if stream_info.failover_urls:
                max_attempts = len(stream_info.failover_urls) * \
                    stream_info.max_retries
            elif stream_info.failover_resolver_url:
                # Allow more attempts for resolver-based failover
                max_attempts = 10 * stream_info.max_retries
            else:
                max_attempts = 0

            if stream_info.failover_attempts < max_attempts:
                return await self._seamless_failover(stream_id, e)

            return None

    # ============================================================================
    # HLS SUPPORT (Keep existing shared approach - it works!)
    # ============================================================================

    async def get_playlist_content(
        self,
        stream_id: str,
        client_id: str,
        base_proxy_url: str
    ) -> Optional[str]:
        """Get and process playlist content for HLS streams"""
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]

        # IMPORTANT: Check if this is a variant stream whose parent has failed over
        # If the parent's current_url differs from its original_url, the parent has switched
        # to a different provider. This variant's URL points to the OLD provider.
        # We need to fetch the NEW master playlist and extract the corresponding variant
        if stream_info.is_variant_stream and stream_info.parent_stream_id:
            parent_info = self.streams.get(stream_info.parent_stream_id)
            if parent_info and parent_info.current_url != parent_info.original_url:
                logger.info(
                    f"Variant stream {stream_id} is stale after parent failover - "
                    f"using parent's current URL: {parent_info.current_url}"
                )
                # Use the parent's current URL as this variant's URL
                # The parent URL should be the master playlist after failover
                stream_info.current_url = parent_info.current_url
                # Also update original_url so future checks work correctly
                stream_info.original_url = parent_info.current_url

        # If this stream is a transcoded HLS, try to get playlist from the pooled manager
        if stream_info.is_transcoded and self.pooled_manager:
            try:
                # Ensure a shared transcoding process exists for this stream (this will create one if necessary)
                stream_key, shared_process = await self.pooled_manager.get_or_create_shared_stream(
                    url=stream_info.current_url or stream_info.original_url,
                    profile=stream_info.transcode_profile or "",
                    ffmpeg_args=stream_info.transcode_ffmpeg_args or [],
                    client_id=client_id,
                    user_agent=stream_info.user_agent,
                    headers=stream_info.headers,
                    stream_id=stream_id,
                    # Reuse existing key if available
                    reuse_stream_key=stream_info.transcode_stream_key,
                )
                # Record the stream key for later mapping
                stream_info.transcode_stream_key = stream_key

                # If the shared process is HLS-mode, read its playlist file directly
                if hasattr(shared_process, 'mode') and getattr(shared_process, 'mode') == 'hls':
                    # Check immediately if FFmpeg has already failed with input error
                    if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                        logger.warning(
                            f"HLS FFmpeg process failed with input error for stream {stream_id}, attempting failover immediately")
                        # Clean up failed process
                        if self.pooled_manager:
                            try:
                                await self.pooled_manager.force_stop_stream(stream_key)
                            except Exception as e:
                                logger.debug(
                                    f"Error cleaning up failed stream {stream_key}: {e}")
                        # Attempt failover
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)
                        if has_failover:
                            logger.info(
                                f"Attempting failover for stream {stream_id}")
                            failover_success = await self._try_update_failover_url(stream_id, "hls_input_error")
                            if failover_success:
                                # Failover successful, retry the stream fetch with new URL
                                logger.info(
                                    f"HLS failover successful for stream {stream_id}, retrying with new URL")
                                return await self.get_playlist_content(stream_id, client_id, base_proxy_url)
                        # No failover available or failover failed
                        logger.error(
                            f"HLS input error for stream {stream_id} and no failover available or failover failed")
                        return None

                    # Wait briefly for FFmpeg to produce the initial playlist if it's not yet present.
                    playlist_text = await shared_process.read_playlist()
                    waited = 0.0
                    poll_interval = 0.5
                    # Allow configurable wait time via settings.HLS_WAIT_TIME (seconds)
                    max_wait = float(getattr(settings, 'HLS_WAIT_TIME', 10))
                    while not playlist_text and waited < max_wait and shared_process.process and shared_process.process.returncode is None:
                        # Check if FFmpeg failed due to input error during the wait
                        if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                            logger.warning(
                                f"HLS FFmpeg process detected input error during playlist generation for stream {stream_id}, attempting failover")
                            # Clean up failed process
                            if self.pooled_manager:
                                try:
                                    await self.pooled_manager.force_stop_stream(stream_key)
                                except Exception as e:
                                    logger.debug(
                                        f"Error cleaning up failed stream {stream_key}: {e}")
                            # Attempt failover
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover:
                                logger.info(
                                    f"Attempting failover for stream {stream_id}")
                                failover_success = await self._try_update_failover_url(stream_id, "hls_input_error")
                                if failover_success:
                                    # Failover successful, retry the stream fetch with new URL
                                    logger.info(
                                        f"HLS failover successful for stream {stream_id}, retrying with new URL")
                                    return await self.get_playlist_content(stream_id, client_id, base_proxy_url)
                            # No failover available or failover failed
                            logger.error(
                                f"HLS input error for stream {stream_id} and no failover available or failover failed")
                            return None

                        await asyncio.sleep(poll_interval)
                        waited += poll_interval
                        playlist_text = await shared_process.read_playlist()

                    # If playlist still not available after waiting, check if it was due to FFmpeg error
                    if not playlist_text:
                        # First, check if FFmpeg failed with an input error
                        if hasattr(shared_process, 'status') and shared_process.status == "input_failed":
                            logger.warning(
                                f"HLS FFmpeg process detected input error for stream {stream_id}, attempting failover")
                            # Clean up failed process
                            if self.pooled_manager:
                                try:
                                    await self.pooled_manager.force_stop_stream(stream_key)
                                except Exception as e:
                                    logger.debug(
                                        f"Error cleaning up failed stream {stream_key}: {e}")
                            # Attempt failover
                            has_failover = bool(
                                stream_info.failover_resolver_url or stream_info.failover_urls)
                            if has_failover:
                                logger.info(
                                    f"Attempting failover for stream {stream_id}")
                                failover_success = await self._try_update_failover_url(stream_id, "hls_input_error")
                                if failover_success:
                                    # Failover successful, retry the stream fetch with new URL
                                    logger.info(
                                        f"HLS failover successful for stream {stream_id}, retrying with new URL")
                                    return await self.get_playlist_content(stream_id, client_id, base_proxy_url)
                            # No failover available or failover failed
                            logger.error(
                                f"HLS input error for stream {stream_id} and no failover available or failover failed")
                            return None

                        # No input error detected, it's just a timeout
                        logger.warning(
                            f"HLS playlist not produced within {max_wait}s for stream {stream_id}; cleaning up transcoder")
                        try:
                            # Attempt to stop and remove the shared process
                            if self.pooled_manager:
                                await self.pooled_manager.force_stop_stream(stream_key)
                        except Exception as e:
                            logger.error(
                                f"Error force-stopping failed HLS transcoder for {stream_key}: {e}")
                        # Return None so caller will treat playlist as unavailable
                        return None

                    if playlist_text:
                        # Construct pseudo-final URL based on local file path so M3U8Processor can compute bases
                        final_url = f"file://{shared_process.hls_dir}/index.m3u8"
                        stream_info.final_playlist_url = final_url

                        parsed_url = urlparse(final_url)
                        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                        if parsed_url.path:
                            path_parts = parsed_url.path.rsplit('/', 1)
                            if len(path_parts) > 1:
                                base_url += path_parts[0] + '/'
                            else:
                                base_url += '/'
                        else:
                            base_url += '/'

                        parent_id = stream_id if not stream_info.is_variant_stream else stream_info.parent_stream_id
                        processor = M3U8Processor(
                            base_proxy_url, client_id, stream_info.user_agent, final_url, parent_stream_id=parent_id)
                        processed_content = processor.process_playlist(
                            playlist_text, base_proxy_url, base_url)

                        stream_info.last_access = datetime.now(timezone.utc)
                        if client_id in self.clients:
                            now = datetime.now(timezone.utc)
                            self.clients[client_id].last_access = now
                            # Playlist fetch is also data activity
                            self.clients[client_id].last_data_time = now

                        return processed_content

            except ConnectionAbortedError:
                # Stream is being managed by another worker. Fall back to fetching via HTTP from that worker if possible.
                logger.debug(
                    "Transcoded HLS is managed by another worker; falling back to HTTP fetch of playlist if available")
            except Exception as e:
                logger.error(
                    f"Error retrieving transcoded playlist from pooled manager: {e}")

        # Try to fetch the playlist with automatic failover support
        has_failovers = bool(
            stream_info.failover_resolver_url or stream_info.failover_urls)
        # For legacy failover_urls, we can count the attempts; for resolver, allow reasonable retries
        max_attempts = len(stream_info.failover_urls) + 1 if stream_info.failover_urls else (
            10 if stream_info.failover_resolver_url else 1)
        attempt = 0
        last_error = None

        while attempt < max_attempts:
            try:
                # Always read current URL from stream_info to pick up manual failover changes
                current_url = stream_info.current_url or stream_info.original_url
                logger.info(
                    f"Fetching HLS playlist from: {current_url} (attempt {attempt + 1}/{max_attempts})")
                headers = {'User-Agent': stream_info.user_agent}
                headers.update(stream_info.headers)
                response = await self.http_client.get(current_url, headers=headers)
                response.raise_for_status()

                content = response.text
                final_url = str(response.url)
                stream_info.final_playlist_url = final_url

                # STICKY SESSION HANDLER:
                # If enabled and we followed a redirect, update current_url to the final URL.
                # This ensures we stick to the specific backend origin for subsequent requests,
                # preventing playback loops caused by non-monotonic sequence numbers when
                # load balancers bounce between origins with different playlist states.
                if stream_info.use_sticky_session and current_url and final_url != current_url:
                    stream_info.current_url = final_url
                    logger.debug(
                        f"Sticky session: Locking stream {stream_id} to origin: {final_url}")

                parsed_url = urlparse(final_url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                if parsed_url.path:
                    path_parts = parsed_url.path.rsplit('/', 1)
                    if len(path_parts) > 1:
                        base_url += path_parts[0] + '/'
                    else:
                        base_url += '/'
                else:
                    base_url += '/'

                # Pass stream_id as parent for variant playlists, unless this is already a variant
                parent_id = stream_id if not stream_info.is_variant_stream else stream_info.parent_stream_id
                processor = M3U8Processor(
                    base_proxy_url, client_id, stream_info.user_agent, final_url, parent_stream_id=parent_id)
                processed_content = processor.process_playlist(
                    content, base_proxy_url, base_url)

                stream_info.last_access = datetime.now(timezone.utc)
                if client_id in self.clients:
                    now = datetime.now(timezone.utc)
                    self.clients[client_id].last_access = now
                    # Playlist fetch is also data activity
                    self.clients[client_id].last_data_time = now

                return processed_content

            except Exception as e:
                last_error = e
                logger.error(
                    f"Error fetching playlist for stream {stream_id}: {e}")
                stream_info.error_count += 1

                # STICKY SESSION RECOVERY:
                # If sticky session is enabled and we were stuck to a specific origin (via redirect)
                # and it failed, revert to the configured URL to allow implicit load balancer failover.
                if stream_info.use_sticky_session:
                    known_urls = [stream_info.original_url] + \
                        (stream_info.failover_urls or [])
                    if stream_info.current_url and stream_info.current_url not in known_urls:
                        logger.warning(
                            f"Sticky origin {stream_info.current_url} failed. Reverting to original configured entry point.")
                        stream_info.current_url = None

                # Try failover if available and not the last attempt
                has_failovers = bool(
                    stream_info.failover_resolver_url or stream_info.failover_urls)
                if has_failovers and attempt < max_attempts - 1:
                    logger.info(
                        f"Attempting failover for playlist fetch (attempt {attempt + 1}/{max_attempts})")
                    failover_success = await self._try_update_failover_url(stream_id, f"playlist_fetch_error_{type(e).__name__}")
                    if failover_success:
                        # current_url will be read from stream_info at the start of the next loop iteration
                        attempt += 1
                        continue

                # No failover available or failover failed
                attempt += 1
                if attempt >= max_attempts:
                    logger.error(
                        f"Failed to fetch playlist after {max_attempts} attempts. Last error: {last_error}")
                    return None

    async def proxy_hls_segment(
        self,
        stream_id: str,
        client_id: str,
        segment_url: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """Proxy individual HLS segment - direct pass-through with automatic failover"""
        logger.info(
            f"Proxying HLS segment for stream {stream_id}, client {client_id}")

        async def segment_generator():
            bytes_served = 0
            retry_count = 0
            max_retries = 3

            while retry_count <= max_retries:
                try:
                    headers = {}
                    if range_header:
                        headers['Range'] = range_header

                    # Get stream info for user agent and custom headers
                    stream_info = None
                    if stream_id in self.streams:
                        stream_info = self.streams[stream_id]
                        headers['User-Agent'] = stream_info.user_agent
                        headers.update(stream_info.headers)

                    # If the segment is a local file generated by an HLS transcoder, read from disk
                    if segment_url.startswith('file://') or os.path.exists(segment_url):
                        # Strip file:// prefix if present
                        path = segment_url[7:] if segment_url.startswith(
                            'file://') else segment_url
                        # Stream the file contents
                        with open(path, 'rb') as fh:
                            while True:
                                chunk = fh.read(32768)
                                if not chunk:
                                    break
                                yield chunk
                                bytes_served += len(chunk)
                    else:
                        async with self.http_client.stream('GET', segment_url, headers=headers, follow_redirects=True) as response:
                            response.raise_for_status()

                            async for chunk in response.aiter_bytes(chunk_size=32768):
                                yield chunk
                                bytes_served += len(chunk)

                    # Success - update stats and exit
                    if client_id in self.clients:
                        now = datetime.now(timezone.utc)
                        self.clients[client_id].bytes_served += bytes_served
                        self.clients[client_id].segments_served += 1
                        self.clients[client_id].last_access = now
                        # Track that data is actively flowing
                        self.clients[client_id].last_data_time = now

                    if stream_id in self.streams:
                        self.streams[stream_id].total_bytes_served += bytes_served
                        self.streams[stream_id].total_segments_served += 1
                        self.streams[stream_id].last_access = datetime.now(
                            timezone.utc)

                    self._stats.total_bytes_served += bytes_served
                    self._stats.total_segments_served += 1

                    return  # Successfully fetched segment

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as e:
                    logger.warning(
                        f"Error fetching HLS segment (attempt {retry_count + 1}/{max_retries + 1}): {e}")

                    # If we have a stream with failover URLs, try triggering failover
                    if stream_info and stream_info.failover_urls and retry_count < max_retries:
                        logger.info(
                            f"Triggering failover due to segment fetch error for stream {stream_id}")
                        await self._try_update_failover_url(stream_id, "segment_fetch_error")
                        retry_count += 1
                        # The segment URL is absolute, so it won't automatically use the new failover
                        # But future playlist fetches will use the new URL
                        # For now, just retry the same segment URL
                        continue
                    else:
                        logger.error(
                            f"Failed to fetch HLS segment after {retry_count + 1} attempts: {e}")
                        raise

                except Exception as e:
                    logger.error(
                        f"Unexpected error streaming HLS segment: {e}")
                    raise

        return StreamingResponse(
            segment_generator(),
            media_type="video/MP2T",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*"
            }
        )

    # ============================================================================
    # HEALTH CHECKS AND CLEANUP
    # ============================================================================

    async def _periodic_health_check(self):
        """Periodic health check for streams"""
        while self._running:
            try:
                for stream_id, stream_info in list(self.streams.items()):
                    if stream_info.client_count == 0:
                        continue

                    if (stream_info.last_health_check is None or
                            (datetime.now(timezone.utc) - stream_info.last_health_check).total_seconds() >= stream_info.health_check_interval):

                        is_healthy = await self._health_check_stream(stream_id)
                        has_failover = bool(
                            stream_info.failover_resolver_url or stream_info.failover_urls)

                        if not is_healthy and has_failover:
                            logger.warning(
                                f"Stream {stream_id} failed health check, triggering failover")
                            # Trigger failover which will signal all clients to reconnect
                            await self._try_update_failover_url(stream_id, "health_check")

                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic health check: {e}")
                await asyncio.sleep(60)

    async def _health_check_stream(self, stream_id: str) -> bool:
        """Check if stream URL is healthy"""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        try:
            headers = {'User-Agent': stream_info.user_agent}
            if stream_info.is_hls:
                response = await self.http_client.get(current_url, headers=headers, timeout=10.0)
            else:
                response = await self.http_client.head(current_url, headers=headers, timeout=10.0)

            response.raise_for_status()
            stream_info.last_health_check = datetime.now(timezone.utc)
            return True
        except Exception as e:
            logger.warning(f"Health check failed for stream {stream_id}: {e}")
            return False

    async def _resolve_next_failover_url(self, stream_id: str) -> Optional[str]:
        """Resolve the next failover URL using either resolver callback or static list

        Prioritizes failover_resolver_url over failover_urls for maximum flexibility.

        Args:
            stream_id: The stream ID to get failover URL for

        Returns:
            Next failover URL or None if no more failovers available
        """
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]

        # Try resolver-based failover first (preferred method)
        if stream_info.failover_resolver_url:
            logger.info(
                f"Using failover resolver callback for stream {stream_id}")
            try:
                # Call the resolver endpoint with current URL and metadata
                payload = {
                    'current_url': stream_info.current_url,
                    'metadata': stream_info.metadata,
                    'current_failover_index': stream_info.current_failover_index
                }

                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        stream_info.failover_resolver_url,
                        json=payload
                    )

                if not response.is_success:
                    logger.warning(
                        f"Failover resolver returned {response.status_code} for stream {stream_id}")
                    return None

                data = response.json()
                next_url = data.get('next_url')

                if next_url:
                    logger.info(f"Failover resolver returned URL: {next_url}")
                    stream_info.current_failover_index += 1
                    return next_url
                else:
                    logger.info(
                        f"No viable failover URL available from resolver for stream {stream_id}")
                    return None

            except Exception as e:
                logger.error(
                    f"Error calling failover resolver for stream {stream_id}: {e}")
                return None

        # Fall back to static failover URLs list
        if stream_info.failover_urls:
            next_index = (stream_info.current_failover_index +
                          1) % len(stream_info.failover_urls)
            next_url = stream_info.failover_urls[next_index]
            stream_info.current_failover_index = next_index
            logger.info(f"Using static failover URL #{next_index}: {next_url}")
            return next_url

        # No failover mechanism available
        return None

    async def _try_update_failover_url(self, stream_id: str, reason: str = "manual") -> bool:
        """Update to next failover URL and signal all clients to reconnect

        Args:
            stream_id: The stream ID to failover
            reason: Reason for failover (manual, error, health_check, etc.)

        Returns:
            True if failover successful, False otherwise
        """
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]

        # Check if any failover mechanism is available
        has_failovers = bool(
            stream_info.failover_resolver_url or stream_info.failover_urls)
        if not has_failovers:
            logger.warning(
                f"No failover mechanism available for stream {stream_id}")
            return False

        # Resolve the next failover URL using the preferred method
        old_url = stream_info.current_url
        next_url = await self._resolve_next_failover_url(stream_id)

        if not next_url:
            logger.warning(
                f"No more failover URLs available for stream {stream_id}")
            return False

        # Update stream info
        stream_info.current_url = next_url
        stream_info.failover_attempts += 1
        stream_info.last_failover_time = datetime.now(timezone.utc)

        logger.info(
            f"Failover triggered for stream {stream_id} (reason: {reason}): {old_url} -> {stream_info.current_url}")

        # Signal all active clients to reconnect with new URL
        # Clear and set the event to notify waiting coroutines
        stream_info.failover_event.clear()
        stream_info.failover_event.set()

        # For transcoded streams, stop and restart the transcoding process
        if stream_info.is_transcoded and self.pooled_manager:
            try:
                # Stop the old transcoding process
                if stream_info.transcode_stream_key:
                    logger.info(
                        f"Stopping transcoding process for failover: {stream_info.transcode_stream_key}")
                    await self.pooled_manager.force_stop_stream(stream_info.transcode_stream_key)
                    stream_info.transcode_stream_key = None
            except Exception as e:
                logger.error(
                    f"Error stopping transcoding process during failover: {e}")

        # Emit failover event
        await self._emit_event("FAILOVER_TRIGGERED", stream_id, {
            "old_url": old_url,
            "new_url": stream_info.current_url,
            "failover_index": stream_info.current_failover_index,
            "attempt_number": stream_info.failover_attempts,
            "reason": reason,
            "client_count": len(stream_info.connected_clients)
        })

        # Reset the event for next failover
        await asyncio.sleep(0.1)  # Give clients time to detect the event
        stream_info.failover_event.clear()

        return True

    async def _periodic_cleanup(self):
        """Periodic cleanup of inactive clients and streams"""
        while self._running:
            try:
                await self._cleanup_inactive_clients()
                await self._cleanup_inactive_streams()
                if settings.ENABLE_CONNECTION_IDLE_MONITORING:
                    await self._monitor_connection_idle_time()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
                await asyncio.sleep(30)

    async def _cleanup_inactive_clients(self):
        """Clean up clients that haven't been accessed recently"""
        current_time = datetime.now(timezone.utc)
        inactive_clients = []

        for client_id, client_info in self.clients.items():
            if (current_time - client_info.last_access).total_seconds() > self.client_timeout:
                inactive_clients.append(client_id)

        for client_id in inactive_clients:
            await self.cleanup_client(client_id)

    async def _cleanup_inactive_streams(self):
        """Clean up streams with no active clients"""
        current_time = datetime.now(timezone.utc)
        inactive_streams = []

        for stream_id, stream_info in self.streams.items():
            # Count only ACTIVE clients for this stream
            active_client_count = 0
            if stream_id in self.stream_clients:
                for client_id in self.stream_clients[stream_id]:
                    if (client_id in self.clients and
                            self.clients[client_id].is_connected):
                        active_client_count += 1

            has_active_clients = active_client_count > 0
            time_diff_seconds = (
                current_time - stream_info.last_access).total_seconds()
            is_old = time_diff_seconds > self.stream_timeout

            if not has_active_clients and is_old:
                logger.info(
                    f"Marking stream {stream_id} for cleanup: no_active_clients={not has_active_clients}, time_diff={time_diff_seconds}s, timeout={self.stream_timeout}s")
                inactive_streams.append(stream_id)
            elif has_active_clients and is_old:
                logger.debug(
                    f"Stream {stream_id} is old ({time_diff_seconds}s) but has {active_client_count} active clients - keeping alive")
            elif not has_active_clients and not is_old:
                logger.debug(
                    f"Stream {stream_id} has no active clients but is recent ({time_diff_seconds}s < {self.stream_timeout}s) - keeping alive")

        for stream_id in inactive_streams:
            if stream_id in self.streams:
                logger.info(f"Cleaning up inactive stream: {stream_id}")

                # Stop any transcoding processes for this stream
                stream_info = self.streams[stream_id]
                if stream_info.is_transcoded and stream_info.transcode_stream_key and self.pooled_manager:
                    try:
                        logger.info(
                            f"Stopping transcoding process for inactive stream: {stream_info.transcode_stream_key}")
                        await self.pooled_manager.force_stop_stream(stream_info.transcode_stream_key)
                    except Exception as e:
                        logger.warning(
                            f"Error stopping transcoding process during cleanup: {e}")

                # Emit stream_stopped event before removing the stream
                await self._emit_event("STREAM_STOPPED", stream_id, {
                    "reason": "inactive_timeout",
                    "timeout_seconds": self.stream_timeout,
                    "metadata": stream_info.metadata
                })

                del self.streams[stream_id]
                if stream_id in self.stream_clients:
                    del self.stream_clients[stream_id]
                self._stats.active_streams -= 1

    async def _monitor_connection_idle_time(self):
        """Monitor long-held idle connections and emit alerts for potential resource leaks.

        Tracks connections that have been idle (no data flowing) for extended periods.
        This helps detect stuck connections or resource exhaustion scenarios.
        """
        current_time = datetime.now(timezone.utc)

        for client_id, client_info in list(self.clients.items()):
            if not client_info.is_connected:
                continue

            idle_seconds = (
                current_time - client_info.last_data_time).total_seconds()

            # ERROR threshold: Very long idle time (30+ minutes)
            if idle_seconds > settings.CONNECTION_IDLE_ERROR_THRESHOLD:
                if not client_info.idle_error_logged:
                    logger.error(
                        f"Connection resource leak warning: client {client_id} "
                        f"(stream={client_info.stream_id}, ip={client_info.ip_address}) "
                        f"idle for {idle_seconds:.0f}s (threshold={settings.CONNECTION_IDLE_ERROR_THRESHOLD}s). "
                        f"Bytes served: {client_info.bytes_served}, "
                        f"Connection age: {(current_time - client_info.created_at).total_seconds():.0f}s"
                    )
                    client_info.idle_error_logged = True
                    # Emit event for external monitoring systems
                    await self._emit_event("CONNECTION_IDLE_ERROR", client_info.stream_id or "unknown", {
                        "client_id": client_id,
                        "idle_seconds": idle_seconds,
                        "threshold_seconds": settings.CONNECTION_IDLE_ERROR_THRESHOLD,
                        "ip_address": client_info.ip_address,
                        "bytes_served": client_info.bytes_served
                    })

            # WARNING threshold: Moderately long idle time (10+ minutes)
            elif idle_seconds > settings.CONNECTION_IDLE_ALERT_THRESHOLD:
                if not client_info.idle_warning_logged:
                    logger.warning(
                        f"Connection idle warning: client {client_id} "
                        f"(stream={client_info.stream_id}, ip={client_info.ip_address}) "
                        f"idle for {idle_seconds:.0f}s (threshold={settings.CONNECTION_IDLE_ALERT_THRESHOLD}s). "
                        f"Bytes served: {client_info.bytes_served}, "
                        f"Connection age: {(current_time - client_info.created_at).total_seconds():.0f}s"
                    )
                    client_info.idle_warning_logged = True
                    # Emit event for external monitoring systems
                    await self._emit_event("CONNECTION_IDLE_WARNING", client_info.stream_id or "unknown", {
                        "client_id": client_id,
                        "idle_seconds": idle_seconds,
                        "threshold_seconds": settings.CONNECTION_IDLE_ALERT_THRESHOLD,
                        "ip_address": client_info.ip_address,
                        "bytes_served": client_info.bytes_served
                    })
            else:
                # Connection is active again - reset warning flags if idle clears
                if idle_seconds < (settings.CONNECTION_IDLE_ALERT_THRESHOLD * 0.5):
                    if client_info.idle_warning_logged or client_info.idle_error_logged:
                        logger.debug(
                            f"Connection {client_id} recovered from idle state "
                            f"(idle: {idle_seconds:.0f}s)"
                        )
                    client_info.idle_warning_logged = False
                    client_info.idle_error_logged = False

    def get_stats(self) -> Dict:
        """Get comprehensive stats - aggregates variant stream stats into parent streams"""
        # Only count non-variant streams
        non_variant_streams = [
            s for s in self.streams.values() if not s.is_variant_stream]

        # Build a map of aggregated stats for parent streams
        stream_stats_map = {}
        for stream in self.streams.values():
            # If this is a variant, aggregate its stats into the parent
            if stream.is_variant_stream and stream.parent_stream_id:
                parent_id = stream.parent_stream_id
                if parent_id not in stream_stats_map:
                    # Initialize with parent stream data if it exists
                    if parent_id in self.streams:
                        parent = self.streams[parent_id]
                        # Count only ACTIVE clients for parent stream
                        active_parent_clients = 0
                        if parent_id in self.stream_clients:
                            for client_id in self.stream_clients[parent_id]:
                                if (client_id in self.clients and
                                        self.clients[client_id].is_connected):
                                    active_parent_clients += 1

                        stream_stats_map[parent_id] = {
                            "bytes": parent.total_bytes_served,
                            "segments": parent.total_segments_served,
                            "errors": parent.error_count,
                            "clients": active_parent_clients
                        }
                    else:
                        stream_stats_map[parent_id] = {
                            "bytes": 0, "segments": 0, "errors": 0, "clients": 0}

                # Add variant's stats to parent
                stream_stats_map[parent_id]["bytes"] += stream.total_bytes_served
                stream_stats_map[parent_id]["segments"] += stream.total_segments_served
                stream_stats_map[parent_id]["errors"] += stream.error_count
                # Don't double-count clients - they're tracked at parent level
            elif not stream.is_variant_stream:
                # Non-variant stream - use its own stats with active client count
                stream_id = stream.stream_id
                if stream_id not in stream_stats_map:
                    # Count only ACTIVE clients for this stream
                    active_stream_clients = 0
                    if stream_id in self.stream_clients:
                        for client_id in self.stream_clients[stream_id]:
                            if (client_id in self.clients and
                                    self.clients[client_id].is_connected):
                                active_stream_clients += 1

                    stream_stats_map[stream_id] = {
                        "bytes": stream.total_bytes_served,
                        "segments": stream.total_segments_served,
                        "errors": stream.error_count,
                        "clients": active_stream_clients
                    }

        # Count active streams (streams with at least one active client)
        active_stream_count = sum(1 for stream in non_variant_streams
                                  if stream_stats_map.get(stream.stream_id, {}).get("clients", 0) > 0 and stream.is_active)

        # Count only connected clients
        active_client_count = sum(1 for client in self.clients.values()
                                  if client.is_connected)

        return {
            "proxy_stats": {
                "total_streams": len(non_variant_streams),
                "active_streams": active_stream_count,
                "total_clients": len(self.clients),
                "active_clients": active_client_count,
                "total_bytes_served": self._stats.total_bytes_served,
                "total_segments_served": self._stats.total_segments_served,
                "uptime_seconds": (datetime.now(timezone.utc) - self._stats.uptime_start).seconds
            },
            "streams": [
                {
                    "stream_id": stream.stream_id,
                    "original_url": stream.original_url,
                    "current_url": stream.current_url,
                    "user_agent": stream.user_agent,
                    "client_count": stream_stats_map.get(stream.stream_id, {}).get("clients", 0),
                    "total_bytes_served": stream_stats_map.get(stream.stream_id, {}).get("bytes", stream.total_bytes_served),
                    "total_segments_served": stream_stats_map.get(stream.stream_id, {}).get("segments", stream.total_segments_served),
                    "error_count": stream_stats_map.get(stream.stream_id, {}).get("errors", stream.error_count),
                    "is_active": stream.is_active,
                    "has_failover": len(stream.failover_urls) > 0 or bool(stream.failover_resolver_url),
                    "failover_urls": stream.failover_urls,
                    "failover_resolver_url": stream.failover_resolver_url,
                    "current_failover_index": stream.current_failover_index,
                    "failover_attempts": stream.failover_attempts,
                    "last_failover_time": stream.last_failover_time.isoformat() if stream.last_failover_time else None,
                    "stream_type": "Transcoding" if stream.metadata.get("transcoding") else ("HLS" if stream.is_hls else ("VOD" if stream.is_vod else "Live Continuous")),
                    "created_at": stream.created_at.isoformat(),
                    "last_access": stream.last_access.isoformat(),
                    "metadata": stream.metadata,
                    "headers": stream.headers
                }
                for stream in non_variant_streams
            ],
            "clients": [
                {
                    "client_id": client.client_id,
                    "stream_id": client.stream_id,
                    "user_agent": client.user_agent,
                    "ip_address": client.ip_address,
                    "username": client.username,
                    "bytes_served": client.bytes_served,
                    "segments_served": client.segments_served,
                    "created_at": client.created_at.isoformat(),
                    "last_access": client.last_access.isoformat(),
                    "is_connected": client.is_connected
                }
                for client in self.clients.values()
                if client.is_connected  # Only include connected clients
            ]
        }
