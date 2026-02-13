# Multi-Instance Deployment with Load Balancer + Shared Redis

This guide provides a complete example for running multiple `m3u-proxy` instances behind an NGINX load balancer with a shared Redis instance and pooling enabled.

## Architecture

```text
Clients
  |
  v
NGINX Load Balancer (:8085)
  |
  +--> m3u-proxy-1 (:8085) --+
  +--> m3u-proxy-2 (:8085) --+--> Redis (:6379)
  +--> m3u-proxy-3 (:8085) --+
```

## Why sticky routing matters

`m3u-proxy` keeps active stream state in memory on the instance that created the stream. To avoid `404 stream not found` during playback, requests from the same client should consistently reach the same backend instance.

This example uses NGINX `ip_hash` for session affinity.

## Folder layout

Create these files in your deployment folder:

```text
.
├── docker-compose.lb.yml
├── .env.lb
└── nginx
    └── nginx.conf
```

## 1) Environment file (shared Redis + pooling)

Create `.env.lb`:

```bash
# m3u-proxy service settings
M3U_PROXY_HOST=0.0.0.0
M3U_PROXY_PORT=8085
API_TOKEN=changeme
LOG_LEVEL=INFO

# Shared Redis settings
REDIS_ENABLED=true
REDIS_HOST=redis
REDIS_SERVER_PORT=6379
REDIS_DB=0

# Enable transcoding process pooling across instances
ENABLE_TRANSCODING_POOLING=true
MAX_CLIENTS_PER_SHARED_STREAM=10
STREAM_SHARING_STRATEGY=url_profile
```

`API_TOKEN` only needs to be defined once in `.env.lb` when services use `env_file`.

## 2) Docker Compose (Redis + 3 instances + NGINX)

Create `docker-compose.lb.yml`:

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: m3u-proxy-redis
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    expose:
      - "6379"
    restart: unless-stopped

  lb:
    image: nginx:1.27-alpine
    container_name: m3u-proxy-lb
    ports:
      - "8085:8085"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - redis
      - m3u-proxy-1
      - m3u-proxy-2
      - m3u-proxy-3
    restart: unless-stopped

  m3u-proxy-1:
    image: sparkison/m3u-proxy:latest
    container_name: m3u-proxy-1
    expose:
      - "8085"
    env_file:
      - .env.lb
    depends_on:
      - redis
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8085/health?api_token=${API_TOKEN}"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  m3u-proxy-2:
    image: sparkison/m3u-proxy:latest
    container_name: m3u-proxy-2
    expose:
      - "8085"
    env_file:
      - .env.lb
    depends_on:
      - redis
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8085/health?api_token=${API_TOKEN}"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  m3u-proxy-3:
    image: sparkison/m3u-proxy:latest
    container_name: m3u-proxy-3
    expose:
      - "8085"
    env_file:
      - .env.lb
    depends_on:
      - redis
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8085/health?api_token=${API_TOKEN}"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

## 3) NGINX load balancer config

Create `nginx/nginx.conf`:

```nginx
events {}

http {
  upstream m3u_proxy_upstream {
    ip_hash;
    server m3u-proxy-1:8085 max_fails=3 fail_timeout=10s;
    server m3u-proxy-2:8085 max_fails=3 fail_timeout=10s;
    server m3u-proxy-3:8085 max_fails=3 fail_timeout=10s;
  }

  server {
    listen 8085;

    location / {
      proxy_pass http://m3u_proxy_upstream;
      proxy_http_version 1.1;

      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;

      proxy_connect_timeout 10s;
      proxy_send_timeout 300s;
      proxy_read_timeout 300s;
      send_timeout 300s;

      proxy_buffering off;
    }
  }
}
```

## 4) Start the stack

```bash
docker compose --env-file .env.lb -f docker-compose.lb.yml up -d
docker compose -f docker-compose.lb.yml ps

# Optional: load API_TOKEN into your current shell for curl examples below
export API_TOKEN=$(grep '^API_TOKEN=' .env.lb | cut -d= -f2-)
```

## 5) Verify health through the load balancer

```bash
curl -s -H "X-API-Token: ${API_TOKEN}" "http://localhost:8085/health" | jq
```

## 6) Create and consume a stream

Create stream via the load balancer:

```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: ${API_TOKEN}" \
  -d '{
    "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
  }'
```

Example response:

```json
{
  "stream_id": "abc123...",
  "stream_url": "http://localhost:8085/hls/abc123.../playlist.m3u8"
}
```

Open the returned `stream_url` in VLC/ffplay/player.

## 7) Validate Redis + pooling is active

Check service info through the load balancer:

```bash
curl -s -H "X-API-Token: ${API_TOKEN}" "http://localhost:8085/" | jq
```

Expected fields:

- `redis.enabled: true`
- `redis.pooling_enabled: true`

Optional Redis sanity check:

```bash
docker exec -it m3u-proxy-redis redis-cli ping
```

Expected output: `PONG`

## 8) Confirm requests stay on one backend

While streaming, check backend logs:

```bash
docker logs -f m3u-proxy-1
docker logs -f m3u-proxy-2
docker logs -f m3u-proxy-3
```

For a single client IP, playback requests should primarily hit one backend due to `ip_hash` affinity.

## Operational notes

- If many users share one public IP (carrier NAT), `ip_hash` may distribute unevenly.
- If you run with a reverse proxy path prefix, set `ROOT_PATH` and update LB routing accordingly.
- This example already uses shared Redis + pooling, which is recommended for most multi-instance deployments.
- For deeper tuning/options, review [Redis Pooling Guide](REDIS_POOLING.md).

## Optional: scale to more instances

Add additional services (for example `m3u-proxy-4`) and include them in the NGINX `upstream` block.
