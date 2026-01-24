from fastapi import FastAPI, HTTPException, Query, Response, Request, Depends, Header
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from contextlib import asynccontextmanager
import logging
import uuid
import hashlib
import subprocess
from urllib.parse import unquote, urlparse
from typing import Optional, List, Dict
from pydantic import BaseModel, field_validator, ValidationError
from datetime import datetime, timezone
import os

from stream_manager import StreamManager
from events import EventManager
from models import StreamEvent, EventType, WebhookConfig
from config import settings, VERSION
from transcoding import get_profile_manager, TranscodingProfileManager
from redis_config import get_redis_config, should_use_pooling
from hwaccel import hw_accel
from broadcast_manager import BroadcastManager, BroadcastConfig, BroadcastStatus

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_ffmpeg_version() -> Optional[str]:
    """Get the ffmpeg version string"""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            # Extract the version from first line (e.g., "ffmpeg version 4.4.2")
            first_line = result.stdout.split('\n')[0]
            return first_line.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"Failed to get ffmpeg version: {e}")
        return None


def get_content_type(url: str) -> str:
    """Determine content type based on URL extension"""
    url_lower = url.lower()
    if url_lower.endswith(('.ts', '?profile=pass')):
        return 'video/mp2t'
    elif url_lower.endswith('.m3u8'):
        return 'application/vnd.apple.mpegurl'
    elif url_lower.endswith('.mp4'):
        return 'video/mp4'
    elif url_lower.endswith('.mkv'):
        return 'video/x-matroska'
    elif url_lower.endswith('.webm'):
        return 'video/webm'
    elif url_lower.endswith('.avi'):
        return 'video/x-msvideo'
    else:
        return 'application/octet-stream'


def is_direct_stream(url: str) -> bool:
    """Check if URL is a direct stream (not HLS playlist)"""
    return url.lower().endswith(('.ts', '.mp4', '.mkv', '.webm', '.avi', '?profile=pass')) or '/live/' in url


def detect_https_from_headers(request: Request) -> bool:
    """
    Universal HTTPS detection from reverse proxy headers.

    Works with all major reverse proxies:
    - NGINX, Caddy, Traefik, Apache (X-Forwarded-Proto)
    - Cloudflare, AWS ELB (X-Forwarded-Ssl)
    - Microsoft IIS, Azure (Front-End-Https)
    - RFC 7239 compliant proxies (Forwarded header)
    - NGINX Proxy Manager, Tailscale, Headscale, Netbird, etc.

    Returns True if HTTPS is detected, False otherwise.

    IMPORTANT: Only trusts forwarded headers if the request came through a reverse proxy.
    If the client connects directly (no X-Forwarded-For), use the actual request scheme.
    """
    # Debug logging (disabled by default - enable if needed for troubleshooting)
    # logger.debug("=" * 80)
    # logger.debug("🔍 DEBUG: ALL HEADERS RECEIVED BY m3u proxy:")
    # for header_name, header_value in request.headers.items():
    #     logger.debug(f"  {header_name}: {header_value}")
    # logger.debug("=" * 80)

    # First check: Is this a direct connection or through a reverse proxy?
    # If X-Forwarded-For is NOT present, the client connected directly to us
    has_forwarded_for = request.headers.get("x-forwarded-for") is not None

    if not has_forwarded_for:
        # Direct connection - use the actual request scheme, ignore forwarded headers
        # These headers might be set by a previous layer but don't represent THIS connection
        actual_scheme = request.url.scheme
        is_https = actual_scheme == "https"
        logger.debug(
            f"🔌 Direct connection detected (no X-Forwarded-For) - using actual scheme: {actual_scheme}")
        return is_https

    # Request came through a reverse proxy - trust the forwarded headers

    # Check X-Forwarded-Proto (most common - NGINX, Caddy, Traefik, NPM)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto and forwarded_proto.lower() == "https":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Proto: https")
        return True

    # Check X-Forwarded-Scheme (NGINX Proxy Manager, some reverse proxies)
    # NPM sets this correctly even when X-Forwarded-Proto is wrong
    forwarded_scheme = request.headers.get("x-forwarded-scheme")
    if forwarded_scheme and forwarded_scheme.lower() == "https":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Scheme: https")
        return True

    # Check X-Forwarded-Ssl (Cloudflare, some load balancers)
    forwarded_ssl = request.headers.get("x-forwarded-ssl")
    if forwarded_ssl == "on":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Ssl: on")
        return True

    # Check Front-End-Https (Microsoft IIS, Azure)
    frontend_https = request.headers.get("front-end-https")
    if frontend_https == "on":
        logger.debug("✅ Detected HTTPS via Front-End-Https: on")
        return True

    # Check Forwarded header (RFC 7239 standard)
    forwarded = request.headers.get("forwarded")
    if forwarded and "proto=https" in forwarded.lower():
        logger.debug("✅ Detected HTTPS via Forwarded header (RFC 7239)")
        return True

    # Check X-Forwarded-Port (if 443, assume HTTPS)
    forwarded_port = request.headers.get("x-forwarded-port")
    if forwarded_port == "443":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Port: 443")
        return True

    # No HTTPS detected
    logger.debug("No HTTPS headers detected - using HTTP")
    return False


def detect_reverse_proxy(request: Request) -> bool:
    """
    Detect if request is coming through a reverse proxy (HTTP or HTTPS).

    Returns True if any reverse proxy headers are present, False otherwise.
    This is used to determine whether to include the internal port in URLs.

    When a reverse proxy is detected, we should NOT include the internal port
    (e.g., :8085) in generated URLs because the reverse proxy handles the
    external port mapping (e.g., :80 or :443).
    """
    # Check for any common reverse proxy headers
    proxy_headers = [
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-scheme",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-ssl",
        "x-real-ip",
        "forwarded",
        "front-end-https",
        "cf-connecting-ip",  # Cloudflare
        "true-client-ip",    # Cloudflare Enterprise
        "x-original-forwarded-for",  # AWS ELB
    ]

    for header in proxy_headers:
        if request.headers.get(header):
            logger.debug(f"🔍 Reverse proxy detected via header: {header}")
            return True

    logger.debug("🔍 No reverse proxy headers detected - direct access")
    return False


def validate_url(url: str) -> str:
    """Validate URL format and security"""
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    # Basic URL parsing validation
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format")

    # Ensure scheme is http or https
    if parsed.scheme.lower() not in ['http', 'https']:
        raise ValueError("URL must use HTTP or HTTPS protocol")

    # Ensure there's a valid netloc (domain)
    if not parsed.netloc:
        raise ValueError("URL must have a valid domain")

    # Security checks
    if url.lower().startswith('javascript:'):
        raise ValueError("JavaScript URLs are not allowed")

    if url.lower().startswith('file:'):
        raise ValueError("File URLs are not allowed")

    # Additional security check for malicious URLs
    dangerous_patterns = ['<script', 'javascript:', 'data:', 'vbscript:']
    url_lower = url.lower()
    for pattern in dangerous_patterns:
        if pattern in url_lower:
            raise ValueError(f"URL contains dangerous pattern: {pattern}")

    return url


# Request models
class StreamCreateRequest(BaseModel):
    url: str
    failover_urls: Optional[List[str]] = None
    failover_resolver_url: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Optional[dict] = None
    headers: Optional[Dict[str, str]] = None
    # Enable Strict Live TS Mode for this stream
    strict_live_ts: Optional[bool] = None

    @field_validator('url')
    @classmethod
    def validate_primary_url(cls, v):
        return validate_url(v)

    @field_validator('failover_urls')
    @classmethod
    def validate_failover_urls(cls, v):
        if v is not None:
            return [validate_url(url) for url in v]
        return v

    @field_validator('failover_resolver_url')
    @classmethod
    def validate_failover_resolver_url(cls, v):
        if v is not None:
            return validate_url(v)
        return v

    @field_validator('metadata')
    @classmethod
    def validate_metadata(cls, v):
        if v is not None:
            # Ensure all keys and values are strings
            if not isinstance(v, dict):
                raise ValueError("metadata must be a dictionary")
            for key, value in v.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"metadata key must be string, got {type(key)}")
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError(
                        f"metadata value for '{key}' must be string, int, float, or bool")
            # Convert all values to strings for consistency
            return {str(k): str(v) for k, v in v.items()}
        return v


class TranscodeCreateRequest(BaseModel):
    url: str
    failover_urls: Optional[List[str]] = None
    failover_resolver_url: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Optional[dict] = None
    profile: Optional[str] = None  # Profile name or custom template
    profile_variables: Optional[Dict[str, str]] = None
    output_format: Optional[str] = None  # mp4, mkv, ts, etc.

    @field_validator('url')
    @classmethod
    def validate_primary_url(cls, v):
        return validate_url(v)

    @field_validator('failover_urls')
    @classmethod
    def validate_failover_urls(cls, v):
        if v is not None:
            return [validate_url(url) for url in v]
        return v

    @field_validator('failover_resolver_url')
    @classmethod
    def validate_failover_resolver_url(cls, v):
        if v is not None:
            return validate_url(v)
        return v

    @field_validator('metadata')
    @classmethod
    def validate_metadata(cls, v):
        if v is not None:
            # Ensure all keys and values are strings
            if not isinstance(v, dict):
                raise ValueError("metadata must be a dictionary")
            for key, value in v.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"metadata key must be string, got {type(key)}")
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError(
                        f"metadata value for '{key}' must be string, int, float, or bool")
            # Convert all values to strings for consistency
            return {str(k): str(v) for k, v in v.items()}
        return v

    @field_validator('profile_variables')
    @classmethod
    def validate_profile_variables(cls, v):
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError("profile_variables must be a dictionary")
            # Ensure all keys and values are strings
            return {str(k): str(val) for k, val in v.items()}
        return v


# Global stream manager and event manager
redis_config = get_redis_config()
redis_url = redis_config.get('redis_url') if should_use_pooling() else None
enable_pooling = should_use_pooling()

stream_manager = StreamManager(
    redis_url=redis_url, enable_pooling=enable_pooling)
event_manager = EventManager()
broadcast_manager = BroadcastManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    logger.info("⚡️ m3u proxy starting up...")
    await event_manager.start()

    # Connect event manager to stream manager
    stream_manager.set_event_manager(event_manager)

    await stream_manager.start()

    # Set up custom event handlers
    def log_event_handler(event: StreamEvent):
        """Simple event handler that logs all events"""
        logger.info(
            f"Event: {event.event_type} for stream {event.stream_id} at {event.timestamp}")

    # Add the handler to the event manager
    event_manager.add_handler(log_event_handler)

    yield

    # Shutdown
    logger.info("m3u proxy shutting down...")
    await broadcast_manager.shutdown()
    await stream_manager.stop()
    await event_manager.stop()


app = FastAPI(
    title="m3u proxy",
    version=VERSION,
    description="Advanced IPTV streaming proxy with client management, stats, and failover support",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH if hasattr(settings, 'ROOT_PATH') else "",
    docs_url=None,  # We'll create a minimal custom docs with logo/favicon
    # docs_url=settings.DOCS_URL if hasattr(settings, 'DOCS_URL') else "/docs",
    redoc_url=settings.REDOC_URL if hasattr(
        settings, 'REDOC_URL') else "/redoc",
    openapi_url=settings.OPENAPI_URL if hasattr(
        settings, 'OPENAPI_URL') else "/openapi.json",
)

# Mount static files for logo and favicon
# Get the parent directory of src/ to access root-level static files
static_path = os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "static")
# If static directory doesn't exist in the expected location, try the actual root
if not os.path.exists(static_path):
    # Fallback to /app/static for Docker or current working directory
    static_path = os.path.join(os.getcwd(), "static")
    if not os.path.exists(static_path):
        # Last resort: try parent directory
        static_path = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))

# Verify static path exists and log it
if os.path.exists(static_path):
    try:
        static_files = os.listdir(static_path)
        logger.info(f"Static files directory found")
    except Exception as e:
        logger.warning(f"❌ Could not list static directory: {e}")
else:
    logger.error(f"❌ Static files directory NOT found at: {static_path}")
    logger.error(f"Current working directory: {os.getcwd()}")
    logger.error(
        f"Script directory: {os.path.dirname(os.path.abspath(__file__))}")

# Configure CORS to allow all origins for streaming compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for maximum compatibility
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, HEAD, OPTIONS, etc.)
    allow_headers=["*"],  # Allow all headers
    expose_headers=["*"],  # Expose all headers to the client
)


# Minimal custom Swagger UI with logo and favicon
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Minimal custom Swagger UI with logo and favicon"""
    return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head>
    <title>{app.title}</title>
    <link rel="icon" type="image/svg+xml" href="{app.root_path}/static/favicon.svg">
    <link rel="icon" type="image/png" href="{app.root_path}/static/favicon.png">
    <link rel="shortcut icon" href="{app.root_path}/static/favicon.ico">
    <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    <style>
        .topbar {{ display: none; }}
        .swagger-ui .info .title {{ display: flex; align-items: center; gap: 15px; }}
        .custom-logo {{ height: 45px; width: auto; }}
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
    const ui = SwaggerUIBundle({{
        url: '{app.root_path}{app.openapi_url}',
        dom_id: '#swagger-ui',
        deepLinking: true,
        showExtensions: true,
        showCommonExtensions: true,
        presets: [
            SwaggerUIBundle.presets.apis,
            SwaggerUIBundle.SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout",
        onComplete: function() {{
            const title = document.querySelector('.info .title');
            if (title && !document.querySelector('.custom-logo')) {{
                const logo = document.createElement('img');
                logo.src = '{app.root_path}/static/logo.svg';
                logo.alt = '{app.title}';
                logo.className = 'custom-logo';
                title.insertBefore(logo, title.firstChild);
            }}
        }}
    }});
    </script>
</body>
</html>
    """)


# Serve static files explicitly (works with root_path)
@app.get("/static/{filename:path}", include_in_schema=False)
async def serve_static_file(filename: str):
    """Serve static files like logo and favicon"""
    file_path = os.path.join(static_path, filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        # Determine media type based on extension
        if filename.endswith('.svg'):
            media_type = 'image/svg+xml'
        elif filename.endswith('.png'):
            media_type = 'image/png'
        elif filename.endswith('.ico'):
            media_type = 'image/x-icon'
        else:
            media_type = None
        return FileResponse(file_path, media_type=media_type)
    raise HTTPException(
        status_code=404, detail=f"Static file not found: {filename}")


# Serve favicon directly at root for browsers
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon for browsers"""
    favicon_path = os.path.join(static_path, "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    # Fallback to SVG if ICO doesn't exist
    favicon_svg = os.path.join(static_path, "favicon.svg")
    if os.path.exists(favicon_svg):
        return FileResponse(favicon_svg, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")


def get_client_info(request: Request):
    """Extract client information from request"""
    # Try to get IP from X-Forwarded-For header first (for proxied requests)
    # X-Forwarded-For can contain multiple IPs: "client, proxy1, proxy2"
    # We want the first one (the original client IP)
    ip_address = "unknown"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        ip_address = forwarded_for.split(",")[0].strip()
    elif request.client:
        # Fallback to direct connection IP
        ip_address = request.client.host

    # Get username from X-Username header (set by m3u-editor for auth tracking)
    username = request.headers.get("x-username")

    # Fallback: IPTV clients won't forward custom headers across redirects (302/301).
    # Allow passing username via querystring to preserve traceability.
    if not username:
        qp = request.query_params
        username = qp.get("username") or qp.get("user") or qp.get("u")

    # Optional debug trace
    if username and settings.APP_DEBUG:
        logger.debug(
            f"Resolved username={username} ip={ip_address} ua={request.headers.get('user-agent')}"
        )

    return {
        "user_agent": request.headers.get("user-agent") or "unknown",
        "ip_address": ip_address,
        "username": username
    }


async def verify_token(
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    api_token: Optional[str] = Query(
        None, description="API token (alternative to X-API-Token header)")
):
    """
    Verify API token if API_TOKEN is configured.
    Token can be provided via:
    - X-API-Token header (recommended)
    - api_token query parameter (for browser access or when headers are difficult)

    If API_TOKEN is not set in environment, authentication is disabled.
    """
    # If no API token is configured, skip authentication
    if not settings.API_TOKEN:
        return True

    # Check for token in either header or query parameter
    provided_token = x_api_token or api_token

    # If API token is configured, require it in the header or query
    if not provided_token:
        raise HTTPException(
            status_code=401,
            detail="API token required. Provide token via X-API-Token header or api_token query parameter.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify the token matches
    if provided_token != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


@app.get("/", dependencies=[Depends(verify_token)])
async def root():
    stats = stream_manager.get_stats()
    proxy_stats = stats["proxy_stats"]
    return {
        "status": "running",
        "message": "m3u proxy is running",
        "version": VERSION,
        "uptime": proxy_stats["uptime_seconds"],
        "stats": proxy_stats
    }


@app.get("/info", dependencies=[Depends(verify_token)])
async def get_info():
    """
    Get comprehensive information about the m3u proxy server configuration and capabilities.
    Includes hardware acceleration status, Redis pooling, transcoding profiles, and other details.
    """
    redis_config = get_redis_config()
    use_pooling = should_use_pooling()
    profile_manager = get_profile_manager()

    # Build the info response
    info = {
        "version": VERSION,
        "ffmpeg_version": get_ffmpeg_version(),
        "hardware_acceleration": {
            "enabled": hw_accel.is_available(),
            "type": hw_accel.get_type(),
            "device": hw_accel.config.device if hw_accel.is_available() else None,
            "ffmpeg_args": hw_accel.config.ffmpeg_args if hw_accel.is_available() else []
        },
        "redis": {
            "enabled": settings.REDIS_ENABLED,
            "pooling_enabled": use_pooling,
            "host": redis_config.get("host") if settings.REDIS_ENABLED else None,
            "port": redis_config.get("port") if settings.REDIS_ENABLED else None,
            "password": redis_config.get("password") if settings.REDIS_ENABLED else None,
            "db": redis_config.get("db") if settings.REDIS_ENABLED else None,
            "max_clients_per_stream": settings.MAX_CLIENTS_PER_SHARED_STREAM if use_pooling else None,
            "stream_timeout": settings.SHARED_STREAM_TIMEOUT if use_pooling else None,
            "sharing_strategy": settings.STREAM_SHARING_STRATEGY if use_pooling else None
        },
        "transcoding": {
            "enabled": True,
            "profiles": profile_manager.list_profiles()
        },
        "configuration": {
            "default_user_agent": settings.DEFAULT_USER_AGENT,
            "client_timeout": settings.CLIENT_TIMEOUT,
            "stream_timeout": settings.STREAM_TIMEOUT,
            "cleanup_interval": settings.CLEANUP_INTERVAL,
            "max_retries": settings.DEFAULT_MAX_RETRIES
        },
        "worker": {
            "worker_id": settings.WORKER_ID if settings.WORKER_ID else "single",
            "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
            "multi_worker_mode": bool(settings.WORKER_ID)
        }
    }

    return info


async def resolve_stream_id(
    stream_id: str,
    url: Optional[str] = Query(
        None, description="Stream URL (for direct access, overrides stream_id in path)"),
    parent: Optional[str] = Query(
        None, description="Parent stream ID (for variant playlists)")
) -> str:
    """
    Dependency to get a stream_id. If a URL is provided in the query,
    it will be used to create/retrieve a stream, overriding the path stream_id.
    Also validates that the stream exists.

    If parent is provided, the created stream will be marked as a variant of the parent.
    """
    if url:
        try:
            decoded_url = unquote(url)
            validate_url(decoded_url)
            # If parent is provided, this is a variant stream
            parent_id = parent if parent else None
            return await stream_manager.get_or_create_stream(decoded_url, parent_stream_id=parent_id)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid URL provided: {e}")
        except Exception as e:
            logger.error(f"Error creating stream from URL parameter: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to process stream from URL")

    if stream_id not in stream_manager.streams:
        raise HTTPException(status_code=404, detail="Stream not found")

    return stream_id


@app.post("/streams", dependencies=[Depends(verify_token)])
async def create_stream(request: StreamCreateRequest):
    """Create a new stream with optional failover URLs, custom user agent, and metadata"""
    try:
        stream_id = await stream_manager.get_or_create_stream(
            request.url,
            request.failover_urls,
            request.failover_resolver_url,
            request.user_agent,
            metadata=request.metadata,
            headers=request.headers,
            strict_live_ts=request.strict_live_ts
        )

        # Emit stream started event
        event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id=stream_id,
            data={
                "primary_url": request.url,
                "failover_urls": request.failover_urls or [],
                "user_agent": request.user_agent,
                "stream_type": "direct" if is_direct_stream(request.url) else "hls",
                "metadata": request.metadata or {}
            }
        )
        await event_manager.emit_event(event)

        # Determine the appropriate endpoint based on stream type
        if is_direct_stream(request.url):
            stream_endpoint = f"/stream/{stream_id}"
            stream_type = "direct"
        else:
            stream_endpoint = f"/hls/{stream_id}/playlist.m3u8"
            stream_type = "hls"

        response = {
            "stream_id": stream_id,
            "primary_url": request.url,
            "failover_urls": request.failover_urls or [],
            "user_agent": request.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "stream_type": stream_type,
            "stream_endpoint": stream_endpoint,
            "playlist_url": stream_endpoint,  # For test compatibility
            # For test compatibility
            "direct_url": f"/stream/{stream_id}" if stream_type == "direct" else stream_endpoint,
            "message": f"Stream created successfully ({stream_type})"
        }

        # Include metadata in response if provided
        if request.metadata:
            response["metadata"] = request.metadata

        return response
    except Exception as e:
        logger.error(f"Error creating stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/transcode", dependencies=[Depends(verify_token)])
async def create_transcode_stream(request: TranscodeCreateRequest):
    """Create a new transcoded stream with optional failover URLs and custom profile"""
    try:
        profile_manager = get_profile_manager()

        # Determine if profile is a predefined name or custom template
        profile_identifier = request.profile or "default"
        profile = None
        profile_name = profile_identifier

        # Check if it looks like custom FFmpeg args (starts with -)
        if profile_identifier.strip().startswith('-'):
            logger.info(
                f"Using custom profile template: {profile_identifier[:50]}...")
            profile = profile_manager.create_profile_from_template(
                name="custom",
                parameters=profile_identifier,
                description="Custom FFmpeg profile from API"
            )
            profile_name = "custom"
        else:
            # Try to get as predefined profile
            profile = profile_manager.get_profile(profile_identifier)

            # If not found, error with available profiles
            if not profile:
                available_profiles = ', '.join(
                    profile_manager.list_profiles().keys())
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile '{profile_identifier}' not found. Available profiles: {available_profiles}"
                )

        # Prepare template variables by merging required vars with user's custom variables
        template_vars = {
            "input_url": request.url,
            "output_args": "pipe:1",  # Output to stdout for streaming
            "format": request.output_format or "mpegts",  # Default to MPEGTS for streaming
        }
        # Merge with user-provided variables (user vars take precedence for overrides)
        if request.profile_variables:
            template_vars.update(request.profile_variables)

        # Generate FFmpeg args from profile and variables
        ffmpeg_args = profile.render(template_vars)

        # Prepare comprehensive metadata including transcoding info
        transcoding_metadata = {
            "transcoding": "true",
            "profile": profile_name,
            "profile_variables": str(request.profile_variables or {}),
            "ffmpeg_args": " ".join(ffmpeg_args)
        }
        if request.metadata:
            transcoding_metadata.update(request.metadata)

        # Create the stream with transcoding parameters and complete metadata
        stream_id = await stream_manager.get_or_create_stream(
            request.url,
            request.failover_urls,
            request.failover_resolver_url,
            request.user_agent,
            metadata=transcoding_metadata,
            is_transcoded=True,
            transcode_profile=profile_name,
            transcode_ffmpeg_args=ffmpeg_args
        )

        # Emit stream started event with transcoding info
        event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id=stream_id,
            data={
                "primary_url": request.url,
                "failover_urls": request.failover_urls or [],
                "user_agent": request.user_agent,
                "stream_type": "transcoded",
                "profile": profile_name,
                "profile_variables": request.profile_variables or {},
                "ffmpeg_args": ffmpeg_args,
                "metadata": request.metadata or {}
            }
        )
        await event_manager.emit_event(event)

        # Determine whether this transcoded profile produces HLS output
        def _detect_hls_from_args(args: List[str]) -> bool:
            try:
                lowered = [str(a).lower() for a in (args or [])]
                for i, a in enumerate(lowered):
                    if a == '-f' and i + 1 < len(lowered) and lowered[i + 1] == 'hls':
                        return True
                    if a.startswith('-f') and 'hls' in a:
                        return True
                    if a.startswith('-hls_time') or 'hls_time' in a:
                        return True
                    if a.endswith('.m3u8'):
                        return True
                # also check joined string as fallback
                if ' -f hls' in ' '.join(lowered) or '-hls_time' in ' '.join(lowered):
                    return True
            except Exception:
                pass
            return False

        is_hls_output = _detect_hls_from_args(ffmpeg_args)

        # Choose endpoint based on output type
        if is_hls_output:
            stream_endpoint = f"/hls/{stream_id}/playlist.m3u8"
            direct_url = stream_endpoint
            out_format = 'hls'
            message = "Transcoded stream created successfully (HLS output)"
        else:
            stream_endpoint = f"/stream/{stream_id}"
            direct_url = stream_endpoint
            out_format = template_vars.get("format", "mpegts")
            message = "Transcoded stream created successfully (direct MPEGTS pipe)"

        response = {
            "stream_id": stream_id,
            "primary_url": request.url,
            "failover_urls": request.failover_urls or [],
            "user_agent": request.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "stream_type": "transcoded",
            "stream_endpoint": stream_endpoint,
            "playlist_url": stream_endpoint if is_hls_output else None,
            "direct_url": direct_url,
            "format": out_format,
            "profile": profile.name,
            "profile_variables": template_vars,  # Show the actual variables used
            "ffmpeg_args": ffmpeg_args,
            "message": message
        }

        # Include metadata in response if provided
        if request.metadata:
            response["metadata"] = request.metadata

        return response
    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except Exception as e:
        logger.error(f"Error creating transcoded stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/transcode/profiles", dependencies=[Depends(verify_token)])
async def list_transcode_profiles():
    """List available transcoding profiles"""
    try:
        profile_manager = get_profile_manager()
        profiles_dict = profile_manager.list_profiles()

        # Get individual profile objects for more details
        profile_details = []
        for name in profiles_dict.keys():
            profile = profile_manager.get_profile(name)
            if profile:
                profile_details.append({
                    "name": profile.name,
                    "description": profile.description or profile.name,
                    "parameters": profile.parameters
                })

        # Check hardware acceleration availability
        from hwaccel import HardwareAccelDetector, is_hwaccel_available
        hw_detector = HardwareAccelDetector()
        hw_available = is_hwaccel_available()
        hw_type = hw_detector.config.type if hw_detector.config else None

        return {
            "profiles": profile_details,
            "hardware_acceleration": {
                "available": hw_available,
                "type": hw_type
            },
            "default_variables": profile_manager.get_default_variables()
        }
    except Exception as e:
        logger.error(f"Error listing transcoding profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/playlist.m3u8")
async def get_hls_playlist(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Get HLS playlist for a stream (supports both direct proxy and transcoded HLS streams)"""
    try:
        # Generate or reuse client ID based on request characteristics
        # Use IP + User-Agent + Stream ID to create a consistent client ID
        if not client_id:
            client_info_data = get_client_info(request)
            client_hash = hashlib.md5(
                f"{client_info_data['ip_address']}-{client_info_data['user_agent']}-{stream_id}".encode()
            ).hexdigest()[:16]
            client_id = f"client_{client_hash}"

        # Only register client if not already registered for this stream
        if client_id not in stream_manager.clients or stream_manager.clients[client_id].stream_id != stream_id:
            client_info_data = get_client_info(request)
            client_info = await stream_manager.register_client(
                client_id,
                stream_id,
                user_agent=client_info_data["user_agent"],
                ip_address=client_info_data["ip_address"],
                username=client_info_data.get("username")
            )

            # Emit client connected event
            stream_info = stream_manager.streams.get(stream_id)
            event = StreamEvent(
                event_type=EventType.CLIENT_CONNECTED,
                stream_id=stream_id,
                data={
                    "client_id": client_id,
                    "user_agent": client_info_data["user_agent"],
                    "ip_address": client_info_data["ip_address"],
                    "username": client_info_data.get("username"),
                    "metadata": stream_info.metadata if stream_info else {}
                }
            )
            await event_manager.emit_event(event)
        else:
            logger.debug(
                f"Reusing existing client {client_id} for stream {stream_id}")

        # Build base URL for playlist rewriting using RELATIVE URLs
        # This eliminates the need for PUBLIC_URL and works with ANY reverse proxy setup.
        # The client's browser will automatically resolve relative URLs using the same
        # host/scheme they used to access this endpoint.
        #
        # For example:
        #   - User accesses: https://example.com/m3u-proxy/hls/stream_id/playlist.m3u8
        #   - Relative URL: /m3u-proxy/hls/stream_id/segment?url=...
        #   - Browser resolves to: https://example.com/m3u-proxy/hls/stream_id/segment?url=...
        #
        # This works for:
        #   - Direct access (http://localhost:8085/...)
        #   - NGINX reverse proxy (https://example.com/m3u-proxy/...)
        #   - Any reverse proxy (Caddy, Traefik, AWS ELB, etc.)
        #   - VPN/Tailscale access (https://host.vpn.ts.net/m3u-proxy/...)
        root_path = getattr(settings, 'ROOT_PATH', '')

        # Use relative URLs (just the path, no scheme/host)
        # This automatically works with whatever host/scheme the client used
        base_proxy_url = f"{root_path}/hls/{stream_id}"

        # Get processed playlist content (works for both direct HLS and transcoded HLS)
        content = await stream_manager.get_playlist_content(stream_id, client_id, base_proxy_url)

        if content is None:
            raise HTTPException(
                status_code=503, detail="Playlist not available")

        # Check if this is a transcoded stream for logging purposes
        stream_info = stream_manager.streams.get(stream_id)
        stream_type = "transcoded HLS" if stream_info and stream_info.is_transcoded else "direct HLS"

        logger.info(
            f"Serving {stream_type} playlist to client {client_id} for stream {stream_id}")

        response = Response(
            content=content, media_type="application/vnd.apple.mpegurl")
        # Add client ID to response headers for tracking
        response.headers["X-Client-ID"] = client_id
        response.headers["X-Stream-ID"] = stream_id
        # Add CORS headers
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "*"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving playlist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/segment")
async def get_hls_segment(
    stream_id: str,
    request: Request,
    client_id: str = Query(..., description="Client ID"),
    url: str = Query(..., description="The segment URL to proxy")
):
    """Proxy HLS segment for a client"""
    try:
        # Decode the URL
        segment_url = unquote(url)

        # Get range header if present
        range_header = request.headers.get('range')

        # Extract additional headers from query parameters (h_ prefixed)
        additional_headers = {}
        for key, value in request.query_params.items():
            if key.startswith("h_"):
                header_name = key[2:].replace('_', '-').lower()
                # Special handling for common headers
                if header_name == 'user-agent':
                    header_name = 'User-Agent'
                elif header_name == 'referer':
                    header_name = 'Referer'
                elif header_name == 'origin':
                    header_name = 'Origin'
                elif header_name == 'accept':
                    header_name = 'Accept'
                elif header_name == 'accept-encoding':
                    header_name = 'Accept-Encoding'
                elif header_name == 'accept-language':
                    header_name = 'Accept-Language'

                additional_headers[header_name] = value
                logger.info(
                    f"Extracted header from query param: {header_name}={value}")

        # For HLS segments, we don't create separate streams for each segment URL
        # Instead, we use the parent HLS stream_id and handle segment fetching directly
        # This prevents creating many individual streams for each .ts segment

        logger.info(
            f"HLS segment request - Stream: {stream_id}, Client: {client_id}, URL: {segment_url}")

        # Register client for the parent HLS stream (not the segment)
        client_info_data = get_client_info(request)
        await stream_manager.register_client(
            client_id,
            stream_id,  # Use the parent HLS stream ID
            user_agent=client_info_data["user_agent"],
            ip_address=client_info_data["ip_address"],
            username=client_info_data.get("username")
        )

        # For HLS segments, we need to fetch the segment directly without creating a separate stream
        # Use a special segment proxy function that doesn't create a new stream
        try:
            response = await stream_manager.proxy_hls_segment(
                stream_id,  # Parent HLS stream
                client_id,
                segment_url,  # The actual segment URL to fetch
                range_header
            )
            return response
        except Exception as stream_error:
            logger.error(f"Stream response error: {stream_error}")
            # Fall back to error response
            return Response(
                content=b"Stream unavailable",
                status_code=503,
                headers={"Content-Type": "text/plain"}
            )

    except Exception as e:
        logger.error(f"Error serving segment: {e}")
        logger.error(f"Exception type: {type(e)}")
        logger.error(f"Exception args: {e.args}")
        # Ensure we have a string representation
        error_detail = str(e) if e else "Unknown error"
        raise HTTPException(status_code=500, detail=error_detail)


@app.get("/hls/{stream_id}/segment.ts")
async def get_hls_segment_ts(
    stream_id: str,
    request: Request,
    client_id: str = Query(..., description="Client ID"),
    url: str = Query(..., description="The segment URL to proxy")
):
    """Proxy HLS segment with .ts extension for better ffplay compatibility"""
    return await get_hls_segment(stream_id, request, client_id, url)


@app.get("/stream/{stream_id}")
async def get_direct_stream(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Serve direct streams (.ts, .mp4, .mkv, etc.) for IPTV"""
    try:
        # The stream_id is now validated by the resolve_stream_id dependency
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url

        # Generate or reuse client ID based on request characteristics
        # Use IP + User-Agent + Stream ID to create a consistent client ID
        if not client_id:
            client_info_data = get_client_info(request)
            client_hash = hashlib.md5(
                f"{client_info_data['ip_address']}-{client_info_data['user_agent']}-{stream_id}".encode()
            ).hexdigest()[:16]
            client_id = f"client_{client_hash}"

        # Only register client if not already registered for this stream
        if client_id not in stream_manager.clients or stream_manager.clients[client_id].stream_id != stream_id:
            client_info_data = get_client_info(request)
            await stream_manager.register_client(
                client_id,
                stream_id,
                user_agent=client_info_data["user_agent"],
                ip_address=client_info_data["ip_address"],
                username=client_info_data.get("username")
            )
            logger.info(
                f"Registered client {client_id} for stream {stream_id}" + (f" (username: {client_info_data.get('username')})" if client_info_data.get('username') else ""))
        else:
            logger.debug(
                f"Reusing existing client {client_id} for stream {stream_id}")

        # Determine content type
        content_type = get_content_type(stream_url)

        # Get range header if present
        range_header = request.headers.get('range')

        logger.info(
            f"Serving direct stream to client {client_id} for stream {stream_id}")
        logger.info(f"Stream URL: {stream_url}")
        logger.info(f"Content-Type: {content_type}")
        if range_header:
            logger.info(f"Range request: {range_header}")

        # Check if this is a transcoded stream
        if stream_info.is_transcoded:
            logger.info(
                f"Using transcoded stream for {stream_id} with profile: {stream_info.transcode_profile}")

            # For transcoded streams outputting to pipe:1 or other non-HLS formats,
            # use streamed transcoding path
            return await stream_manager.stream_transcoded(
                stream_id,
                client_id,
                range_header=range_header
            )
        else:
            # Use direct proxy for continuous streams
            # This provides true byte-for-byte proxying with per-client connections
            return await stream_manager.stream_continuous_direct(
                stream_id,
                client_id,
                range_header=range_header
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving direct stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.head("/stream/{stream_id}")
async def head_direct_stream(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Handle HEAD requests for direct streams (needed for MP4 duration/seeking)

    In Strict Live TS Mode, this returns quickly without upstream hits for live TS streams.
    """
    try:
        # The stream_id is now validated by the resolve_stream_id dependency
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url

        # Determine content type
        content_type = get_content_type(stream_url)

        # Check for Range header
        range_header = request.headers.get('range')

        # Determine if strict mode is enabled (global or per-stream)
        strict_mode_enabled = settings.STRICT_LIVE_TS or stream_info.strict_live_ts

        # STRICT MODE OPTIMIZATION: For live TS streams in strict mode, return immediately
        # without hitting upstream to prevent redundant requests and connection issues
        if strict_mode_enabled and stream_info.is_live_continuous:
            logger.info(
                f"STRICT MODE: HEAD request for live TS stream {stream_id} - returning quick response without upstream hit")

            response_headers = {
                "Content-Type": content_type,
                "Accept-Ranges": "none",  # Live streams don't support ranges
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*",
                "Connection": "keep-alive"
            }

            # Do NOT include Content-Length for live streams
            return Response(
                content=None,
                status_code=200,  # Always 200 for live streams, never 206
                headers=response_headers
            )

        # For HEAD requests, we need to make a HEAD request to the origin server
        # to get metadata like Content-Length for MP4 files
        headers = {
            'User-Agent': stream_info.user_agent or 'Mozilla/5.0 (compatible)',
            'Accept': '*/*',
        }

        # If this is a range request, add the Range header
        if range_header:
            headers['Range'] = range_header
            logger.info(
                f"HEAD range request for stream {stream_id}: {stream_url}, Range: {range_header}")
        else:
            logger.info(f"HEAD request for stream {stream_id}: {stream_url}")

        try:
            async with stream_manager.http_client.stream('HEAD', stream_url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()

                # Build response headers based on origin server response
                response_headers = {
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*"
                }

                # Determine status code
                status_code = 200
                if range_header and response.status_code == 206:
                    status_code = 206
                    # Forward range-related headers
                    if 'content-range' in response.headers:
                        response_headers["Content-Range"] = response.headers['content-range']

                # Forward important headers from origin
                if 'content-length' in response.headers:
                    response_headers["Content-Length"] = response.headers['content-length']
                    logger.info(
                        f"Content-Length for {stream_id}: {response.headers['content-length']}")

                if 'last-modified' in response.headers:
                    response_headers["Last-Modified"] = response.headers['last-modified']

                return Response(
                    content=None,
                    status_code=status_code,
                    headers=response_headers
                )

        except Exception as e:
            logger.warning(f"HEAD request failed for {stream_url}: {e}")
            # Return basic HEAD response even if origin HEAD fails
            return Response(
                content=None,
                status_code=200,
                headers={
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*"
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling HEAD request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/hls/{stream_id}/clients/{client_id}", dependencies=[Depends(verify_token)])
async def disconnect_client(stream_id: str, client_id: str):
    """Disconnect a specific client"""
    try:
        await stream_manager.cleanup_client(client_id)
        return {"message": f"Client {client_id} disconnected"}
    except Exception as e:
        logger.error(f"Error disconnecting client {client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats", dependencies=[Depends(verify_token)])
async def get_stats():
    """Get comprehensive proxy statistics"""
    try:
        stats = stream_manager.get_stats()
        # Flatten the response for test compatibility
        result = stats["proxy_stats"].copy()
        result["streams"] = stats["streams"]
        result["clients"] = stats["clients"]
        return result
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/detailed", dependencies=[Depends(verify_token)])
async def get_detailed_stats():
    """Get detailed statistics including performance and monitoring metrics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "proxy_stats": stats["proxy_stats"],
            "connection_pool_stats": stats["proxy_stats"].get("connection_pool_stats", {}),
            "failover_stats": stats["proxy_stats"].get("failover_stats", {}),
            "performance_metrics": stats["proxy_stats"].get("performance_metrics", {}),
            "error_stats": stats["proxy_stats"].get("error_stats", {}),
            "stream_count": len(stats["streams"]),
            "client_count": len(stats["clients"])
        }
    except Exception as e:
        logger.error(f"Error getting detailed stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/performance", dependencies=[Depends(verify_token)])
async def get_performance_stats():
    """Get performance-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        proxy_stats = stats["proxy_stats"]
        return {
            "connection_pool": proxy_stats.get("connection_pool_stats", {}),
            "performance_metrics": proxy_stats.get("performance_metrics", {}),
            "failover_stats": proxy_stats.get("failover_stats", {}),
            "error_stats": proxy_stats.get("error_stats", {}),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting performance stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/streams", dependencies=[Depends(verify_token)])
async def get_stream_stats():
    """Get stream-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "total_streams": len(stats["streams"]),
            "streams": stats["streams"]
        }
    except Exception as e:
        logger.error(f"Error getting stream stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/clients", dependencies=[Depends(verify_token)])
async def get_client_stats():
    """Get client-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "total_clients": len(stats["clients"]),
            "clients": stats["clients"]
        }
    except Exception as e:
        logger.error(f"Error getting client stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients", dependencies=[Depends(verify_token)])
async def list_clients():
    """List all active clients (alias for /stats/clients)"""
    return await get_client_stats()


@app.get("/streams", dependencies=[Depends(verify_token)])
async def list_streams():
    """List all active streams"""
    try:
        stats = stream_manager.get_stats()
        return {
            "streams": stats["streams"],
            "total": len(stats["streams"])
        }
    except Exception as e:
        logger.error(f"Error listing streams: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/streams/by-metadata", dependencies=[Depends(verify_token)])
async def get_streams_by_metadata(
    field: str = Query(..., description="Metadata field to filter by"),
    value: str = Query(..., description="Value to match"),
    active_only: bool = Query(
        True, description="Only return streams with active clients")
):
    """Get active streams filtered by any metadata field with real-time status"""
    matching_streams = []

    for stream_id, stream_info in stream_manager.streams.items():
        # Skip variant streams - they don't have independent client counts
        if stream_info.is_variant_stream:
            continue

        # Check metadata field match
        if stream_info.metadata.get(field) != value:
            continue

        # Count only ACTIVE clients, not all clients in the set
        active_client_count = 0
        if stream_id in stream_manager.stream_clients:
            for client_id in stream_manager.stream_clients[stream_id]:
                if (client_id in stream_manager.clients and
                        stream_manager.clients[client_id].is_connected):
                    active_client_count += 1

        # Check if stream has active clients (if requested)
        if active_only and active_client_count == 0:
            continue

        # Check if stream is still considered active
        if not stream_info.is_active:
            continue

        matching_streams.append({
            'stream_id': stream_id,
            'client_count': active_client_count,  # Use active count, not total count
            'metadata': stream_info.metadata,
            'last_access': stream_info.last_access.isoformat(),
            'is_active': stream_info.is_active,
            'url': stream_info.current_url or stream_info.original_url,
            'stream_type': 'Transcoding' if stream_info.metadata.get('transcoding') else ('HLS' if stream_info.is_hls else ('VOD' if stream_info.is_vod else 'Live Continuous'))
        })

    return {
        'filter': {'field': field, 'value': value},
        'active_only': active_only,
        'matching_streams': matching_streams,
        'total_matching': len(matching_streams),
        'total_clients': sum(stream['client_count'] for stream in matching_streams)
    }


@app.get("/streams/{stream_id}", dependencies=[Depends(verify_token)])
async def get_stream_info(stream_id: str):
    """Get information about a specific stream"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stats = stream_manager.get_stats()
        stream_stats = next(
            (s for s in stats["streams"] if s["stream_id"] == stream_id), None)

        if not stream_stats:
            raise HTTPException(status_code=404, detail="Stream not found")

        # Get only ACTIVE clients for this stream
        active_stream_clients = []
        if stream_id in stream_manager.stream_clients:
            for client_id in stream_manager.stream_clients[stream_id]:
                if (client_id in stream_manager.clients and
                        stream_manager.clients[client_id].is_connected):
                    # Find the client in stats
                    client_stats = next(
                        (c for c in stats["clients"]
                         if c["client_id"] == client_id),
                        None
                    )
                    if client_stats:
                        active_stream_clients.append(client_stats)

        return {
            "stream": stream_stats,
            "clients": active_stream_clients,
            "client_count": len(active_stream_clients)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting stream info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# IMPORTANT: Routes with literal paths must come BEFORE parameterized routes
# Otherwise FastAPI will match /streams/oldest-by-metadata as /streams/{stream_id}
@app.delete("/streams/oldest-by-metadata", dependencies=[Depends(verify_token)])
async def delete_oldest_stream_by_metadata(
    field: str = Query(..., description="Metadata field to filter by"),
    value: str = Query(..., description="Value to match"),
    exclude_channel_id: Optional[str] = Query(
        None, description="Channel ID to exclude from deletion (keep this stream)")
):
    """
    Delete the OLDEST stream matching a specific metadata field/value.

    This is useful for connection limit management with "latest wins" behavior:
    - When a playlist reaches its connection limit, stop the oldest stream to make room
    - This allows instant channel switching for single-connection providers

    Only deletes ONE stream (the oldest), unlike /streams/by-metadata which deletes all matches.
    """
    try:
        oldest_stream_id = None
        oldest_created_at = None

        # Find the oldest stream that matches the metadata criteria
        for stream_id, stream_info in stream_manager.streams.items():
            metadata = stream_info.metadata or {}

            # Check if this stream matches the filter criteria
            if field in metadata and str(metadata[field]) == str(value):
                # Check if this stream should be excluded
                if exclude_channel_id and str(metadata.get('id')) == str(exclude_channel_id):
                    continue

                # Track the oldest stream
                if oldest_created_at is None or stream_info.created_at < oldest_created_at:
                    oldest_stream_id = stream_id
                    oldest_created_at = stream_info.created_at

        # If no matching stream found
        if oldest_stream_id is None:
            return {
                "message": f"No streams found matching {field}={value}",
                "deleted_stream": None,
                "deleted_count": 0
            }

        # Delete the oldest stream
        stream_info = stream_manager.streams[oldest_stream_id]
        logger.info(
            f"Deleting oldest stream {oldest_stream_id} (created at {oldest_created_at}) matching {field}={value}")

        # For transcoded streams, force stop the FFmpeg process immediately
        if stream_info.is_transcoded and stream_manager.pooled_manager:
            try:
                stream_key = stream_manager.pooled_manager._generate_stream_key(
                    stream_info.current_url or stream_info.original_url,
                    stream_info.transcode_profile or "default"
                )
                await stream_manager.pooled_manager.force_stop_stream(stream_key)
            except Exception as e:
                logger.warning(
                    f"Error stopping transcoding for stream {oldest_stream_id}: {e}")

        # Clean up all clients for this stream
        if oldest_stream_id in stream_manager.stream_clients:
            client_ids = list(stream_manager.stream_clients[oldest_stream_id])
            for client_id in client_ids:
                await stream_manager.cleanup_client(client_id)

        # Emit stream_stopped event
        await stream_manager._emit_event("STREAM_STOPPED", oldest_stream_id, {
            "reason": "oldest_stream_deletion",
            "filter_field": field,
            "filter_value": value,
            "was_transcoded": stream_info.is_transcoded,
            "stream_age_seconds": (datetime.now(timezone.utc) - oldest_created_at).total_seconds() if oldest_created_at else None,
            "metadata": stream_info.metadata
        })

        # Remove stream
        if oldest_stream_id in stream_manager.streams:
            del stream_manager.streams[oldest_stream_id]
        if oldest_stream_id in stream_manager.stream_clients:
            del stream_manager.stream_clients[oldest_stream_id]

        stream_manager._stats.active_streams -= 1

        return {
            "message": f"Deleted oldest stream matching {field}={value}",
            "deleted_stream": oldest_stream_id,
            "stream_age_seconds": (datetime.now(timezone.utc) - oldest_created_at).total_seconds() if oldest_created_at else None,
            "deleted_count": 1
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting oldest stream by metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/streams/by-metadata", dependencies=[Depends(verify_token)])
async def delete_streams_by_metadata(
    field: str = Query(..., description="Metadata field to filter by"),
    value: str = Query(..., description="Value to match"),
    exclude_channel_id: Optional[str] = Query(
        None, description="Channel ID to exclude from deletion (keep this stream)")
):
    """
    Delete all streams matching a specific metadata field/value.

    This is useful for connection limit management:
    - When switching channels on a limited connection playlist, stop the old stream first
    - Pass exclude_channel_id to keep the new stream you're about to create

    Common use cases:
    - Stop all streams for a playlist: field=playlist_uuid, value=<uuid>
    - Stop all streams for a specific type: field=type, value=channel|episode
    """
    try:
        deleted_streams = []
        skipped_streams = []

        # Find all streams that match the metadata criteria
        for stream_id, stream_info in list(stream_manager.streams.items()):
            metadata = stream_info.metadata or {}

            # Check if this stream matches the filter criteria
            if field in metadata and str(metadata[field]) == str(value):
                # Check if this stream should be excluded (e.g., keeping the new channel)
                if exclude_channel_id and metadata.get('id') == str(exclude_channel_id):
                    skipped_streams.append({
                        "stream_id": stream_id,
                        "reason": "excluded_by_channel_id"
                    })
                    continue

                # Delete this stream
                logger.info(
                    f"Deleting stream {stream_id} matching {field}={value}")

                # For transcoded streams, force stop the FFmpeg process immediately
                if stream_info.is_transcoded and stream_manager.pooled_manager:
                    try:
                        stream_key = stream_manager.pooled_manager._generate_stream_key(
                            stream_info.current_url or stream_info.original_url,
                            stream_info.transcode_profile or "default"
                        )
                        await stream_manager.pooled_manager.force_stop_stream(stream_key)
                    except Exception as e:
                        logger.warning(
                            f"Error stopping transcoding for stream {stream_id}: {e}")

                # Clean up all clients for this stream
                if stream_id in stream_manager.stream_clients:
                    client_ids = list(stream_manager.stream_clients[stream_id])
                    for client_id in client_ids:
                        await stream_manager.cleanup_client(client_id)

                # Emit stream_stopped event
                await stream_manager._emit_event("STREAM_STOPPED", stream_id, {
                    "reason": "metadata_filter_deletion",
                    "filter_field": field,
                    "filter_value": value,
                    "was_transcoded": stream_info.is_transcoded,
                    "metadata": stream_info.metadata
                })

                # Remove stream
                if stream_id in stream_manager.streams:
                    del stream_manager.streams[stream_id]
                if stream_id in stream_manager.stream_clients:
                    del stream_manager.stream_clients[stream_id]

                stream_manager._stats.active_streams -= 1
                deleted_streams.append(stream_id)

        return {
            "message": f"Deleted {len(deleted_streams)} stream(s) matching {field}={value}",
            "deleted_streams": deleted_streams,
            "skipped_streams": skipped_streams,
            "deleted_count": len(deleted_streams)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting streams by metadata: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/streams/{stream_id}", dependencies=[Depends(verify_token)])
async def delete_stream(stream_id: str):
    """Delete a stream and disconnect all its clients"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = stream_manager.streams[stream_id]

        # For transcoded streams, force stop the FFmpeg process immediately
        if stream_info.is_transcoded and stream_manager.pooled_manager:
            logger.info(f"Force stopping transcoded stream {stream_id}")
            # Get the stream key used by pooled manager
            # This is typically based on URL and profile
            from pooled_stream_manager import PooledStreamManager
            stream_key = stream_manager.pooled_manager._generate_stream_key(
                stream_info.current_url or stream_info.original_url,
                stream_info.transcode_profile or "default"
            )
            await stream_manager.pooled_manager.force_stop_stream(stream_key)

        # Get all clients for this stream and clean them up
        if stream_id in stream_manager.stream_clients:
            client_ids = list(stream_manager.stream_clients[stream_id])
            for client_id in client_ids:
                await stream_manager.cleanup_client(client_id)

        # Emit stream_stopped event before removing the stream
        await stream_manager._emit_event("STREAM_STOPPED", stream_id, {
            "reason": "manual_deletion",
            "was_transcoded": stream_info.is_transcoded,
            "metadata": stream_info.metadata
        })

        # Remove stream
        if stream_id in stream_manager.streams:
            del stream_manager.streams[stream_id]
        if stream_id in stream_manager.stream_clients:
            del stream_manager.stream_clients[stream_id]

        stream_manager._stats.active_streams -= 1

        return {"message": f"Stream {stream_id} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/streams/{stream_id}/failover", dependencies=[Depends(verify_token)])
async def trigger_failover(stream_id: str):
    """Manually trigger failover for a stream - will seamlessly switch all active clients to the next failover URL"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = stream_manager.streams[stream_id]

        # Check if any failover mechanism is available
        has_failovers = bool(
            stream_info.failover_resolver_url or stream_info.failover_urls)
        if not has_failovers:
            raise HTTPException(
                status_code=400,
                detail="No failover mechanism configured for this stream (neither failover_urls nor failover_resolver_url)"
            )

        # Trigger failover which will:
        # 1. Update current_url to next failover URL (via resolver or array)
        # 2. Signal all active clients via failover_event
        # 3. For transcoded streams, restart FFmpeg with new URL
        # 4. Emit FAILOVER_TRIGGERED event
        success = await stream_manager._try_update_failover_url(stream_id, "manual")

        if success:
            return {
                "message": "Failover triggered successfully - all clients will seamlessly reconnect",
                "new_url": stream_info.current_url,
                "failover_index": stream_info.current_failover_index,
                "failover_attempts": stream_info.failover_attempts,
                "active_clients": len(stream_info.connected_clients),
                "stream_type": "Transcoded" if stream_info.is_transcoded else (
                    "HLS" if stream_info.is_hls else (
                        "VOD" if stream_info.is_vod else "Live Continuous"
                    )
                )
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Failover failed - no working failover URLs available"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error triggering failover: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health", dependencies=[Depends(verify_token)])
async def health_check():
    """Health check endpoint with detailed status"""
    try:
        stats = stream_manager.get_stats()
        proxy_stats = stats["proxy_stats"]
        # Note: PUBLIC_URL is now deprecated in favor of relative URLs.
        # The proxy returns null if not configured (which is now the norm).

        # Get the host accessing this endpoint from request headers and use as the public URL

        return {
            "status": "healthy",
            "root_path": getattr(settings, 'ROOT_PATH', '/m3u-proxy'),
            "version": VERSION,
            "uptime_seconds": proxy_stats["uptime_seconds"],
            "active_streams": proxy_stats["active_streams"],
            "active_clients": proxy_stats["active_clients"],
            "total_bytes_served": proxy_stats["total_bytes_served"],
            "stats": proxy_stats
        }
    except Exception as e:
        logger.error(f"Error in health check: {e}")
        return {
            "status": "error",
            "error": str(e)
        }, 500


class TestConnectionRequest(BaseModel):
    """Request model for testing connectivity to an external URL"""
    url: str


@app.post("/test-connection", dependencies=[Depends(verify_token)])
async def test_url_connectivity(request: TestConnectionRequest):
    """
    Test connectivity to an external URL by calling it from the proxy to verify connectivity.
    This is used to verify the proxy can reach external services for failover resolution.
    """
    import httpx

    # Ensure URL doesn't have trailing slash
    test_url = request.url.rstrip('/')

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(test_url)

            if response.status_code == 200:
                return {
                    "success": True,
                    "message": f"Successfully connected to {test_url}",
                    "status_code": response.status_code,
                    "url_tested": test_url
                }
            else:
                return {
                    "success": False,
                    "message": f"URL returned non-200 status code",
                    "status_code": response.status_code,
                    "url_tested": test_url
                }
    except httpx.ConnectError as e:
        logger.warning(f"Connection error testing URL {test_url}: {e}")
        return {
            "success": False,
            "message": f"Connection failed: Unable to connect to {request.url}",
            "error": str(e),
            "url_tested": test_url
        }
    except httpx.TimeoutException as e:
        logger.warning(f"Timeout testing URL {test_url}: {e}")
        return {
            "success": False,
            "message": f"Connection timed out after 10 seconds",
            "error": str(e),
            "url_tested": test_url
        }
    except Exception as e:
        logger.error(f"Error testing URL {test_url}: {e}")
        return {
            "success": False,
            "message": f"Error testing connection: {str(e)}",
            "error": str(e),
            "url_tested": test_url
        }


# Webhook Management Endpoints


@app.post("/webhooks", dependencies=[Depends(verify_token)])
async def add_webhook(webhook: WebhookConfig):
    """Add a new webhook configuration"""
    try:
        event_manager.add_webhook(webhook)
        return {
            "message": "Webhook added successfully",
            "webhook_url": str(webhook.url),
            "events": [event.value for event in webhook.events]
        }
    except Exception as e:
        logger.error(f"Error adding webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/webhooks", dependencies=[Depends(verify_token)])
async def list_webhooks():
    """List all configured webhooks"""
    try:
        webhooks = [
            {
                "url": str(wh.url),
                "events": [event.value for event in wh.events],
                "timeout": wh.timeout,
                "retry_attempts": wh.retry_attempts
            }
            for wh in event_manager.webhooks
        ]
        return {"webhooks": webhooks}
    except Exception as e:
        logger.error(f"Error listing webhooks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/webhooks", dependencies=[Depends(verify_token)])
async def remove_webhook(webhook_url: str = Query(..., description="Webhook URL to remove")):
    """Remove a webhook configuration"""
    try:
        removed = event_manager.remove_webhook(webhook_url)
        if removed:
            return {"message": f"Webhook {webhook_url} removed successfully"}
        else:
            raise HTTPException(status_code=404, detail="Webhook not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/test", dependencies=[Depends(verify_token)])
async def test_webhook(webhook_url: str = Query(..., description="Webhook URL to test")):
    """Send a test event to a webhook"""
    try:
        # Create test event
        test_event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id="test_stream_123",
            data={
                "test": True,
                "message": "This is a test webhook event",
                "primary_url": "http://example.com/test.m3u8"
            }
        )

        # Find webhook and send test
        webhook_found = False
        for webhook in event_manager.webhooks:
            if str(webhook.url) == webhook_url:
                webhook_found = True
                await event_manager._send_webhook(webhook, test_event)
                break

        if not webhook_found:
            raise HTTPException(status_code=404, detail="Webhook not found")

        return {
            "message": f"Test event sent to {webhook_url}",
            "event_id": test_event.event_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# Network Broadcast Endpoints
# ============================================================================


class BroadcastStartRequest(BaseModel):
    """Request model for starting a network broadcast."""
    stream_url: str
    seek_seconds: int = 0
    duration_seconds: int = 0  # 0 = unlimited
    segment_start_number: int = 0
    add_discontinuity: bool = False
    segment_duration: int = 6
    hls_list_size: int = 20
    transcode: bool = False
    video_bitrate: Optional[str] = None
    audio_bitrate: int = 192
    video_resolution: Optional[str] = None
    # Optional codec/preset/hwaccel to pass to the broadcast process
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    preset: Optional[str] = None
    hwaccel: Optional[str] = None
    callback_url: Optional[str] = None

    @field_validator('stream_url')
    @classmethod
    def validate_stream_url(cls, v):
        return validate_url(v)

    @field_validator('callback_url')
    @classmethod
    def validate_callback_url(cls, v):
        if v is not None:
            return validate_url(v)
        return v


class BroadcastStatusResponse(BaseModel):
    """Response model for broadcast status."""
    network_id: str
    status: str
    current_segment_number: int
    started_at: Optional[str]
    stream_url: str
    ffmpeg_pid: Optional[int] = None
    error_message: Optional[str] = None


@app.post("/broadcast/{network_id}/start", dependencies=[Depends(verify_token)])
async def start_broadcast(
    network_id: str,
    request: BroadcastStartRequest
) -> BroadcastStatusResponse:
    """
    Start or transition a network broadcast.

    If a broadcast is already running for this network, it will be stopped
    gracefully and the new broadcast will continue with proper segment numbering.

    The FFmpeg process will run with a duration limit (-t flag) to stop at
    the programme boundary. When FFmpeg exits, a webhook callback is sent
    to the callback_url if provided.
    """
    try:
        config = BroadcastConfig(
            network_id=network_id,
            stream_url=request.stream_url,
            seek_seconds=request.seek_seconds,
            duration_seconds=request.duration_seconds,
            segment_start_number=request.segment_start_number,
            add_discontinuity=request.add_discontinuity,
            segment_duration=request.segment_duration,
            hls_list_size=request.hls_list_size,
            transcode=request.transcode,
            video_bitrate=request.video_bitrate,
            audio_bitrate=request.audio_bitrate,
            video_resolution=request.video_resolution,
            video_codec=request.video_codec,
            audio_codec=request.audio_codec,
            preset=request.preset,
            hwaccel=request.hwaccel,
            callback_url=request.callback_url
        )
        status = await broadcast_manager.start_broadcast(config)
        return BroadcastStatusResponse(
            network_id=status.network_id,
            status=status.status,
            current_segment_number=status.current_segment_number,
            started_at=status.started_at,
            stream_url=status.stream_url,
            ffmpeg_pid=status.ffmpeg_pid,
            error_message=status.error_message
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting broadcast {network_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/broadcast/{network_id}/stop", dependencies=[Depends(verify_token)])
async def stop_broadcast(network_id: str) -> dict:
    """
    Stop a network broadcast.

    Gracefully terminates the FFmpeg process and returns the final segment number
    for use in subsequent broadcasts.
    """
    try:
        status = await broadcast_manager.stop_broadcast(network_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Broadcast not found")
        return {
            "message": "Broadcast stopped",
            "network_id": network_id,
            "final_segment_number": status.current_segment_number,
            "status": status.status
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping broadcast {network_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/broadcast/{network_id}/live.m3u8")
async def get_broadcast_playlist(network_id: str) -> Response:
    """
    Serve the HLS playlist for a network broadcast.

    Returns the live.m3u8 playlist file with appropriate headers for
    HLS streaming compatibility.
    """
    content = await broadcast_manager.read_playlist(network_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Playlist not available")
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.get("/broadcast/{network_id}/segment/{filename}")
async def get_broadcast_segment(network_id: str, filename: str) -> FileResponse:
    """
    Serve a segment file for a network broadcast.

    Segments are served with caching headers since they are immutable
    once created.
    """
    segment_path = broadcast_manager.get_segment_path(network_id, filename)
    if segment_path is None or not os.path.exists(segment_path):
        raise HTTPException(status_code=404, detail="Segment not found")
    return FileResponse(
        segment_path,
        media_type="video/MP2T",
        headers={
            "Cache-Control": "max-age=86400",  # Segments are immutable
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.get("/broadcast/{network_id}/status", dependencies=[Depends(verify_token)])
async def get_broadcast_status(network_id: str) -> BroadcastStatusResponse:
    """
    Get current broadcast status for a network.

    Returns information about the running FFmpeg process including
    PID, current segment number, and any error messages.
    """
    status = broadcast_manager.get_status(network_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    return BroadcastStatusResponse(
        network_id=status.network_id,
        status=status.status,
        current_segment_number=status.current_segment_number,
        started_at=status.started_at,
        stream_url=status.stream_url,
        ffmpeg_pid=status.ffmpeg_pid,
        error_message=status.error_message
    )


@app.get("/broadcast", dependencies=[Depends(verify_token)])
async def list_broadcasts() -> dict:
    """
    List all active broadcasts with their current status.
    """
    statuses = broadcast_manager.get_all_statuses()
    return {
        "broadcasts": [
            {
                "network_id": status.network_id,
                "status": status.status,
                "current_segment_number": status.current_segment_number,
                "started_at": status.started_at,
                "stream_url": status.stream_url,
                "ffmpeg_pid": status.ffmpeg_pid
            }
            for status in statuses.values()
        ],
        "count": len(statuses)
    }


@app.delete("/broadcast/{network_id}", dependencies=[Depends(verify_token)])
async def cleanup_broadcast(network_id: str) -> dict:
    """
    Stop broadcast and clean up all files for a network.

    This removes the broadcast directory and all segment files.
    Use this when a network is deleted or when you want a fresh start.
    """
    try:
        success = await broadcast_manager.cleanup_broadcast(network_id)
        if success:
            return {"message": f"Broadcast {network_id} cleaned up successfully"}
        else:
            raise HTTPException(
                status_code=500, detail="Failed to clean up broadcast")
    except Exception as e:
        logger.error(f"Error cleaning up broadcast {network_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Event Handler Examples
# Custom event handlers are now set up in the lifespan context manager above
