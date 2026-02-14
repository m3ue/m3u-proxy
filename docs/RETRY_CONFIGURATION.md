# Stream Retry Configuration

## Overview

The m3u-proxy now includes configurable retry logic similar to [iptv-proxy](https://github.com/kvaster/iptv-proxy) to handle temporary connection issues and improve stream reliability. When a stream encounters connection problems, the proxy will now automatically retry the connection before attempting failover or giving up entirely.

**📚 Related Documentation:**
- [Retry vs Failover Logic](RETRY_VS_FAILOVER.md) - Detailed explanation of how retries and failover work together
- [Sticky Session Handler](STICKY_SESSION.md) - How sticky sessions interact with retries  

## How It Works

When a stream encounters an error (connection timeout, network error, read error, etc.), the retry mechanism follows this flow:

1. **Retry Current URL**: The proxy will retry the current stream URL up to `STREAM_RETRY_ATTEMPTS` times with a delay of `STREAM_RETRY_DELAY` seconds between attempts
2. **Respect Total Timeout**: If `STREAM_TOTAL_TIMEOUT` is exceeded, retries stop regardless of retry count
3. **Attempt Failover**: After exhausting retries, if failover URLs are available, the proxy will attempt failover
4. **Fail Stream**: Only after all retries and failovers are exhausted will the stream be marked as failed

## Configuration Parameters

Add these settings to your `.env` file or Docker environment:

### STREAM_RETRY_ATTEMPTS
- **Type**: Integer
- **Default**: `3`
- **Description**: Number of retry attempts for the same URL before trying failover
- **Example**: `STREAM_RETRY_ATTEMPTS=5`

### STREAM_RETRY_DELAY
- **Type**: Float (seconds)
- **Default**: `1.0`
- **Description**: Delay in seconds between retry attempts
- **Example**: `STREAM_RETRY_DELAY=2.0`

### STREAM_TOTAL_TIMEOUT
- **Type**: Float (seconds)
- **Default**: `60.0`
- **Description**: Total timeout across all retry attempts before giving up. Set to `0` to disable total timeout constraint.
- **Example**: `STREAM_TOTAL_TIMEOUT=120.0`

### STREAM_RETRY_EXPONENTIAL_BACKOFF
- **Type**: Boolean
- **Default**: `false`
- **Description**: Whether to use exponential backoff for retry delays (multiplies delay by 1.5 each retry)
- **Example**: `STREAM_RETRY_EXPONENTIAL_BACKOFF=true`

## Comparison with iptv-proxy

The configuration is similar to iptv-proxy's settings:

| iptv-proxy | m3u-proxy | Description |
|------------|-----------|-------------|
| `channels_timeout_sec` | `LIVE_CHUNK_TIMEOUT_SECONDS` | Timeout for each chunk read |
| `channels_total_timeout_sec` | `STREAM_TOTAL_TIMEOUT` | Total timeout across all retries |
| `channels_retry_delay_ms` | `STREAM_RETRY_DELAY` | Delay between retries |
| N/A | `STREAM_RETRY_ATTEMPTS` | Number of retry attempts |
| N/A | `STREAM_RETRY_EXPONENTIAL_BACKOFF` | Use exponential backoff |

## Example Configurations

### Conservative (Quick Failover)
```env
STREAM_RETRY_ATTEMPTS=2
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=30.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
```

### Moderate (Recommended)
```env
STREAM_RETRY_ATTEMPTS=3
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=60.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
```

### Aggressive (Maximum Persistence)
```env
STREAM_RETRY_ATTEMPTS=5
STREAM_RETRY_DELAY=2.0
STREAM_TOTAL_TIMEOUT=120.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=true
```

### iptv-proxy Compatible
```env
STREAM_RETRY_ATTEMPTS=12
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=60.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
LIVE_CHUNK_TIMEOUT_SECONDS=5.0
```

## Logging

The proxy will log retry attempts with detailed information:

```
2026-02-10 09:53:44 - stream_manager - WARNING - No data received for 15.0s from upstream for stream ABCD, client client_1234
2026-02-10 09:53:44 - stream_manager - INFO - Retrying connection for stream ABCD, client client_1234 (attempt 1/3, delay: 1.0s)
2026-02-10 09:53:45 - stream_manager - INFO - Connection successful after 1 retries, resetting retry counter
```

If retries are exhausted:

```
2026-02-10 09:53:50 - stream_manager - INFO - Retries exhausted, attempting failover due to chunk timeout for client client_1234 (failover attempt 1/3)
```

## When Retries Are Applied

Retries are automatically applied for:

- **Chunk Timeouts**: When no data is received from upstream within `LIVE_CHUNK_TIMEOUT_SECONDS`
- **Connection Errors**: Network errors, timeout exceptions, HTTP errors
- **Read Errors**: When connection is lost before data is received
- **Unknown Errors**: Unexpected exceptions during streaming

Retries are NOT applied for:

- **VOD Mid-Stream Failures**: After bytes have started flowing, VOD uses a reconnection strategy with Range headers instead of same-URL retry loops
- **Client Disconnections**: Connection errors from the client side
- **Streams with Active Data**: Retries only apply when connection fails before data is received

## Best Practices

1. **Start Conservative**: Begin with the recommended settings and adjust based on your provider's reliability
2. **Monitor Logs**: Watch for retry patterns to understand your provider's behavior
3. **Consider Network Latency**: If your provider is geographically distant, increase `STREAM_RETRY_DELAY`
4. **Balance Failover**: If you have reliable failover URLs, keep retry attempts lower
5. **Use Exponential Backoff**: Enable for providers with rate limiting or burst issues

## Troubleshooting

### Streams still dropping frequently
- Increase `STREAM_RETRY_ATTEMPTS` to 5-10
- Increase `STREAM_TOTAL_TIMEOUT` to 120-180 seconds
- Enable `STREAM_RETRY_EXPONENTIAL_BACKOFF`
- Verify `LIVE_CHUNK_TIMEOUT_SECONDS` isn't too aggressive (try 20-30 seconds)

### Streams taking too long to fail
- Decrease `STREAM_RETRY_ATTEMPTS` to 1-2
- Decrease `STREAM_TOTAL_TIMEOUT` to 20-30 seconds
- Decrease `STREAM_RETRY_DELAY` to 0.5 seconds

### Streams work but buffer frequently
- Decrease `LIVE_CHUNK_TIMEOUT_SECONDS` to detect stalls faster
- Increase `STREAM_RETRY_DELAY` to give upstream time to recover
- Enable `STREAM_RETRY_EXPONENTIAL_BACKOFF`
