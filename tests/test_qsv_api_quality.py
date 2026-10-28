"""Short real API -> owned source -> FFmpegStreamer -> QSV moving-video test.

Opt-in only in the disposable --network none render-node sandbox. Loopback
servers are owned by the test; no deployed API, provider or viewer is contacted.
"""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.test_normalization_quality import DEINTERLACE, probe, run


@pytest.mark.parametrize("source", ["i25", "p25", "p50"])
def test_qsv_real_api_moving_video(tmp_path, source):
    if os.environ.get("RUN_QSV_QUALITY") != "1":
        pytest.skip("explicit disposable QSV device sandbox required")
    rate = 25 if source == "p25" else 50
    original = tmp_path / "source.ts"
    fixture = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={rate}:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=8",
    ]
    if source == "i25":
        fixture += [
            "-vf",
            "tinterlace=mode=interleave_top,setfield=tff",
            "-flags",
            "+ilme+ildct",
            "-x264-params",
            "tff=1",
        ]
    run(
        *fixture,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        original,
    )
    data = original.read_bytes()
    stop = threading.Event()

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for offset in range(0, len(data), 1316):
                    if stop.is_set():
                        break
                    self.wfile.write(data[offset : offset + 1316])
                    self.wfile.flush()
                    stop.wait(8 * 1316 / len(data))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "TMPDIR",
            "LIBVA_DRIVER_NAME",
            "LIBVA_DRIVERS_PATH",
            "LD_LIBRARY_PATH",
        )
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(root / "src"),
        ROOT_PATH="",
        REDIS_ENABLED="false",
        ENABLE_TRANSCODING_POOLING="true",
        ENABLE_SINGLE_PROCESS_TRANSCODING="true",
        LIVE_NORMALIZE_OUTPUT="false",
        WORKER_ID="",
        API_TOKEN="",
        STRICT_LIVE_TS_PREBUFFER_SIZE="0",
        DEFAULT_MAX_RETRIES="0",
        STREAM_RETRY_ATTEMPTS="0",
        LOG_LEVEL="warning",
        TEMP_DIR=str(tmp_path / "streams"),
        LOG_DIR=str(tmp_path / "logs"),
        HLS_TEMP_DIR=str(tmp_path / "hls"),
        HLS_BROADCAST_DIR=str(tmp_path / "broadcast"),
        DVR_RECORDING_DIR=str(tmp_path / "dvr"),
    )
    with (tmp_path / "proxy.log").open("wb") as log:
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
            pass_fds=(listener.fileno(),),
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=log,
        )
        listener.close()
        capture = tmp_path / "capture.ts"
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False
            ) as client:
                for _ in range(100):
                    assert process.poll() is None, (tmp_path / "proxy.log").read_text()
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("owned API did not start")
                created = client.post(
                    "/streams",
                    json={
                        "url": f"http://127.0.0.1:{server.server_port}/live/source.ts",
                        "normalize_live_output": True,
                        "normalization_encoder": "qsv",
                    },
                )
                assert created.status_code == 200, created.text
                info = created.json()
                assert info["metadata"]["normalization_encoder"] == "qsv"
                started = time.monotonic()
                gpu = None
                with (
                    client.stream("GET", info["stream_endpoint"]) as response,
                    capture.open("wb") as out,
                ):
                    assert response.status_code == 200
                    for chunk in response.iter_bytes():
                        out.write(chunk)
                        if gpu is None:
                            children = (
                                Path(f"/proc/{process.pid}/task/{process.pid}/children")
                                .read_text()
                                .split()
                            )
                            for child in children:
                                proc = Path(f"/proc/{child}")
                                if b"h264_qsv" in (proc / "cmdline").read_bytes():
                                    fds = [
                                        str(fd.readlink())
                                        for fd in (proc / "fd").iterdir()
                                    ]
                                    assert "/dev/dri/renderD128" in fds
                                    gpu = {
                                        "pid": int(child),
                                        "render_node": "/dev/dri/renderD128",
                                    }
                            assert gpu is not None
                        if time.monotonic() - started >= 5:
                            break
                state = client.get(f"/streams/{info['stream_id']}").json()["stream"]
                assert (
                    client.delete(
                        f"/streams/{state['normalization_input_stream_id']}"
                    ).status_code
                    == 409
                )
                assert client.delete(f"/streams/{info['stream_id']}").status_code == 200
                assert client.get("/streams").json()["streams"] == []
        except BaseException:
            print((tmp_path / "proxy.log").read_text())
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    frames = probe(capture)["frames"]
    count = len(frames)
    assert count >= (50 if source == "p25" else 100)
    assert all(f["interlaced_frame"] == 0 for f in frames)
    pts = [int(f["pts"]) for f in frames]
    step = 3600 if source == "p25" else 1800
    assert {b - a for a, b in zip(pts, pts[1:])} == {step}
    hashes = run(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        capture,
        "-an",
        "-fps_mode",
        "passthrough",
        "-f",
        "framemd5",
        "-",
    ).stdout.decode()
    checksums = [
        line.rsplit(",", 1)[-1].strip()
        for line in hashes.splitlines()
        if not line.startswith("#")
    ]
    assert len(checksums) == len(set(checksums)) == count
    # Only compare after the independent frame/PTS checks above have passed.
    comparison = run(
        "ffmpeg",
        "-v",
        "info",
        "-i",
        capture,
        "-i",
        original,
        "-lavfi",
        f"[0:v]setpts=PTS-STARTPTS[a];[1:v]{DEINTERLACE},trim=end_frame={count},setpts=PTS-STARTPTS[b];[a][b]ssim",
        "-an",
        "-f",
        "null",
        "-",
    ).stderr.decode()
    score = float(re.search(r"All:([0-9.]+)", comparison).group(1))
    assert score >= 0.97, comparison
    print(
        "API_QSV_QUALITY",
        json.dumps(
            dict(
                source=source,
                frames=count,
                unique_hashes=len(set(checksums)),
                pts_step=step,
                progressive=True,
                ssim=score,
                gpu=gpu,
            )
        ),
    )
