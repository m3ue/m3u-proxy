# Retry Logic vs Failover: How They Work Together

## Overview

The proxy has **two separate mechanisms** for handling stream failures:

1. **Retry Logic** (`STREAM_RETRY_*` settings) - Retries the **same URL**
2. **Failover Logic** (`failover_urls`) - Switches to a **different URL**

These work **sequentially**, not in parallel. Understanding their interaction is crucial for optimal configuration.

## The Decision Flow

When a stream encounters an error, here's the exact sequence:

```
┌─────────────────────────────────────────────┐
│ Stream Error Occurs                         │
│ (timeout, network error, connection lost)   │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ Step 1: CHECK RETRY COUNT                   │
│ Has retry_count < STREAM_RETRY_ATTEMPTS?    │
│ AND total time < STREAM_TOTAL_TIMEOUT?      │
└─────────────────┬───────────────────────────┘
                  │
         ┌────────┴────────┐
         │ YES             │ NO
         ▼                 ▼
┌──────────────────┐  ┌──────────────────────────┐
│ RETRY SAME URL   │  │ Step 2: CHECK FAILOVER   │
│                  │  │ Has failover URLs?       │
│ • Increment retry│  │ AND failover_count < 3?  │
│ • Wait delay     │  └────────┬─────────────────┘
│ • Reconnect      │           │
│                  │  ┌────────┴────────┐
│ • On success:    │  │ YES             │ NO
│   Reset retry=0  │  ▼                 ▼
│                  │ ┌────────────┐  ┌──────────┐
│ • On failure:    │ │ FAILOVER   │  │ FAIL     │
│   Loop to Step 1 │ │            │  │ STREAM   │
└──────────────────┘ │ • Switch   │  │          │
                     │   to next  │  │ • Emit   │
                     │   URL      │  │   event  │
                     │ • Reset    │  │ • Stop   │
                     │   retry=0  │  │   client │
                     │ • Loop to  │  └──────────┘
                     │   Step 1   │
                     └────────────┘
```

## Detailed Example

Let's walk through a real scenario with these settings:

```bash
STREAM_RETRY_ATTEMPTS=3
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=60.0
LIVE_CHUNK_TIMEOUT_SECONDS=15.0
```

And a stream with failover:

```json
{
  "url": "https://provider1.com/stream.ts",
  "failover_urls": [
    "https://provider2.com/stream.ts",
    "https://provider3.com/stream.ts"
  ]
}
```

### Timeline

| Time | Event | retry_count | failover_count | Current URL |
|------|-------|-------------|----------------|-------------|
| 0s | Stream starts | 0 | 0 | provider1.com |
| 15s | ⚠️ Chunk timeout (no data) | 0 | 0 | provider1.com |
| 15s | 📝 Log: "Retrying connection (attempt 1/3, delay: 1.0s)" | 1 | 0 | provider1.com |
| 16s | 🔄 Retry connection to provider1.com | 1 | 0 | provider1.com |
| 31s | ⚠️ Chunk timeout again | 1 | 0 | provider1.com |
| 31s | 📝 Log: "Retrying connection (attempt 2/3, delay: 1.0s)" | 2 | 0 | provider1.com |
| 32s | 🔄 Retry connection to provider1.com | 2 | 0 | provider1.com |
| 47s | ⚠️ Chunk timeout again | 2 | 0 | provider1.com |
| 47s | 📝 Log: "Retrying connection (attempt 3/3, delay: 1.0s)" | 3 | 0 | provider1.com |
| 48s | 🔄 Retry connection to provider1.com | 3 | 0 | provider1.com |
| 63s | ⚠️ Chunk timeout again (retries exhausted) | 3 | 0 | provider1.com |
| 63s | 📝 Log: "Retries exhausted, attempting failover (attempt 1/3)" | 3 | 1 | provider1.com |
| 63s | 🔀 **FAILOVER** to provider2.com | 0 | 1 | provider2.com |
| 63s | 📝 Log: "Connection successful after 0 retries, resetting retry counter" | 0 | 1 | provider2.com |
| 78s | ⚠️ Chunk timeout on provider2.com | 0 | 1 | provider2.com |
| 78s | 📝 Log: "Retrying connection (attempt 1/3, delay: 1.0s)" | 1 | 1 | provider2.com |
| 79s | ✅ Retry successful, data flowing | 0 | 1 | provider2.com |

## Key Points

### 1. Retries Happen FIRST

- **Before** attempting failover, the proxy will retry the current URL
- This makes sense: temporary network hiccups shouldn't trigger failover
- Failover is more expensive (different provider, different connection)

### 2. Retry Counter Resets on Failover

```python
# After successful failover
retry_count = 0  # Fresh retries for new URL
failover_count += 1
```

Each provider gets the full retry count. This is intentional - we want to give each provider a fair chance.

### 3. Retry Counter Resets on Success

```python
# After successful connection
if retry_count > 0:
    logger.info(f"Connection successful after {retry_count} retries")
    retry_count = 0  # Ready for next issue
```

Once data flows, we reset retries. This prevents cascading failures.

### 4. Total Timeout Applies Across Everything

```python
elapsed_total = current_time - total_timeout_start
if STREAM_TOTAL_TIMEOUT > 0 and elapsed_total > STREAM_TOTAL_TIMEOUT:
    # Skip retries, go straight to failover or fail
```

The `STREAM_TOTAL_TIMEOUT` is a safety net to prevent infinite retry loops.

## Configuration Strategies

### Strategy 1: Aggressive Retries, Conservative Failover

**Use Case:** Primary provider is generally reliable but has occasional hiccups. Failover providers are backups only.

```bash
STREAM_RETRY_ATTEMPTS=5
STREAM_RETRY_DELAY=2.0
STREAM_TOTAL_TIMEOUT=120.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=true
LIVE_CHUNK_TIMEOUT_SECONDS=10.0
```

**Result:**

- Tolerates brief outages on primary (up to 5 retries × 10s timeout = 50s+ of attempts)
- Only fails over if primary is truly down
- Good for: Residential connections, CDNs with temporary issues

### Strategy 2: Quick Failover

**Use Case:** You have reliable failover providers and want to switch quickly.

```bash
STREAM_RETRY_ATTEMPTS=1
STREAM_RETRY_DELAY=0.5
STREAM_TOTAL_TIMEOUT=30.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
LIVE_CHUNK_TIMEOUT_SECONDS=5.0
```

**Result:**

- One retry attempt (5s timeout + 1 retry = ~10s max on one provider)
- Quick switch to backup provider
- Good for: Multiple equal-quality providers, load balancing

### Strategy 3: Balanced (Recommended)

**Use Case:** General purpose, works for most scenarios.

```bash
STREAM_RETRY_ATTEMPTS=3
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=60.0
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
LIVE_CHUNK_TIMEOUT_SECONDS=15.0
```

**Result:**

- Moderate retries (15s × 3 = ~45s max on one provider)
- Reasonable failover timing
- Good for: Default configuration

### Strategy 4: Maximum Persistence (iptv-proxy compatible)

**Use Case:** Replicating iptv-proxy behavior - keep trying indefinitely.

```bash
STREAM_RETRY_ATTEMPTS=10
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=0  # Disable total timeout
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
LIVE_CHUNK_TIMEOUT_SECONDS=5.0
```

**Result:**

- Up to 10 retries per provider (5s × 10 = 50s per provider)
- No total timeout limit
- Will keep trying providers in rotation
- Good for: Unstable connections, unreliable providers

## Does Retry Affect Failover? Yes, But Positively!

### The Short Answer

**Retries happen BEFORE failover**, which is actually a **good thing**:

✅ **Prevents unnecessary failovers** from temporary network hiccups  
✅ **Gives each provider a fair chance** before moving to next  
✅ **Reduces provider switching** and connection churn  
✅ **Maintains stream stability** - same provider = same quality/features

### Without Retries (Old Behavior)

```
Network hiccup → Immediate failover → Connection churn → More failures
```

### With Retries (New Behavior)

```
Network hiccup → Retry same URL → Success → Stable stream
              ↓
         (if retries fail)
              ↓
           Failover → Fresh retries for new provider
```

## Debugging: Understanding Logs

### Retry Attempt

```log
2026-02-11 10:30:45 - stream_manager - WARNING - No data received for 15.0s from upstream for stream ABC, client xyz
2026-02-11 10:30:45 - stream_manager - INFO - Retrying connection for stream ABC, client xyz (attempt 2/3, delay: 1.0s)
```

**What's happening:**

- Chunk timeout occurred
- This is retry attempt #2 out of 3
- Waiting 1 second before reconnecting
- Still using the **same URL**

### Retry Exhausted, Attempting Failover

```log
2026-02-11 10:30:50 - stream_manager - INFO - Retries exhausted, attempting failover due to chunk timeout for client xyz (failover attempt 1/3)
```

**What's happening:**

- All 3 retry attempts failed
- Now switching to **different URL** (failover)
- This is failover attempt #1 out of 3

### Successful Recovery After Retry

```log
2026-02-11 10:30:46 - stream_manager - INFO - Connection successful after 1 retries, resetting retry counter
```

**What's happening:**

- One retry was needed
- Connection is now working
- Retry counter reset to 0
- Ready to handle future issues

### Total Timeout Exceeded

```log
2026-02-11 10:31:45 - stream_manager - INFO - Event: EventType.STREAM_FAILED for stream ABC
    ...
    "reason": "total_timeout_exceeded",
    "retry_count": 2
```

**What's happening:**

- `STREAM_TOTAL_TIMEOUT` was exceeded
- Stopped trying after 2 retries (didn't reach max of 3)
- No failover attempted because total time budget was exhausted

## Best Practices

### 1. Start Conservative, Tune Based on Logs

```bash
# Start here
STREAM_RETRY_ATTEMPTS=3
STREAM_RETRY_DELAY=1.0
STREAM_TOTAL_TIMEOUT=60.0
```

Monitor logs for patterns, then adjust.

### 2. Match Retry Settings to Provider Behavior

```bash
# Flaky provider with many brief outages
STREAM_RETRY_ATTEMPTS=5
STREAM_RETRY_EXPONENTIAL_BACKOFF=true

# Stable provider, quick failover desired
STREAM_RETRY_ATTEMPTS=1
```

### 3. Don't Make Retry Delay Too Long

```bash
# ❌ Bad - users wait too long
STREAM_RETRY_DELAY=10.0

# ✅ Good - quick recovery
STREAM_RETRY_DELAY=1.0
```

### 4. Use Total Timeout as Safety Net

```bash
# Prevent infinite retry loops
STREAM_TOTAL_TIMEOUT=120.0  # 2 minutes max

# Or disable if you want indefinite retries (iptv-proxy style)
STREAM_TOTAL_TIMEOUT=0
```

### 5. Combine with Sticky Sessions for Load-Balanced Providers

When using sticky sessions, retries will stay on the same backend:

```bash
USE_STICKY_SESSION=true
STREAM_RETRY_ATTEMPTS=3
```

**Result:** If a specific backend has issues, retries happen on that backend before reverting to load balancer.

## Summary

| Feature | Purpose | When It Happens | Effect on Failover |
|---------|---------|----------------|-------------------|
| **Retry Logic** | Handle temporary issues | Before failover | Delays failover (good!) |
| **Failover** | Switch providers/sources | After retries exhausted | Provides redundancy |
| **Total Timeout** | Prevent infinite loops | Across all retries | May skip some retries |
| **Sticky Session** | Lock to specific backend | On redirect | Works with retries |

**The Bottom Line:** Retries make failover **smarter**, not **slower**. They ensure you only fail over when truly necessary, reducing connection churn and improving stream stability.
