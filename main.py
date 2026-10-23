#!/usr/bin/env python3
"""
m3u-proxy - Main Entry Point
A high-performance IPTV streaming proxy with client management and failover support.
"""

import uvicorn
import logging
import logging.handlers
import sys
import os
import asyncio

# Add the src directory to Python path so local modules in `src/` can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import configs AFTER setting up the path
from redis_config import get_redis_config, should_use_pooling
from config import settings, VERSION

def _build_uvicorn_log_config(log_level: str, anonymize: bool) -> dict:
    """Return a dictConfig-compatible logging config for uvicorn.

    When anonymize=True the AnonymizingFilter is added to both the default
    and access handlers so access-log request lines are also scrubbed.
    """
    config: dict = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.error": {"level": log_level.upper()},
            "uvicorn.access": {"handlers": ["access"], "level": log_level.upper(), "propagate": False},
        },
    }

    if anonymize:
        config["filters"] = {
            "anonymizing": {"()": "log_anonymizer.AnonymizingFilter"},
        }
        for handler in config["handlers"].values():
            handler["filters"] = ["anonymizing"]

    return config


def main():
    """Main function to start the m3u-proxy server."""

    # Try to use uvloop for better async performance (2-4x faster).
    # uvicorn creates the uvloop event loop itself via loop="uvloop" below;
    # asyncio.set_event_loop_policy() is deprecated as of Python 3.14.
    try:
        import uvloop  # noqa: F401
        use_uvloop = True
    except ImportError:
        use_uvloop = False

    # Configure logging
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format=log_format
    )

    # Add rotating file handler so logs are persisted to the mounted volume
    log_dir = settings.LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, settings.LOG_FILE),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(settings.LOG_LEVEL.upper())
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)

    if settings.LOG_ANONYMIZE:
        from log_anonymizer import AnonymizingFilter
        anon_filter = AnonymizingFilter()
        for handler in logging.getLogger().handlers:
            handler.addFilter(anon_filter)

    logger = logging.getLogger(__name__)
    logger.info("="*60)
    logger.info(
        f"⚡️ Starting m3u-proxy v{VERSION} on {settings.HOST}:{settings.PORT}")
    logger.info("="*60)
    logger.info(f"ℹ️  Log level set to: {settings.LOG_LEVEL}")
    if use_uvloop:
        logger.info("✅ Using uvloop for optimized async I/O performance")
    else:
        logger.info(
            "✅ Using standard asyncio (install uvloop for better performance)")

    logger.info("✅ Connection pooling enabled (HLS, Transcoded, and live Direct/TS streams)")
    logger.info("✅ Transcoding support via FFmpeg")
    logger.info("✅ Streamlink and yt-dlp resolver support")
    logger.info("✅ Seamless failover support")

    if settings.RELOAD:
        logger.info("🔄 Auto-reload is enabled.")

    # If pooling is enabled, perform a quick Redis connectivity check and log result
    if should_use_pooling() and settings.REDIS_ENABLED:
        try:
            import redis.asyncio as redis_async

            redis_cfg = get_redis_config()
            redis_url = redis_cfg.get('redis_url')

            async def _check_redis():
                try:
                    # protocol=2 keeps connections on RESP2: the proxy uses no
                    # RESP3-only features, and RESP3 makes newer redis-py probe
                    # the server with CLIENT MAINT_NOTIFICATIONS, which logs a
                    # harmless "unknown subcommand" against Redis < 8.
                    client = redis_async.from_url(
                        redis_url, decode_responses=True, protocol=2)
                    await client.ping()
                    await client.aclose()
                    return True
                except Exception:
                    try:
                        if 'client' in locals() and client:
                            await client.aclose()
                    except Exception:
                        pass
                    return False

            ok = asyncio.run(_check_redis())
            if ok:
                logger.info(
                    f"✅ Redis available and reachable for pooling")
            else:
                logger.warning(
                    f"❌  Redis configured but ping failed for: {redis_url}; pooling will be unavailable")

        except ImportError:
            logger.warning(
                "❌ Redis async library not installed; pooling requested but unavailable")
        except Exception as e:
            logger.warning(f"❌ Redis pooling check failed: {e}")

    # Start the server using settings from the config object
    uvicorn.run(
        "api:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        log_level=settings.LOG_LEVEL.lower(),
        log_config=_build_uvicorn_log_config(settings.LOG_LEVEL, settings.LOG_ANONYMIZE),
        loop="uvloop" if use_uvloop and not settings.RELOAD else "asyncio"
    )


if __name__ == "__main__":
    main()
