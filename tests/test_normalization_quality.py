"""Real moving-video regression; no providers. VAAPI is explicitly opt-in.

Use RUN_VAAPI_QUALITY=1 only inside a disposable render-node sandbox.
SSIM >= .97 is a synthetic encode-regression floor, not a perceptual guarantee.
The reference is field-rate bwdif on the *same decoded* interlaced source, not
an impossible lossless reconstruction of the pre-interlaced progressive source.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from config import Settings
from live_normalization import VAAPI_PROFILE

DEINTERLACE = "bwdif=mode=send_field:parity=auto:deint=interlaced"


def run(*args):
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result


def probe(path):
    return json.loads(run("ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_frames", "-show_streams", "-of", "json", path).stdout)


def test_standard_profile_quality_contract():
    for profile in (Settings.model_fields["LIVE_NORMALIZE_PROFILE"].default, VAAPI_PROFILE):
        args = shlex.split(profile)
        assert args[args.index("-vf") + 1].startswith(DEINTERLACE)
        assert args[args.index("-fps_mode") + 1] == "passthrough"
        assert "-r" not in args and "fps=" not in profile
    assert "-crf 18" in Settings.model_fields["LIVE_NORMALIZE_PROFILE"].default
    assert "-rc_mode CQP -qp 18" in VAAPI_PROFILE


@pytest.mark.parametrize("encoder", ["cpu", "vaapi"])
@pytest.mark.parametrize("source", ["i25", "p25", "p50"])
def test_moving_video_field_rate_and_quality(tmp_path, encoder, source):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required for moving-video acceptance")
    if encoder == "vaapi" and os.environ.get("RUN_VAAPI_QUALITY") != "1":
        pytest.skip("VAAPI quality requires an explicit disposable device sandbox")
    size = os.environ.get("NORMALIZATION_QUALITY_SIZE", "320x180")
    rate = 25 if source == "p25" else 50
    original = tmp_path / "source.mkv"
    filters = "tinterlace=mode=interleave_top,setfield=tff" if source == "i25" else "setfield=prog"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        f"testsrc2=size={size}:rate={rate}:duration=2", "-vf", filters,
        "-c:v", "ffv1", "-threads", "2", original)
    before = probe(original)
    frames = before["frames"]
    assert len(frames) == (50 if source != "p50" else 100)
    assert all(f["interlaced_frame"] == (source == "i25") for f in frames)
    if source == "i25":
        assert all(f["top_field_first"] == 1 for f in frames)
    reference = tmp_path / "reference.mkv"
    run("ffmpeg", "-v", "error", "-i", original, "-vf", DEINTERLACE,
        "-fps_mode", "passthrough", "-c:v", "ffv1", "-threads", "2", reference)
    template = VAAPI_PROFILE if encoder == "vaapi" else Settings.model_fields["LIVE_NORMALIZE_PROFILE"].default
    # Exercise the exact server-owned output template; local fixture input avoids
    # reconnect/HTTP options, which apply only to the live private HTTP input.
    args = shlex.split(template.format(input_url="fixture", output_args="pipe:1"))
    output_args = args[args.index("-map"):]
    output_args[-1] = str(tmp_path / "output.ts")
    output_args[output_args.index("0:a:0")] = "0:a:0?"
    device = ["-vaapi_device", "/dev/dri/renderD128"] if encoder == "vaapi" else []
    run("ffmpeg", "-v", "error", *device, "-i", original, *output_args)
    output = Path(output_args[-1])
    after = probe(output)
    actual, expected = after["frames"], probe(reference)["frames"]
    expected_count = 50 if source == "p25" else 100
    assert len(actual) == len(expected) == expected_count
    assert after["streams"][0]["field_order"] == "progressive"
    assert all(f["interlaced_frame"] == 0 for f in actual)
    pts = [float(f["best_effort_timestamp_time"]) for f in actual]
    reference_pts = [float(f["best_effort_timestamp_time"]) for f in expected]
    assert [p - pts[0] for p in pts] == pytest.approx(reference_pts, abs=0.00002)
    step = 0.04 if source == "p25" else 0.02
    assert [b - a for a, b in zip(pts, pts[1:])] == pytest.approx([step] * (expected_count - 1), abs=0.00002)
    comparison = run("ffmpeg", "-v", "info", "-i", output, "-i", reference,
                     "-lavfi", "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]ssim",
                     "-an", "-f", "null", "-").stderr.decode()
    score = float(re.search(r"All:([0-9.]+)", comparison).group(1))
    assert score >= 0.97, comparison
    # No CFR-created duplicates: each field-derived moving output is distinct.
    hashes = run("ffmpeg", "-v", "error", "-i", output, "-an", "-fps_mode",
                 "passthrough", "-f", "framemd5", "-").stdout.decode()
    checksums = [line.rsplit(",", 1)[-1].strip() for line in hashes.splitlines() if not line.startswith("#")]
    assert len(checksums) == expected_count and len(set(checksums)) == expected_count
    print(f"QUALITY {encoder} {source} {size}: frames={len(actual)} step={step} progressive SSIM={score:.6f}")
