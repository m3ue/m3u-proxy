# Sticky Session Handler

## Overview

The Sticky Session Handler is a feature that "locks" a client to a specific backend origin after following redirects. This prevents playback issues caused by load balancers bouncing clients between different backend servers that may have different playlist states.

## The Problem It Solves

Many IPTV providers use load balancers that redirect requests to different backend servers. For example:

```
Client Request → Load Balancer (provider.com) → Backend Server 1 (backend1.provider.com)
                                               → Backend Server 2 (backend2.provider.com)
                                               → Backend Server 3 (backend3.provider.com)
```

### Without Sticky Sessions

When sticky sessions are disabled:

1. Client requests playlist from `provider.com`
2. Load balancer redirects to `backend1.provider.com`
3. Proxy fetches playlist with sequence #1000
4. Next request goes to `provider.com` again
5. Load balancer redirects to `backend2.provider.com` (different backend!)
6. Playlist has sequence #500 (this backend is behind)
7. **Media player sees non-monotonic sequence numbers and fails/loops**

This creates a "playback loop" where the player thinks the stream has jumped backwards.

### With Sticky Sessions

When sticky sessions are enabled:

1. Client requests playlist from `provider.com`
2. Load balancer redirects to `backend1.provider.com`
3. **Proxy "locks" to `backend1.provider.com` for all future requests**
4. Next request goes directly to `backend1.provider.com` (bypassing load balancer)
5. Playlist has sequence #1001 (monotonic progression)
6. **Playback is smooth and stable**

## When to Use Sticky Sessions

### Enable (USE_STICKY_SESSION=true) When:

- ✅ Provider uses load balancers that redirect to multiple backends
- ✅ You experience playback loops or repeated buffering
- ✅ Stream restarts frequently without clear reason
- ✅ Logs show playlist sequence numbers jumping backwards
- ✅ Provider has multiple CDN endpoints that aren't synchronized

### Disable (USE_STICKY_SESSION=false) When:

- ❌ Provider uses a single origin (no load balancing)
- ❌ Provider's load balancer maintains session affinity on its own
- ❌ All backends are perfectly synchronized (rare)
- ❌ You want to leverage load balancer health checks
- ❌ Provider's backends are geographically distributed and you want geo-failover

## Configuration

### Global Setting

Set in your `.env` file or Docker environment:

```bash
# Enable sticky sessions by default for all streams
USE_STICKY_SESSION=true
```

Default: `false`

### Per-Stream Override

You can override the global setting when creating a stream:

```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://provider.com/stream.m3u8",
    "use_sticky_session": true
  }'
```

The per-stream setting takes precedence over the global configuration.

## How It Works

### Initial Request (Redirect Detected)

```
1. Request: https://provider.com/playlist.m3u8
2. Redirect: → https://backend3.provider.com/playlist.m3u8
3. ✓ Proxy locks current_url to https://backend3.provider.com/playlist.m3u8
4. Log: "Sticky session: Locking stream ABC123 to origin: https://backend3.provider.com/playlist.m3u8"
```

### Subsequent Requests

```
1. Proxy uses locked URL: https://backend3.provider.com/playlist.m3u8
2. No redirect occurs (already at final destination)
3. Consistent playlist state maintained
```

### Automatic Recovery

If the "sticky" backend fails, the proxy automatically reverts to the original URL:

```
1. Locked backend fails: https://backend3.provider.com/playlist.m3u8 ❌
2. Log: "Sticky origin failed. Reverting to original configured entry point."
3. Proxy resets current_url to original: https://provider.com/playlist.m3u8
4. Load balancer can redirect to a healthy backend
5. New sticky lock established if redirect occurs
```

## Interaction with Failover

Sticky sessions work **together** with failover URLs:

1. **Sticky Session**: Locks to specific backend within a provider
2. **Failover**: Switches between different providers/sources

Example:

```json
{
  "url": "https://provider1.com/stream.m3u8",
  "failover_urls": [
    "https://provider2.com/stream.m3u8",
    "https://provider3.com/stream.m3u8"
  ],
  "use_sticky_session": true
}
```

**Behavior:**

1. Start with provider1.com → redirects to backend-a.provider1.com → **locked**
2. If backend-a.provider1.com fails → reverts to provider1.com
3. If provider1.com still fails → **failover to provider2.com**
4. provider2.com redirects to backend-x.provider2.com → **locked again**

## Interaction with Retry Logic

Retries happen **before** sticky session recovery:

1. Fetch from sticky URL fails
2. **Retry same URL** up to `STREAM_RETRY_ATTEMPTS` times
3. If all retries fail → **revert sticky session** to original URL
4. If original URL fails → **attempt failover**

This ensures maximum stability - we try to keep the working connection before resetting.

## Logging and Monitoring

### Successful Sticky Session Lock

```log
2026-02-11 10:30:45 - stream_manager - DEBUG - Sticky session: Locking stream ABC123 to origin: https://backend2.provider.com/live/stream.m3u8
```

### Sticky Session Recovery

```log
2026-02-11 10:35:12 - stream_manager - WARNING - Sticky origin https://backend2.provider.com/live/stream.m3u8 failed. Reverting to original configured entry point.
```

### Playlist Fetch with Sticky Session

```log
2026-02-11 10:30:46 - stream_manager - INFO - Fetching playlist for stream ABC123 from https://backend2.provider.com/live/stream.m3u8 (client: client_456)
```

## Troubleshooting

### Symptom: Playback loops every few seconds

**Diagnosis:**

```bash
# Watch proxy logs for sequence number patterns
docker logs m3u-proxy -f | grep "sequence"
```

If you see:

```
Playlist sequence: #1000
Playlist sequence: #500  ← Jumped backwards!
Playlist sequence: #1001
Playlist sequence: #502  ← Jumped backwards again!
```

**Solution:** Enable sticky sessions

```bash
USE_STICKY_SESSION=true
```

### Symptom: Stream won't recover after temporary outage

**Diagnosis:** Sticky session may be locked to a dead backend

**Solution:** 

1. Check if sticky session is enabled: `USE_STICKY_SESSION=true`
2. Check logs for "Sticky origin failed" messages
3. The proxy should auto-recover, but you can also:
   - Delete and recreate the stream
   - Or restart the proxy

### Symptom: Provider uses geo-based backends and I want location failover

**Diagnosis:** Sticky session prevents load balancer from redirecting to geographically closer backends

**Solution:** Disable sticky sessions

```bash
USE_STICKY_SESSION=false
```

Or use per-stream override:

```json
{
  "url": "https://geo-balanced-provider.com/stream.m3u8",
  "use_sticky_session": false
}
```

## Best Practices

1. **Start Disabled**: Begin with `USE_STICKY_SESSION=false` (default)
2. **Enable if Needed**: Only enable if you experience playback loops
3. **Monitor Logs**: Watch for redirect patterns and sequence number issues
4. **Test Per-Stream**: Use per-stream overrides to test with specific providers
5. **Combine with Failover**: Use both sticky sessions and failover URLs for maximum reliability

## Technical Details

### Implementation

The sticky session is implemented at the HLS playlist fetch level:

```python
# When fetching playlist
response = await self.http_client.get(stream_info.current_url or stream_info.original_url)
final_url = str(response.url)  # URL after redirects

# If redirect occurred and sticky enabled
if stream_info.use_sticky_session and final_url != original_url:
    stream_info.current_url = final_url  # Lock to final URL
    
# On error and sticky enabled
if stream_info.use_sticky_session and stream_info.current_url:
    if stream_info.current_url not in known_urls:
        stream_info.current_url = None  # Revert to original
```

### Stored State

The sticky session state is stored in the `StreamInfo` object:

- `original_url`: The URL provided when creating the stream (never changes)
- `current_url`: The "locked" URL after redirect (changes when sticky session locks/resets)
- `use_sticky_session`: Boolean flag indicating if sticky session is enabled

### Reset Conditions

Sticky session automatically resets (reverts to original URL) when:

1. The locked backend returns an error
2. The locked backend is not in the list of known URLs (original + failover URLs)
3. Failover is triggered manually
4. Stream is recreated

## Examples

### Example 1: Provider with Load Balancer

**Provider:** `streams.iptv.com` (has 3 backends)

**Config:**

```bash
USE_STICKY_SESSION=true
```

**Create Stream:**

```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://streams.iptv.com/live/channel1.m3u8"
  }'
```

**Result:**

- First request: `streams.iptv.com` → redirects to `backend2.iptv.com` → locked
- All subsequent requests go directly to `backend2.iptv.com`
- Stable playback with monotonic sequence numbers

### Example 2: Provider WITHOUT Load Balancer

**Provider:** `direct-stream.tv` (single server, no redirects)

**Config:**

```bash
USE_STICKY_SESSION=true  # Still works, just not needed
```

**Result:**

- First request: `direct-stream.tv/stream.m3u8` → no redirect → no lock
- All subsequent requests: `direct-stream.tv/stream.m3u8`
- No effect (but no harm either)

### Example 3: Multi-Provider with Failover

**Config:**

```bash
USE_STICKY_SESSION=true
```

**Create Stream:**

```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://provider1.com/stream.m3u8",
    "failover_urls": [
      "https://provider2.com/stream.m3u8",
      "https://provider3.com/stream.m3u8"
    ]
  }'
```

**Result:**

- Provider1 redirects to backend-a.provider1.com → locked
- If backend-a fails → reverts to provider1.com
- If provider1.com fails → failover to provider2.com
- Provider2 redirects to backend-x.provider2.com → locked

**Perfect combination of stability and redundancy!**
