# m3u proxy

![logo](./static/favicon.png)

A high-performance HTTP proxy server for IPTV content with **true live proxying**, per-client connection management, and seamless failover support. Built with FastAPI and optimized for efficiency.

### Questions/issues/suggestions

Feel free to [open an issue](https://github.com/m3ue/m3u-proxy/issues/new?template=bug_report.md) on this repo, or hit us up on [Discord](https://discord.gg/rS3abJ5dz7)

### Join us on Discord

Join our [Discord](https://discord.gg/rS3abJ5dz7) server to ask questions and get help, help others, suggest new ideas, and offer suggestions for improvements! You can also try out and help test new features! 🎉

## Features

### Core Streaming
- 🚀 **Pure HTTP Proxy**: Zero transcoding, direct byte-for-byte streaming
- 🔗 **Connection Sharing**: Multiple viewers share one upstream provider connection (HLS, Transcoded, and live Direct/TS streams)
- ⚡️ **Truly Ephemeral**: Provider connections open only when clients are consuming; close when the last client disconnects
- 📺 **HLS Support**: Optimized playlist and segment handling (.m3u8)
- 📡 **Continuous Streams**: Primary/subscriber broadcast for .ts, .mp4, .mkv, .webm, .avi live streams
- 🔄 **Real-time URL Rewriting**: Automatic playlist modification for proxied content
- 📱 **Full VOD Support**: Per-client connections with byte-range requests and seeking
- 🎬 **Strict Live TS Mode**: Enhanced stability for live MPEG-TS with pre-buffering & circuit breaker

### Performance & Reliability
- ⚡ **uvloop Integration**: 2-4x faster async I/O operations
- 🔄 **Seamless Failover**: <100ms transparent URL switching per client
- 🎯 **Immediate Cleanup**: Connections close instantly when client stops

### Transcoding Available with Hardware Acceleration
- 🎬 **FFmpeg Integration**: Built-in hardware-accelerated video processing
- 🚀 **GPU Acceleration**: Automatic detection of NVIDIA, Intel, and AMD GPUs
- ⚡ **VAAPI Support**: Intel/AMD hardware encoding (3-8x faster than CPU)
- 🎯 **NVENC Support**: NVIDIA hardware encoding (10-20x faster than CPU)
- 🔧 **Auto-Configuration**: Zero-config hardware acceleration setup
- 📊 **Multiple Codecs**: H.264, H.265/HEVC, VP8, VP9, AV1 support

### Management & Monitoring
- 👥 **Client Tracking**: Individual client sessions and bandwidth monitoring
- 📊 **Real-time Statistics**: Live metrics on streams, clients, and data usage
- 🔎 **Stream Type Detection**: Automatic HLS/VOD/Live detection
- 🧹 **Automatic Cleanup**: Inactive streams and clients auto-removed
- 📣 **Event System**: Real-time events and webhook notifications
- 🩺 **Health Checks**: Built-in health endpoints for monitoring
- 🏷️ **Custom Metadata**: Attach arbitrary key/value pairs to streams for identification

## Quick Start

#### 🐳 Docker compose example

Use the below example to run using the precompiled Dockerhub image.
You can also replace `latest` with `dev` or `experimental` to try another branch.

```yaml
services:
  m3u-proxy:
    image: sparkison/m3u-proxy:latest
    container_name: m3u-proxy
    ports:
      - "8085:8085"
    # Hardware acceleration (optional)
    devices:
      - /dev/dri:/dev/dri  # Intel/AMD GPU support
    # For NVIDIA GPUs, use this instead:
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: all
    #           capabilities: [gpu]
    environment:
      # Server Configuration
      - M3U_PROXY_HOST=0.0.0.0
      - M3U_PROXY_PORT=8085
      - LOG_LEVEL=INFO
      
      # Base path (default: /m3u-proxy for m3u-editor integration)
      # Set to empty string if not using reverse proxy: ROOT_PATH=
      - ROOT_PATH=/m3u-proxy
      
      # Hardware acceleration (optional)
      - LIBVA_DRIVER_NAME=i965  # For older Intel GPUs
      # - LIBVA_DRIVER_NAME=iHD  # For newer Intel GPUs
      
      # Timeouts (optional)
      - CLIENT_TIMEOUT=300
      - CLEANUP_INTERVAL=60
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8085/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

## Building from source

### 1. Clone Repo & Install Dependencies

#### Prerequisits

- `python` installed on your system: `>=3.10`
- `pip` installed on your system: `>=23`

```bash
git clone https://github.com/sparkison/m3u-proxy.git && cd m3u-proxy
pip install -r requirements.txt
```

### 2. Start the Server

```bash
python main.py --debug
```

Server will start on `http://localhost:8085`

### 3. Create a Stream

```bash
# HLS stream with custom user agent
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8", "user_agent": "MyApp/1.0"}'

# Direct IPTV stream with failover
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://server.com/stream.ts",
    "failover_urls": ["http://backup.com/stream.ts"],
    "user_agent": "VLC/3.0.18"
  }'

# Using the CLI client
python m3u_client.py create "https://your-stream.m3u8" --user-agent "MyApp/1.0"
python m3u_client.py create "http://server.com/movie.mkv" --failover "http://backup.com/movie.mkv"
```

## API Documentation

### Stream Management

#### Create Stream
```bash
POST /streams
Content-Type: application/json

{
  "url": "stream_url",
  "failover_urls": ["backup_url1", "backup_url2"],
  "user_agent": "Custom User Agent String"  
}
```

#### List Streams
```bash
GET /streams
```

#### Stream Information
```bash
GET /streams/{stream_id}
```

#### Delete Stream
```bash
DELETE /streams/{stream_id}
```

#### Trigger Failover
```bash
POST /streams/{stream_id}/failover
```

### Statistics & Monitoring

#### Comprehensive Stats
```bash
GET /stats
```

#### Health Check
```bash
GET /health
```

#### Client Information
```bash
GET /clients
GET /clients/{client_id}
```

## CLI Client Usage

The included CLI client (`m3u_client.py`) provides easy access to all proxy features:

```bash
# Create a stream with failover
python m3u_client.py create "https://primary.m3u8" --failover "https://backup1.m3u8" "https://backup2.m3u8"

# List all active streams
python m3u_client.py list

# View comprehensive statistics
python m3u_client.py stats

# Monitor in real-time (updates every 5 seconds)
python m3u_client.py monitor

# Check health status
python m3u_client.py health

# Get detailed stream information
python m3u_client.py info <stream_id>

# Trigger manual failover
python m3u_client.py failover <stream_id>

# Delete a stream
python m3u_client.py delete <stream_id>
```
## Configuration

### Environment Variables

```bash
# Server configuration
M3U_PROXY_HOST=0.0.0.0
M3U_PROXY_PORT=8085

# Base path for API routes (useful for reverse proxy integration)
# Default: /m3u-proxy (optimized for m3u-editor integration)
# Set to empty string for root path
ROOT_PATH=/m3u-proxy

# API Authentication (optional)
# Set API_TOKEN to require authentication for management endpoints
# Leave unset or empty to disable authentication
API_TOKEN=your_secret_token_here

# Client timeout (seconds)
CLIENT_TIMEOUT=300

# Cleanup interval (seconds)
CLEANUP_INTERVAL=60

# Stream Retry Configuration (improves reliability for unstable connections)
# Number of retry attempts before failover or giving up
STREAM_RETRY_ATTEMPTS=3
# Delay between retries (seconds)
STREAM_RETRY_DELAY=1.0
# Total timeout across all retries (seconds, 0 to disable)
STREAM_TOTAL_TIMEOUT=60.0
# Use exponential backoff for retry delays (false/true)
STREAM_RETRY_EXPONENTIAL_BACKOFF=false
# Timeout for receiving data chunks (seconds)
LIVE_CHUNK_TIMEOUT_SECONDS=15.0
# When a shared stream's reading client disconnects, how long (seconds) the
# client taking over waits to reuse its upstream connection before opening a new one
PRIMARY_HANDOFF_WAIT_SECONDS=2.0

# Sticky Session Handler (prevents playback loops with load-balanced providers)
# Locks to specific backend after redirect to maintain playlist consistency
USE_STICKY_SESSION=false
```

### API Authentication

When `API_TOKEN` is set in the environment, all management endpoints require authentication via the `X-API-Token` header. This includes:

- `/` - Root endpoint
- `/streams` - Create, list, get, delete streams
- `/stats/*` - All statistics endpoints
- `/clients` - Client management
- `/health` - Health check endpoint
- `/webhooks` - Webhook management
- `/streams/{stream_id}/failover` - Failover control
- `/hls/{stream_id}/clients/{client_id}` - Client disconnect

**Stream endpoints (the actual streaming URLs) do NOT require authentication** since they are accessed by media players that identify streams via `stream_id`.

Example with authentication:

```bash
# Set your API token
export API_TOKEN="my_secret_token"

# Method 1: Using header (recommended for API calls)
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: my_secret_token" \
  -d '{"url": "https://your-stream.m3u8"}'

# Method 2: Using query parameter (useful for browser access)
curl -X POST "http://localhost:8085/streams?api_token=my_secret_token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8"}'

# Browser access example
# Visit: http://localhost:8085/stats?api_token=my_secret_token

# Without token - will get 401 error
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8"}'
```

To disable authentication, simply leave `API_TOKEN` unset or set it to an empty string.

## Hardware Acceleration

m3u-proxy includes comprehensive hardware acceleration support for video transcoding operations using FFmpeg with GPU acceleration.

### Supported Hardware

- **🔥 NVIDIA GPUs**: CUDA, NVENC, NVDEC (10-20x faster than CPU)
- **⚡ Intel GPUs**: VAAPI, QuickSync (QSV) (3-8x faster than CPU)  
- **🚀 AMD GPUs**: VAAPI acceleration (3-5x faster than CPU)
- **💻 CPU Fallback**: Software encoding when no GPU available

### Automatic Detection

The container automatically detects available hardware on startup:

```
🔍 Running hardware acceleration check...
✅ Device /dev/dri/renderD128 is accessible.
🔰 Intel GPU: Intel GPU (Device ID: 0x041e)
✅ FFmpeg VAAPI acceleration: AVAILABLE
💡 For older Intel GPUs, try: LIBVA_DRIVER_NAME=i965
```

### Docker Setup Examples

#### Intel/AMD GPU Support
```yaml
services:
  m3u-proxy:
    image: sparkison/m3u-proxy:latest
    devices:
      - /dev/dri:/dev/dri
    environment:
      - LIBVA_DRIVER_NAME=i965  # For older Intel GPUs
      # - LIBVA_DRIVER_NAME=iHD  # For newer Intel GPUs
```

#### NVIDIA GPU Support
```yaml
services:
  m3u-proxy:
    image: sparkison/m3u-proxy:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

#### Docker Run Commands
```bash
# Intel/AMD GPU
docker run -d --name m3u-proxy \
  --device /dev/dri:/dev/dri \
  -e LIBVA_DRIVER_NAME=i965 \
  -p 8085:8085 \
  sparkison/m3u-proxy:latest

# NVIDIA GPU  
docker run -d --name m3u-proxy \
  --gpus all \
  -p 8085:8085 \
  sparkison/m3u-proxy:latest
```

### Programming Interface

The hardware acceleration is available through Python APIs:

```python
from hwaccel import get_ffmpeg_hwaccel_args, is_hwaccel_available

# Check if hardware acceleration is available
if is_hwaccel_available():
    # Get optimized FFmpeg arguments
    hwaccel_args = get_ffmpeg_hwaccel_args("h264")
    
    # Example: Hardware-accelerated transcoding
    cmd = ["ffmpeg"] + hwaccel_args + [
        "-i", "input_stream.m3u8",
        "-c:v", "h264_vaapi",  # Hardware encoder
        "-preset", "fast",
        "-b:v", "2M",
        "output_stream.mp4"
    ]
```

### Performance Benefits

| Hardware | Encoding Speed | Concurrent Streams | CPU Usage Reduction |
|----------|---------------|-------------------|-------------------|
| NVIDIA GPU | 10-20x faster | 4-8 streams | 95%+ |
| Intel GPU | 3-8x faster | 2-4 streams | 90%+ |
| AMD GPU | 3-5x faster | 2-3 streams | 85%+ |
| CPU Only | Baseline | 1 stream | N/A |

### Supported Codecs

- **H.264/AVC**: High compatibility, universal support
- **H.265/HEVC**: Better compression, 4K/8K content
- **VP8/VP9**: WebM containers, streaming optimized
- **AV1**: Next-gen codec, best compression
- **MJPEG**: Low latency, surveillance applications

For detailed hardware acceleration setup and troubleshooting, see [Hardware Acceleration Guide](docs/HARDWARE_ACCELERATION.md).

### Server Startup Options

```python
# Main server with all features
python main.py

# With custom options
python main.py --port 8002 --debug --reload
```

## Troubleshooting

### Common Issues

1. **Stream Won't Load**
   - Check original URL accessibility
   - Verify CORS headers if accessing from browser
   - Check server logs for detailed errors

2. **High Memory Usage**
   - Reduce `CLIENT_TIMEOUT` for faster cleanup
   - Monitor client connections and cleanup inactive ones
   - Consider horizontal scaling for high loads

3. **Failover Not Working**
   - Verify failover URLs are accessible
   - Check failover trigger conditions in logs
   - Test manual failover via API

### Debug Mode

```bash
# Enable detailed logging
export LOG_LEVEL=DEBUG
python main.py --debug
```

## Integration Examples

### HTML5 Video Player
```html
<video controls>
  <source src="http://localhost:8085/hls/{stream_id}/playlist.m3u8" type="application/x-mpegURL">
</video>
```

### FFmpeg
```bash
ffplay "http://localhost:8085/hls/{stream_id}/playlist.m3u8"
```

### VLC
```bash
vlc "http://localhost:8085/hls/{stream_id}/playlist.m3u8"
```

## 📡 Event System & Webhooks

The proxy includes a comprehensive event system for monitoring and integration:

### Webhook Configuration
```bash
# Add webhook to receive events
curl -X POST "http://localhost:8085/webhooks" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-server.com/webhook",
    "events": ["stream_started", "client_connected", "failover_triggered"],
    "timeout": 10,
    "retry_attempts": 3
  }'
```

### Available Events
- `stream_started` - New stream created
- `stream_stopped` - Stream ended
- `client_connected` - Client joined stream  
- `client_disconnected` - Client left stream
- `failover_triggered` - Switched to backup URL
- `connection_idle_warning` - Connected client idle past `CONNECTION_IDLE_ALERT_THRESHOLD` (default 600s)
- `connection_idle_error` - Connected client idle past `CONNECTION_IDLE_ERROR_THRESHOLD` (default 1800s); likely connection leak

### Webhook Payload Example
```json
{
  "event_id": "uuid",
  "event_type": "stream_started", 
  "stream_id": "abc123",
  "timestamp": "2025-09-25T22:38:34.392830",
  "data": {
    "primary_url": "http://example.com/stream.m3u8",
    "user_agent": "MyApp/1.0"
  }
}
```

### Demo Events
```bash
# Try the event system demo
python demo_events.py
```

📖 **Full Documentation**: See [EVENT_SYSTEM.md](docs/EVENT_SYSTEM.md) for complete webhook integration guide.

## Additional Documentation

- **[Documentation Index](docs/README.md)** - Complete, always-current list of all documentation

## Development

### Project Structure
```
├── docker/                  # Container and deployment assets
├── docs/                    # Full documentation set
│   ├── README.md            # Canonical docs index
│   └── *.md                 # Architecture, failover, retry, auth, etc.
├── logs/                    # Runtime logs (local/dev)
├── src/
│   ├── __init__.py          # Package marker
│   ├── api.py               # FastAPI server application
│   ├── broadcast_manager.py # Client broadcast coordination
│   ├── config.py            # Configuration management
│   ├── events.py            # Event system with webhooks
│   ├── hwaccel.py           # Hardware acceleration detection/helpers
│   ├── models.py            # Data models and schemas
│   ├── pooled_stream_manager.py # Shared/pooling stream orchestration
│   ├── redis_config.py      # Redis settings
│   ├── redis_manager.py     # Redis coordination layer
│   ├── stream_manager.py    # Live proxy core (broadcast model for direct/TS streams)
│   └── transcoding.py       # FFmpeg transcoding pipeline
├── static/                  # Static assets (icons, images)
├── tests/                   # Test suite
│   ├── integration/         # Integration tests
│   └── test_*.py            # Unit tests
├── tools/                   # Utility scripts and tools
│   ├── performance_test.py  # Performance testing
│   ├── m3u_client.py        # CLI client
│   ├── demo_events.py       # Event system demo
│   └── run_tests.py         # Enhanced test runner
├── docker-compose.yml       # Default compose stack
├── Dockerfile               # Container build definition
├── main.py                  # Server entry point (uvloop support)
├── requirements.txt         # Python dependencies
├── pytest.ini               # Test configuration
└── README.md                # This file
```
---

## 🤝 Want to Contribute?

> Whether it’s writing docs, squashing bugs, or building new features, your contribution matters! ❤️

We welcome **PRs, issues, ideas, and suggestions**!\
Here’s how you can join the party:

- Follow our coding style and best practices.
- Be respectful, helpful, and open-minded.
- Respect the **CC BY-NC-SA license**.

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

---

## ⚖️ License  

> m3u editor is licensed under **CC BY-NC-SA 4.0**:  

- **BY**: Give credit where credit’s due.  
- **NC**: No commercial use.  
- **SA**: Share alike if you remix.  

For full license details, see [LICENSE](https://creativecommons.org/licenses/by-nc-sa/4.0/).

