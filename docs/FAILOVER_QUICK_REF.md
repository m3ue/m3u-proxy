# Failover Quick Reference

## Creating Streams with Failover URLs

### Direct/Continuous Stream
```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "url": "http://primary-source.com/live/stream.ts",
    "failover_urls": [
      "http://backup1.com/live/stream.ts",
      "http://backup2.com/live/stream.ts",
      "http://backup3.com/live/stream.ts"
    ],
    "user_agent": "Custom-Agent/1.0",
    "metadata": {
      "channel": "ESPN",
      "category": "sports"
    }
  }'
```

### HLS Stream
```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "url": "http://primary-source.com/playlist.m3u8",
    "failover_urls": [
      "http://backup1.com/playlist.m3u8",
      "http://backup2.com/playlist.m3u8"
    ]
  }'
```

### Transcoded Stream
```bash
curl -X POST "http://localhost:8085/transcode" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "url": "http://primary-source.com/stream.ts",
    "failover_urls": [
      "http://backup1.com/stream.ts",
      "http://backup2.com/stream.ts"
    ],
    "profile": "720p",
    "output_format": "ts"
  }'
```

## Triggering Manual Failover

### Basic Failover
```bash
curl -X POST "http://localhost:8085/streams/{stream_id}/failover" \
  -H "Authorization: Bearer your-token"
```

**Response:**
```json
{
  "message": "Failover triggered successfully - all clients will seamlessly reconnect",
  "new_url": "http://backup1.com/stream.ts",
  "failover_index": 1,
  "failover_attempts": 1,
  "active_clients": 5,
  "stream_type": "Live Continuous"
}
```

### Check Stream Status Before Failover
```bash
# Get stream details
curl "http://localhost:8085/stats/detailed" \
  -H "Authorization: Bearer your-token" | jq '.streams[] | select(.stream_id == "your-stream-id")'
```

## Automatic Failover Scenarios

### 1. Connection Error
**Scenario:** Primary source becomes unreachable
**Action:** Automatically tries next failover URL
**Retries:** Up to 3 attempts
**Log:** `INFO: Attempting automatic failover for client xxx (attempt 1/3)`

### 2. Timeout
**Scenario:** Stream times out or becomes unresponsive
**Action:** Automatically switches to failover
**Log:** `WARNING: Stream error for client xxx: TimeoutException`

### 3. Stream Freeze
**Scenario:** Manual detection of freezing stream
**Action:** Call manual failover endpoint
**Command:**
```bash
curl -X POST "http://localhost:8085/streams/{stream_id}/failover" \
  -H "Authorization: Bearer your-token"
```

### 4. FFmpeg Process Failure (Transcoded Streams)
**Scenario:** FFmpeg crashes or fails to start
**Action:** Automatically restarts with failover URL
**Log:** `WARNING: Transcoding process exited, attempting failover`

## Monitoring Failover Events

### Using Webhooks
```bash
# Register webhook for failover events
curl -X POST "http://localhost:8085/webhooks" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "url": "http://your-server.com/webhook",
    "events": ["FAILOVER_TRIGGERED", "STREAM_FAILED"]
  }'
```

**Webhook Payload:**
```json
{
  "event_id": "uuid",
  "event_type": "FAILOVER_TRIGGERED",
  "stream_id": "abc123",
  "timestamp": "2025-11-05T10:30:00Z",
  "data": {
    "old_url": "http://primary.com/stream",
    "new_url": "http://backup1.com/stream",
    "failover_index": 1,
    "attempt_number": 1,
    "reason": "manual",
    "client_count": 3
  }
}
```

### Checking Stats
```bash
# Get comprehensive stats
curl "http://localhost:8085/stats/detailed" \
  -H "Authorization: Bearer your-token"
```

**Stream-level failover info:**
```json
{
  "streams": [
    {
      "stream_id": "abc123",
      "original_url": "http://primary.com/stream",
      "current_url": "http://backup1.com/stream",
      "has_failover": true,
      "failover_urls": [
        "http://backup1.com/stream",
        "http://backup2.com/stream"
      ],
      "failover_resolver_url": "http://m3u-editor:36400/api/failover",
      "current_failover_index": 1,
      "failover_attempts": 2,
      "last_failover_time": "2025-11-05T10:30:00Z"
    }
  ]
}
```

### Stream Failover Fields Reference

| Field | Type | Description |
|:------|:-----|:------------|
| `has_failover` | boolean | True if failover URLs or resolver configured |
| `failover_urls` | array | List of configured backup URLs |
| `failover_resolver_url` | string/null | Dynamic resolver URL for failover |
| `current_failover_index` | integer | Index of currently active URL (0=primary) |
| `failover_attempts` | integer | Total number of failover attempts |
| `last_failover_time` | string/null | ISO timestamp of last failover event |

## Player Integration

### HLS Player (video.js)
```html
<video id="player" class="video-js"></video>
<script>
  const player = videojs('player', {
    sources: [{
      src: 'http://localhost:8085/hls/{stream_id}/playlist.m3u8?client_id=player1',
      type: 'application/x-mpegURL'
    }]
  });
  
  // Player will automatically handle playlist refreshes
  // Failover is transparent - no player code changes needed
</script>
```

### Direct Stream Player (VLC, ffplay)
```bash
# VLC
vlc "http://localhost:8085/stream/{stream_id}?client_id=vlc1"

# ffplay
ffplay "http://localhost:8085/stream/{stream_id}?client_id=ffplay1"

# Both will automatically reconnect on failover
```

### Custom Player with Reconnection
```javascript
async function streamWithFailover(streamId, clientId) {
  const response = await fetch(
    `http://localhost:8085/stream/${streamId}?client_id=${clientId}`
  );
  
  const reader = response.body.getReader();
  
  while (true) {
    try {
      const { done, value } = await reader.read();
      if (done) break;
      
      // Process chunk
      processVideoChunk(value);
      
    } catch (error) {
      console.log('Connection interrupted, reconnecting...');
      // The proxy handles failover automatically
      // Just reconnect with same client_id
      return streamWithFailover(streamId, clientId);
    }
  }
}
```

## Troubleshooting

### Failover Not Working

**Check 1:** Verify failover URLs are configured
```bash
curl "http://localhost:8085/stats/detailed" | jq '.streams[] | select(.stream_id == "xxx") | .has_failover'
```

**Check 2:** Review logs for failover events
```bash
docker logs m3u-proxy 2>&1 | grep -i failover
```

**Check 3:** Verify failover URLs are accessible
```bash
curl -I "http://backup-url.com/stream.ts"
```

### Failover Loop

**Symptom:** Continuous failover attempts
**Cause:** All failover URLs are inaccessible
**Solution:** 
- Verify at least one failover URL is working
- Check network connectivity
- Review authentication/headers

### Client Disconnects During Failover

**Symptom:** Client drops connection during failover
**Cause:** Client timeout shorter than reconnection time
**Solution:**
- Increase client read timeout
- Use HLS (better failover support in players)
- Implement client-side reconnection logic

### Transcoded Stream Fails to Restart

**Symptom:** FFmpeg doesn't restart after failover
**Cause:** Transcoding process cleanup issue
**Solution:**
```bash
# Check for stuck processes
docker exec m3u-proxy ps aux | grep ffmpeg

# Force cleanup if needed
curl -X DELETE "http://localhost:8085/hls/{stream_id}/clients/{client_id}" \
  -H "Authorization: Bearer your-token"
```

## Best Practices

1. **Use Multiple Failover URLs:** Configure at least 2-3 backup sources
2. **Test Failover URLs:** Verify all URLs work before going live
3. **Monitor Events:** Set up webhooks to track failover events
4. **HLS for Live Streams:** HLS handles failover more gracefully than direct streams
5. **Consistent Formats:** Ensure all failover URLs provide same format/quality
6. **Regular Health Checks:** Enable health checks to proactively detect issues
7. **Client Tracking:** Use consistent client_id for reconnection tracking

## API Quick Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/streams` | POST | Create stream with failover URLs |
| `/transcode` | POST | Create transcoded stream with failover |
| `/streams/{stream_id}/failover` | POST | Manually trigger failover |
| `/stats/detailed` | GET | View stream status and failover history |
| `/webhooks` | POST | Register webhook for failover events |
| `/broadcast/{network_id}/start` | POST | Start or transition a network broadcast. Supports optional `headers` object (key/value) to be sent to FFmpeg when fetching the input URL |

## Environment Variables

```bash
# Configure failover behavior (if needed)
DEFAULT_CONNECTION_TIMEOUT=30  # Connection timeout in seconds
DEFAULT_READ_TIMEOUT=300       # Read timeout in seconds
DEFAULT_MAX_RETRIES=3          # Max automatic failover attempts
```
