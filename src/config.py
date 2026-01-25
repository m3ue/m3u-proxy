from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

# Application version
VERSION = "0.3.3"


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables.
    Utilizes pydantic-settings for robust validation and type-casting.
    """

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8085
    # DEPRECATED: PUBLIC_URL is no longer required. The proxy now uses relative URLs which
    # work automatically with any reverse proxy setup.
    # PUBLIC_URL: Optional[str] = None
    LOG_LEVEL: str = "error"
    APP_DEBUG: bool = False
    RELOAD: bool = False
    DOCS_URL: str = "/docs"
    REDOC_URL: str = "/redoc"
    OPENAPI_URL: str = "/openapi.json"

    # Route Configuration
    ROOT_PATH: str = "/m3u-proxy"  # Default base path for integration with m3u-editor
    CLIENT_TIMEOUT: int = 10
    STREAM_TIMEOUT: int = 15
    SHARED_STREAM_TIMEOUT: int = 30
    CLEANUP_INTERVAL: int = 30
    # Grace period (seconds) to wait before cleaning up a shared FFmpeg process after
    # the last client disconnects. This helps avoid churn for brief reconnects.
    SHARED_STREAM_GRACE: int = 3
    STREAM_SHARING_STRATEGY: str = "url_profile"  # url_profile, url_only, disabled

    # Default stream properties (can be overridden per stream)
    DEFAULT_USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    DEFAULT_CONNECTION_TIMEOUT: float = 10.0
    DEFAULT_READ_TIMEOUT: float = 30.0
    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_BACKOFF_FACTOR: float = 1.5
    DEFAULT_HEALTH_CHECK_INTERVAL: float = 300.0

    # HTTP Client Timeout Configuration for Streaming
    # VOD (Video On Demand) timeout - allows clients to pause content for extended periods
    # 5 minutes - allows upstream CDN stalls/re-buffering
    VOD_READ_TIMEOUT: float = 300.0
    # 1 hour - allows client pause without losing session
    VOD_WRITE_TIMEOUT: float = 3600.0
    # Live TV timeout - emphasizes keeping connection alive during client buffering
    # 30 minutes - safety net while supporting client backpressure
    LIVE_TV_WRITE_TIMEOUT: float = 1800.0

    # Connection Idle Monitoring - detect and alert on long-held connections that may be resource leaks
    # Alert threshold for connections held idle (warning log when exceeded)
    # 10 minutes - emit WARNING when connection idle exceeds this
    CONNECTION_IDLE_ALERT_THRESHOLD: int = 600
    # Alert threshold for very long-held connections (error log)
    # 30 minutes - emit ERROR when connection idle exceeds this
    CONNECTION_IDLE_ERROR_THRESHOLD: int = 1800
    # Enable connection idle monitoring (can be disabled for high-throughput scenarios)
    ENABLE_CONNECTION_IDLE_MONITORING: bool = True

    # Additional configuration from .env file
    DEFAULT_RETRY_ATTEMPTS: int = 3
    DEFAULT_RETRY_DELAY: int = 5
    TEMP_DIR: str = "/tmp/m3u-proxy-streams"
    LOG_FILE: str = "m3u-proxy.log"

    # Redis Configuration for pooling and multi-worker coordination
    REDIS_HOST: str = "localhost"
    REDIS_SERVER_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    REDIS_ENABLED: bool = False
    ENABLE_TRANSCODING_POOLING: bool = True
    MAX_CLIENTS_PER_SHARED_STREAM: int = 10
    CHANGE_BUFFER_CHUNKS: int = 100

    # Worker configuration
    WORKER_ID: Optional[str] = None
    HEARTBEAT_INTERVAL: int = 30  # seconds

    # Transcoding configuration
    # HLS garbage collection configuration
    HLS_GC_ENABLED: bool = True
    HLS_GC_INTERVAL: int = 600
    HLS_GC_AGE_THRESHOLD: int = 3600  # seconds (1 hour)
    # Optional base directory for HLS transcoding output. If unset, the
    # system temp dir (tempfile.gettempdir()) will be used. Set this to
    # a directory that all workers can access if running multiple workers.
    HLS_TEMP_DIR: Optional[str] = None
    # How long (seconds) to wait for FFmpeg to produce the initial HLS playlist
    # before considering the transcoder failed and cleaning it up.
    HLS_WAIT_TIME: int = 10

    # Network Broadcast Configuration
    # Base directory for broadcast HLS output. Each network gets a subdirectory.
    HLS_BROADCAST_DIR: str = "/tmp/m3u-proxy-broadcasts"
    # Timeout (seconds) for webhook callbacks to the broadcast creator when broadcasts end
    BROADCAST_CALLBACK_TIMEOUT: int = 3
    BROADCAST_START_RETRY_WINDOW: float = 300.0  # seconds
    BROADCAST_START_FAILURE_GRACE: float = 2.0  # seconds

    # API Authentication
    API_TOKEN: Optional[str] = None

    # Strict Live TS Mode Configuration
    # Enable strict mode for live MPEG-TS streams to improve playback stability
    STRICT_LIVE_TS: bool = False
    # Pre-buffer size in bytes (256KB-512KB recommended for 0.5-1s of data)
    STRICT_LIVE_TS_PREBUFFER_SIZE: int = 262144  # 256 KB default
    # Circuit breaker timeout - if no data received for this many seconds, mark upstream as bad
    STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT: int = 2
    # How long to mark a failed upstream as bad (seconds) before retrying
    STRICT_LIVE_TS_CIRCUIT_BREAKER_COOLDOWN: int = 60
    # Maximum chunk wait time in seconds for pre-buffer (to avoid infinite wait)
    STRICT_LIVE_TS_PREBUFFER_TIMEOUT: int = 10

    # Bitrate Quality Monitoring - detect slow/degraded streams and trigger failover
    # Enable bitrate monitoring for automatic failover on degraded streams
    ENABLE_BITRATE_MONITORING: bool = False
    # Minimum expected bitrate in bytes per second (default: 500 Kbps = 62500 bytes/sec)
    # Streams consistently below this threshold will trigger failover
    MIN_BITRATE_THRESHOLD: int = 62500
    # How often to check bitrate (in seconds) - measurement window
    BITRATE_CHECK_INTERVAL: float = 5.0
    # Number of consecutive low-bitrate checks before triggering failover
    # This prevents failover on brief network hiccups
    BITRATE_FAILOVER_THRESHOLD: int = 3
    # Grace period (seconds) at stream start before bitrate monitoring kicks in
    # Allows for initial buffering/connection establishment
    BITRATE_MONITORING_GRACE_PERIOD: float = 10.0

    # Per-chunk read timeout (seconds) to detect silent upstream stalls
    # When the upstream keeps the TCP connection open but stops sending data,
    # using a short per-chunk timeout allows the proxy to detect the stall
    # and trigger existing failover/reconnect logic instead of hanging.
    LIVE_CHUNK_TIMEOUT_SECONDS: float = 15.0
    # Model configuration
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="",  # No prefix, read directly from .env
        extra="ignore"  # Ignore extra environment variables from container
    )


# Global settings instance
settings = Settings()
