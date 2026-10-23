# QSV live normalization

## Usage and scope

`POST /streams` accepts `normalization_encoder: "cpu" | "vaapi" | "qsv"` when
`normalize_live_output` is enabled. CPU remains the default, normalization is off
by default, and choosing QSV does not silently fall back to another encoder.
Stream Profiles are independent of this normalization mode.

QSV requires an Intel GPU, a compatible driver/runtime and access to its render
node. The supported input is clear TS-188 with a single program, one H.264 video
stream and one AAC ADTS or AC-3 audio stream, including recognized DVB AC-3
signaling. Unsupported codecs, encrypted/multi-program input, incompatible
program changes and unreviewed descriptors fail closed rather than forwarding
unvalidated bytes. This is not a general-purpose MPEG-TS remuxer.

## Processing and hardware admission

The static pipeline is **software decoding, CPU deinterlacing and QSV hardware
encoding**, not full-GPU processing and not a runtime fallback:

```text
-init_hw_device vaapi=va:/dev/dri/renderD128
-init_hw_device qsv=qs@va -filter_hw_device qs
-hwaccel none -err_detect:v explode -ec:v 0
-vf bwdif=mode=send_field:parity=auto:deint=interlaced,format=nv12,hwupload=extra_hw_frames=64
-fps_mode passthrough -enc_time_base 1/90000
-c:v h264_qsv -global_quality 18 -g 25 -bf 0
```

The complete preset is server-owned. Conditional bwdif preserves progressive
25p/50p cadence and produces 50 progressive pictures per second from TFF-i25.
There is no fixed output-FPS filter, arbitrary client command, inherited user
bitrate cap or B-frame reordering. Strict software decoding is intentional:
QSV hardware decoding can conceal a damaged unbounded H.264 PES and output an
additional damaged picture even when strict error flags are requested.

Admission generates a tiny compatible H.264 TFF fixture in RAM and exercises the
selected decode/deinterlace/upload/encode chain before allocating streams.
Missing devices/executables, failed fixture generation or initialization, and
nonzero probe exits return 503. Each subprocess is bounded by a ten-second
timeout and killed/reaped on timeout or cancellation. This checks the worker,
not every future source, resolution or driver state.

## Ownership, source changes and transport loss

The effective preset and encoder salt both the public output identity and its
private input identity. CPU custom templates do not affect QSV. Server metadata
overrides encoder-marker spoofing. Only reciprocally server-owned QSV inputs
activate TS canonicalization; CPU, VAAPI and ordinary passthrough remain outside
this path. Deleting a private owned input returns 409; deleting its output
cascades cleanup. Source controls retain their existing ownership rules.

A source-generation boundary resets PAT/PMT discovery before replacement bytes.
The adapter keeps selected program/PMT/elementary PIDs stable while preserving
PES payloads, PTS/DTS, PCR and adaptation data. Media continuity-counter deltas
within a generation remain visible: real packet loss and retransmissions must
not be turned into a falsely clean stream. The decoder can then reject damaged
packets. Canonicalization errors terminate the input, never trigger raw fallback.

Stable encoder PID and a successful backup fetch do not by themselves prove
playback: acceptance decodes both source images and audio through one real POST
and one continuous player GET with automatic EOF failover, changed PMT/ES PIDs
and independently verified timestamp resets.

## Damaged-input timing policy and audible limits

Missing source pictures cannot be recovered by timestamp smoothing. Progressive
source-loss cases preserve their source-derived missing-picture time slots.
For interlaced input, stock FFmpeg bwdif places a picture's second output field
at the midpoint to the next **decoded** picture. If an intervening damaged i25
picture is rejected, this can shift the final valid A picture's second field by
20 ms. This standard-FFmpeg policy is retained; no custom FFmpeg patch is used.
It must not be described as exact preservation of every original field timestamp.

The regression oracle keeps the original source-position vector and the
presentation-policy vector separate. Only the precisely identified unbounded,
truncated-video i25 cut permits the input-derived +1800-tick field adjustment;
all other positions/cases remain exact. No output-derived fit, general gap
allowance, additional concealed picture, valid-frame drop, repeated image or
relaxed SSIM/A/V threshold is accepted.

Audio packet continuity is not proof of inaudible recovery. Short synthetic
handoffs measured approximately 140–320 ms of PCM silence, depending on which
source PES was truncated. Failover is therefore not promised to be seamless.
Normal-source cadence, damaged-input behavior, audio and source corruption are
reported separately.

## Verification

Tests generate compatible moving H.264 p25/p50/TFF-i25 sources and inspect input
field flags. They check exact frame/field positions, counts, unique images,
PTS-policy vectors, progressive output, per-frame reference similarity, audio
tones, PCM silence and A/V synchronization. The 18 cut cases cover complete
video, truncated audio and truncated video, each with bounded/unbounded video
PES lengths and all three input cadences. A separate missing-slice regression
requires canonicalized damaged input to match strict raw decoding exactly.

Run hardware tests only on an owned Intel render-node sandbox, serially, without
provider inputs or live viewers:

```sh
PYTHONPATH=src RUN_QSV_CADENCE=1 RUN_QSV_QUALITY=1 \
RUN_VAAPI_QUALITY=1 RUN_QSV_HANDOFF=1 \
python -m pytest tests -q

PYTHONPATH=src RUN_QSV_QUALITY=1 NORMALIZATION_QUALITY_SIZE=1920x1080 \
python -m pytest tests/test_normalization_quality.py -k qsv -q

ruff check src tests
```

`QSV_CADENCE_CONTROL` optionally identifies an additional retained synthetic
control fixture; moving cases generate their own inputs. Ordinary non-hardware
CI intentionally skips explicitly opt-in hardware cases.

The accepted candidate passed 616 tests with all four hardware flags enabled,
plus three separate Full-HD cases. Two-second Full-HD i25/p25/p50 inputs produced
100/50/100 distinct progressive pictures with 20/40/20-ms cadence. Reference SSIM
was 0.998530/0.998968/0.998896. These are synthetic correctness measurements, not
provider/client visual acceptance, throughput guarantees or GPU stress results.
