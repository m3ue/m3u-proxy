# API Token Authentication

## Overview

The m3u-proxy server supports optional API token authentication to secure management endpoints. When enabled, all administrative operations require a valid API token to be provided via the `X-API-Token` HTTP header.

## Configuration

### Enabling Authentication

Set the `API_TOKEN` environment variable:

```bash
# In your .env file
API_TOKEN=your_secret_token_here

# Or as an environment variable
export API_TOKEN="your_secret_token_here"
```

### Disabling Authentication

To disable authentication (default behavior), leave `API_TOKEN` unset or set it to an empty string:

```bash
# In your .env file
# API_TOKEN=

# Or unset the variable
unset API_TOKEN
```

## Protected Endpoints

When authentication is enabled, the following endpoints require the `X-API-Token` header:

### Management Endpoints
- `GET /` - Root/status endpoint
- `POST /streams` - Create a new stream
- `GET /streams` - List all streams
- `GET /streams/{stream_id}` - Get stream information
- `DELETE /streams/{stream_id}` - Delete a stream
- `POST /streams/{stream_id}/failover` - Trigger failover

### Statistics Endpoints
- `GET /stats` - Get proxy statistics
- `GET /stats/detailed` - Get detailed statistics
- `GET /stats/performance` - Get performance statistics
- `GET /stats/streams` - Get stream statistics
- `GET /stats/clients` - Get client statistics
- `GET /clients` - List all clients

### Health & Monitoring
- `GET /health` - Health check endpoint (returns HTTP 500 when status cannot be collected)

### Webhook Management
- `POST /webhooks` - Add a webhook
- `GET /webhooks` - List webhooks
- `DELETE /webhooks` - Remove a webhook
- `POST /webhooks/test` - Test a webhook

### Client Management
- `DELETE /hls/{stream_id}/clients/{client_id}` - Disconnect a client

## Unprotected Endpoints

The following endpoints do **NOT** require authentication, as they are used by media players to stream content:

- `GET /hls/{stream_id}/playlist.m3u8` - Get HLS playlist
- `GET /hls/{stream_id}/segment` (and `.ts`, `.m4s`, `.mp4`) - Get HLS segment
- `GET /dash/{stream_id}/manifest.mpd` - Get DASH manifest
- `GET /dash/{stream_id}/segment/...` and `GET /dash/{stream_id}/file` - Get DASH segment
- `GET /stream/{stream_id}` - Stream direct content
- `GET /health/live` - Liveness probe for container healthchecks (returns only `{"status": "ok"}`)

Playlists and manifests are served for streams created through the protected API. When the proxy rewrites them, it adds an HMAC signature to every upstream URL (`&sig=` on HLS and DASH file URLs, inside the path for DASH segment bases). The segment and file endpoints reject any URL without a valid signature, so they can't be used to fetch arbitrary URLs. Local files are only served from transcoder output directories (`HLS_TEMP_DIR/m3u_proxy_hls_*`).

The signing key is derived from `API_TOKEN` (PBKDF2), so every worker sharing a token accepts each other's URLs. Signed URLs are visible to anyone who can load a playlist, so use a long random token (for example `openssl rand -hex 24`) rather than a memorable password. Without a token, a random key is generated at startup, and rewritten URLs stop working after a restart (players pick up new ones on their next playlist refresh).

The `?url=` parameter on the playlist, manifest and stream endpoints creates a stream for that URL. It only accepts a signed URL (variant playlists the proxy rewrote) or a request that passes the API token check above.

## Usage Examples

### With cURL

```bash
# Set your token
export API_TOKEN="my_secret_token"

# Method 1: Using X-API-Token header (recommended)
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: ${API_TOKEN}" \
  -d '{"url": "https://example.com/stream.m3u8"}'

# Method 2: Using query parameter (useful for browser access)
curl -X POST "http://localhost:8085/streams?api_token=${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/stream.m3u8"}'

# Get statistics (header method)
curl "http://localhost:8085/stats" \
  -H "X-API-Token: ${API_TOKEN}"

# Get statistics (query parameter method)
curl "http://localhost:8085/stats?api_token=${API_TOKEN}"

# List all streams
curl "http://localhost:8085/streams?api_token=${API_TOKEN}"

# Delete a stream
curl -X DELETE "http://localhost:8085/streams/abc123?api_token=${API_TOKEN}"
```

### Browser Access

You can access authenticated endpoints directly in a browser using the query parameter:

```
# View statistics
http://localhost:8085/stats?api_token=my_secret_token

# View health status
http://localhost:8085/health?api_token=my_secret_token

# List all streams
http://localhost:8085/streams?api_token=my_secret_token
```

### With Python

```python
import requests

API_TOKEN = "my_secret_token"
BASE_URL = "http://localhost:8085"

headers = {
    "X-API-Token": API_TOKEN,
    "Content-Type": "application/json"
}

# Create a stream
response = requests.post(
    f"{BASE_URL}/streams",
    headers=headers,
    json={"url": "https://example.com/stream.m3u8"}
)
print(response.json())

# Get statistics
response = requests.get(f"{BASE_URL}/stats", headers=headers)
print(response.json())

# List streams
response = requests.get(f"{BASE_URL}/streams", headers=headers)
print(response.json())
```

### With JavaScript/Node.js

```javascript
const API_TOKEN = 'my_secret_token';
const BASE_URL = 'http://localhost:8085';

const headers = {
    'X-API-Token': API_TOKEN,
    'Content-Type': 'application/json'
};

// Create a stream
fetch(`${BASE_URL}/streams`, {
    method: 'POST',
    headers: headers,
    body: JSON.stringify({
        url: 'https://example.com/stream.m3u8'
    })
})
.then(response => response.json())
.then(data => console.log(data));

// Get statistics
fetch(`${BASE_URL}/stats`, { headers })
    .then(response => response.json())
    .then(data => console.log(data));
```

## Error Responses

### 401 Unauthorized
Returned when the API token is missing:

```json
{
    "detail": "API token required. Provide token via X-API-Token header."
}
```

### 403 Forbidden
Returned when the API token is invalid:

```json
{
    "detail": "Invalid API token"
}
```

## Authentication Methods

The proxy supports two methods for providing the API token:

### 1. Header Method (Recommended)
Using the `X-API-Token` HTTP header:
```bash
curl -H "X-API-Token: your_token" http://localhost:8085/stats
```

**Pros:**
- More secure (not logged in URL access logs)
- Standard HTTP authentication pattern
- Suitable for API-to-API communication

**Cons:**
- Requires ability to set custom headers
- Not usable directly in browser address bar

### 2. Query Parameter Method
Using the `api_token` query parameter:
```bash
curl "http://localhost:8085/stats?api_token=your_token"
```

**Pros:**
- Easy to use in browsers (just paste URL)
- Works everywhere, no special configuration needed
- Good for quick manual testing

**Cons:**
- Token appears in URL access logs
- Token visible in browser history
- Can be leaked via Referer header

**When to use query parameters:**
- Quick testing and debugging
- Browser-based access for manual inspection
- Environments where setting headers is difficult

**When to use headers:**
- Production API calls
- Automated scripts and tools
- Any security-sensitive operation

## Security Best Practices

1. **Use Strong Tokens**: Generate a strong, random token:
   ```bash
   # Generate a secure random token
   openssl rand -hex 32
   ```

2. **Use HTTPS**: Always use HTTPS in production to prevent token interception.

3. **Prefer Headers Over Query Parameters**: Headers are more secure as they don't appear in logs.

4. **Rotate Tokens**: Regularly rotate your API tokens.

5. **Environment Variables**: Store tokens in environment variables or secret management systems, never in code.

6. **Limit Scope**: If possible, use different tokens for different applications or environments.

7. **Monitor Access**: Regularly review logs for unauthorized access attempts.

8. **Browser Usage**: Be cautious when using query parameters in browsers - the token will be visible in history.

## Docker Configuration

### Docker Compose

```yaml
services:
  m3u-proxy:
    image: sparkison/m3u-proxy:latest
    environment:
      - API_TOKEN=your_secret_token_here
    ports:
      - "8085:8085"
```

### Docker Run

```bash
docker run -d \
  -p 8085:8085 \
  -e API_TOKEN="your_secret_token_here" \
  sparkison/m3u-proxy:latest
```

## Troubleshooting

### "API token required" error
- Ensure the `X-API-Token` header is being sent with your request
- Check that the header name is spelled correctly (case-insensitive)
- Verify the token is not empty

### "Invalid API token" error
- Verify the token matches the `API_TOKEN` environment variable
- Check for extra spaces or newlines in the token
- Ensure the token hasn't changed since your application started

### Authentication not working
- Verify `API_TOKEN` is set in your environment
- Restart the application after setting the environment variable
- Check application logs for any configuration errors

### Stream playback requires token
- Stream playback endpoints (`/hls/*` and `/stream/*`) should NOT require authentication
- If they do, there may be a misconfiguration - check your reverse proxy or load balancer settings
