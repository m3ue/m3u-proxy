#!/usr/bin/env python3
"""Offline synthetic acceptance: real loopback HTTP source, API and FFmpeg.

Run with the project's installed Python environment. No provider URLs or mocks.
FFmpeg needs lavfi, mpeg2video/mp2, libx264/aac, and rawvideo/PCM decoders.
Temporary artifacts use TMPDIR and are removed after success or failure.
"""

import array
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def generate(path, color, frequency, video, audio, pid, offset):
    run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=160x120:r=25",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate=48000",
        "-t",
        "2",
        "-c:v",
        video,
        "-threads",
        "1",
        "-g",
        "12",
        "-c:a",
        audio,
        "-b:a",
        "128k",
        "-mpegts_start_pid",
        str(pid),
        "-output_ts_offset",
        str(offset),
        "-f",
        "mpegts",
        str(path),
    )


def decode_check(path, color_channel, frequency):
    # Inspect every decoded frame, not just a thumbnail at the beginning.
    frames = run(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-an",
        "-vf",
        "scale=1:1",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    )
    assert len(frames) >= 25 * 3
    pixels = [frames[i : i + 3] for i in range(0, len(frames), 3)]
    assert all(
        p[color_channel] > 180
        and all(p[c] < 70 for c in range(3) if c != color_channel)
        for p in pixels
    ), "unexpected visual content / mixed generation"
    pcm = run(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-f",
        "s16le",
        "pipe:1",
    )
    samples = array.array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    # Ignore encoder delay; count positive zero crossings in the central second.
    samples = samples[12000:60000]
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
    assert abs(crossings - frequency) < 8, (crossings, frequency)
    probe = json.loads(
        run(
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        )
    )
    return {
        "bytes": path.stat().st_size,
        "frames": len(pixels),
        "tone_hz": crossings,
        "start_time": probe["format"]["start_time"],
        "streams": [{k: s[k] for k in ("codec_name", "id")} for s in probe["streams"]],
    }


def main():
    with tempfile.TemporaryDirectory(prefix="live-recovery-") as temp:
        temp = Path(temp)
        a, b = temp / "a.ts", temp / "b.ts"
        generate(a, "red", 440, "mpeg2video", "mp2", 256, 20)
        generate(b, "blue", 880, "libx264", "aac", 512, 0)
        payloads = {"/live/a.ts": a.read_bytes(), "/live/b.ts": b.read_bytes()}
        requests = []

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                data = payloads.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        source = f"http://127.0.0.1:{server.server_port}"
        # Reserve an ephemeral listening socket and pass it to uvicorn; no port race.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        # Do not inherit proxy/provider/API/Redis settings from the operator's
        # shell. The server also runs in the fixture directory, not beside .env.
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "TMPDIR")
            if key in os.environ
        }
        env.update(
            PYTHONPATH=str(ROOT / "src"),
            ROOT_PATH="",
            REDIS_ENABLED="false",
            ENABLE_TRANSCODING_POOLING="false",
            WORKER_ID="",
            API_TOKEN="",
            LIVE_END_SESSION_ON_RECONNECT="false",
            LIVE_SINGLE_RECOVERY_PER_EXIT="false",
            STRICT_LIVE_TS="false",
            STRICT_LIVE_TS_PREBUFFER_SIZE="0",
            LIVE_SILENT_RECONNECT_MIN_CHUNKS="100000",
            STREAM_RETRY_ATTEMPTS="0",
            TEMP_DIR=str(temp / "streams"),
            LOG_DIR=str(temp / "logs"),
            HLS_TEMP_DIR=str(temp / "hls"),
            HLS_BROADCAST_DIR=str(temp / "broadcast"),
            DVR_RECORDING_DIR=str(temp / "dvr"),
        )
        with (temp / "proxy.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "api:app",
                    "--fd",
                    str(listener.fileno()),
                    "--log-level",
                    "warning",
                ],
                env=env,
                cwd=temp,
                stdout=log,
                stderr=log,
                pass_fds=(listener.fileno(),),
            )
            listener.close()
            try:
                with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
                    for _ in range(150):
                        if process.poll() is not None:
                            raise RuntimeError((temp / "proxy.log").read_text())
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("proxy did not become ready")
                    options = {
                        "url": source + "/live/a.ts",
                        "failover_urls": [source + "/live/b.ts"],
                        "end_session_on_reconnect": True,
                        "single_recovery_per_exit": True,
                        "strict_live_ts": True,
                    }
                    created = client.post("/streams", json=options)
                    created.raise_for_status()
                    endpoint = created.json()["stream_endpoint"]
                    first = client.get(endpoint)
                    first.raise_for_status()
                    assert first.content == payloads["/live/a.ts"], (
                        "first body must contain only A"
                    )
                    assert requests == ["/live/a.ts"], requests
                    # POST reuse must not discard the selected failover source.
                    reused = client.post("/streams", json=options)
                    reused.raise_for_status()
                    assert reused.json()["stream_endpoint"] == endpoint
                    second = client.get(endpoint)
                    second.raise_for_status()
                    assert second.content == payloads["/live/b.ts"], (
                        "same endpoint must now deliver only B"
                    )
                    assert requests == ["/live/a.ts", "/live/b.ts"], requests
                    first_path, second_path = temp / "first.ts", temp / "second.ts"
                    first_path.write_bytes(first.content)
                    second_path.write_bytes(second.content)
                    results: dict = {
                        "A": decode_check(first_path, 0, 440),
                        "B": decode_check(second_path, 2, 880),
                    }
                    assert results["A"]["streams"] != results["B"]["streams"]
                    assert (
                        float(results["A"]["start_time"])
                        > float(results["B"]["start_time"]) + 10
                    )
                    # Both new options disabled: legacy still splices A and B in one
                    # HTTP body. Strict mode avoids the historical upstream length
                    # header; no claim that arbitrary-codec splices decode seamlessly.
                    options.update(
                        end_session_on_reconnect=False, single_recovery_per_exit=False
                    )
                    legacy = client.post("/streams", json=options)
                    legacy.raise_for_status()
                    mixed = client.get(legacy.json()["stream_endpoint"])
                    mixed.raise_for_status()
                    assert mixed.content.startswith(payloads["/live/a.ts"])
                    assert len(mixed.content) >= sum(map(len, payloads.values()))
                    assert payloads["/live/b.ts"][32768:] in mixed.content
                    assert requests == ["/live/a.ts", "/live/b.ts"] * 2, requests
                    results["legacy"] = {
                        "bytes": len(mixed.content),
                        "single_body_contains_A_and_B": True,
                    }
                    results["requests"] = requests
                    print(json.dumps(results, indent=2))
            except BaseException:
                print((temp / "proxy.log").read_text(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    main()
