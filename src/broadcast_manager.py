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
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from pathlib import Path

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
    callback_url: Optional[str] = None


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
        'error opening input',
        'failed to resolve hostname',
        'connection refused',
        'connection timed out',
        'server returned 4',  # 403, 404, etc.
        'server returned 5',  # 500, 502, etc.
        'invalid data found',
        'no such file or directory',
        'protocol not found',
    ]

    def __init__(self, config: BroadcastConfig, hls_base_dir: str):
        self.config = config
        self.network_id = config.network_id
        self.hls_dir = os.path.join(
            hls_base_dir, f"broadcast_{config.network_id}")
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

        # Input-level seeking (BEFORE -i for accuracy)
        if self.config.seek_seconds > 0:
            cmd.extend(["-ss", str(self.config.seek_seconds)])

        # Real-time pacing - critical for live streaming
        cmd.append("-re")

        # Reconnection options for network streams
        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10"
        ])

        # Input URL
        cmd.extend(["-i", self.config.stream_url])

        # Duration limiting for programme boundary
        if self.config.duration_seconds > 0:
            cmd.extend(["-t", str(self.config.duration_seconds)])

        # Stream mapping - video + audio only (drop subtitles, data streams)
        # Use -map 0:a:0? to make audio optional (some streams may be video-only)
        cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

        # Codec selection
        if self.config.transcode:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast"])
            if self.config.video_bitrate:
                cmd.extend(["-b:v", f"{self.config.video_bitrate}k"])
            if self.config.video_resolution:
                cmd.extend(["-vf", f"scale={self.config.video_resolution}"])
            cmd.extend(
                ["-c:a", "aac", "-b:a", f"{self.config.audio_bitrate}k"])
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
            "independent_segments"
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
                logger.warning(
                    f"Failed to set permissions on {self.hls_dir}: {e}")

            cmd = self._build_ffmpeg_command()
            logger.info(
                f"Starting broadcast {self.network_id}: {' '.join(cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )

            self.started_at = datetime.now(timezone.utc)
            self.status = "running"

            # Start monitoring tasks
            self._stderr_task = asyncio.create_task(self._log_stderr())
            self._monitor_task = asyncio.create_task(self._monitor_process())

            logger.info(
                f"Broadcast {self.network_id} started with PID {self.process.pid}")
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
                            f"Broadcast {self.network_id} did not terminate gracefully, killing")
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
            f"Broadcast {self.network_id} stopped, final segment: {final_segment}")
        return final_segment

    # Patterns to skip in FFmpeg output (verbose/noisy messages)
    SKIP_LOG_PATTERNS = [
        'frame=',           # Progress output
        'fps=',             # FPS stats
        'time=',            # Time stats
        'bitrate=',         # Bitrate stats
        'speed=',           # Speed stats
        'size=',            # Size stats
        'resumed reading',  # Reconnection noise
        'opening',          # File opening messages (lowercase)
        'muxing overhead',  # Summary stats
        'video:',           # Summary stats
        'audio:',           # Summary stats
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
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if not line_str:
                        continue

                    line_lower = line_str.lower()

                    # Check for input errors - these are always logged
                    for pattern in self.INPUT_ERROR_PATTERNS:
                        if pattern in line_lower:
                            self.error_message = line_str
                            self.status = "failed"
                            logger.error(
                                f"Broadcast {self.network_id} error: {line_str}")
                            # Send failure callback
                            await self._send_callback("broadcast_failed", {
                                "error": line_str,
                                "error_type": "input_error"
                            })
                            return

                    # Skip verbose/noisy messages entirely
                    should_skip = any(
                        pattern in line_lower for pattern in self.SKIP_LOG_PATTERNS)
                    if should_skip:
                        continue

                    # Log warnings and errors only
                    if 'error' in line_lower or 'warning' in line_lower or 'failed' in line_lower:
                        logger.warning(
                            f"Broadcast {self.network_id}: {line_str}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"Error reading FFmpeg stderr for {self.network_id}: {e}")

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
                duration_streamed = (datetime.now(
                    timezone.utc) - self.started_at).total_seconds()

            exit_code = self.process.returncode
            if exit_code == 0:
                # Normal completion (duration limit reached)
                self.status = "stopped"
                await self._send_callback("programme_ended", {
                    "exit_code": exit_code,
                    "final_segment_number": final_segment,
                    "duration_streamed": duration_streamed
                })
            else:
                # Abnormal exit
                self.status = "failed"
                self.error_message = f"FFmpeg exited with code {exit_code}"
                await self._send_callback("broadcast_failed", {
                    "exit_code": exit_code,
                    "final_segment_number": final_segment,
                    "duration_streamed": duration_streamed,
                    "error": self.error_message
                })

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error monitoring broadcast {self.network_id}: {e}")

    async def _send_callback(self, event: str, data: dict):
        """Send webhook callback to Laravel."""
        if not self.config.callback_url:
            logger.debug(
                f"No callback URL for broadcast {self.network_id}, skipping callback")
            return

        payload = {
            "network_id": self.network_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data
        }

        try:
            timeout = getattr(settings, 'BROADCAST_CALLBACK_TIMEOUT', 10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.config.callback_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "M3U-Proxy-Broadcast/1.0"
                    }
                )
                if response.status_code >= 400:
                    logger.warning(
                        f"Callback to {self.config.callback_url} failed with status {response.status_code}"
                    )
                else:
                    logger.info(
                        f"Callback sent for broadcast {self.network_id}: {event}")
        except Exception as e:
            logger.error(
                f"Error sending callback for broadcast {self.network_id}: {e}")

    def _get_final_segment_number(self) -> int:
        """Get the highest segment number from existing files."""
        try:
            if not os.path.exists(self.hls_dir):
                return self.config.segment_start_number

            # Pattern: live000001.ts -> extract 000001
            pattern = re.compile(r'live(\d{6})\.ts$')
            max_segment = self.config.segment_start_number

            for filename in os.listdir(self.hls_dir):
                match = pattern.match(filename)
                if match:
                    segment_num = int(match.group(1))
                    max_segment = max(max_segment, segment_num)

            return max_segment
        except Exception as e:
            logger.error(
                f"Error getting final segment number for {self.network_id}: {e}")
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
            error_message=self.error_message
        )

    def get_playlist_path(self) -> Optional[str]:
        """Get path to the HLS playlist file."""
        path = os.path.join(self.hls_dir, "live.m3u8")
        return path if os.path.exists(path) else None

    def get_segment_path(self, filename: str) -> Optional[str]:
        """Get path to a specific segment file."""
        # Sanitize filename to prevent directory traversal
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith('.ts'):
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
            settings, 'HLS_BROADCAST_DIR', '/tmp/m3u-proxy-broadcasts'
        )
        self.broadcasts: Dict[str, NetworkBroadcastProcess] = {}
        self._lock = asyncio.Lock()

        # Ensure base directory exists
        os.makedirs(self.hls_base_dir, exist_ok=True)
        logger.info(
            f"BroadcastManager initialized with base dir: {self.hls_base_dir}")

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
                logger.info(
                    f"Transitioning broadcast {network_id} to new programme")

                # Stop existing process gracefully
                final_segment = await existing.stop(graceful=True)

                # Auto-continue segment numbering if not specified
                if config.segment_start_number == 0:
                    config.segment_start_number = final_segment + 1
                    # Force discontinuity on transition
                    config.add_discontinuity = True

                # Note: We intentionally do NOT delete the old playlist here.
                # The old segments remain valid until FFmpeg's delete_segments removes them.
                # The new FFmpeg will overwrite the playlist with a discontinuity marker,
                # allowing players to smoothly transition to the new content.

                del self.broadcasts[network_id]

            # Create and start new process
            process = NetworkBroadcastProcess(config, self.hls_base_dir)
            success = await process.start()

            if not success:
                raise RuntimeError(
                    f"Failed to start broadcast: {process.error_message}")

            self.broadcasts[network_id] = process
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
                self.hls_base_dir, f"broadcast_{network_id}", "live.m3u8")
            if os.path.exists(playlist_path):
                try:
                    with open(playlist_path, 'r') as f:
                        return f.read()
                except Exception as e:
                    logger.error(
                        f"Error reading playlist for {network_id}: {e}")
            return None

        process = self.broadcasts[network_id]
        playlist_path = process.get_playlist_path()

        if not playlist_path:
            return None

        try:
            with open(playlist_path, 'r') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading playlist for {network_id}: {e}")
            return None

    def get_segment_path(self, network_id: str, filename: str) -> Optional[str]:
        """Get path to a segment file for a network."""
        # Sanitize filename
        safe_filename = os.path.basename(filename)
        if not safe_filename.endswith('.ts'):
            return None

        # Check active broadcast first
        if network_id in self.broadcasts:
            return self.broadcasts[network_id].get_segment_path(filename)

        # Check directory even without active broadcast
        segment_path = os.path.join(
            self.hls_base_dir, f"broadcast_{network_id}", safe_filename)
        return segment_path if os.path.exists(segment_path) else None

    async def cleanup_broadcast(self, network_id: str) -> bool:
        """Clean up broadcast directory and files."""
        async with self._lock:
            # Stop if running
            if network_id in self.broadcasts:
                await self.broadcasts[network_id].stop(graceful=False)
                del self.broadcasts[network_id]

            # Remove directory
            broadcast_dir = os.path.join(
                self.hls_base_dir, f"broadcast_{network_id}")
            if os.path.exists(broadcast_dir):
                try:
                    import shutil
                    shutil.rmtree(broadcast_dir)
                    logger.info(
                        f"Cleaned up broadcast directory: {broadcast_dir}")
                    return True
                except Exception as e:
                    logger.error(
                        f"Error cleaning up broadcast {network_id}: {e}")
                    return False

            return True

    async def shutdown(self):
        """Stop all broadcasts gracefully."""
        logger.info("Shutting down BroadcastManager...")
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
