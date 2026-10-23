# Opt-in direct-live recovery

## Persistent normalized output (separate, default-off opt-in)

For players that **do not reconnect** when a live HTTP body ends, the existing
`POST /streams` API can compose a private direct-live input with the existing
pooled FFmpeg transcoder. No manual inner `/streams` + `/transcode` creation, URL
rewrite, second player GET, or operator action is needed for automatic failover.

Prerequisites (one process/instance, FFmpeg with libx264/AAC):

```dotenv
REDIS_ENABLED=false
ENABLE_TRANSCODING_POOLING=true
ENABLE_SINGLE_PROCESS_TRANSCODING=true
LIVE_NORMALIZE_OUTPUT=false
```

```http
POST /streams
Content-Type: application/json

{
  "url": "http://source.example/live/channel.ts",
  "failover_urls": ["http://backup.example/live/channel.ts"],
  "normalize_live_output": true,
  "metadata": {"id": "123", "original_channel_id": "123", "playlist_uuid": "provider"}
}
```

Consume the returned `/stream/{stream_id}` normally. Nullable
`normalize_live_output` inherits `LIVE_NORMALIZE_OUTPUT` (default `false`);
explicit `false` overrides global `true`. Disabled mode keeps legacy identities
and behavior. Redis, known multi-worker topologies, disabled local pooling,
DASH/HLS/VOD inputs and missing ASGI server-port information are rejected before
allocation. Replicas/external prefork deployments are not supported even if they
cannot be detected. URL shape is validated without provider probing; sources and
resolver-returned backups must actually be compatible direct live MPEG-TS.

The owned input uses strict-live + sticky sessions, `single_recovery_per_exit=true`
and **`end_session_on_reconnect=false`**, irrespective of those input request
flags: its continuous raw TS body feeds one persistent normalizer. Ending the
chunked input body at a source switch can make FFmpeg stop instead of reconnect.
Existing handoff/source-selection behavior is reused, not replaced. Original
metadata, upstream headers, failover resolver and silence settings reach the
private input. Public metadata is original metadata plus
`normalize_live_output="true"` and server-set `normalization_encoder="cpu"` or
`"vaapi"`; client metadata cannot override either marker. No
`transcoding="true"` marker is injected, so
ordinary-direct editor pooling can still find it.

Only the outer ID is needed for management. `POST /streams/{id}/failover` routes
to its owned input without restarting FFmpeg; listing, metadata search/counts and
stream detail expose one provider stream and the input's current source/failover
attempts, not the loopback URL. An optional diagnostic
`normalization_input_stream_id` identifies the private source. Internal client
and lifecycle webhooks do not count a second provider/viewer; source failover is
reported against the public outer ID. Ownership is private server state, never a
client-supplied metadata flag. Policy/profile-salted private IDs prevent ordinary
raw clients from accidentally joining the normalizer's input. Repeated identical
POSTs retain the active encoder and selected backup. GET activity holds the
owned input; deleting/expiring the outer cascades only to its owned input.
Cancellation/partial creation failures roll back new allocations.

The standard profiles decode compatible audio/video into H.264/AAC MPEG-TS,
normalizing timestamp discontinuities with the existing FFmpeg pipeline. Both use
`bwdif=mode=send_field:parity=auto:deint=interlaced` and `-fps_mode passthrough`:
correctly flagged interlaced 25-frame/s input becomes 50 progressive frames/s;
progressive 25/50-frame/s input retains its cadence. There is no fixed FPS filter
or output `-r`. This relies on decoded frame interlace/parity flags; mislabeled
sources are not auto-detected or repaired. CPU uses explicit x264 CRF 18 (the
existing ultrafast/zerolatency preset remains); VAAPI uses CQP/QP 18. These are
lossy encode settings, not equivalent quality scales or a perceptual guarantee.
The GOP limit remains 25 **frames**, not an FPS constraint. This
costs latency/CPU and changes bytes/codecs/quality. It is **not a claim of arbitrary
codec, track-layout, resolution or protocol switching**; audio and video tracks
are required. HLS/DASH/VOD remain outside this feature. Repeated failures still
follow existing retry/exhaustion behavior and may eventually terminate playback.

`normalization_encoder` accepts only `"cpu"` (default) or `"vaapi"`. Missing
encoder fields, including older channel metadata, mean CPU; there is no GPU
auto-selection. The field has no effect when normalization is off. Both private
IDs include the encoder and effective profile, isolating CPU/GPU viewers.

VAAPI requires an accessible Intel `/dev/dri/renderD128` plus an FFmpeg build
and VAAPI driver supporting `h264_vaapi`. A bounded synthetic upload/encode probe
runs before allocation; absent/inaccessible/non-Intel hardware or a failed probe
returns HTTP 503, never silently falls back to CPU. Device loss after creation
can still end playback. The server-owned template uses software decoding,
`-vaapi_device /dev/dri/renderD128`,
`bwdif=mode=send_field:parity=auto:deint=interlaced,format=nv12,hwupload`,
`-fps_mode passthrough -c:v h264_vaapi -rc_mode CQP -qp 18 -g 25 -bf 0`,
and the same audio resampling/AAC, timestamp,
PMT and reconnect policy as CPU. Hardware decoding is deliberately not enabled
for source-switch robustness. Clients cannot choose a command or device.

Optional trusted `LIVE_NORMALIZE_PROFILE` overrides the **CPU** default below
(and is ignored for the fixed VAAPI template); required
placeholders are `{input_url}` and `{output_args}`, with optional `{format}`.
Changing the profile creates an isolated stream policy/ID; it does not modify
already-running encoders. Keep a persistent MPEG-TS pipe output and test any
customizations offline before using them.

```text
-analyzeduration 1000000 -probesize 1000000 -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 2 -reconnect_max_retries 5 -fflags +genpts+discardcorrupt -merge_pmt_versions 1 -dts_delta_threshold 1 -i {input_url} -map 0:v:0 -map 0:a:0 -vf bwdif=mode=send_field:parity=auto:deint=interlaced -fps_mode passthrough -af aresample=async=1:first_pts=0 -c:v libx264 -preset ultrafast -crf 18 -tune zerolatency -threads 2 -g 25 -bf 0 -c:a aac -b:a 128k -f mpegts {output_args}
```

Offline acceptance (installed project dependencies; no providers or Emby needed):

```sh
python -m pytest tests/test_live_normalization.py tests/test_normalization_quality.py -q
# In a disposable --rm --network none --device /dev/dri sandbox only:
RUN_VAAPI_QUALITY=1 NORMALIZATION_QUALITY_SIZE=1920x1080 python -m pytest tests/test_normalization_quality.py -q -s
python tools/verify_live_normalization.py --encoder cpu
# On an Intel VAAPI host/container with the render node and drivers available:
python tools/verify_live_normalization.py --encoder vaapi
```

The moving-video tests interleave a 50p `testsrc2` into real TFF fields before
lossless fixture encoding, verify decoded interlace/parity flags, then check exact
frame counts, PTS intervals, progressive output and distinct frame hashes. They
compare to a lossless bwdif field-rate reference of the same decoded source.
SSIM >= 0.97 is a synthetic regression floor for these high-quality encodes;
count/PTS checks independently reject dropped fields or CFR duplicate frames.
This is not a subjective/provider/player quality or throughput certification.
The VAAPI transport verifier also requires a stable owned FFmpeg PID, an open
render-node FD and increasing Intel DRM video-engine time across A→B.

The acceptance uses one initial API POST and **one player GET**, faults A's real
HTTP body, then observes B's red→blue/440→880 Hz media in that same response.
Both sources use H.264/AAC, with different PIDs and reset timestamps. It checks
all decoded video frames, head/tail audio, zero backwards per-stream DTS, one
provider count/current source and DELETE reader cleanup. A separate default-off
control proves byte-exact direct delivery without a normalizer. FFmpeg decoder
warnings from the deliberately truncated final capture are included in output;
no claim of warning-free packet truncation or installed-client acceptance is made.
For rollback, set `normalize_live_output=false`, consume that returned endpoint,
and delete the owned normalized stream; global changes require restart/recreation.

## Session-end recovery (independent options)

These independent options are **disabled by default**. They apply to direct live
HTTP streams, not VOD/timeshift, transcoding, HLS playlists or DASH manifests.
Existing TS discontinuity-marker and overlap-trimming behavior is unchanged with
both options off. They do not convert media, normalize PTS/PIDs/codecs, or offer
seamless playback across arbitrary upstream sources.

| ENV (default) | Nullable `POST /streams` field | Effect |
| --- | --- | --- |
| `LIVE_END_SESSION_ON_RECONNECT=false` | `end_session_on_reconnect` | Finish the old downstream HTTP bodies before opening a replacement upstream generation. |
| `LIVE_SINGLE_RECOVERY_PER_EXIT=false` | `single_recovery_per_exit` | Do not reinterpret an already handled timeout/retry/failover exit as EOF or a circuit-breaker failure. |

For each field, omitted or `null` inherits ENV; explicit `false` overrides a global
`true`. Effective policies are frozen at stream creation. Different effective
policy combinations use different stream IDs, so incompatible clients cannot
silently share the wrong recovery policy. Both-off IDs retain their old format.
Changing ENV does not mutate already-created streams.

## Scoped activation

Start with per-stream activation, rather than switching every channel globally:

```http
POST /streams
Content-Type: application/json

{
  "url": "http://source.example/live/channel.ts",
  "failover_urls": ["http://backup.example/live/channel.ts"],
  "end_session_on_reconnect": true,
  "single_recovery_per_exit": true
}
```

Consume the returned `stream_endpoint`. At an upstream reconnect boundary, that
HTTP body ends. Make a **new GET to the same endpoint** to consume the chosen
source. The old primary, all subscribers (including backlogged queues), and any
unclaimed upstream handoff are detached from the old generation. Already-sent
bytes cannot be retracted. Subscriber backlog is discarded, not drained into a
new session. A healthy connection can still be handed to a subscriber when only
the primary viewer disconnects: this does not create a new upstream generation.
If a promoted subscriber cannot reuse the handed-off upstream, its existing body
ends rather than splicing a fresh connection into it.
In session-end mode a still-active primary owns stall recovery; subscribers do
not start competing source selections merely because it is slow. In-flight
source selection is serialized with generation reset before a new primary is
exposed. This does not make selection durable across process restarts.

The selected `current_url` survives the reset, including a subsequent identical
`POST /streams`. This also covers reconnecting to the **same URL**, because a new
connection to the same URL may still reset timestamps or codec configuration.
No replacement-generation bytes are sent in the old bodies. A reconnecting live
body has no inherited upstream `Content-Length`/`Content-Range` and uses HTTP 200,
so early session-end does not falsely promise the remainder of an upstream body.
Normal stream expiry, explicit deletion or a process restart still loses this
in-memory selection; this is not durable source pinning.

The reset option does not change the existing source-selection/retry algorithm.
The single-recovery option is separate on purpose: with it off, legacy timeout
post-loop classification remains enabled, even if session-end is enabled.
Enabling both is recommended when testing a timeout-sensitive channel. The
existing same-URL silent-close retry window is another independent option, not
implicitly enabled by either switch.

## Supported topology and limits

Use **one proxy process/instance**, without Redis or `WORKER_ID`. Policy-enabled
live stream creation returns **HTTP 400 before allocating a stream or clients**
if Redis is enabled, a Redis URL was supplied to the manager, or `WORKER_ID` is
set. This deliberately rejects distributed/pool-coordinated operation rather
than pretending that process-local generation barriers coordinate workers.
Local, non-Redis transcoding-pool infrastructure may remain enabled; these
policies only affect the direct-live path, never pooled transcoding output.
External multi-process launchers/load balancers without Redis/worker configuration
cannot be auto-detected: do not activate these options there. Sticky routing is
not a substitute for generation coordination. Redis/device acceptance is not
claimed; distributed support would need a separate implementation and tests.

This is **session-end**, not a player reconnect instruction. Emby, Kodi, TVs and
other clients may stop playback instead of issuing a new request. Test actual
client versions and reverse-proxy buffering separately. In particular, the
[Emby M3U tuner documentation](https://emby.media/support/articles/M3U-Tuners.html)
does not guarantee automatic reconnection after the HTTP body ends.

## Why not just add a discontinuity flag?

- [FFmpeg MPEG-TS muxer](https://ffmpeg.org/ffmpeg-formats.html#mpegts):
  `initial_discontinuity` marks the initial packet of each stream. It does not
  normalize arbitrary codec/track/timestamp changes or guarantee player recovery.
  Generating missing timestamps (`genpts`) is not equivalent to rewriting all
  existing timestamps into a continuous timeline either.
- [RFC 8216 §4.3.2.3](https://www.rfc-editor.org/rfc/rfc8216#section-4.3.2.3):
  HLS requires `EXT-X-DISCONTINUITY` for changes in format, track identifiers or
  timestamp sequence. That is a **playlist/segment boundary**, not an in-band tag
  that can be attached to a direct continuous HTTP TS response. Implementing that
  honestly would require a separate segmented-output architecture.

## Rollback

Set both per-stream fields to `false`, or remove them with both ENV values still
`false`, and request the newly returned endpoint. When rolling back global
activation, set both ENV variables to `false` and restart the single process, then
recreate affected streams. Existing policy-enabled IDs remain policy-enabled
until deleted/expired/restarted; do not expect editing ENV or posting an override
to change an already-open body. No release/deploy step is implied by this guide.

## Reproducible local verification

With project requirements installed and FFmpeg/ffprobe on PATH:

```sh
python -m pytest tests/test_live_recovery.py tests/test_live_recovery_headers.py -q
python -m pytest --tb=short -q -m 'not integration and not slow'
python tools/verify_live_recovery.py
```

The executable acceptance script uses real loopback source HTTP requests and a
real uvicorn proxy API. It generates red/440 Hz MPEG-2/MP2 TS at PID 0x100/0x101,
then blue/880 Hz H.264/AAC TS at PID 0x200/0x201 with a timestamp reset. It asserts
byte-exact first-body=A, same-endpoint reconnect=B (also after POST reuse), decodes
**all frames and audio** of each response separately, checks track/timestamp
changes, and checks that feature-disabled legacy still emits A+B in one body.
Both compared runs explicitly enable existing Strict Live TS mode (zero fixture
prebuffer), so only the two new policy switches differ.
It does not claim that legacy mixed-codec bytes decode seamlessly. No providers,
production streams or external network are used by the acceptance script.
