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
    # Optional explicit codec/preset/hwaccel options (populated from Network transcode config)
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    preset: Optional[str] = None
    hwaccel: Optional[str] = None
    callback_url: Optional[str] = None
    # Optional custom headers to include when FFmpeg fetches the input URL
    headers: Optional[Dict[str, str]] = None


@dataclass
class BroadcastStatus:
    """Status of a running broadcast."""

    network_id: str
    status: str  # starting, running, stopping, stopped, failed
    current_segment_number: int
    started_at: Optional[str]
    stream_url: str
    ffmpeg_pid: Optional[int] = None
    error_message: Optional[str] = None


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

    # Lines matching INPUT_ERROR_PATTERNS are suppressed (not treated as fatal)
    # if they also match any of these patterns. For example, FFmpeg's HLS muxer
    # logs "failed to delete old segment ... No such file or directory" when
    # delete_segments can't find a segment — this is a non-fatal warning.
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
        self._stopping = False

    def _build_ffmpeg_command(self) -> List[str]:
        """Build the FFmpeg command for HLS broadcast output."""
        cmd = ["ffmpeg", "-y"]

        # Hardware acceleration for DECODING must come BEFORE -i (input options)
        # Only add if it's a valid value (not None, empty, or "none")
        if self.config.transcode:
            hwaccel = getattr(self.config, "hwaccel", None)
            if hwaccel and hwaccel.lower() not in ("none", ""):
                cmd.extend(["-hwaccel", hwaccel])

        # Input-level seeking (BEFORE -i for accuracy)
        if self.config.seek_seconds > 0:
            cmd.extend(["-ss", str(self.config.seek_seconds)])

        # Real-time pacing - critical for live streaming
        cmd.append("-re")

        # Reconnection options for network streams
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

        # Input URL
        # If headers are provided explicitly in the BroadcastConfig, prefer them.
        if (
            getattr(self.config, "headers", None)
            and isinstance(self.config.headers, dict)
            and isinstance(self.config.stream_url, str)
            and (
                "://" in self.config.stream_url
                and not self.config.stream_url.startswith("file://")
            )
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
                    cmd.extend(["-headers", header_str, "-i", self.config.stream_url])
                else:
                    cmd.extend(["-i", self.config.stream_url])
            except Exception as e:
                logger.warning(f"Failed to construct headers for FFmpeg input: {e}")
                cmd.extend(["-i", self.config.stream_url])

        # If no input has been added by the header logic above, append it as a plain -i.
        # This is a defensive measure to avoid malformed commands when headers are not
        # provided and the URL is a simple network resource (e.g., Emby/Jellyfin stream.ts).
        if "-i" not in cmd:
            cmd.extend(["-i", self.config.stream_url])

        # Duration limiting for programme boundary
        if self.config.duration_seconds > 0:
            cmd.extend(["-t", str(self.config.duration_seconds)])

        # Stream mapping - video + audio only (drop subtitles, data streams)
        # Use -map 0:a:0? to make audio optional (some streams may be video-only)
        cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

        # Codec selection
        if self.config.transcode:
            # Video codec selection (allow explicit codec like libx264 or h264_nvenc)
            video_codec = self.config.video_codec or "libx264"
            cmd.extend(["-c:v", video_codec])

            # Preset - default to 'veryfast' for real-time encoding if not specified
            # This is critical for avoiding encoding bottlenecks that cause audio drift
            preset = getattr(self.config, "preset", None) or "veryfast"
            cmd.extend(["-preset", preset])

            if self.config.video_bitrate:
                cmd.extend(["-b:v", f"{self.config.video_bitrate}k"])
            if self.config.video_resolution:
                cmd.extend(["-vf", f"scale={self.config.video_resolution}"])

            # Audio codec and bitrate
            audio_codec = self.config.audio_codec or "aac"
            cmd.extend(["-c:a", audio_codec, "-b:a", f"{self.config.audio_bitrate}k"])

            # Force standard broadcast audio settings to prevent sample rate mismatches
            # that cause "deep/slow" audio playback issues
            cmd.extend(["-ar", "48000"])  # 48kHz is standard for broadcast
            cmd.extend(["-ac", "2"])  # Force stereo output
        else:
            cmd.extend(["-c:v", "copy", "-c:a", "copy"])

        # HLS output configuration
        cmd.extend(["-f", "hls"])
        cmd.extend(["-hls_time", str(self.config.segment_duration)])
        cmd.extend(["-hls_list_size", str(self.config.hls_list_size)])
        cmd.extend(["-start_number", str(self.config.segment_start_number)])

        # HLS flags
        hls_flags = [
            "delete_segments",
            "program_date_time",
            "omit_endlist",
            "independent_segments",
        ]
        if self.config.add_discontinuity:
            hls_flags.append("discont_start")
        cmd.extend(["-hls_flags", "+".join(hls_flags)])

        # Segment filename template (6-digit zero-padded)
        segment_pattern = os.path.join(self.hls_dir, "live%06d.ts")
        cmd.extend(["-hls_segment_filename", segment_pattern])

        # Output playlist
        playlist_path = os.path.join(self.hls_dir, "live.m3u8")
        cmd.append(playlist_path)

        return cmd

    async def start(self) -> bool:
        """Start the FFmpeg broadcast process."""
        try:
            # Ensure HLS directory exists with proper permissions
            os.makedirs(self.hls_dir, exist_ok=True)
            try:
                os.chmod(self.hls_dir, 0o755)
            except Exception as e:
                logger.warning(f"Failed to set permissions on {self.hls_dir}: {e}")

            # Clean stale segment/playlist files before launching FFmpeg.
            # When starting fresh (start_number=0, no discontinuity), any pre-existing
            # .ts/.m3u8 files are leftovers from a previous run (e.g. after a container
            # reboot where the DELETE cleanup failed or was incomplete). If FFmpeg starts
            # writing at segment 0 with delete_segments enabled, its rolling-window
            # deletion will eventually try to remove segment files that it didn't write
            # itself, producing "failed to delete old segment" errors.
            # For programme transitions (start_number > 0), BroadcastManager.start_broadcast()
            # already calls cleanup_orphaned_segments() before creating this process.
            if self.config.segment_start_number == 0 and not self.config.add_discontinuity:
                stale_count = 0
                for filename in list(os.listdir(self.hls_dir)):
                    if filename.endswith((".ts", ".m3u8")):
                        try:
                            os.remove(os.path.join(self.hls_dir, filename))
                            stale_count += 1
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            logger.warning(
                                f"Failed to remove stale file {filename} from {self.hls_dir}: {e}"
                            )
                if stale_count:
                    logger.info(
                        f"Cleaned {stale_count} stale file(s) from {self.hls_dir} before fresh start"
                    )

            cmd = self._build_ffmpeg_command()
            logger.info(f"Starting broadcast {self.network_id}: {' '.join(cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )

            self.started_at = datetime.now(timezone.utc)
            self.status = "running"

            # Start monitoring tasks
            self._stderr_task = asyncio.create_task(self._log_stderr())
            self._monitor_task = asyncio.create_task(self._monitor_process())

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
        for task in [self._monitor_task, self._stderr_task]:
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
        "size=",  # Size stats
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

                    # Check for input errors - these are always logged
                    is_input_error = any(
                        pattern in line_lower
                        for pattern in self.INPUT_ERROR_PATTERNS
                    )
                    if is_input_error:
                        # Check if this is a suppressed (non-fatal) error
                        is_suppressed = any(
                            pattern in line_lower
                            for pattern in self.INPUT_ERROR_SUPPRESSIONS
                        )
                        if is_suppressed:
                            logger.warning(
                                f"Broadcast {self.network_id} (non-fatal): {line_str}"
                            )
                            continue

                        self.error_message = line_str
                        self.status = "failed"
                        logger.error(
                            f"Broadcast {self.network_id} error: {line_str}"
                        )
                        # Send failure callback
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

            # Don't send callback if we initiated the stop
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
                # Normal completion (duration limit reached)
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

    def get_status(self) -> BroadcastStatus:
        """Get current broadcast status."""
        return BroadcastStatus(
            network_id=self.network_id,
            status=self.status,
            current_segment_number=self._get_final_segment_number(),
            started_at=self.started_at.isoformat() if self.started_at else None,
            stream_url=self.config.stream_url,
            ffmpeg_pid=self.process.pid if self.process else None,
            error_message=self.error_message,
        )

    def get_playlist_path(self) -> Optional[str]:
        """Get path to the HLS playlist file."""
        path = os.path.join(self.hls_dir, "live.m3u8")
        return path if os.path.exists(path) else None

    def get_segment_path(self, filename: str) -> Optional[str]:
        """Get path to a specific segment file."""
        # Sanitize filename to prevent directory traversal
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith(".ts"):
            return None

        path = os.path.join(self.hls_dir, safe_filename)
        return path if os.path.exists(path) else None

    @staticmethod
    def parse_playlist_segments(playlist_path: str) -> Set[str]:
        """Parse an HLS playlist and return the set of referenced .ts segment filenames."""
        segments: Set[str] = set()
        try:
            if not os.path.exists(playlist_path):
                return segments
            with open(playlist_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # Segment lines are bare filenames like "live000042.ts"
                        segments.add(os.path.basename(line))
        except Exception as e:
            logger.warning(f"Failed to parse playlist {playlist_path}: {e}")
        return segments

    def cleanup_orphaned_segments(self, age_threshold: int = 0) -> int:
        """Remove .ts segment files not referenced by the current playlist.

        Args:
            age_threshold: Only remove segments older than this many seconds.
                           0 means no age check (used during transitions when FFmpeg is stopped).

        Returns the number of files removed.
        """
        playlist_path = os.path.join(self.hls_dir, "live.m3u8")
        referenced = self.parse_playlist_segments(playlist_path)

        removed = 0
        now = time.time()
        try:
            for filename in list(os.listdir(self.hls_dir)):
                if not filename.endswith(".ts"):
                    continue
                if filename not in referenced:
                    filepath = os.path.join(self.hls_dir, filename)
                    try:
                        if age_threshold > 0:
                            mtime = os.path.getmtime(filepath)
                            if now - mtime <= age_threshold:
                                continue
                        os.remove(filepath)
                        removed += 1
                    except FileNotFoundError:
                        pass  # Already removed between listdir and here
                    except OSError as e:
                        logger.warning(f"Failed to remove orphaned segment {filepath}: {e}")
        except Exception as e:
            logger.warning(f"Failed to scan broadcast dir {self.hls_dir}: {e}")

        if removed:
            logger.info(
                f"Cleaned {removed} orphaned segment(s) from broadcast {self.network_id}"
            )
        return removed


class BroadcastManager:
    """
    Manages multiple network broadcasts.

    Coordinates:
    - Starting/stopping broadcasts
    - Programme transitions with segment continuity
    - HLS directory lifecycle
    """

    def __init__(self, hls_base_dir: Optional[str] = None):
        # Resolve broadcast HLS directory:
        # 1. Explicit hls_base_dir argument (highest priority)
        # 2. HLS_BROADCAST_DIR env var / setting (if explicitly set)
        # 3. HLS_TEMP_DIR + "/broadcasts" (derive from transcoding temp dir)
        # 4. /tmp/m3u-proxy-broadcasts (final fallback)
        hls_temp = getattr(settings, "HLS_TEMP_DIR", None)
        self.hls_base_dir = (
            hls_base_dir
            or getattr(settings, "HLS_BROADCAST_DIR", None)
            or (os.path.join(hls_temp, "broadcasts") if hls_temp else None)
            or "/tmp/m3u-proxy-broadcasts"
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

        # Broadcast GC configuration
        self.gc_enabled = bool(
            getattr(settings, "BROADCAST_GC_ENABLED", True)
        )
        self.gc_interval = int(
            getattr(settings, "BROADCAST_GC_INTERVAL", 300)
        )
        self.gc_age_threshold = int(
            getattr(settings, "BROADCAST_GC_AGE_THRESHOLD", 600)
        )
        self._gc_task: Optional[asyncio.Task] = None
        self._running = False

        # Ensure base directory exists
        os.makedirs(self.hls_base_dir, exist_ok=True)
        logger.info(f"BroadcastManager initialized with base dir: {self.hls_base_dir}")

    async def start(self):
        """Start the broadcast manager background tasks (GC loop)."""
        self._running = True
        if self.gc_enabled:
            self._gc_task = asyncio.create_task(self._gc_loop())
            logger.info(
                f"Broadcast GC enabled: interval={self.gc_interval}s, "
                f"age_threshold={self.gc_age_threshold}s"
            )
        else:
            logger.info("Broadcast GC disabled")

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

                # Clean up orphaned segments from the previous FFmpeg process.
                # The old FFmpeg's delete_segments flag only managed its OWN playlist
                # window. When it exits, ~hls_list_size segments remain on disk that the
                # new FFmpeg process will never reference or clean up. Remove all .ts files
                # not listed in the current playlist so they don't accumulate across
                # programme transitions.
                existing.cleanup_orphaned_segments()

                del self.broadcasts[network_id]

            # Create and start new process
            process = NetworkBroadcastProcess(config, self.hls_base_dir)

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
            # Check if directory exists even without active broadcast (for recovery)
            playlist_path = os.path.join(
                self.hls_base_dir, f"broadcast_{network_id}", "live.m3u8"
            )
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
        """Get path to a segment file for a network."""
        # Sanitize filename
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith(".ts"):
            return None

        # Check active broadcast first
        if network_id in self.broadcasts:
            return self.broadcasts[network_id].get_segment_path(filename)

        # Check directory even without active broadcast
        segment_path = os.path.join(
            self.hls_base_dir, f"broadcast_{network_id}", safe_filename
        )
        return segment_path if os.path.exists(segment_path) else None

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

    async def _gc_loop(self):
        """Periodic garbage collection loop for broadcast directories."""
        while self._running:
            try:
                await asyncio.sleep(self.gc_interval)
                if not self._running:
                    break
                await self._gc_broadcast_dirs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Broadcast GC loop error: {e}")

    async def _gc_broadcast_dirs(self):
        """Scan broadcast base directory and clean up orphaned segments and stale dirs.

        For ACTIVE broadcasts: remove .ts files not referenced by the current playlist.
        For INACTIVE broadcasts (no matching entry in self.broadcasts): remove the entire
        directory if it is older than gc_age_threshold (crash recovery).
        """
        try:
            try:
                entries = os.listdir(self.hls_base_dir)
            except Exception as e:
                logger.warning(f"Failed to list broadcast base dir {self.hls_base_dir}: {e}")
                return

            now = time.time()
            prefix = "broadcast_"
            removed_segments = 0
            removed_dirs = 0
            skipped_active = 0
            skipped_too_young = 0

            # Snapshot active network IDs under lock, then release immediately.
            # We intentionally do NOT hold the lock during I/O to avoid blocking
            # start/stop operations. The 60-second age check on segment deletion
            # and playlist re-validation before each delete provide safety against
            # races with concurrent programme transitions.
            async with self._lock:
                active_network_ids = set(self.broadcasts.keys())

            for entry in entries:
                if not entry.startswith(prefix):
                    continue

                full_path = os.path.join(self.hls_base_dir, entry)
                if not os.path.isdir(full_path):
                    continue

                # Extract network_id from directory name (broadcast_{network_id})
                network_id = entry[len(prefix):]

                if network_id in active_network_ids:
                    # Active broadcast — clean orphaned segments not in current playlist
                    skipped_active += 1
                    playlist_path = os.path.join(full_path, "live.m3u8")
                    referenced = NetworkBroadcastProcess.parse_playlist_segments(playlist_path)

                    if not referenced:
                        # No playlist or empty playlist — don't delete segments from
                        # an active broadcast that may still be starting up
                        continue

                    for filename in list(os.listdir(full_path)):
                        if not filename.endswith(".ts"):
                            continue
                        if filename not in referenced:
                            filepath = os.path.join(full_path, filename)
                            try:
                                # Only remove segments older than 60s to avoid
                                # deleting segments that FFmpeg just wrote but
                                # hasn't added to the playlist yet
                                mtime = os.path.getmtime(filepath)
                                if now - mtime > 60:
                                    # Re-read the playlist before deleting to guard
                                    # against races with programme transitions that
                                    # may have rewritten the playlist since our
                                    # initial read.
                                    fresh_referenced = NetworkBroadcastProcess.parse_playlist_segments(playlist_path)
                                    if filename not in fresh_referenced:
                                        os.remove(filepath)
                                        removed_segments += 1
                            except FileNotFoundError:
                                pass  # Already removed between listdir and here
                            except OSError:
                                pass
                else:
                    # Inactive broadcast — check age and remove entire directory
                    try:
                        mtime = os.path.getmtime(full_path)
                    except FileNotFoundError:
                        continue  # Directory removed between listdir and here
                    except Exception:
                        continue

                    age = now - mtime
                    if age > self.gc_age_threshold:
                        try:
                            shutil.rmtree(full_path)
                            removed_dirs += 1
                            logger.info(
                                f"Removed stale broadcast dir: {full_path} (age: {int(age)}s)"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to remove broadcast dir {full_path}: {e}")
                    else:
                        skipped_too_young += 1

            if removed_segments or removed_dirs:
                logger.info(
                    f"Broadcast GC: removed {removed_segments} orphaned segment(s), "
                    f"{removed_dirs} stale dir(s), scanned {skipped_active} active broadcast(s), "
                    f"skipped {skipped_too_young} too-young dir(s)"
                )
            elif skipped_active or skipped_too_young:
                logger.debug(
                    f"Broadcast GC: no cleanup needed, scanned {skipped_active} active broadcast(s), "
                    f"skipped {skipped_too_young} too-young dir(s)"
                )

        except Exception as e:
            logger.error(f"Error during broadcast GC: {e}")

    async def shutdown(self):
        """Stop all broadcasts and background tasks gracefully."""
        logger.info("Shutting down BroadcastManager...")
        self._running = False

        # Cancel GC task
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
