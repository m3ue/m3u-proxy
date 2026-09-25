"""
Enhanced stream manager with Redis support for shared transcoding processes.
Implements connection pooling and multi-worker coordination.
"""

import asyncio
import json
import re
import time
import uuid
import hashlib
from typing import Dict, List, Optional, Tuple, Any
import logging
from config import settings
from hls_input import hls_extension_args, is_hls_url
import os
import tempfile

# Live ffmpeg progress fields written to stderr (e.g.
# "frame=  243 fps= 30 q=28.0 size=    1152kB time=00:00:08.13 bitrate=1162.5kbits/s speed=1.01x")
_FFMPEG_PROGRESS_FIELD_RE = re.compile(
    r"\b(?P<key>frame|fps|bitrate|speed|q|size|time)\s*=\s*(?P<val>\S+)"
)

# "Input #0, mpegts, from 'http://...':" or
# "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file.mp4':" — captures the comma-
# separated container synonyms list (split into the canonical name later).
_FFMPEG_INPUT_LINE_RE = re.compile(
    r"^\s*Input #\d+,\s*(?P<container>[\w,]+),\s*from\s+"
)

# "Output #0, mpegts, to 'pipe:1':" or "Output #0, hls, to '/path/index.m3u8':".
# Once this line is emitted by ffmpeg, every subsequent Stream # line refers to
# the encoder/muxer output rather than the source input.
_FFMPEG_OUTPUT_LINE_RE = re.compile(
    r"^\s*Output #\d+,\s*(?P<container>[\w,]+),\s*to\s+"
)

# "Stream #0:0[0x100]: Video: h264 (Main) ([27][0][0][0] / 0x001B), yuv420p..."
# "Stream #0:0[0x100][0x200]: Video: hevc ..." (dual PID bracket groups in MPEG-TS)
# "Stream #0:1[0x101](eng): Audio: aac (LC) ..."
_FFMPEG_STREAM_LINE_RE = re.compile(
    r"Stream #\d+:\d+(?:\[[^\]]+\])*(?:\([^)]+\))?:\s+"
    r"(?P<type>Video|Audio):\s+(?P<details>.+)$"
)

# Audio channel layout: "stereo", "mono", "5.1", "5.1(side)", "5.1(back)", "7.1", "quad", etc.
# The (?:\([^)]*\))? tolerates the variant suffixes ffmpeg appends to 5.1/7.1.
_AUDIO_LAYOUT_RE = re.compile(
    r",\s*(?P<layout>stereo|mono|5\.1|7\.1|quad)(?:\([^)]*\))?[,\s]"
)

# "1280x720" or "1920x1080 [SAR 1:1 DAR 16:9]" inside a Stream line.
_RESOLUTION_RE = re.compile(r"\b(?P<w>\d{2,5})x(?P<h>\d{2,5})\b")

# stderr substrings (case-insensitive match) that signal ffmpeg has been told
# to write somewhere it can't. These mark the stream as failed for cleanup.
_FFMPEG_WRITE_ERROR_PATTERNS = (
    "no space left on device",
    "permission denied",
    "i/o error",
    "disk full",
    "cannot write",
    "failed to open",
    "error writing",
)

# stderr substrings (case-insensitive match) that signal a genuine upstream
# input failure and should trip failover. We deliberately do NOT include the
# bare "end of file" string: ffmpeg 8.1 writes "Error reading HTTP response:
# End of file" on every HLS segment fetch end (the HTTP layer closes when the
# segment body is exhausted) and immediately reconnects via -reconnect 1, so
# that line is normal traffic — not a stream failure. Genuine upstream loss
# is still caught by:
#   * the more specific patterns below ("connection refused", "server returned
#     4xx/5xx", "error opening input", etc. — emitted when reconnect fails)
#   * the low-bitrate / silence detectors in stream_manager.py, which trigger
#     on actual data starvation regardless of what stderr says.
_FFMPEG_INPUT_ERROR_PATTERNS = (
    "error opening input",
    "failed to resolve hostname",
    "connection refused",
    "connection timed out",
    "input/output error",
    "server returned 4",  # Matches 403, 404, etc.
    "server returned 5",  # Matches 500, 502, 503, etc.
    "invalid data found",
    "protocol not found",
)

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis  # noqa: F401

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available - falling back to single-worker mode")


class SharedTranscodingProcess:
    """Represents a shared transcoding process (FFmpeg, streamlink, or yt-dlp) with broadcasting to multiple clients"""

    def __init__(
        self,
        stream_id: str,
        url: str,
        profile: str,
        ffmpeg_args: List[str],
        user_agent: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        hls_base_dir: Optional[str] = None,
        resolver_type: Optional[str] = None,
        resolver_args: Optional[str] = None,
        resolver_cookies_path: Optional[str] = None,
    ):
        self.stream_id = stream_id
        self.url = url
        self.profile = profile
        self.ffmpeg_args = ffmpeg_args
        self.user_agent = user_agent
        self.headers = headers or {}
        self.metadata = metadata or {}
        self.resolver_type = resolver_type  # "streamlink" or "ytdlp"
        self.resolver_args = resolver_args  # quality/format string + optional flags
        self.resolver_cookies_path = (
            resolver_cookies_path  # path to cookies.txt on proxy host
        )
        # Base directory to create HLS per-stream directories in. If None,
        # the process will fall back to the system tempdir.
        self.hls_base_dir = hls_base_dir
        self.process: Optional[asyncio.subprocess.Process] = None
        self.clients: Dict[str, float] = {}  # client_id -> last_access_time
        self.created_at = time.time()
        self.last_access = time.time()
        self.total_bytes_served = 0
        self.status = "starting"
        # Live media info parsed from the active ffmpeg process's own stderr —
        # codec/container/resolution/audio from the "Input #" and "Stream #"
        # lines printed at startup, plus live bitrate/fps from the periodic
        # progress line. We do NOT run a separate ffprobe against the source
        # because that would double the upstream connection count and IPTV
        # providers reject the second connection. Empty for resolver streams
        # (streamlink/yt-dlp) that don't go through ffmpeg.
        self.media_info: Dict[str, Any] = {}
        # Output-side counterpart describing what ffmpeg is producing right now
        # (encoder codec / target resolution / muxer container) plus the live
        # progress fields, which are technically output stats. Populated only
        # after ffmpeg prints its "Output #" line, so it stays empty for plain
        # passthrough where no ffmpeg process is involved.
        self.output_media_info: Dict[str, Any] = {}
        # Parser state: flips True once "Output #" is seen in stderr so the
        # Stream-line parser knows where to route subsequent codec lines.
        self._parsing_output: bool = False

        # Broadcasting support - each client gets its own queue
        self.client_queues: Dict[str, asyncio.Queue] = {}
        self._broadcaster_task: Optional[asyncio.Task] = None
        self._broadcaster_lock = asyncio.Lock()

        self.last_chunk_time = time.time()  # Track when last chunk was produced
        self.output_timeout = 30  # Seconds without output before considering failed
        # Detect output mode (stdout stream vs HLS files)
        self.mode = "stdout"
        self.hls_dir: Optional[str] = None
        # If ffmpeg_args suggest HLS output, switch to hls mode
        joined_args = " ".join(self.ffmpeg_args).lower()
        if (
            "-hls_time" in joined_args
            or "-hls_list_size" in joined_args
            or "-f hls" in joined_args
        ):
            self.mode = "hls"

        if self.mode == "hls":
            # Determine base dir for file-based outputs
            base_dir = None
            if self.hls_base_dir:
                base_dir = self.hls_base_dir
            else:
                try:
                    base_dir = tempfile.gettempdir()
                except Exception:
                    base_dir = None

            # Ensure base dir exists if provided
            if base_dir:
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except Exception:
                    pass

        if self.mode == "hls":
            # Create a per-stream directory for HLS segments
            try:
                self.hls_dir = tempfile.mkdtemp(
                    prefix=f"m3u_proxy_hls_{self.stream_id}_", dir=base_dir
                )
                # Fix #6: Set explicit permissions to ensure NGINX can read segments
                # rwxr-xr-x (755) allows owner full access, group/others can read/execute
                try:
                    os.chmod(self.hls_dir, 0o755)
                except Exception as e:
                    logger.warning(
                        f"Failed to set permissions on HLS dir {self.hls_dir}: {e}"
                    )
            except Exception as e:
                # Fallback to system tempdir without dir param
                logger.warning(
                    f"Failed to create HLS dir in {base_dir}: {e}, falling back to system tempdir"
                )
                self.hls_dir = tempfile.mkdtemp(
                    prefix=f"m3u_proxy_hls_{self.stream_id}_"
                )
                try:
                    os.chmod(self.hls_dir, 0o755)
                except Exception:
                    pass

            logger.info(
                f"SharedTranscodingProcess {self.stream_id} will run in HLS mode, hls_dir={self.hls_dir}"
            )

    async def _start_resolver_process(self) -> bool:
        """Start a streamlink or yt-dlp subprocess and pipe its stdout to clients"""
        import shlex

        resolver_binary = "yt-dlp" if self.resolver_type == "ytdlp" else "streamlink"
        args_str = self.resolver_args or (
            "best" if self.resolver_type == "streamlink" else "bestvideo+bestaudio/best"
        )

        try:
            extra = shlex.split(args_str)
        except ValueError:
            extra = [args_str]

        if self.resolver_type == "streamlink":
            # streamlink URL QUALITY [OPTIONS] -O
            # First token of extra is the quality selector; remainder are flags
            cmd = ["streamlink", self.url] + extra + ["-O"]
        else:
            # yt-dlp URL [-f FORMAT] [OPTIONS] -o -
            # If the first token doesn't start with '-', treat it as a format selector
            if extra and not extra[0].startswith("-"):
                cmd = ["yt-dlp", self.url, "-f", extra[0]] + extra[1:] + ["-o", "-"]
            else:
                cmd = ["yt-dlp", self.url] + extra + ["-o", "-"]

        if self.resolver_cookies_path and self.resolver_cookies_path.strip():
            cookies_path = self.resolver_cookies_path.strip()
            if os.path.isfile(cookies_path) and os.access(cookies_path, os.R_OK):
                cmd.extend(["--cookies", cookies_path])
                logger.info(
                    f"Using cookies file for {resolver_binary} stream {self.stream_id}: {cookies_path}"
                )
            else:
                logger.warning(
                    f"Cookies file not found or not readable for stream {self.stream_id}: {cookies_path!r} — skipping"
                )

        logger.info(
            f"Starting {resolver_binary} process for stream {self.stream_id}: {' '.join(cmd)}"
        )

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self.status = "running"
        logger.info(f"{resolver_binary} process started with PID: {self.process.pid}")

        # Start broadcaster and stderr logger (same as FFmpeg path)
        self._broadcaster_task = asyncio.create_task(self._broadcast_loop())
        asyncio.create_task(self._log_stderr())

        return True

    async def start_process(self):
        """Start the transcoding/resolver process"""
        try:
            # --- Resolver path (streamlink / yt-dlp) ---
            if self.resolver_type:
                return await self._start_resolver_process()

            logger.info(f"Starting shared FFmpeg process for stream {self.stream_id}")

            # Build FFmpeg command - ensure output to stdout
            ffmpeg_cmd = ["ffmpeg"]

            # Add user agent / headers only for network inputs (http/rtsp/etc.)
            if (
                self.user_agent
                and isinstance(self.url, str)
                and ("://" in self.url and not self.url.startswith("file://"))
            ):
                ffmpeg_cmd.extend(["-user_agent", self.user_agent])

            # Add headers if provided, ensuring proper format and only for network inputs
            if (
                self.headers
                and isinstance(self.url, str)
                and ("://" in self.url and not self.url.startswith("file://"))
            ):
                header_str = "".join([f"{k}: {v}\r\n" for k, v in self.headers.items()])
                ffmpeg_cmd.extend(["-headers", header_str])

            # Process ffmpeg_args and insert HLS-specific options right before -i flag
            # For HLS inputs with extensionless segment URLs, we need special handling
            processed_args = []
            is_hls_input = is_hls_url(self.url)
            i = 0
            while i < len(self.ffmpeg_args):
                arg = self.ffmpeg_args[i]
                if arg == "-i" and i + 1 < len(self.ffmpeg_args):
                    # Found -i flag - inject HLS options right before it if needed
                    if is_hls_input:
                        # Add protocol whitelist to allow http/https URLs in playlists
                        processed_args.extend(
                            ["-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
                        )
                        # Enable following HTTP redirects for HLS streams
                        processed_args.extend(
                            [
                                "-reconnect",
                                "1",
                                "-reconnect_streamed",
                                "1",
                                "-reconnect_delay_max",
                                "2",
                            ]
                        )
                        # Allow any segment extensions (including extensionless or
                        # disguised .jpg/.css segments)
                        processed_args.extend(hls_extension_args(self.url))
                    # Add -i flag and use self.url as the input
                    processed_args.append(arg)
                    # Use current URL (updated during failover)
                    processed_args.append(self.url)

                    # Add stream mapping if transcoding (not copy mode)
                    # Check if this is a transcoding profile (has -c:v or -c:a that's not 'copy')
                    is_transcoding = False
                    for j, cmd_arg in enumerate(self.ffmpeg_args):
                        if cmd_arg in ["-c:v", "-c:a"] and j + 1 < len(
                            self.ffmpeg_args
                        ):
                            if self.ffmpeg_args[j + 1] not in ["copy", "Copy"]:
                                is_transcoding = True
                                break

                    # Only add stream mapping for transcoding profiles
                    if is_transcoding:
                        # Check if user already specified -map
                        has_map = any(str(a) == "-map" for a in self.ffmpeg_args)
                        if not has_map:
                            # Map all video and audio streams
                            # The '?' makes streams optional - won't fail if stream type doesn't exist (e.g. audio only)
                            processed_args.extend(["-map", "0:v:0?", "-map", "0:a:0?"])
                            logger.debug(
                                "Added stream mapping for transcoding profile: -map 0:v:0? -map 0:a:0?"
                            )

                    i += 2  # Skip the old URL in ffmpeg_args
                else:
                    processed_args.append(arg)
                    i += 1

            ffmpeg_cmd.extend(processed_args)

            # If HLS mode, ensure we write to the hls_dir index.m3u8
            if self.mode == "hls":
                # If the ffmpeg args already include an output filename, respect it
                # Otherwise append the playlist target into the hls dir
                playlist_path = os.path.join(
                    self.hls_dir if self.hls_dir else tempfile.gettempdir(),
                    "index.m3u8",
                )
                # If ffmpeg_args already specify an output playlist, replace any m3u8 token with absolute path
                replaced = False
                for i, token in enumerate(ffmpeg_cmd):
                    try:
                        if not isinstance(token, str):
                            continue
                        t_lower = token.lower()
                        # Skip tokens that look like input specs ("-i <url>" or "-i<url>")
                        if t_lower.endswith(".m3u8"):
                            prev = ffmpeg_cmd[i - 1] if i > 0 else None
                            if isinstance(prev, str) and prev == "-i":
                                # This is an input URL; do NOT replace it with our output path
                                continue
                            if t_lower.startswith("-i") and t_lower[2:].endswith(
                                ".m3u8"
                            ):
                                # Token like '-ihttp://.../playlist.m3u8' - treat as input, skip
                                continue
                            # Otherwise this looks like an output playlist token, replace it
                            ffmpeg_cmd[i] = playlist_path
                            replaced = True
                    except Exception:
                        continue

                if not replaced:
                    # Remove any pipe outputs which are inappropriate for file-based HLS output
                    ffmpeg_cmd = [
                        t
                        for t in ffmpeg_cmd
                        if not (isinstance(t, str) and t.startswith("pipe:"))
                    ]
                    # Append absolute playlist path as the intended HLS output
                    ffmpeg_cmd.append(playlist_path)

            else:
                # Ensure we're outputting to stdout in MPEGTS format only if no -f specified
                if "-f" not in [a.lower() for a in ffmpeg_cmd]:
                    ffmpeg_cmd.extend(["-f", "mpegts"])
                # Use pipe:1 for stdout-based broadcasting unless an output file is specified
                if (
                    "pipe:1" not in ffmpeg_cmd
                    and "-" not in ffmpeg_cmd
                    and self.mode == "stdout"
                ):
                    ffmpeg_cmd.append("pipe:1")

            logger.info(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self.status = "running"
            logger.info(f"Shared FFmpeg process started with PID: {self.process.pid}")

            # Start stderr logging task — also parses codec/resolution/bitrate
            # from ffmpeg's own output (no extra upstream connections).
            asyncio.create_task(self._log_stderr())

            # Start broadcaster task to read from FFmpeg and send to all clients
            if self.mode == "stdout":
                self._broadcaster_task = asyncio.create_task(self._broadcast_loop())
            else:
                # In HLS mode, watch for new segments to update last_chunk_time
                self._broadcaster_task = asyncio.create_task(self._hls_watch_loop())

            return True

        except Exception as e:
            logger.error(f"Failed to start shared FFmpeg process: {e}")
            self.status = "failed"
            return False

    async def _broadcast_loop(self):
        """Read from FFmpeg stdout and broadcast to all client queues"""
        if not self.process or not self.process.stdout:
            logger.error(
                f"Cannot start broadcaster - no process or stdout for {self.stream_id}"
            )
            return

        logger.info(f"Starting broadcaster for stream {self.stream_id}")

        try:
            while self.process and self.process.returncode is None:
                # Read chunk from FFmpeg
                chunk = await self.process.stdout.read(32768)
                if not chunk:
                    logger.info(f"FFmpeg stdout closed for stream {self.stream_id}")
                    break

                # Update last chunk time
                self.last_chunk_time = time.time()

                # Broadcast to all client queues
                async with self._broadcaster_lock:
                    dead_clients = []
                    for client_id, queue in self.client_queues.items():
                        try:
                            # Use put_nowait to avoid blocking if a client's queue is full
                            queue.put_nowait(chunk)
                        except asyncio.QueueFull:
                            # When the queue is full, remove the oldest chunk to make space
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                # Should not happen if QueueFull was just raised, but defensive
                                pass
                            # Retry putting the new chunk
                            try:
                                queue.put_nowait(chunk)
                            except asyncio.QueueFull:
                                logger.warning(
                                    f"Client {client_id} queue full after dropping chunk"
                                )
                        except Exception as e:
                            logger.error(f"Error sending to client {client_id}: {e}")
                            dead_clients.append(client_id)

                    # Remove dead clients
                    for client_id in dead_clients:
                        self.client_queues.pop(client_id, None)

                # Update stats
                self.total_bytes_served += len(chunk)
                self.last_access = time.time()

        except Exception as e:
            logger.error(f"Broadcaster error for stream {self.stream_id}: {e}")
        finally:
            logger.info(f"Broadcaster stopped for stream {self.stream_id}")
            # Signal all clients that the stream has ended
            async with self._broadcaster_lock:
                for queue in self.client_queues.values():
                    try:
                        queue.put_nowait(None)  # None signals end of stream
                    except Exception:
                        pass

    async def _hls_watch_loop(self):
        """Watch HLS output directory and update last_chunk_time when new segments appear"""
        if not self.hls_dir:
            return
        try:
            known = set()
            while self.process and self.process.returncode is None:
                # list files
                try:
                    files = os.listdir(self.hls_dir)
                except FileNotFoundError:
                    files = []

                new_files = [f for f in files if f not in known]
                if new_files:
                    self.last_chunk_time = time.time()
                    for f in new_files:
                        known.add(f)
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"HLS watch loop ended for {self.stream_id}: {e}")

    def _parse_ffmpeg_input_line(self, line_str: str) -> None:
        """
        Parse ffmpeg's "Input #0, FORMAT, from 'URL':" line for the container
        format. ffmpeg prints this once per input when it opens the stream.
        """
        match = _FFMPEG_INPUT_LINE_RE.match(line_str)
        if not match:
            return
        container = match.group("container").strip().split(",")[0].strip()
        if container and "container" not in self.media_info:
            self.media_info["container"] = container.upper()

    def _parse_ffmpeg_output_line(self, line_str: str) -> None:
        """
        Parse ffmpeg's "Output #0, FORMAT, to 'TARGET':" line. Captures the
        muxer container into output_media_info and flips the parser state so
        that subsequent Stream # lines are recognised as output streams. Only
        the first Output block is honoured — multi-output configs (e.g. tee)
        would each restate the marker but we keep the first match.
        """
        match = _FFMPEG_OUTPUT_LINE_RE.match(line_str)
        if not match:
            return
        self._parsing_output = True
        container = match.group("container").strip().split(",")[0].strip()
        if container and "container" not in self.output_media_info:
            self.output_media_info["container"] = container.upper()

    def _parse_ffmpeg_stream_line(self, line_str: str) -> None:
        """
        Parse ffmpeg's "Stream #0:N[...]: Video|Audio: ..." lines for codec
        details. ffmpeg prints one per stream right after the input is opened
        — this is free metadata that doesn't require a second connection.
        Routes into media_info or output_media_info based on whether the
        "Output #" marker has been seen yet.
        """
        match = _FFMPEG_STREAM_LINE_RE.search(line_str)
        if not match:
            return
        target = self.output_media_info if self._parsing_output else self.media_info
        stream_type = match.group("type").lower()
        details = match.group("details")
        if stream_type == "video":
            # Codec name is the first token, may be followed by a parenthesised
            # profile (e.g. "h264 (Main) (HEVC / 0x...)" — strip parens).
            codec = details.split(",", 1)[0].strip()
            codec = re.sub(r"\s*\(.*", "", codec).strip()
            if codec and "video_codec" not in target:
                target["video_codec"] = codec
            res_match = _RESOLUTION_RE.search(details)
            if res_match and "resolution" not in target:
                target["resolution"] = f"{res_match.group('w')}x{res_match.group('h')}"
        elif stream_type == "audio":
            codec = details.split(",", 1)[0].strip()
            codec = re.sub(r"\s*\(.*", "", codec).strip()
            if codec and "audio_codec" not in target:
                target["audio_codec"] = codec
            layout_match = _AUDIO_LAYOUT_RE.search(details)
            if layout_match and "audio_channels" not in target:
                target["audio_channels"] = layout_match.group("layout")

    def _parse_ffmpeg_progress(self, line_str: str) -> None:
        """
        Extract live progress fields (bitrate, fps, frame, speed) from a single
        ffmpeg stderr line. Progress numbers describe the encoder output, so
        once the "Output #" marker has been seen we mirror them into
        output_media_info. We also keep writing them to media_info so existing
        callers (Stream Info badge row in m3u-editor PR #1089) don't regress.
        No-op for non-progress lines.
        """
        # Cheap pre-filter — most stderr lines aren't progress
        if "bitrate=" not in line_str and "fps=" not in line_str:
            return
        targets = [self.media_info]
        if self._parsing_output:
            targets.append(self.output_media_info)
        for match in _FFMPEG_PROGRESS_FIELD_RE.finditer(line_str):
            key = match.group("key")
            raw = match.group("val")
            if key == "bitrate":
                if raw.endswith("kbits/s"):
                    try:
                        value = round(float(raw[:-7]), 1)
                    except ValueError:
                        continue
                    for t in targets:
                        t["bitrate_kbps"] = value
            elif key == "fps":
                try:
                    value = round(float(raw), 2)
                except ValueError:
                    continue
                for t in targets:
                    t["fps"] = value
            elif key == "frame":
                try:
                    value = int(raw)
                except ValueError:
                    continue
                for t in targets:
                    t["frame"] = value
            elif key == "speed":
                if raw.endswith("x"):
                    try:
                        value = round(float(raw[:-1]), 2)
                    except ValueError:
                        continue
                    for t in targets:
                        t["speed"] = value

    async def _log_stderr(self):
        """Log FFmpeg stderr output and monitor for write errors and input failures"""
        if not self.process or not self.process.stderr:
            return

        try:
            # Read stderr in small chunks and buffer lines ourselves to avoid
            # asyncio.StreamReader's LimitOverrunError when ffmpeg writes very
            # long lines without a newline (which results in the message
            # "Separator is not found, and chunk exceed the limit"). This keeps
            # the stderr reader alive instead of letting an exception kill the
            # task and potentially lead to the stream being cleaned up later.
            buf = b""
            CHUNK_SIZE = 4096
            MAX_BUFFER = 10 * 1024 * 1024  # 10 MB

            while self.process and self.process.returncode is None:
                # Read a small chunk from stderr; this will never raise
                # LimitOverrunError because we are not using readline/readuntil.
                chunk = await self.process.stderr.read(CHUNK_SIZE)
                if not chunk:
                    break

                buf += chunk

                # Split on \n OR \r — ffmpeg writes its periodic stats line with
                # a trailing \r so it overwrites in-place in a terminal. Without
                # splitting on \r those stats lines are buffered until the next
                # newline arrives and we miss the live bitrate/fps updates.
                while True:
                    n_idx = buf.find(b"\n")
                    r_idx = buf.find(b"\r")
                    if n_idx == -1 and r_idx == -1:
                        break
                    if n_idx == -1:
                        idx = r_idx
                    elif r_idx == -1:
                        idx = n_idx
                    else:
                        idx = min(n_idx, r_idx)
                    line, buf = buf[:idx], buf[idx + 1 :]
                    line_str = line.decode("utf-8", errors="ignore").strip()
                    if not line_str:
                        continue

                    # Skip debug logging for periodic progress lines (~2/s per stream) —
                    # they're already captured into media_info and add no diagnostic value.
                    if "bitrate=" not in line_str and "fps=" not in line_str:
                        logger.debug(f"FFmpeg [{self.stream_id}]: {line_str}")
                    # Skip parsers for resolver (yt-dlp/streamlink) stderr — their
                    # output format is not ffmpeg's, so the regexes won't match but
                    # a coincidental "fps=" or "bitrate=" in debug output could
                    # populate stale values.
                    if not self.resolver_type:
                        self._parse_ffmpeg_input_line(line_str)
                        self._parse_ffmpeg_output_line(line_str)
                        self._parse_ffmpeg_stream_line(line_str)
                        self._parse_ffmpeg_progress(line_str)

                    line_lower = line_str.lower()

                    # Check for write errors
                    for pattern in _FFMPEG_WRITE_ERROR_PATTERNS:
                        if pattern in line_lower:
                            logger.error(
                                f"FFmpeg write error detected for {self.stream_id}: {line_str}"
                            )
                            # Mark stream as failed to trigger cleanup
                            self.status = "failed"
                            break

                    # Check for input/connection errors that should trigger failover
                    for pattern in _FFMPEG_INPUT_ERROR_PATTERNS:
                        if pattern in line_lower:
                            logger.error(
                                f"FFmpeg input error detected for {self.stream_id}: {line_str}"
                            )
                            # Mark stream as failed due to input error
                            self.status = "input_failed"
                            break

                # Safety: prevent the buffer from growing without bound
                if len(buf) > MAX_BUFFER:
                    logger.warning(
                        f"Stderr buffer exceeded {MAX_BUFFER} bytes for {self.stream_id}, truncating"
                    )
                    buf = b""

        except Exception as e:
            logger.error(f"Error reading FFmpeg stderr for {self.stream_id}: {e}")

    async def read_playlist(self) -> Optional[str]:
        """Read the generated HLS playlist (index.m3u8) if available"""
        if not self.hls_dir:
            return None
        playlist_path = os.path.join(self.hls_dir, "index.m3u8")
        try:
            if os.path.exists(playlist_path):
                with open(playlist_path, "r", encoding="utf-8", errors="ignore") as fh:
                    return fh.read()
        except Exception as e:
            logger.error(f"Error reading playlist {playlist_path}: {e}")
        return None

    def get_segment_path(self, segment_name: str) -> Optional[str]:
        if not self.hls_dir:
            return None
        candidate = os.path.join(self.hls_dir, segment_name)
        if os.path.exists(candidate):
            return candidate
        return None

    async def add_client(self, client_id: str) -> asyncio.Queue:
        """Add a client to this shared process and return their queue"""
        async with self._broadcaster_lock:
            # Create a queue for this client (max 100 chunks buffered)
            client_queue = asyncio.Queue(maxsize=settings.CHANGE_BUFFER_CHUNKS)
            self.client_queues[client_id] = client_queue
            self.clients[client_id] = time.time()
            self.last_access = time.time()
            logger.info(
                f"Client {client_id} joined shared stream {self.stream_id} ({len(self.clients)} total)"
            )
            return client_queue

    async def remove_client(self, client_id: str):
        """Remove a client from this shared process"""
        async with self._broadcaster_lock:
            if client_id in self.clients:
                del self.clients[client_id]
                self.last_access = time.time()
                logger.info(
                    f"Client {client_id} left shared stream {self.stream_id} ({len(self.clients)} remaining)"
                )

            # Remove client's queue, sending None sentinel first so the
            # streaming generator exits its wait loop promptly.
            if client_id in self.client_queues:
                try:
                    self.client_queues[client_id].put_nowait(None)
                except Exception:
                    pass
                del self.client_queues[client_id]

    async def prune_stale_clients(self, timeout: int):
        """Remove clients that have been inactive for a while"""
        stale_clients = [
            cid
            for cid, last_seen in self.clients.items()
            if time.time() - last_seen > timeout
        ]
        for client_id in stale_clients:
            await self.remove_client(client_id)

    def should_cleanup(self, timeout: int = 300) -> bool:
        """Check if this process should be cleaned up (no clients for timeout seconds)"""
        return not self.clients and (time.time() - self.last_access > timeout)

    def health_check(self):
        """Check the health of the FFmpeg process."""
        if self.process and self.process.returncode is not None:
            if self.status != "failed":
                logger.warning(
                    f"FFmpeg process for stream {self.stream_id} has exited with code {self.process.returncode}."
                )
                self.status = "failed"
                return False

        # Also check if process exists but is not responding
        if self.process is None and self.status == "running":
            logger.warning(
                f"FFmpeg process for stream {self.stream_id} is None but status is running"
            )
            self.status = "failed"
            return False

        # Check if no output for too long (indicates stuck process)
        if time.time() - self.last_chunk_time > self.output_timeout:
            logger.warning(
                f"FFmpeg process for stream {self.stream_id} has produced no output for {self.output_timeout}s, marking as failed"
            )
            self.status = "failed"
            return False

        return self.status == "running" and self.process is not None

    async def cleanup(self):
        """Clean up the FFmpeg process and any output artifacts"""
        # Fix #5: Improved cleanup with better error handling and logging
        try:
            if self.process and self.process.returncode is None:
                logger.info(
                    f"Terminating shared FFmpeg process for stream {self.stream_id}"
                )
                try:
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "FFmpeg process didn't terminate cleanly, killing it"
                    )
                    self.process.kill()
                    await self.process.wait()
                except Exception as e:
                    logger.error(f"Error cleaning up FFmpeg process: {e}")

            self.status = "stopped"
            self.clients.clear()
        finally:
            # Always attempt HLS cleanup in finally block to ensure it runs even if FFmpeg cleanup fails
            await self._cleanup_hls_directory()

    async def _cleanup_hls_directory(self):
        """Clean up HLS directory and all segments"""
        if not self.hls_dir or not os.path.isdir(self.hls_dir):
            return

        try:
            # Count files for logging
            files = os.listdir(self.hls_dir)
            file_count = len(files)

            # Remove all files in the directory
            removed_count = 0
            failed_count = 0
            for fname in files:
                try:
                    file_path = os.path.join(self.hls_dir, fname)
                    os.remove(file_path)
                    removed_count += 1
                except Exception as e:
                    failed_count += 1
                    logger.warning(f"Failed to remove HLS file {fname}: {e}")

            # Try to remove the directory itself
            try:
                os.rmdir(self.hls_dir)
                logger.info(
                    f"Cleaned up HLS directory for {self.stream_id}: removed {removed_count}/{file_count} files"
                )
            except OSError as e:
                # Directory not empty or other error
                logger.warning(
                    f"Failed to remove HLS directory {self.hls_dir}: {e} ({failed_count} files failed to delete)"
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error removing HLS directory {self.hls_dir}: {e}"
                )

        except Exception as e:
            logger.error(f"Error cleaning up HLS directory for {self.stream_id}: {e}")


class PooledStreamManager:
    """Stream manager with Redis support and connection pooling"""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        worker_id: Optional[str] = None,
        enable_sharing: bool = True,
    ):

        self.redis_url = redis_url or "redis://localhost:6379/0"
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        self.enable_sharing = enable_sharing and REDIS_AVAILABLE

        # Redis client
        # Use Any to avoid type issues when Redis not available
        self.redis_client: Optional[Any] = None

        # Event manager (set by stream manager)
        self.event_manager = None
        self.parent_stream_manager = None

        # Local process management
        self.shared_processes: Dict[str, SharedTranscodingProcess] = {}
        # client_id -> stream_id mapping
        self.client_streams: Dict[str, str] = {}
        # stream_key -> stream_id mapping (for event emission)
        self.stream_key_to_id: Dict[str, str] = {}

        # Configuration
        # seconds - how often to run cleanup loop
        self.cleanup_interval = int(getattr(settings, "CLEANUP_INTERVAL", 60))
        # seconds - Redis worker heartbeat
        self.heartbeat_interval = int(getattr(settings, "HEARTBEAT_INTERVAL", 30))
        # seconds - fallback timeout for streams with no clients
        self.stream_timeout = int(getattr(settings, "STREAM_TIMEOUT", 300))
        # Default 30 seconds - timeout for inactive clients
        self.client_timeout = int(getattr(settings, "CLIENT_TIMEOUT", 30))
        # HLS GC configuration (defaults from config.settings)
        self.hls_gc_enabled = bool(getattr(settings, "HLS_GC_ENABLED", True))
        # How often to scan filesystem for stale HLS dirs (seconds)
        self.hls_gc_interval = int(getattr(settings, "HLS_GC_INTERVAL", 600))
        # Age threshold for removing HLS dirs (seconds)
        self.hls_gc_age_threshold = int(
            getattr(settings, "HLS_GC_AGE_THRESHOLD", 60 * 60)
        )
        # Track last time GC ran so we can respect hls_gc_interval cadence
        self._last_hls_gc_run = 0
        # Base directory for HLS per-stream output. If settings.HLS_TEMP_DIR is not set,
        # fall back to the system tempdir used by tempfile.gettempdir()
        self.hls_base_dir = (
            getattr(settings, "HLS_TEMP_DIR", None) or tempfile.gettempdir()
        )

        # Tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False

    def set_event_manager(self, event_manager):
        """Set the event manager for emitting events"""
        self.event_manager = event_manager

    def set_parent_stream_manager(self, stream_manager):
        """Set the parent stream manager for accessing stream info"""
        self.parent_stream_manager = stream_manager

    async def _emit_event(self, event_type: str, stream_id: str, data: dict):
        """Helper method to emit events if event manager is available"""
        if self.event_manager:
            try:
                from models import StreamEvent, EventType

                event = StreamEvent(
                    event_type=getattr(EventType, event_type),
                    stream_id=stream_id,
                    data=data,
                )
                await self.event_manager.emit_event(event)
            except Exception as e:
                logger.error(f"Error emitting event: {e}")

    async def start(self):
        """Start the pooled stream manager"""
        self._running = True

        if self.enable_sharing and REDIS_AVAILABLE:
            try:
                # Import here to avoid issues if redis not installed
                import redis.asyncio as redis_async

                # protocol=2: the proxy uses no RESP3-only features, and RESP3
                # makes newer redis-py probe the server with
                # CLIENT MAINT_NOTIFICATIONS, which logs a harmless
                # "unknown subcommand" against Redis < 8.
                self.redis_client = redis_async.from_url(
                    self.redis_url, decode_responses=True, protocol=2
                )
                await self.redis_client.ping()
                logger.info(f"Redis connected for worker {self.worker_id}")

                # Start heartbeat task
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            except Exception as e:
                logger.warning(
                    f"Failed to connect to Redis: {e}. Running in single-worker mode"
                )
                self.enable_sharing = False
                self.redis_client = None

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        mode = "multi-worker with Redis" if self.enable_sharing else "single-worker"
        logger.info(f"Pooled stream manager started in {mode} mode")

    async def stop(self):
        """Stop the pooled stream manager"""
        self._running = False

        # Cancel tasks
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        # Clean up all local processes
        for process in list(self.shared_processes.values()):
            await process.cleanup()
        self.shared_processes.clear()

        # Close Redis connection
        if self.redis_client:
            await self._cleanup_worker_streams()
            await self.redis_client.close()

        logger.info("Pooled stream manager stopped")

    async def _heartbeat_loop(self):
        """Send periodic heartbeat to Redis"""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if self.redis_client:
                    now = time.time()
                    # Update heartbeat score
                    await self.redis_client.zadd(
                        "worker_heartbeats", {self.worker_id: now}
                    )

                    # Update worker data
                    worker_data = {
                        "last_seen": now,
                        "streams": list(self.shared_processes.keys()),
                        "worker_id": self.worker_id,
                    }
                    await self.redis_client.hset(
                        "workers", self.worker_id, json.dumps(worker_data)
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _cleanup_loop(self):
        """Periodic cleanup of stale streams and processes"""
        while self._running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_stale_processes()

                # Optionally run HLS temp-dir GC (scans system temp dir for leftover m3u_proxy_hls_*)
                if self.hls_gc_enabled:
                    now = time.time()
                    if (
                        now - getattr(self, "_last_hls_gc_run", 0)
                        >= self.hls_gc_interval
                    ):
                        await self._gc_hls_temp_dirs()
                        self._last_hls_gc_run = now

                if self.enable_sharing and self.redis_client:
                    await self._cleanup_stale_redis_streams()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_stale_processes(self):
        """Clean up local processes with no clients"""
        to_cleanup = []

        for stream_id, process in self.shared_processes.items():
            process.health_check()
            await process.prune_stale_clients(self.client_timeout)
            if (
                process.should_cleanup(self.stream_timeout)
                or process.status == "failed"
            ):
                to_cleanup.append(stream_id)

        for stream_id in to_cleanup:
            logger.info(f"Cleaning up stale process for stream {stream_id}")
            await self._cleanup_local_process(stream_id)

    async def _cleanup_local_process(self, stream_key: str, emit_event: bool = False):
        """
        Clean up a specific local process.

        Args:
            stream_key: The key identifying the transcoding process
            emit_event: Whether to emit stream_stopped event (default: False)
                       Set to True only when this is the sole cleanup point
        """
        if stream_key in self.shared_processes:
            process = self.shared_processes.pop(stream_key)
            await process.cleanup()

            # Only emit event if explicitly requested
            # Normally, the parent StreamManager handles event emission
            if emit_event:
                stream_id = self.stream_key_to_id.get(stream_key, stream_key)
                await self._emit_event(
                    "STREAM_STOPPED",
                    stream_id,
                    {"reason": "transcoding_cleanup", "stream_key": stream_key},
                )

            # Clean up the mapping
            if stream_key in self.stream_key_to_id:
                del self.stream_key_to_id[stream_key]

            # Update Redis
            if self.redis_client:
                redis_key = f"stream:{stream_key}"
                await self.redis_client.delete(redis_key)
                await self.redis_client.srem(
                    f"worker:{self.worker_id}:streams", redis_key
                )

    async def _cleanup_stale_redis_streams(self):
        """Clean up stale streams from Redis (dead workers)"""
        if not self.redis_client:
            return

        try:
            # Find stale workers
            stale_threshold = time.time() - (self.heartbeat_interval * 3)
            stale_workers = await self.redis_client.zrangebyscore(
                "worker_heartbeats", -1, stale_threshold
            )

            if not stale_workers:
                return

            # Clean up streams from stale workers
            for worker_id in stale_workers:
                worker_streams_key = f"worker:{worker_id}:streams"
                stream_keys = await self.redis_client.smembers(worker_streams_key)
                if stream_keys:
                    await self.redis_client.delete(*stream_keys)
                    logger.info(
                        f"Cleaned up {len(stream_keys)} streams for stale worker {worker_id}"
                    )
                await self.redis_client.delete(worker_streams_key)

            # Remove stale workers from heartbeats and data
            await self.redis_client.zremrangebyscore(
                "worker_heartbeats", -1, stale_threshold
            )
            await self.redis_client.hdel("workers", *stale_workers)
            logger.info(f"Removed stale workers: {stale_workers}")

        except Exception as e:
            logger.error(f"Error cleaning up stale Redis streams: {e}")

    async def _cleanup_worker_streams(self):
        """Clean up streams owned by this worker from Redis"""
        if not self.redis_client:
            return

        try:
            worker_streams_key = f"worker:{self.worker_id}:streams"
            stream_keys = await self.redis_client.smembers(worker_streams_key)
            if stream_keys:
                await self.redis_client.delete(*stream_keys)
            await self.redis_client.delete(worker_streams_key)
        except Exception as e:
            logger.error(f"Error cleaning up worker streams from Redis: {e}")

    async def _gc_hls_temp_dirs(self):
        """Scan the HLS temp dir for leftover HLS directories and remove stale ones.

        Fix #3: Improved GC to avoid race conditions with active streams
        Fix #4: Only delete empty directories to avoid conflicts with FFmpeg's own segment deletion

        We look for directories created with the prefix 'm3u_proxy_hls_' and remove them if they
        are not currently in use by any active SharedTranscodingProcess and meet cleanup criteria.
        """
        try:
            # Use configured HLS base dir (may be system tempdir by default)
            tmpdir = getattr(self, "hls_base_dir", None) or tempfile.gettempdir()
            prefix = "m3u_proxy_hls_"
            now = time.time()

            # Build a set of active hls dirs to avoid deleting currently-used ones
            active_dirs = set()
            for p in self.shared_processes.values():
                if p.hls_dir:
                    active_dirs.add(os.path.abspath(p.hls_dir))

            # Iterate entries in tmpdir
            try:
                entries = os.listdir(tmpdir)
            except Exception as e:
                logger.warning(f"Failed to list HLS temp directory {tmpdir}: {e}")
                entries = []

            removed = 0
            skipped_active = 0
            skipped_not_empty = 0
            skipped_too_young = 0

            for entry in entries:
                if not entry.startswith(prefix):
                    continue

                full_path = os.path.abspath(os.path.join(tmpdir, entry))
                if not os.path.isdir(full_path):
                    continue

                # Skip any dir that's currently active
                if full_path in active_dirs:
                    skipped_active += 1
                    continue

                # Fix #4: Only delete EMPTY directories
                # FFmpeg handles segment deletion via -hls_delete_threshold
                # We only clean up directories that are completely empty (stream ended)
                try:
                    dir_contents = os.listdir(full_path)
                    if dir_contents:
                        # Directory not empty - skip it
                        # FFmpeg is still managing this directory or cleanup failed
                        skipped_not_empty += 1
                        continue
                except Exception as e:
                    logger.warning(f"Failed to check contents of {full_path}: {e}")
                    continue

                # Fix #3: Check age to avoid deleting recently-created directories
                # Use mtime for empty directories (they won't be updated anymore)
                try:
                    mtime = os.path.getmtime(full_path)
                except Exception as e:
                    logger.warning(f"Failed to get mtime for {full_path}: {e}")
                    continue

                age = now - mtime
                if age > self.hls_gc_age_threshold:
                    # Directory is empty and old enough - safe to remove
                    try:
                        # Use rmdir instead of rmtree for safety (only works on empty dirs)
                        os.rmdir(full_path)
                        removed += 1
                        logger.info(
                            f"Removed empty stale HLS dir: {full_path} (age: {int(age)}s)"
                        )
                    except OSError as e:
                        # Directory not empty or permission error
                        logger.warning(f"Failed to remove HLS dir {full_path}: {e}")
                    except Exception as e:
                        logger.error(
                            f"Unexpected error removing HLS dir {full_path}: {e}"
                        )
                else:
                    skipped_too_young += 1

            if removed or skipped_active or skipped_not_empty:
                logger.info(
                    f"HLS GC scan complete: removed {removed} empty dirs, "
                    f"skipped {skipped_active} active, {skipped_not_empty} non-empty, "
                    f"{skipped_too_young} too young (from {tmpdir})"
                )

        except Exception as e:
            logger.error(f"Error while running HLS GC: {e}")

    def _generate_stream_key(
        self,
        url: str,
        profile: str,
        ffmpeg_args: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        resolver_type: Optional[str] = None,
        resolver_args: Optional[str] = None,
    ) -> str:
        """Generate a consistent key for stream sharing.

        The key includes the detected output mode (hls / file / direct) so
        that two transcoded streams with the same URL and profile name but
        different delivery formats (e.g. MPEGTS pipe vs HLS segments) are
        never incorrectly pooled together.  This matters when both profiles
        resolve to the same name (e.g. "custom" for user-provided templates).

        For resolver-based streams, the key includes the resolver type and args
        instead of the FFmpeg output mode.
        """
        if resolver_type:
            data = f"{url}|resolver|{resolver_type}|{resolver_args or ''}"
        else:
            from stream_manager import StreamManager

            output_mode = StreamManager._detect_output_mode(ffmpeg_args)
            data = f"{url}|{profile}|{output_mode}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    async def get_or_create_shared_stream(
        self,
        url: str,
        profile: str,
        ffmpeg_args: List[str],
        client_id: str,
        user_agent: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        stream_id: Optional[str] = None,
        reuse_stream_key: Optional[str] = None,
        resolver_type: Optional[str] = None,
        resolver_args: Optional[str] = None,
        resolver_cookies_path: Optional[str] = None,
    ) -> Tuple[str, SharedTranscodingProcess]:
        """Get existing shared stream or create new one

        Args:
            reuse_stream_key: If provided, reuse this stream key instead of generating a new one.
                              This is useful for failover scenarios where we want to maintain the
                              same stream key even though the URL has changed.
        """

        stream_key = reuse_stream_key or self._generate_stream_key(
            url,
            profile,
            ffmpeg_args=ffmpeg_args,
            metadata=metadata,
            resolver_type=resolver_type,
            resolver_args=resolver_args,
        )

        # Track the stream_id -> stream_key mapping for event emission
        if stream_id:
            self.stream_key_to_id[stream_key] = stream_id

        # First check if we have it locally
        if stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]

            # Check if the process is still healthy before reusing it
            process.health_check()

            # Check if URL has changed (failover scenario) or if process has failed
            url_changed = process.url != url
            process_failed = process.status in ("failed", "input_failed") or (
                process.process and process.process.returncode is not None
            )

            if url_changed or process_failed:
                if url_changed:
                    logger.info(
                        f"Stream {stream_key} URL changed (failover), restarting with new URL: {url}"
                    )
                else:
                    logger.warning(
                        f"Existing process for stream {stream_key} is unhealthy, recreating..."
                    )
                await self._cleanup_local_process(stream_key)
                # Process will be recreated below with new URL
            else:
                # Process is healthy and URL unchanged, reuse it
                await process.add_client(client_id)
                self.client_streams[client_id] = stream_key
                return stream_key, process

        # If sharing enabled, check Redis for existing streams
        if self.enable_sharing and self.redis_client:
            redis_key = f"stream:{stream_key}"
            stream_data = await self.redis_client.hgetall(redis_key) or {}

            if stream_data and await self._is_redis_stream_healthy(stream_data):
                owner = stream_data.get("owner")
                if owner != self.worker_id:
                    logger.info(
                        f"Stream {stream_key} is managed by another worker ({owner}). This worker will not create a local copy."
                    )
                    raise ConnectionAbortedError(f"Stream is on another worker {owner}")

        # Create new local process
        process = SharedTranscodingProcess(
            stream_key,
            url,
            profile,
            ffmpeg_args,
            user_agent=user_agent,
            headers=headers,
            metadata=metadata,
            hls_base_dir=self.hls_base_dir,
            resolver_type=resolver_type,
            resolver_args=resolver_args,
            resolver_cookies_path=resolver_cookies_path,
        )

        if await process.start_process():
            self.shared_processes[stream_key] = process
            await process.add_client(client_id)
            self.client_streams[client_id] = stream_key

            # Restore stream_id -> stream_key mapping (may have been deleted by _cleanup_local_process)
            if stream_id:
                self.stream_key_to_id[stream_key] = stream_id

            # Register in Redis
            if self.redis_client:
                redis_key = f"stream:{stream_key}"
                stream_data = {
                    "url": url,
                    "profile": profile,
                    "owner": self.worker_id,
                    "created_at": time.time(),
                    "last_access": time.time(),
                    "status": "running",
                    "ffmpeg_pid": process.process.pid if process.process else 0,
                }
                await self.redis_client.hset(redis_key, mapping=stream_data)
                await self.redis_client.sadd(
                    f"worker:{self.worker_id}:streams", redis_key
                )

            logger.info(
                f"Created new shared stream {stream_key} for {len(process.clients)} clients"
            )
            return stream_key, process
        else:
            raise Exception(
                f"Failed to start transcoding process for stream {stream_key}"
            )

    async def _is_redis_stream_healthy(self, stream_data: Dict) -> bool:
        """Check if a Redis stream entry represents a healthy stream"""

        # Check if owner worker is alive
        owner = stream_data.get("owner")
        if not owner or not self.redis_client:
            return False

        worker_data = await self.redis_client.hget("workers", owner)
        if not worker_data:
            return False

        try:
            worker_info = json.loads(worker_data)
            last_seen = worker_info.get("last_seen", 0)
            return time.time() - last_seen < self.heartbeat_interval * 2
        except json.JSONDecodeError:
            return False

    async def remove_client_from_stream(self, client_id: str):
        """Remove a client from its stream"""
        if client_id not in self.client_streams:
            return

        stream_key = self.client_streams.pop(client_id, None)

        if stream_key and stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]
            await process.remove_client(client_id)

            # If no more clients, immediately schedule cleanup
            if not process.clients:
                logger.info(
                    f"No more clients for stream {stream_key}, scheduling immediate cleanup"
                )
                # Use configured grace period (SHARED_STREAM_GRACE) so short client
                # reconnects (e.g. range-based reconnects from players) don't
                # immediately kill the transcoding process. Fall back to 1s if
                # the setting isn't available.
                grace = int(getattr(settings, "SHARED_STREAM_GRACE", 1))
                asyncio.create_task(
                    self._delayed_cleanup_if_empty(stream_key, grace_period=grace)
                )

    async def force_stop_stream(self, stream_key: str):
        """
        Immediately stop a stream and its FFmpeg process without grace period.
        Used when explicitly deleting a stream via API and during failover.

        Pops the entry up front and cleans up the captured reference so a
        concurrent get_or_create_shared_stream that re-inserts at the same key
        (e.g. a failover client racing this teardown) isn't torn down by the
        trailing cleanup.
        """
        process = self.shared_processes.pop(stream_key, None)
        if process is None:
            logger.info(f"Stream {stream_key} not found in local processes")
            return False

        logger.info(
            f"Force stopping stream {stream_key} and terminating FFmpeg process"
        )

        # Remove all clients from this stream immediately
        clients_to_remove = list(process.clients.keys())
        for client_id in clients_to_remove:
            await process.remove_client(client_id)
            if client_id in self.client_streams:
                del self.client_streams[client_id]

        # Tear down the captured process — NOT whatever is at stream_key now.
        await process.cleanup()

        # Mirror the auxiliary cleanup that _cleanup_local_process does so the
        # state stays consistent regardless of which path called us.
        if stream_key in self.stream_key_to_id:
            del self.stream_key_to_id[stream_key]
        if self.redis_client:
            try:
                redis_key = f"stream:{stream_key}"
                await self.redis_client.delete(redis_key)
                await self.redis_client.srem(
                    f"worker:{self.worker_id}:streams", redis_key
                )
            except Exception as e:
                logger.warning(
                    f"Error cleaning up Redis state for stream {stream_key}: {e}"
                )

        logger.info(f"Stream {stream_key} force stopped, FFmpeg process terminated")
        return True

    async def _delayed_cleanup_if_empty(self, stream_key: str, grace_period: int = 10):
        """
        Clean up a stream after a grace period if it still has no clients.
        This prevents immediate termination on brief disconnects while ensuring
        resources are freed quickly when streaming actually stops.
        """
        await asyncio.sleep(grace_period)

        if stream_key not in self.shared_processes:
            return  # Already cleaned up

        process = self.shared_processes[stream_key]

        # Check if clients reconnected during grace period
        if not process.clients:
            logger.info(
                f"Grace period expired for stream {stream_key} with no clients, cleaning up FFmpeg process"
            )
            await self._cleanup_local_process(stream_key)
        else:
            logger.info(
                f"Clients reconnected to stream {stream_key} during grace period, keeping process alive"
            )

    def update_client_activity(self, client_id: str):
        """Update the last access time for a client."""
        if client_id in self.client_streams:
            stream_key = self.client_streams[client_id]
            if stream_key in self.shared_processes:
                process = self.shared_processes[stream_key]
                if client_id in process.clients:
                    process.clients[client_id] = time.time()

    async def stream_shared_process(
        self, client_id: str
    ) -> Optional[asyncio.subprocess.Process]:
        """Get the FFmpeg process for a client's stream"""

        if client_id not in self.client_streams:
            return None

        stream_key = self.client_streams[client_id]
        if stream_key not in self.shared_processes:
            return None

        process = self.shared_processes[stream_key]
        return process.process

    async def get_stream_stats(self) -> Dict[str, Any]:
        """Get statistics about active streams"""

        stats = {
            "worker_id": self.worker_id,
            "sharing_enabled": self.enable_sharing,
            "local_streams": len(self.shared_processes),
            "total_clients": len(self.client_streams),
            "streams": [],
        }

        for stream_id, process in self.shared_processes.items():
            stream_stats = {
                "stream_id": stream_id,
                "url": process.url,
                "profile": process.profile,
                "client_count": len(process.clients),
                "created_at": process.created_at,
                "last_access": process.last_access,
                "status": process.status,
                "total_bytes_served": process.total_bytes_served,
            }
            stats["streams"].append(stream_stats)

        return stats
