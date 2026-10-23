# Redis Pooling Integration Guide

## Overview

This guide shows how to add Redis support to enable connection pooling and multi-worker coordination.

## Opt-in single-process transcoding without Redis

`ENABLE_SINGLE_PROCESS_TRANSCODING` defaults to `false`. To expose the existing
local FFmpeg manager through `/transcode` without Redis, set:

```sh
REDIS_ENABLED=false
ENABLE_TRANSCODING_POOLING=true
ENABLE_SINGLE_PROCESS_TRANSCODING=true
```

Run exactly one server process, with `WORKER_ID` unset. The application rejects
`WORKER_ID`, spawned multiprocessing workers (including Uvicorn `--workers`), and
`WEB_CONCURRENCY` / `UVICORN_WORKERS` values other than an unset value or `1`.
Arbitrary external prefork launchers or multiple replicas are not supported;
operators must explicitly configure one process and one replica. No Redis client
or distributed sharing is enabled, and Redis health/info fields remain disabled.
Existing configuration defaults and Redis mode remain unchanged.

This switch only enables access to the existing transcoder. It does not change
FFmpeg profiles or guarantee automatic source recovery, codec switching, or
client playback continuity.

## Current Architecture

### ✅ What We Have

```python
# 1. Hybrid Architecture - Direct + Transcoded
POST /streams          # Direct proxy
POST /transcode        # Transcoded
GET  /stream/{id}      # Direct MPEGTS streaming

# 2. FFmpeg Integration
- Subprocess execution with stdout piping
- Direct MPEGTS output (no HLS segments)
- Profile-based configuration system
- Real-time stderr logging and stats

# 3. Individual Process Model (Our Enhancement)
- One FFmpeg process per client connection
- Better isolation and debugging
- Simpler resource management
```

### 🔄 What Redis Pooling Adds

```python
# 1. Shared Process Model
- One FFmpeg process shared across multiple clients
- Redis coordination between workers
- Automatic process lifecycle management
- Connection tracking and cleanup

# 2. Multi-Worker Support
- Multiple API servers can share transcoding processes
- Worker health monitoring and failover
- Distributed client management
- Stream sharing across workers
```

## Implementation Steps

### 1. Install Redis Dependencies

```bash
# Install Redis server
brew install redis  # macOS
# or
apt install redis-server  # Ubuntu

# Install Python Redis client
pip install -r requirements-redis.txt
```

### 2. Start Redis Server

```bash
# Start Redis
redis-server

# Test connection
redis-cli ping  # Should return PONG
```

### 3. Update Stream Manager Initialization

```python
# In main.py or wherever StreamManager is created
from src.stream_manager import StreamManager

# Enable Redis pooling
stream_manager = StreamManager(
    redis_url="redis://localhost:6379/0",
    enable_pooling=True
)

# Or disable pooling (current behavior)
stream_manager = StreamManager(enable_pooling=False)
```

### 4. Environment Configuration

```bash
# Set environment variables (using existing .env format)
export REDIS_ENABLED=true
export REDIS_HOST=localhost
export REDIS_SERVER_PORT=6379
export REDIS_DB=0
export ENABLE_TRANSCODING_POOLING=true
export MAX_CLIENTS_PER_SHARED_STREAM=10
export WORKER_ID=worker-1  # Unique per instance

# Or update .env file directly:
# REDIS_ENABLED=true
# REDIS_HOST=localhost  
# REDIS_SERVER_PORT=6379
# REDIS_DB=0
```

### 5. Test Multi-Worker Setup

```bash
# Terminal 1 - Worker 1
export WORKER_ID=worker-1
export PORT=8085
python main.py

# Terminal 2 - Worker 2  
export WORKER_ID=worker-2
export PORT=8086
python main.py

# Both workers will share Redis state and transcoding processes
```

## Architecture Comparison

### Individual Processes (Current - No Redis)

```
Client 1 ──► FFmpeg Process 1 ──► MPEGTS Stream 1
Client 2 ──► FFmpeg Process 2 ──► MPEGTS Stream 2  
Client 3 ──► FFmpeg Process 3 ──► MPEGTS Stream 3

Pros: Perfect isolation, easy debugging
Cons: Higher resource usage
```

### Shared Processes

```
Client 1 ──┐
Client 2 ──┼──► Shared FFmpeg Process ──► MPEGTS Stream
Client 3 ──┘

Pros: Lower resource usage, multi-worker support
Cons: Shared failure points, more complex
```

## Code Examples

### Creating Pooled Transcoded Stream

```python
import requests

# Same API - pooling happens automatically based on URL+profile
response = requests.post('http://localhost:8085/transcode', 
    headers={'X-API-Token': 'your-token'},
    json={
        'url': 'https://example.com/video.mp4',
        'profile': 'low_quality',
        'profile_variables': {
            'video_bitrate': '500k',
            'audio_bitrate': '96k'  
        }
    }
)

stream_data = response.json()
# Multiple clients to same URL+profile will share FFmpeg process
```

### Monitoring Shared Processes

```python
# Get stream statistics (if pooling enabled)
if stream_manager.pooled_manager:
    stats = await stream_manager.pooled_manager.get_stream_stats()
    print(f"Active shared streams: {stats['local_streams']}")
    print(f"Total clients: {stats['total_clients']}")
    
    for stream in stats['streams']:
        print(f"Stream {stream['stream_id']}: {stream['client_count']} clients")
```

## Benefits of Redis Pooling

### 1. Resource Efficiency
- **Before**: 100 clients = 100 FFmpeg processes  
- **After**: 100 clients = ~10 shared FFmpeg processes (same URL+profile)

### 2. Scalability  
- Multiple API workers can share transcoding processes
- Automatic load balancing across workers
- Centralized process coordination

### 3. Features
- Redis-based state management
- Multi-worker coordination
- Worker health monitoring

### 4. Operational Benefits
- Lower memory and CPU usage
- Centralized monitoring via Redis
- Automatic cleanup of stale processes
- Worker failover support

## Configuration Options

```python
# Redis Configuration (using existing .env variables)
REDIS_HOST = "localhost"         # Redis server host
REDIS_SERVER_PORT = 6379         # Redis server port  
REDIS_DB = 0                     # Redis database number
REDIS_ENABLED = True             # Enable Redis pooling

# Alternative: Full Redis URL (overrides individual components)
# REDIS_URL = "redis://localhost:6379/0"

# Pooling Behavior  
ENABLE_TRANSCODING_POOLING = True
MAX_CLIENTS_PER_SHARED_STREAM = 10
SHARED_STREAM_TIMEOUT = 300  # 5 minutes

# Stream Sharing Strategy
STREAM_SHARING_STRATEGY = "url_profile"  # Share based on URL + profile
# STREAM_SHARING_STRATEGY = "url_only"   # Share based on URL only  
# STREAM_SHARING_STRATEGY = "disabled"   # No sharing (individual processes)

# Worker Settings
WORKER_ID = "worker-1"           # Unique worker identifier
HEARTBEAT_INTERVAL = 30          # Worker heartbeat frequency
CLEANUP_INTERVAL = 60            # Stale process cleanup frequency
```

## Monitoring and Debugging

### Redis Keys Used

```bash
# Worker tracking
workers                    # Hash: worker_id -> worker_data

# Stream metadata  
stream:{stream_id}         # Hash: stream metadata and status
client:{stream_id}:{id}    # Hash: client connection info

# Check active workers
redis-cli HGETALL workers

# Check active streams
redis-cli KEYS "stream:*"

# Monitor stream for specific URL+profile
redis-cli HGETALL "stream:{hash_of_url_profile}"
```

### Process Monitoring

```bash
# Check FFmpeg processes  
ps aux | grep ffmpeg

# With Redis pooling: Fewer processes, more clients per process
# Without Redis: One process per client

# Monitor Redis memory usage
redis-cli INFO memory
```

## Migration Strategy

### Phase 1: Install Redis Support (No Behavior Change)
```python
# Keep existing behavior, just add Redis libraries
stream_manager = StreamManager(enable_pooling=False)
```

### Phase 2: Enable Single-Worker Pooling  
```python
# Enable pooling without Redis (local sharing only)
stream_manager = StreamManager(enable_pooling=True)  # No redis_url
```

### Phase 3: Full Redis Multi-Worker
```python  
# Enable full Redis coordination
stream_manager = StreamManager(
    redis_url="redis://localhost:6379/0",
    enable_pooling=True
)
```

## Conclusion

Redis pooling transforms m3u-proxy system from individual-process-per-client to a shared-process architecture while maintaining:

- ✅ Same API compatibility  
- ✅ Same MPEGTS direct streaming
- ✅ Same profile system
- ✅ Backward compatibility (can disable pooling)

The main benefit is **resource efficiency** - instead of 100 FFmpeg processes for 100 clients watching the same transcoded stream, you get 1 shared FFmpeg process serving all clients.