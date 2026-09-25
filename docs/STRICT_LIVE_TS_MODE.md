## 📺 Strict Live TS Mode

**NEW**: Enhanced stability for live MPEG-TS streams with PVR clients like Kodi!

Strict Live TS Mode solves the common "1 second play → cache → repeat" problem when using live TV streams with IPTV clients:

### Features
- 🎯 **Neutralizes Range Headers** - Strips problematic Range headers from live TS requests
- ⚡ **Pre-buffering** - Reads 256-512 KB before streaming for smoother playback start
- 🔌 **Circuit Breaker** - Detects stalled upstream connections and triggers automatic failover
- 🚀 **Fast HEAD Requests** - Optimized HEAD responses without upstream hits

### Quick Enable

**Global (all streams):**
```bash
# In your .env or docker-compose.yml
STRICT_LIVE_TS=true
```

**Per-Stream (API):**
```bash
curl -X POST "http://localhost:8085/m3u-proxy/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://provider.com/channel.ts",
    "strict_live_ts": true,
    "failover_urls": ["http://backup.com/channel.ts"]
  }'
```

### Advanced Configuration
```bash
# Pre-buffer size (256 KB default, ~0.5-1s of data)
STRICT_LIVE_TS_PREBUFFER_SIZE=262144

# Circuit breaker timeout (2 seconds default)
STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT=2

# Bad upstream cooldown (60 seconds default)
STRICT_LIVE_TS_CIRCUIT_BREAKER_COOLDOWN=60
```

## Overview

**Strict Live TS Mode** is an optional feature designed to improve robustness and playback stability for raw MPEG-TS streams delivered over HTTP without transcoding. This mode is particularly beneficial when using PVR add-ons like Kodi's PVR IPTV Simple, which often exhibit "1 second play → cache → 1 second play" patterns during channel switches or stream interruptions.

## Problem Statement

When proxying live MPEG-TS streams over HTTP without transcoding, many IPTV clients experience:

- **Frequent buffering loops** immediately after channel switch
- **"1 second play → cache"** pattern that repeats continuously
- **Slow channel change times** due to first-byte delays
- **Connection instability** from Range header conflicts
- **Rapid reconnection loops** when upstream stalls

This results in a visibly poor user experience despite the upstream source being functional.

## Features

When Strict Live TS Mode is enabled (globally or per-stream), the proxy applies several optimizations:

### 1. **Neutralized Range Header Behavior**
- **Strips incoming Range headers** completely for live TS streams
- Responds with **HTTP 200 OK** (never 206 Partial Content)
- **No Content-Length** header sent for live streams
- **Accept-Ranges: none** explicitly set to prevent client retries

### 2. **Startup Pre-buffering**
- Reads **256-512 KB** (~0.5-1 second of data) from upstream before emitting to client
- Smooths first-byte delay and reduces initial buffering
- Configurable buffer size and timeout to prevent infinite waits
- Logs pre-buffer progress for monitoring

### 3. **Circuit Breaker for Upstream Stalls**
- Monitors data flow from upstream
- If **no data received for 2+ seconds** (configurable), marks upstream as "bad"
- **Temporarily blacklists** the endpoint for 60 seconds (configurable)
- **Automatic failover** to alternative sources if available
- Prevents rapid reconnection loops to stalled endpoints

### 4. **Optimized HEAD Request Handling**
- For live TS streams in strict mode, HEAD requests return **immediately**
- **No upstream connection** made for HEAD requests
- Prevents redundant upstream hits that can interfere with live streams
- Reduces latency for client probing

## Configuration

### Global Configuration (Environment Variables)

Add these to your `.env` file or set as environment variables:

```bash
# Enable Strict Live TS Mode globally for all live TS streams
STRICT_LIVE_TS=true

# Pre-buffer size in bytes (default: 262144 = 256 KB)
# Recommended: 256KB-512KB for 0.5-1 second of buffering
STRICT_LIVE_TS_PREBUFFER_SIZE=262144

# Circuit breaker timeout in seconds (default: 2)
# If no data received for this duration, mark upstream as bad
STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT=2

# Circuit breaker cooldown in seconds (default: 60)
# How long to avoid a failed upstream before retrying
STRICT_LIVE_TS_CIRCUIT_BREAKER_COOLDOWN=60

# Pre-buffer timeout in seconds (default: 10)
# Maximum time to wait for pre-buffer to complete
STRICT_LIVE_TS_PREBUFFER_TIMEOUT=10

# Overlap trimming on silent reconnect (default: true)
# Providers that close the connection periodically usually restart from their
# rolling buffer, slightly behind what the client already received. The proxy
# locates the last delivered bytes in the new connection and drops everything
# up to them, so playback continues from the exact next byte (no jump-back).
# If no match is found, the held data is forwarded unchanged.
STRICT_LIVE_TS_OVERLAP_TRIM=true
STRICT_LIVE_TS_OVERLAP_SIGNATURE_SIZE=16384
STRICT_LIVE_TS_OVERLAP_MAX_SEARCH_SIZE=8388608
STRICT_LIVE_TS_OVERLAP_MAX_WAIT=0.5
```

### Per-Stream Configuration (API)

You can enable Strict Live TS Mode for individual streams via the API:

```bash
curl -X POST "http://localhost:8085/m3u-proxy/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://example.com/live/stream.ts",
    "strict_live_ts": true,
    "failover_urls": [
      "http://backup1.example.com/live/stream.ts",
      "http://backup2.example.com/live/stream.ts"
    ]
  }'
```

Per-stream configuration **overrides** the global setting for that specific stream.

## Use Cases

Strict Live TS Mode is ideal for:

1. **Live TV Streams** delivered as MPEG-TS over HTTP
2. **Low-latency scenarios** where direct pass-through is preferred over transcoding
3. **Clients with Range header issues** (Kodi PVR IPTV Simple, some Android players)
4. **Unreliable upstream sources** that occasionally stall or drop connections
5. **Fast channel switching** requirements where pre-buffering helps smooth transitions

## How It Works

### Normal Mode (Strict Mode Disabled)

```
Client Request → Proxy → Upstream
                    ↓
       First byte → Stream directly to client
                    ↓
         Client may send Range headers
                    ↓
         Proxy forwards Range headers
                    ↓
         May cause connection issues
```

### Strict Live TS Mode (Enabled)

```
Client Request → Proxy → Upstream
                    ↓
    Strip Range headers (if live TS)
                    ↓
    Pre-buffer 256-512 KB from upstream
                    ↓
    Monitor circuit breaker timeout
                    ↓
    Emit pre-buffer + stream rest to client
                    ↓
    Return HTTP 200 (no Content-Length)
                    ↓
    Smoother, more stable playback
```

### Circuit Breaker Flow

```
Streaming data → Check last chunk time
                    ↓
    If > 2s since last chunk:
                    ↓
    Mark upstream as BAD for 60s
                    ↓
    Trigger automatic failover
                    ↓
    Try next failover URL
                    ↓
    If failover succeeds: Continue streaming
    If no failover: Connection closes
```

## Logging

When Strict Live TS Mode is active, you'll see enhanced logging:

```
[INFO] STRICT MODE: Starting direct proxy with STRICT LIVE TS MODE for client abc123, stream xyz789
[INFO] STRICT MODE: Completely stripping Range header for live TS stream: bytes=0-1024
[INFO] STRICT MODE: Pre-buffering 262144 bytes (~0.5-1s) before streaming to client abc123
[INFO] STRICT MODE: Pre-buffer complete: 262144 bytes in 8 chunks
[INFO] STRICT MODE: Emitted pre-buffer, now streaming live for client abc123
[INFO] STRICT MODE: Returning 200 OK without Content-Length for live TS stream
```

If circuit breaker triggers:

```
[ERROR] STRICT MODE: Circuit breaker triggered - no data for 3.5s (threshold: 2s)
[WARNING] STRICT MODE: Marking upstream as bad for 60s until 2025-11-05T12:35:00Z
[INFO] STRICT MODE: Attempting failover due to circuit breaker
```

## Performance Impact

- **Minimal CPU overhead**: No transcoding, just buffering logic
- **Memory usage**: ~256-512 KB per active stream for pre-buffer
- **Latency**: Adds ~0.5-1 second initial delay (pre-buffer time)
- **Network**: Slightly increased upstream traffic due to pre-buffering
- **Compatibility**: Works with existing pass-through architecture

## Compatibility

### Tested Clients

- ✅ **Kodi PVR IPTV Simple** (primary use case)
- ✅ **VLC Media Player**
- ✅ **MPV Player**
- ✅ **FFplay**
- ✅ **Android IPTV apps** (varies by app)

### Stream Types

- ✅ **MPEG-TS over HTTP** (.ts extension)
- ✅ **Live continuous streams** (detected by URL pattern)
- ⚠️ **HLS streams** (not affected, already handled separately)
- ⚠️ **VOD content** (not affected unless explicitly enabled)

## Troubleshooting

### Issue: Still seeing buffering loops

**Solutions:**
- Increase `STRICT_LIVE_TS_PREBUFFER_SIZE` to 524288 (512 KB) or higher
- Check if upstream is actually stalling (look for circuit breaker logs)
- Verify failover URLs are configured and working
- Try increasing `STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT` to 3-5 seconds

### Issue: Too much delay during channel switch

**Solutions:**
- Decrease `STRICT_LIVE_TS_PREBUFFER_SIZE` to 131072 (128 KB)
- Reduce `STRICT_LIVE_TS_PREBUFFER_TIMEOUT` to 5 seconds
- Ensure upstream has good network connectivity

### Issue: Circuit breaker triggering too often

**Solutions:**
- Increase `STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT` to 5-10 seconds
- Check upstream source stability (may have natural pauses)
- Verify network connectivity to upstream

### Issue: Streams not marked as "live continuous"

**Solutions:**
- Check if URL ends with `.ts` or contains `/live/` in path
- Manually verify stream type in logs: `"is_live_continuous": true`
- Force strict mode per-stream via API with `"strict_live_ts": true`

## Best Practices

1. **Enable globally** if most of your streams are live TS
2. **Configure failover URLs** for critical streams to maximize circuit breaker effectiveness
3. **Monitor logs** during initial rollout to tune pre-buffer and timeout settings
4. **Start with defaults** and adjust based on your specific network conditions
5. **Test channel switching** extensively with your client before production deployment

## API Examples

### Create Stream with Strict Mode

```bash
curl -X POST "http://localhost:8085/m3u-proxy/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://provider.example.com/channel1.ts",
    "strict_live_ts": true,
    "failover_urls": [
      "http://provider2.example.com/channel1.ts"
    ],
    "user_agent": "MyIPTVApp/1.0"
  }'
```

Response:
```json
{
  "stream_id": "abc123def456...",
  "stream_type": "direct",
  "stream_endpoint": "/stream/abc123def456...",
  "message": "Stream created successfully (direct)"
}
```

### Check Stream Info

```bash
curl "http://localhost:8085/m3u-proxy/streams/abc123def456..."
```

Response includes `strict_live_ts` status:
```json
{
  "stream_id": "abc123def456...",
  "original_url": "http://provider.example.com/channel1.ts",
  "is_live_continuous": true,
  "strict_live_ts": true,
  "upstream_marked_bad_until": null,
  ...
}
```

## Version History

- **v0.2.21** (2025-11-05): Initial implementation of Strict Live TS Mode
  - Range header neutralization
  - Startup pre-buffering
  - Circuit breaker with failover
  - HEAD request optimization
