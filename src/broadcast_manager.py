"""
Network Broadcast Manager for m3u-proxy.

Manages FFmpeg processes for network broadcasting with:
- Duration-limited streaming for programme boundaries
- Segment sequence continuity across transitions
- Discontinuity marker support
- Webhook callbacks to Laravel when programmes end
"""

import asyncio
import os
import re
import shutil
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import httpx

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class BroadcastConfig:
    """Configuration for a network broadcast."""

    network_id: str
    stream_url: str
    seek_seconds: int = 0
    duration_seconds: int = 0  # 0 = unlimited
    segment_start_number: int = 0
    add_discontinuity: bool = False
    segment_duration: int = 6
    hls_list_size: int = 20
    transcode: bool = False
    video_bitrate: Optional[str] = None
    audio_bitrate: int = 192
    video_resolution: Optional[str] = None
    # Deinterlace (yadif) the source before encoding. Only applies when
    # transcode=True (a stream copy cannot deinterlace). Needed for interlaced
    # MPEG-2 sources such as ATSC OTA / HDHomeRun.
    deinterlace: bool = False
    # Optional explicit codec/preset/hwaccel options (populated from Network transcode config)
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    preset: Optional[str] = None
    hwaccel: Optional[str] = None
    callback_url: Optional[str] = None
    # Optional custom headers to include when FFmpeg fetches the input URL
    headers: Optional[Dict[str, str]] = None
    # DVR mode: preserve all HLS segments (no rolling deletion) for post-processing
    dvr_mode: bool = False
    metadata: Optional[Dict] = None
    # Preferred audio track: an ISO 639 code (network-level default; FFmpeg
    # -map 0:a:m:language:XX) or a numeric type-relative stream position from a
    # per-item override (FFmpeg -map 0:a:N? — see NetworkBroadcastService's
    # resolveTrackPreference on the editor side for the composite value this is
    # resolved from).
    preferred_audio_language: Optional[str] = None
    # Whether to expose embedded subtitle streams via -map 0:s?
    subtitles_enabled: bool = False
    # External subtitle URL (Emby sidecar). When present, added as a second
    # FFmpeg input and mapped into the HLS output. Takes precedence over 0:s?.
    subtitle_url: Optional[str] = None
    # Language tag for the external subtitle (passed as FFmpeg metadata).
    subtitle_language: Optional[str] = None
    # Seek offset for the external subtitle input. 0 = server-rebased (Emby
    # startPositionTicks); positive = full-file subtitle needing local -ss.
    subtitle_seek_seconds: float = 0.0


@dataclass
class BroadcastStatus:
    """Status of a running broadcast."""

    network_id: str
    status: str  # starting, running, stopping, stopped, failed
    current_segment_number: int
    started_at: Optional[str]
    stream_url: str
    hls_dir: Optional[str] = None
    ffmpeg_pid: Optional[int] = None
    error_message: Optional[str] = None
    metadata: Optional[Dict] = None
    bytes_written: int = 0


class NetworkBroadcastProcess:
    """
    Manages a single network broadcast FFmpeg process.

    Key features:
    - Duration limiting via -t flag for programme boundaries
    - Segment number continuity via -start_number
    - Discontinuity injection via HLS flags
    - Webhook callback when FFmpeg exits
    """

    # Error patterns to detect in FFmpeg stderr
    INPUT_ERROR_PATTERNS = [
        "error opening input",
        "failed to resolve hostname",
        "connection refused",
        "connection timed out",
        "server returned 4",  # 403, 404, etc.
        "server returned 5",  # 500, 502, etc.
        "invalid data found",
        "no such file or directory",
        "protocol not found",
    ]

    # Patterns that match INPUT_ERROR_PATTERNS but are non-fatal — log as warning and continue.
    # e.g. FFmpeg's HLS muxer emits "failed to delete old segment" when it can't remove a
    # segment that was already cleaned up externally; this should never kill the broadcast.
    INPUT_ERROR_SUPPRESSIONS = [
        "failed to delete old segment",
    ]

    def __init__(self, config: BroadcastConfig, hls_base_dir: str):
        self.config = config
        self.network_id = config.network_id
        self.hls_dir = os.path.join(hls_base_dir, f"broadcast_{config.network_id}")
        self.process: Optional[asyncio.subprocess.Process] = None
        self.status = "starting"
        self.current_segment_number = config.segment_start_number
        self.started_at: Optional[datetime] = None
        self.error_message: Optional[str] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._bytes_written: int = 0  # Cumulative bytes across all segments ever seen
        # Segment filenames already counted
        self._seen_segments: Set[str] = set()

    # Confirmed live (Plex, via Safari): the embedded-subtitle second input's
    # cues consistently show up ~1s EARLY relative to the dialogue they belong
    # to (each input independently zeroes its own -ss landing point to
    # timestamp 0, and the subtitle-only input's landing point leads the
    # primary's by this small, constant amount). A positive -itsoffset on that
    # input delays its local zero point by the same amount, cancelling the
    # lead directly. Tuned empirically against one specific setup: 1.5
    # overcorrected (subs then ran late), 1.0 lines up correctly.
    #
    # This isn't necessarily a universal constant — the gap comes from real
    # connection/timing behavior (how long the second connection to the source
    # takes to land relative to the first), which plausibly varies with
    # network latency to the media server, the media server's own response
    # time, and per-server/version quirks. Overridable via
    # BROADCAST_SUBTITLE_SYNC_OFFSET_SECONDS so a different deployment can
    # retune it without a code change, instead of baking in one setup's
    # measurement as gospel.
    #
    # An earlier attempt used -copyts to preserve each input's true source
    # timestamps instead (making the gap explicit rather than compensating for
    # it blindly) but had to be reverted: -copyts also disables FFmpeg's normal
    # timestamp-discontinuity sanitization, which this live Plex source
    # apparently relies on — confirmed live, it broke playback outright and
    # left the proxy stuck retry-looping. Plain -itsoffset needs none of that;
    # it only shifts one input's local timeline, sanitization included.
    _DEFAULT_SUBTITLE_SYNC_OFFSET_SECONDS = 1.0

    def _build_ffmpeg_command(self) -> List[str]:
        """Build the FFmpeg command for HLS broadcast output."""
        cmd = ["ffmpeg", "-y"]

        # Hardware acceleration for DECODING must come BEFORE -i (input options)
        # Only add if it's a valid value (not None, empty, or "none")
        if self.config.transcode:
            hwaccel = getattr(self.config, "hwaccel", None)
            if hwaccel and hwaccel.lower() not in ("none", ""):
                cmd.extend(["-hwaccel", hwaccel])

        def add_source_input(url: str, seek_seconds: float, *, realtime: bool) -> None:
            """Append an -i for `url`, matching this proxy's header/seek/reconnect
            conventions.

            `realtime` controls whether `-re` precedes this input. Pass False for
            a second, subtitle-only read of the same source: FFmpeg's `-re`
            real-time governor paces the shared demux loop against wall-clock
            time using EVERY mapped stream's packets, but a container's embedded
            subtitle packets are often extremely sparse/bursty — confirmed
            against a real broadcast, the governor's reported "lag" for the
            subtitle stream climbed without bound (0.6s -> 50s+ over under a
            minute), and since the demux loop is shared, that dragged video/audio
            segment output down to a crawl too, even though only the subtitle
            stream was the one falling behind. Reading the embedded subtitle
            track from its own un-paced second input sidesteps the governor
            entirely — verified against the same real broadcast, segment cadence
            returned to normal immediately. This mirrors the external-subtitle
            input below, which was never `-re`-throttled in the first place and
            never hit this bug.
            """
            if seek_seconds > 0:
                cmd.extend(["-ss", str(seek_seconds)])
            if realtime:
                cmd.append("-re")
            cmd.extend(
                [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "10",
                ]
            )

            # If headers are provided explicitly in the BroadcastConfig, prefer them.
            if (
                getattr(self.config, "headers", None)
                and isinstance(self.config.headers, dict)
                and isinstance(url, str)
                and ("://" in url and not url.startswith("file://"))
            ):
                try:
                    headers = []
                    for hk, hv in self.config.headers.items():
                        # sanitize header names/values
                        k = str(hk).replace("\r", "").replace("\n", "").strip()
                        v = str(hv).replace("\r", "").replace("\n", "").strip()
                        if not k:
                            continue
                        headers.append(f"{k}: {v}")

                    if headers:
                        header_str = "\r\n".join(headers) + "\r\n"
                        cmd.extend(["-headers", header_str, "-i", url])
                        return
                except Exception as e:
                    logger.warning(f"Failed to construct headers for FFmpeg input: {e}")

            cmd.extend(["-i", url])

        # Primary input: video + audio (+ embedded subtitle, when nothing forces
        # a second input for it). Real-time paced, since this governs the actual
        # broadcast's live pacing.
        add_source_input(
            self.config.stream_url, self.config.seek_seconds, realtime=True
        )

        # External subtitle as a second FFmpeg input (Emby sidecar file).
        # The seek offset is applied per-input: when subtitle_seek_seconds > 0 the
        # subtitle file is a full-file fetch that needs local -ss to align with the
        # video's timeline; when 0 the media server already rebased the cues (Emby's
        # startPositionTicks path segment) and the proxy must NOT re-seek.
        has_external_subtitle = bool(
            getattr(self.config, "subtitle_url", None)
            and self.config.subtitle_url.strip()
        )
        # Type-relative input index the subtitle -map should address. Embedded
        # subtitles need their own second input (see add_source_input's realtime
        # note) whenever there's no external subtitle already covering it.
        subtitle_input_index = 0
        if has_external_subtitle:
            sub_seek = getattr(self.config, "subtitle_seek_seconds", 0.0)
            if sub_seek and sub_seek > 0:
                cmd.extend(["-ss", str(sub_seek)])
            cmd.extend(["-i", self.config.subtitle_url])
        elif self.config.subtitles_enabled:
            # See _DEFAULT_SUBTITLE_SYNC_OFFSET_SECONDS above: cancels the
            # sync lag confirmed live between this input's cues and the
            # dialogue they belong to. Overridable per-deployment since the
            # exact gap isn't necessarily universal.
            sync_offset = getattr(
                settings,
                "BROADCAST_SUBTITLE_SYNC_OFFSET_SECONDS",
                self._DEFAULT_SUBTITLE_SYNC_OFFSET_SECONDS,
            )
            cmd.extend(["-itsoffset", str(sync_offset)])
            add_source_input(
                self.config.stream_url, self.config.seek_seconds, realtime=False
            )
            subtitle_input_index = 1

        # Duration limiting for programme boundary
        if self.config.duration_seconds > 0:
            cmd.extend(["-t", str(self.config.duration_seconds)])

        # Stream mapping — video (always), audio (by language or first), subtitles (optional)
        # Video and audio are optional (? suffix) to support audio-only and video-only streams.
        cmd.extend(["-map", "0:v:0?"])

        if self.config.preferred_audio_language:
            lang = self.config.preferred_audio_language.strip()
            if lang.isdigit():
                # A per-item override (see NetworkBroadcastService::resolveTrackPreference
                # on the editor side) resolves to the exact type-relative stream position
                # (e.g. "1" = the 2nd audio stream) rather than a language — precise, and
                # a plain index specifier degrades gracefully via '?' if the position no
                # longer exists, unlike the metadata form below.
                cmd.extend(["-map", f"0:a:{lang}?"])
            else:
                # NOTE: no trailing '?' here. FFmpeg's metadata-based stream specifier
                # (`m:key:value`) does not support the optional-map suffix at all — it
                # fails "Invalid argument" whether or not a stream actually matches, not
                # just when the match is empty (confirmed against a real FFmpeg build).
                # Appending '?' to `0:a:0` (a plain index specifier) is fine; appending it
                # to `0:a:m:language:XX` is not. Without it, this succeeds immediately
                # when the language matches, and fails cleanly (caught and retried by
                # _retry_without_broken_language_maps()) only when it genuinely doesn't.
                cmd.extend(["-map", f"0:a:m:language:{lang}"])
        else:
            cmd.extend(["-map", "0:a:0?"])

        # Subtitle mapping: external sidecar (input 1:s) takes precedence over
        # embedded so we don't duplicate subtitle tracks when both exist. Embedded
        # subtitles are read from their own un-throttled second input (see
        # add_source_input's realtime note above) at subtitle_input_index, NOT
        # from the primary input — mapping them from input 0 would re-subject
        # them to the -re governor's shared demux loop and reintroduce the stall.
        # Embedded subtitles are also language-aware (mirroring the audio mapping
        # above) when subtitle_language is set, so a per-item override can pick a
        # specific embedded track out of several rather than always the first one.
        if has_external_subtitle:
            cmd.extend(["-map", "1:s:0?"])
        elif self.config.subtitles_enabled:
            sub_lang = getattr(self.config, "subtitle_language", None)
            sub_prefix = f"{subtitle_input_index}:s"
            if sub_lang and sub_lang.strip():
                sub_lang = sub_lang.strip()
                if sub_lang.isdigit():
                    # Per-item override — exact type-relative subtitle stream position.
                    # See the audio map above for why this uses a plain (gracefully
                    # optional) index specifier instead of the metadata form.
                    cmd.extend(["-map", f"{sub_prefix}:{sub_lang}?"])
                else:
                    # No trailing '?' — see the audio map above for why.
                    cmd.extend(["-map", f"{sub_prefix}:m:language:{sub_lang}"])
            else:
                cmd.extend(["-map", f"{sub_prefix}?"])

        # Codec selection
        if self.config.transcode:
            # Video codec selection (allow explicit codec like libx264 or h264_nvenc)
            # Only applied when a video stream is actually present (0:v:0? maps nothing for audio-only)
            video_codec = self.config.video_codec or "libx264"
            cmd.extend(["-c:v", video_codec])

            # Preset - default to 'veryfast' for real-time encoding if not specified
            # This is critical for avoiding encoding bottlenecks that cause audio drift
            preset = getattr(self.config, "preset", None) or "veryfast"
            cmd.extend(["-preset", preset])

            if self.config.video_bitrate:
                cmd.extend(["-b:v", f"{self.config.video_bitrate}k"])

            # Build a single -vf chain: FFmpeg only honours the last -vf, so
            # deinterlace (yadif) and scale must be combined rather than passed
            # as two separate flags.
            video_filters = []
            if getattr(self.config, "deinterlace", False):
                video_filters.append("yadif")
            if self.config.video_resolution:
                video_filters.append(f"scale={self.config.video_resolution}")
            if video_filters:
                cmd.extend(["-vf", ",".join(video_filters)])

            # Audio codec and bitrate
            audio_codec = self.config.audio_codec or "aac"
            cmd.extend(["-c:a", audio_codec, "-b:a", f"{self.config.audio_bitrate}k"])

            # Force standard broadcast audio settings to prevent sample rate mismatches
            # that cause "deep/slow" audio playback issues
            cmd.extend(["-ar", "48000"])  # 48kHz is standard for broadcast
            cmd.extend(["-ac", "2"])  # Force stereo output
        else:
            cmd.extend(["-c:v", "copy", "-c:a", "copy"])
            # Subtitle codec is deliberately left unspecified here, even in this
            # video/audio-passthrough branch: HLS's .vtt segments must actually be
            # WebVTT. `-c:s copy` was tried here previously, under the assumption
            # that it "preserves" the source subtitle format the same way -c:v/-c:a
            # copy do — but a source in SRT/ASS copied as-is produces bytes that
            # merely look like WebVTT (SRT's comma decimal separator instead of a
            # period, no "WEBVTT" header, etc.), which some players parse leniently
            # enough to render the very first cue and then silently drop every
            # cue after it — the exact "one line of dialogue, then nothing" bug
            # reported live. Leaving -c:s unset lets FFmpeg's HLS muxer apply its
            # own default (real webvtt transcoding) regardless of the video/audio
            # copy mode, identical to what already happens in the transcode branch
            # above.

        # Tag the external subtitle's language so the HLS variant carries metadata
        # (DEFAULT=NO/AUTOSELECT=YES is flipped by NetworkHlsController on the Laravel
        # side so the subtitle is available-but-not-forced).
        if has_external_subtitle and getattr(self.config, "subtitle_language", None):
            cmd.extend(["-metadata:s:s:0", f"language={self.config.subtitle_language}"])

        # HLS output configuration
        cmd.extend(["-f", "hls"])
        cmd.extend(["-hls_time", str(self.config.segment_duration)])
        # DVR mode: hls_list_size=0 keeps all segments in the manifest for concat
        hls_list_size = 0 if self.config.dvr_mode else self.config.hls_list_size
        cmd.extend(["-hls_list_size", str(hls_list_size)])
        cmd.extend(["-start_number", str(self.config.segment_start_number)])

        # HLS flags — DVR mode keeps all segments for post-processing concat
        hls_flags = [
            "program_date_time",
            "omit_endlist",
            "independent_segments",
        ]
        if not self.config.dvr_mode:
            # Rolling-window live broadcasts delete old segments to save space
            hls_flags.insert(0, "delete_segments")
        if self.config.add_discontinuity:
            hls_flags.append("discont_start")
        cmd.extend(["-hls_flags", "+".join(hls_flags)])

        # Segment filename template (6-digit zero-padded)
        segment_pattern = os.path.join(self.hls_dir, "live%06d.ts")
        cmd.extend(["-hls_segment_filename", segment_pattern])

        # The negative -itsoffset above can produce a negative starting
        # timestamp on the subtitle input; this keeps the HLS muxer from
        # choking on it at the very start of the broadcast.
        if subtitle_input_index == 1:
            cmd.extend(["-avoid_negative_ts", "make_zero"])

        # Output playlist
        playlist_path = os.path.join(self.hls_dir, "live.m3u8")
        cmd.append(playlist_path)

        return cmd

    # Matches FFmpeg's option-parsing failure for a `-map` value that matches zero
    # streams, e.g. Failed to set value '0:a:m:language:eng' for option 'map': Invalid argument
    _MAP_FAILURE_PATTERN = re.compile(r"Failed to set value '([^']*)' for option 'map'")

    # FFmpeg's HLS output defaults to WebVTT for any mapped subtitle stream, which
    # only supports text-to-text conversion — a bitmap format (PGS/VobSub, common on
    # Blu-ray rips) hits this and crashes outright, confirmed against a real
    # broadcast. Unlike the map-failure pattern above, this message doesn't quote
    # which stream caused it, so there's nothing more specific to clear than
    # "subtitles" as a whole.
    _SUBTITLE_ENCODING_FAILURE = "subtitle encoding currently only possible from text to text or bitmap to bitmap"

    async def _retry_without_broken_language_maps(
        self,
    ) -> asyncio.subprocess.Process:
        """
        Give self.process a brief window to fail near-instantly, before any real
        encoding, on either of two known-recoverable FFmpeg failures, and retry
        without whichever part caused it:

        1. A `-map 0:X:m:language:YY` metadata specifier matching zero streams of
           type X (FFmpeg's option parsing rejects this outright rather than
           degrading gracefully) — clears whichever of preferred_audio_language /
           subtitle_language caused it, read from FFmpeg's own error message
           (which quotes the exact map spec) rather than assumed.
        2. A mapped subtitle stream that's a bitmap format FFmpeg's WebVTT encoder
           can't convert — disables subtitles entirely, since the failure message
           doesn't identify which stream caused it.

        Loops up to three times since a single broadcast can hit more than one of
        these across the audio map, the subtitle map, and the subtitle codec — each
        failure only reveals one broken piece at a time.
        """
        for _ in range(3):
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                return self.process

            stderr = b""
            if self.process.stderr:
                stderr = await self.process.stderr.read()
            stderr_text = stderr.decode(errors="ignore")

            if self._SUBTITLE_ENCODING_FAILURE in stderr_text.lower():
                logger.warning(
                    f"Broadcast {self.network_id}: mapped subtitle stream is a bitmap "
                    "format (e.g. PGS/VobSub) that FFmpeg cannot convert to WebVTT; "
                    "disabling subtitles for this broadcast."
                )
                self.config.subtitles_enabled = False
                self.config.subtitle_language = None
            else:
                match = self._MAP_FAILURE_PATTERN.search(stderr_text)
                if not match:
                    # Exited for some other reason — leave it for the normal failure path.
                    return self.process

                bad_map = match.group(1)

                if bad_map.startswith("0:a:") and self.config.preferred_audio_language:
                    logger.warning(
                        f"Broadcast {self.network_id}: preferred audio language "
                        f"'{self.config.preferred_audio_language}' matched no audio "
                        f"stream in the source (FFmpeg rejected '{bad_map}'); retrying "
                        "with the default audio track."
                    )
                    self.config.preferred_audio_language = None
                elif (
                    # Matches "N:s:..." for any input index N, not just "0:s:" —
                    # embedded subtitles are mapped from their own second,
                    # un-throttled input (see add_source_input's realtime note),
                    # so a genuine failure quotes "1:s:m:language:XX", not "0:s:...".
                    re.match(r"^\d+:s:", bad_map) and self.config.subtitle_language
                ):
                    logger.warning(
                        f"Broadcast {self.network_id}: subtitle language "
                        f"'{self.config.subtitle_language}' matched no subtitle stream "
                        f"in the source (FFmpeg rejected '{bad_map}'); retrying without "
                        "embedded subtitle language matching."
                    )
                    self.config.subtitle_language = None
                else:
                    # A map failure we don't have a specific fallback for — leave it for
                    # the normal failure path rather than retrying blindly.
                    return self.process

            cmd = self._build_ffmpeg_command()
            logger.info(f"Restarting broadcast {self.network_id}: {' '.join(cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )

        return self.process

    async def start(self) -> bool:
        """Start the FFmpeg broadcast process."""
        try:
            # Ensure HLS directory exists with proper permissions
            os.makedirs(self.hls_dir, exist_ok=True)
            try:
                os.chmod(self.hls_dir, 0o755)
            except Exception as e:
                logger.warning(f"Failed to set permissions on {self.hls_dir}: {e}")

            # On a fresh start (not a transition), remove any leftover segments/playlists
            # so FFmpeg's rolling-window deletion doesn't hit files it didn't write.
            if (
                self.config.segment_start_number == 0
                and not self.config.add_discontinuity
            ):
                stale_count = 0
                for filename in (
                    list(os.listdir(self.hls_dir))
                    if os.path.isdir(self.hls_dir)
                    else []
                ):
                    if filename.endswith((".ts", ".m3u8")):
                        try:
                            os.remove(os.path.join(self.hls_dir, filename))
                            stale_count += 1
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            logger.warning(
                                f"Broadcast {self.network_id}: could not remove stale file {filename}: {e}"
                            )
                if stale_count:
                    logger.info(
                        f"Broadcast {self.network_id}: removed {stale_count} stale file(s) before fresh start"
                    )

            cmd = self._build_ffmpeg_command()
            logger.info(f"Starting broadcast {self.network_id}: {' '.join(cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )

            # Give the process a brief window to hit either of two known-recoverable
            # failures — a `-map 0:X:m:language:YY` matching zero streams, or a mapped
            # subtitle stream FFmpeg's WebVTT encoder can't convert (bitmap formats) —
            # and retry without whichever part caused it, so the broadcast still
            # starts instead of failing outright. subtitles_enabled covers the plain
            # `-map 0:s?` case (no language/position preference) too, since that can
            # still hit a bitmap-only subtitle stream in Local (transcode) mode.
            if (
                self.config.preferred_audio_language
                or self.config.subtitle_language
                or self.config.subtitles_enabled
            ):
                self.process = await self._retry_without_broken_language_maps()

            self.started_at = datetime.now(timezone.utc)
            self.status = "running"

            # Start monitoring tasks
            self._stderr_task = asyncio.create_task(self._log_stderr())
            self._monitor_task = asyncio.create_task(self._monitor_process())
            self._poll_task = asyncio.create_task(self._poll_bytes())

            logger.info(
                f"Broadcast {self.network_id} started with PID {self.process.pid}"
            )
            return True

        except Exception as e:
            self.status = "failed"
            self.error_message = str(e)
            logger.error(f"Failed to start broadcast {self.network_id}: {e}")
            return False

    async def stop(self, graceful: bool = True) -> int:
        """
        Stop the FFmpeg process.

        Args:
            graceful: If True, send SIGTERM and wait; if False, send SIGKILL immediately.

        Returns:
            The final segment number.
        """
        self._stopping = True
        self.status = "stopping"

        if self.process and self.process.returncode is None:
            try:
                if graceful:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Broadcast {self.network_id} did not terminate gracefully, killing"
                        )
                        self.process.kill()
                        await self.process.wait()
                else:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass  # Process already dead

        # Cancel monitoring tasks
        for task in [self._monitor_task, self._stderr_task, self._poll_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Get final segment number from files
        final_segment = self._get_final_segment_number()
        self.current_segment_number = final_segment
        self.status = "stopped"

        logger.info(
            f"Broadcast {self.network_id} stopped, final segment: {final_segment}"
        )
        return final_segment

    # Patterns to skip in FFmpeg output (verbose/noisy messages)
    SKIP_LOG_PATTERNS = [
        "frame=",  # Progress output
        "fps=",  # FPS stats
        "time=",  # Time stats
        "bitrate=",  # Bitrate stats
        "speed=",  # Speed stats
        # Size stats (N/A for HLS stream-copy; tracked via segment polling)
        "size=",
        "resumed reading",  # Reconnection noise
        "opening",  # File opening messages (lowercase)
        "muxing overhead",  # Summary stats
        "video:",  # Summary stats
        "audio:",  # Summary stats
    ]

    async def _log_stderr(self):
        """Monitor FFmpeg stderr for errors only. Suppresses verbose output."""
        if not self.process or not self.process.stderr:
            return

        buf = b""
        try:
            while self.process.returncode is None:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    break

                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="ignore").strip()
                    if not line_str:
                        continue

                    line_lower = line_str.lower()

                    # Check for input errors
                    is_input_error = any(
                        p in line_lower for p in self.INPUT_ERROR_PATTERNS
                    )
                    if is_input_error:
                        # Some error patterns are non-fatal (e.g. segment already deleted)
                        is_suppressed = any(
                            p in line_lower for p in self.INPUT_ERROR_SUPPRESSIONS
                        )
                        if is_suppressed:
                            logger.warning(
                                f"Broadcast {self.network_id} (non-fatal): {line_str}"
                            )
                            continue

                        self.error_message = line_str
                        self.status = "failed"
                        logger.error(f"Broadcast {self.network_id} error: {line_str}")
                        await self._send_callback(
                            "broadcast_failed",
                            {"error": line_str, "error_type": "input_error"},
                        )
                        return

                    # Skip verbose/noisy messages entirely
                    should_skip = any(
                        pattern in line_lower for pattern in self.SKIP_LOG_PATTERNS
                    )
                    if should_skip:
                        continue

                    # Log warnings and errors only
                    if (
                        "error" in line_lower
                        or "warning" in line_lower
                        or "failed" in line_lower
                    ):
                        logger.warning(f"Broadcast {self.network_id}: {line_str}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading FFmpeg stderr for {self.network_id}: {e}")

    async def _monitor_process(self):
        """Monitor FFmpeg process and send callback when it exits."""
        if not self.process:
            return

        try:
            await self.process.wait()

            # Skip callback on intentional stop — the editor initiated it and handles
            # post-processing directly without waiting for a proxy callback.
            if self._stopping:
                return

            # Determine final segment number
            final_segment = self._get_final_segment_number()
            self.current_segment_number = final_segment

            # Calculate duration streamed
            duration_streamed = 0.0
            if self.started_at:
                duration_streamed = (
                    datetime.now(timezone.utc) - self.started_at
                ).total_seconds()

            exit_code = self.process.returncode
            if exit_code == 0:
                # Normal completion (duration limit reached) or intentional DVR stop
                self.status = "stopped"
                await self._send_callback(
                    "programme_ended",
                    {
                        "exit_code": exit_code,
                        "final_segment_number": final_segment,
                        "duration_streamed": duration_streamed,
                    },
                )
            else:
                # Abnormal exit
                self.status = "failed"
                self.error_message = f"FFmpeg exited with code {exit_code}"
                await self._send_callback(
                    "broadcast_failed",
                    {
                        "exit_code": exit_code,
                        "final_segment_number": final_segment,
                        "duration_streamed": duration_streamed,
                        "error": self.error_message,
                    },
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error monitoring broadcast {self.network_id}: {e}")

    async def _send_callback(self, event: str, data: dict):
        """Send webhook callback to Laravel."""
        if not self.config.callback_url:
            logger.debug(
                f"No callback URL for broadcast {self.network_id}, skipping callback"
            )
            return

        payload = {
            "network_id": self.network_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dvr_mode": self.config.dvr_mode,
            "hls_dir": self.hls_dir if self.config.dvr_mode else None,
            "data": data,
        }

        try:
            timeout = getattr(settings, "BROADCAST_CALLBACK_TIMEOUT", 10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.config.callback_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "M3U-Proxy-Broadcast/1.0",
                    },
                )
                if response.status_code >= 400:
                    logger.warning(
                        f"Callback to {self.config.callback_url} failed with status {response.status_code}"
                    )
                else:
                    logger.info(
                        f"Callback sent for broadcast {self.network_id}: {event}"
                    )
        except Exception as e:
            logger.error(f"Error sending callback for broadcast {self.network_id}: {e}")

    async def _poll_bytes(self, interval: float = 1.0) -> None:
        """
        Track cumulative bytes by watching for new .ts segment files.

        FFmpeg's progress output reports ``size=N/A`` for HLS stream-copy output,
        so we can't use stderr parsing. Instead, we scan the HLS directory every
        ``interval`` seconds. Each new segment file we haven't seen before is
        measured and added to ``_bytes_written``.

        For DVR broadcasts the files are never deleted, so every segment is counted
        exactly once. For non-DVR broadcasts FFmpeg deletes old segments via its
        ``delete_segments`` flag, but the rolling window keeps several segments on
        disk at any time (hls_list_size × hls_time seconds), giving us a comfortable
        window to measure each file before it disappears.
        """
        try:
            while not self._stopping:
                try:
                    if os.path.exists(self.hls_dir):
                        for filename in os.listdir(self.hls_dir):
                            if (
                                filename.endswith(".ts")
                                and filename not in self._seen_segments
                            ):
                                self._seen_segments.add(filename)
                                try:
                                    self._bytes_written += os.path.getsize(
                                        os.path.join(self.hls_dir, filename)
                                    )
                                except OSError:
                                    pass
                except Exception:
                    pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _get_bytes_written(self) -> int:
        """Return cumulative bytes written across all segments ever seen."""
        return self._bytes_written

    def _get_final_segment_number(self) -> int:
        """Get the highest segment number from existing files."""
        try:
            if not os.path.exists(self.hls_dir):
                return self.config.segment_start_number

            # Pattern: live000001.ts -> extract 000001
            pattern = re.compile(r"live(\d{6})\.ts$")
            max_segment = self.config.segment_start_number

            for filename in os.listdir(self.hls_dir):
                match = pattern.match(filename)
                if match:
                    segment_num = int(match.group(1))
                    max_segment = max(max_segment, segment_num)

            return max_segment
        except Exception as e:
            logger.error(
                f"Error getting final segment number for {self.network_id}: {e}"
            )
            return self.config.segment_start_number

    @staticmethod
    def parse_playlist_segments(playlist_path: str) -> Set[str]:
        """Return the set of .ts filenames referenced by an HLS playlist."""
        segments: Set[str] = set()
        try:
            with open(playlist_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        segments.add(os.path.basename(line))
        except Exception:
            pass
        return segments

    def cleanup_orphaned_segments(self, age_threshold: int = 0) -> int:
        """
        Remove .ts files in the HLS dir that are not referenced by the current playlist.

        Args:
            age_threshold: Only remove files older than this many seconds.
                           0 = remove immediately (used during programme transitions).

        Returns:
            Number of files removed.
        """
        playlist_path = os.path.join(self.hls_dir, "live.m3u8")
        if not os.path.exists(playlist_path):
            return 0

        referenced = self.parse_playlist_segments(playlist_path)
        removed = 0
        now = time.time()

        try:
            for filename in os.listdir(self.hls_dir):
                if not filename.endswith(".ts"):
                    continue
                if filename in referenced:
                    continue

                full_path = os.path.join(self.hls_dir, filename)
                if age_threshold > 0:
                    try:
                        if now - os.path.getmtime(full_path) < age_threshold:
                            continue
                    except OSError:
                        continue

                try:
                    os.remove(full_path)
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning(
                        f"Broadcast {self.network_id}: could not remove orphaned segment {filename}: {e}"
                    )
        except Exception as e:
            logger.error(
                f"Broadcast {self.network_id}: error during orphan cleanup: {e}"
            )

        if removed:
            logger.info(
                f"Broadcast {self.network_id}: removed {removed} orphaned segment(s)"
            )
        return removed

    def get_status(self) -> BroadcastStatus:
        """Get current broadcast status."""
        return BroadcastStatus(
            network_id=self.network_id,
            status=self.status,
            current_segment_number=self._get_final_segment_number(),
            started_at=self.started_at.isoformat() if self.started_at else None,
            stream_url=self.config.stream_url,
            hls_dir=self.hls_dir,
            ffmpeg_pid=self.process.pid if self.process else None,
            error_message=self.error_message,
            metadata=self.config.metadata,
            bytes_written=self._get_bytes_written(),
        )

    def get_playlist_path(self) -> Optional[str]:
        """Get path to the HLS playlist file."""
        path = os.path.join(self.hls_dir, "live.m3u8")
        return path if os.path.exists(path) else None

    def get_segment_path(self, filename: str) -> Optional[str]:
        """Get path to a specific segment or sub-playlist file.

        Covers .ts (video segments), .vtt (WebVTT subtitle segments), and .m3u8
        (the subtitle sub-playlist FFmpeg auto-derives, and the video sub-playlist
        the editor's hls-variant route also fetches through this same endpoint) —
        not just .ts, which this used to hard-code to before subtitle support.
        """
        # Sanitize filename to prevent directory traversal
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith((".ts", ".vtt", ".m3u8")):
            return None

        path = os.path.join(self.hls_dir, safe_filename)
        return path if os.path.exists(path) else None


class BroadcastManager:
    """
    Manages multiple network broadcasts.

    Coordinates:
    - Starting/stopping broadcasts
    - Programme transitions with segment continuity
    - HLS directory lifecycle
    """

    def __init__(self, hls_base_dir: Optional[str] = None):
        self.hls_base_dir = hls_base_dir or getattr(
            settings, "HLS_BROADCAST_DIR", "/tmp/m3u-proxy-broadcasts"
        )
        self.dvr_base_dir: str = getattr(
            settings, "DVR_RECORDING_DIR", "/tmp/m3u-proxy-dvr"
        )
        self.broadcasts: Dict[str, NetworkBroadcastProcess] = {}
        self._lock = asyncio.Lock()

        # Start attempt tracking (avoid infinite restart loops)
        # Map network_id -> {count: int, first_attempt_at: float}
        self._start_attempts: Dict[str, dict] = {}

        # Configuration (can be overridden via settings)
        self.MAX_START_RETRIES = int(
            getattr(settings, "BROADCAST_MAX_START_RETRIES", 3)
        )
        self.START_RETRY_WINDOW = float(
            getattr(settings, "BROADCAST_START_RETRY_WINDOW", 300.0)
        )
        self.START_RETRY_COOLDOWN = float(
            getattr(settings, "BROADCAST_START_RETRY_COOLDOWN", 15.0)
        )
        self.START_FAILURE_GRACE = float(
            getattr(settings, "BROADCAST_START_FAILURE_GRACE", 2.0)
        )

        # Broadcast GC configuration (reuses HLS_GC_* thresholds)
        self.broadcast_gc_enabled = bool(
            getattr(settings, "BROADCAST_GC_ENABLED", True)
        )
        self.broadcast_gc_interval = int(getattr(settings, "HLS_GC_INTERVAL", 600))
        self.broadcast_gc_age_threshold = int(
            getattr(settings, "HLS_GC_AGE_THRESHOLD", 3600)
        )
        self._gc_task: Optional[asyncio.Task] = None

        # Ensure base directory exists
        os.makedirs(self.hls_base_dir, exist_ok=True)
        logger.info(f"BroadcastManager initialized with base dir: {self.hls_base_dir}")

    async def start_broadcast(self, config: BroadcastConfig) -> BroadcastStatus:
        """
        Start or transition a network broadcast.

        If a broadcast is already running for this network, it will be stopped
        gracefully and the new broadcast will continue with the next segment number.
        """
        async with self._lock:
            network_id = config.network_id

            # Check if broadcast already running
            if network_id in self.broadcasts:
                existing = self.broadcasts[network_id]
                logger.info(f"Transitioning broadcast {network_id} to new programme")

                # Stop existing process gracefully
                final_segment = await existing.stop(graceful=True)

                # Auto-continue segment numbering if not specified
                if config.segment_start_number == 0:
                    config.segment_start_number = final_segment + 1
                    # Force discontinuity on transition
                    config.add_discontinuity = True

                # Clean up segments no longer referenced by the playlist before handing
                # off to the new FFmpeg process. The playlist itself is left in place so
                # the new process can overwrite it with a discontinuity marker.
                existing.cleanup_orphaned_segments(age_threshold=0)

                del self.broadcasts[network_id]

            # Create and start new process
            # DVR recordings use a dedicated directory separate from live broadcasts
            base_dir = (
                getattr(settings, "DVR_RECORDING_DIR", "/tmp/m3u-proxy-dvr")
                if config.dvr_mode
                else self.hls_base_dir
            )
            process = NetworkBroadcastProcess(config, base_dir)

            # Check start retry policy
            now = time.time()
            attempts = self._start_attempts.get(network_id)
            if attempts:
                # reset window if expired
                if (
                    now - attempts.get("first_attempt_at", now)
                    > self.START_RETRY_WINDOW
                ):
                    attempts = None
                    del self._start_attempts[network_id]

            # If we've hit the max retries, check cooldown period to allow automatic retry
            if attempts and attempts.get("count", 0) >= self.MAX_START_RETRIES:
                last = attempts.get(
                    "last_attempt_at", attempts.get("first_attempt_at", now)
                )
                # If cooldown elapsed, clear attempts and allow retry
                if now - last >= self.START_RETRY_COOLDOWN:
                    logger.info(
                        f"Cooldown elapsed for broadcast {network_id}; resetting start retry counter and allowing automatic start."
                    )
                    del self._start_attempts[network_id]
                    attempts = None
                else:
                    seconds_left = int(self.START_RETRY_COOLDOWN - (now - last))
                    logger.error(
                        f"Exceeded max start retries ({self.MAX_START_RETRIES}) for broadcast {network_id}; refusing to start for another {seconds_left}s."
                    )
                    raise RuntimeError(
                        f"Exceeded max start retries for broadcast {network_id}; retry allowed after {seconds_left}s"
                    )

            success = await process.start()

            if not success:
                # Record failure immediately
                at = self._start_attempts.setdefault(
                    network_id,
                    {"count": 0, "first_attempt_at": now, "last_attempt_at": now},
                )
                at["count"] += 1
                at["last_attempt_at"] = now
                logger.warning(
                    f"Start attempt {at['count']} failed for {network_id}: {process.error_message}"
                )
                raise RuntimeError(
                    f"Failed to start broadcast: {process.error_message}"
                )

            # Add to active broadcasts and give a short grace period to detect immediate failures
            self.broadcasts[network_id] = process

            # Wait a small grace period to detect immediate startup failures (e.g., input errors)
            try:
                await asyncio.sleep(self.START_FAILURE_GRACE)
            except asyncio.CancelledError:
                pass

            # If process already failed within the grace period, treat as a start failure
            if process.status == "failed" or (
                process.process
                and process.process.returncode is not None
                and process.process.returncode != 0
            ):
                at = self._start_attempts.setdefault(
                    network_id,
                    {"count": 0, "first_attempt_at": now, "last_attempt_at": now},
                )
                at["count"] += 1
                at["last_attempt_at"] = now
                logger.warning(
                    f"Start attempt {at['count']} failed (post-start) for {network_id}: {process.error_message}"
                )

                # Clean up the failed process to avoid stale entries
                try:
                    await process.stop(graceful=False)
                except Exception:
                    pass
                if network_id in self.broadcasts:
                    del self.broadcasts[network_id]

                # If we've exceeded attempts, log an error (cooldown will be enforced on next start attempt)
                if at["count"] >= self.MAX_START_RETRIES:
                    logger.error(
                        f"Exceeded max start retries ({self.MAX_START_RETRIES}) for broadcast {network_id}; refusing further automatic starts until cooldown elapses."
                    )
                raise RuntimeError(
                    f"Broadcast {network_id} failed shortly after start: {process.error_message}"
                )

            # Successful start; clear any previous attempts
            if network_id in self._start_attempts:
                del self._start_attempts[network_id]

            return process.get_status()

    async def stop_broadcast(self, network_id: str) -> Optional[BroadcastStatus]:
        """Stop a network broadcast and clean up."""
        async with self._lock:
            if network_id not in self.broadcasts:
                return None

            process = self.broadcasts[network_id]
            await process.stop(graceful=True)

            status = process.get_status()
            del self.broadcasts[network_id]

            # Reset any tracked start attempts for this network since we stopped it manually
            if network_id in self._start_attempts:
                del self._start_attempts[network_id]

            return status

    def get_status(self, network_id: str) -> Optional[BroadcastStatus]:
        """Get current broadcast status."""
        if network_id not in self.broadcasts:
            return None
        return self.broadcasts[network_id].get_status()

    def get_all_statuses(self) -> Dict[str, BroadcastStatus]:
        """Get status of all active broadcasts."""
        return {
            network_id: process.get_status()
            for network_id, process in self.broadcasts.items()
        }

    async def read_playlist(self, network_id: str) -> Optional[str]:
        """Read the HLS playlist content for a network."""
        if network_id not in self.broadcasts:
            # Check if directory exists even without active broadcast (for recovery).
            # DVR recordings use dvr_base_dir; live broadcasts use hls_base_dir.
            for base_dir in (self.dvr_base_dir, self.hls_base_dir):
                playlist_path = os.path.join(
                    base_dir, f"broadcast_{network_id}", "live.m3u8"
                )
                if os.path.exists(playlist_path):
                    break
            else:
                return None
            if os.path.exists(playlist_path):
                try:
                    with open(playlist_path, "r") as f:
                        return f.read()
                except Exception as e:
                    logger.error(f"Error reading playlist for {network_id}: {e}")
            return None

        process = self.broadcasts[network_id]
        playlist_path = process.get_playlist_path()

        if not playlist_path:
            return None

        try:
            with open(playlist_path, "r") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading playlist for {network_id}: {e}")
            return None

    def get_segment_path(self, network_id: str, filename: str) -> Optional[str]:
        """Get path to a segment or sub-playlist file for a network — see
        NetworkBroadcastProcess.get_segment_path() for which extensions and why."""
        # Sanitize filename
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith((".ts", ".vtt", ".m3u8")):
            return None

        # Check active broadcast first
        if network_id in self.broadcasts:
            return self.broadcasts[network_id].get_segment_path(filename)

        # Check directory even without active broadcast.
        # DVR recordings use dvr_base_dir; live broadcasts use hls_base_dir.
        for base_dir in (self.dvr_base_dir, self.hls_base_dir):
            segment_path = os.path.join(
                base_dir, f"broadcast_{network_id}", safe_filename
            )
            if os.path.exists(segment_path):
                return segment_path
        return None

    async def cleanup_broadcast(self, network_id: str) -> bool:
        """Clean up broadcast directory and files."""
        async with self._lock:
            # Stop if running
            if network_id in self.broadcasts:
                await self.broadcasts[network_id].stop(graceful=False)
                del self.broadcasts[network_id]

            # Remove directory
            broadcast_dir = os.path.join(self.hls_base_dir, f"broadcast_{network_id}")
            if os.path.exists(broadcast_dir):
                try:
                    import shutil

                    shutil.rmtree(broadcast_dir)
                    logger.info(f"Cleaned up broadcast directory: {broadcast_dir}")
                    # Clear start attempts on successful cleanup
                    if network_id in self._start_attempts:
                        del self._start_attempts[network_id]
                    return True
                except Exception as e:
                    logger.error(f"Error cleaning up broadcast {network_id}: {e}")
                    return False

            return True

    async def start(self):
        """Start background tasks (GC loop)."""
        if self.broadcast_gc_enabled:
            self._gc_task = asyncio.create_task(self._gc_loop())
            logger.info(
                f"Broadcast GC started (interval={self.broadcast_gc_interval}s, "
                f"age_threshold={self.broadcast_gc_age_threshold}s)"
            )

    async def _gc_loop(self):
        """Periodically scan for and remove stale broadcast directories."""
        while True:
            try:
                await asyncio.sleep(self.broadcast_gc_interval)
                await self._gc_broadcast_dirs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Broadcast GC loop error: {e}")

    async def _gc_broadcast_dirs(self):
        """
        Two-phase broadcast GC:

        1. Active broadcasts — remove .ts files that are no longer referenced by the
           current playlist and are older than 60 seconds (guard against race with
           a concurrent transition writing new segments).
        2. Inactive directories — remove entire stale broadcast dirs (no active process,
           age > gc_age_threshold) using shutil.rmtree.
        """
        dirs_removed = skipped_too_young = 0

        # Snapshot active processes without holding the lock during I/O
        async with self._lock:
            active_snapshot = {
                hls_dir: process
                for hls_dir, process in (
                    (p.hls_dir, p) for p in self.broadcasts.values()
                )
            }

        # Phase 1: clean orphaned segments from active broadcasts
        for process in active_snapshot.values():
            process.cleanup_orphaned_segments(age_threshold=60)

        # Phase 2: remove entire stale inactive directories
        try:
            entries = os.listdir(self.hls_base_dir)
        except Exception as e:
            logger.error(f"Broadcast GC: cannot list {self.hls_base_dir}: {e}")
            return

        now = time.time()
        for entry in entries:
            if not entry.startswith("broadcast_"):
                continue

            full_path = os.path.join(self.hls_base_dir, entry)
            if not os.path.isdir(full_path):
                continue

            if full_path in active_snapshot:
                continue

            try:
                age = now - os.path.getmtime(full_path)
            except Exception:
                continue

            if age < self.broadcast_gc_age_threshold:
                skipped_too_young += 1
                continue

            try:
                shutil.rmtree(full_path)
                dirs_removed += 1
                logger.info(
                    f"Broadcast GC: removed stale directory {full_path} (age={age:.0f}s)"
                )
            except Exception as e:
                logger.error(f"Broadcast GC: failed to remove {full_path}: {e}")

        if dirs_removed or skipped_too_young:
            logger.info(
                f"Broadcast GC: dirs_removed={dirs_removed}, skipped_too_young={skipped_too_young}"
            )

    async def shutdown(self):
        """Stop all broadcasts gracefully."""
        logger.info("Shutting down BroadcastManager...")

        if self._gc_task and not self._gc_task.done():
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for network_id, process in list(self.broadcasts.items()):
                try:
                    await process.stop(graceful=True)
                except Exception as e:
                    logger.error(f"Error stopping broadcast {network_id}: {e}")

            self.broadcasts.clear()
        logger.info("BroadcastManager shutdown complete")


# Global instance (initialized in api.py lifespan)
broadcast_manager: Optional[BroadcastManager] = None
