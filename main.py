#!/usr/bin/env python3
"""
m3u-proxy - Main Entry Point
A high-performance IPTV streaming proxy with client management and failover support.
"""

import uvicorn
import logging
import sys
import os
import asyncio

# Add the src directory to Python path so local modules in `src/` can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import configs AFTER setting up the path
from redis_config import get_redis_config, should_use_pooling
from config import settings, VERSION

def main():
    """Main function to start the m3u-proxy server."""

    # Try to use uvloop for better async performance (2-4x faster)
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        use_uvloop = True
    except ImportError:
        use_uvloop = False

    # Configure logging
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

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
    if should_use_pooling():
        try:
            import redis.asyncio as redis_async

            redis_cfg = get_redis_config()
            redis_url = redis_cfg.get('redis_url')

            async def _check_redis():
                try:
                    client = redis_async.from_url(
                        redis_url, decode_responses=True)
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
        loop="uvloop" if use_uvloop and not settings.RELOAD else "asyncio"
    )


if __name__ == "__main__":
    main()
