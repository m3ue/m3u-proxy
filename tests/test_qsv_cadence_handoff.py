"""Fully drained software-decode/QSV-encode gate, not proxy API evidence.

RUN_QSV_CADENCE=1 requires an owned network-none render-node sandbox.
The timeline oracle uses uncut input frame/PES positions and an audio epoch;
no arbitrary gap allowance or output-derived timestamp expectation is used.
QSV_CADENCE_CONTROL optionally names the retained original control fixture.
"""
import array
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from live_normalization import QSV_PROFILE


def run(*args):
    return subprocess.run([str(a) for a in args], capture_output=True, check=True, timeout=90)


def frames(path):
    return json.loads(run("ffprobe", "-v", "error", "-select_streams", "v", "-show_frames", "-of", "json", path).stdout)["frames"]


def pes_headers(data, pid):
    """Independent fixture-only PUSI/PTS parser (one access unit per PES)."""
    headers = []
    for at in range(0, len(data), 188):
        packet = data[at:at + 188]
        assert len(packet) == 188 and packet[0] == 0x47
        if not packet[1] & 64 or ((packet[1] & 31) << 8 | packet[2]) != pid:
            continue
        start = 5 + packet[4] if packet[3] & 32 else 4
        pes = packet[start:]
        assert pes[:3] == b"\x00\x00\x01" and pes[7] & 128
        p = pes[9:14]
        assert p[0] & 1 and p[2] & 1 and p[4] & 1
        pts = ((p[0] >> 1 & 7) << 30 | p[1] << 22
               | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1)
        headers.append(dict(byte_offset=at, pts=pts,
                            pes_length=int.from_bytes(pes[4:6], "big")))
    return headers


def source_positions(a_pts, b_pts, *, fields, last_demux_pts, audio_epoch,
                     output_audio_epoch, audio_delay, source_period):
    """Keep each valid source field at its own time, including around a cut.

    The damaged unbounded PES may consume one demux clock slot, but it does not
    contain a valid picture/field. B starts one source period after that slot.
    The global mux translation is anchored by audio, not video output fitting.
    """
    translation = output_audio_epoch + audio_delay - audio_epoch
    b_epoch = last_demux_pts + source_period
    return ([p + field * (source_period // fields) + translation
             for p in a_pts for field in range(fields)]
            + [p - b_pts[0] + b_epoch + field * (source_period // fields) + translation
               for p in b_pts for field in range(fields)])


def standard_bwdif_positions(source_pts, *, kind, cut_mode, bounded_video_pes,
                             complete_frames_a):
    """Explicit accepted FFmpeg policy; never derive expectations from output.

    Only unbounded truncated-video i25 shifts the last valid A second field
    to the midpoint of valid A/B input PTS. Keep source positions separately.
    """
    presentation = source_pts.copy()
    if kind == "i25" and cut_mode == "truncated-video" and not bounded_video_pes:
        second_field = complete_frames_a * 2 - 1
        midpoint = (source_pts[second_field - 1] + source_pts[second_field + 1]) // 2
        assert midpoint - source_pts[second_field] == 1800
        presentation[second_field] = midpoint
    return presentation


def normalize(source, target, *, discard=True):
    args = shlex.split(QSV_PROFILE.format(input_url=source, output_args=target))
    # Only network reconnect options are inapplicable to this finite local file.
    for option in ("-reconnect", "-reconnect_at_eof", "-reconnect_streamed",
                   "-reconnect_delay_max", "-reconnect_max_retries"):
        at = args.index(option)
        del args[at:at + 2]
    if not discard:
        args[args.index("+genpts+discardcorrupt")] = "+genpts"
    command = ["ffmpeg", "-hide_banner", "-debug_ts", "-y", *args]
    result = subprocess.run(command, capture_output=True, timeout=90)
    Path(str(target) + ".log").write_bytes(result.stderr)
    result.check_returncode()
    return frames(target)


def test_qsv_cadence_uses_existing_corruption_policy():
    args = shlex.split(QSV_PROFILE)
    assert args[args.index("-fflags") + 1] == "+genpts+discardcorrupt"
    assert args[args.index("-fps_mode") + 1] == "passthrough"
    assert "-r" not in args
    assert "setpts" not in args[args.index("-vf") + 1]


def test_qsv_discardcorrupt_retained_control(tmp_path):
    if os.environ.get("RUN_QSV_CADENCE") != "1":
        pytest.skip("explicit disposable QSV hardware sandbox required")
    if not os.environ.get("QSV_CADENCE_CONTROL"):
        pytest.skip("retained 1077-frame control fixture not supplied")
    report = {}
    for discard in (False, True):
        target = tmp_path / f"control-{discard}.ts"
        output = normalize(os.environ["QSV_CADENCE_CONTROL"], target, discard=discard)
        pts = [int(frame["pts"]) for frame in output]
        log = Path(str(target) + ".log").read_text()
        report[str(discard)] = {
            "frames": len(output),
            "bad_deltas": [b - a for a, b in zip(pts, pts[1:]) if b - a != 3600],
            "mux_clamps": [line for line in log.splitlines() if "Non-monotonic" in line],
            "discontinuities": [line for line in log.splitlines() if "timestamp discontinuity" in line],
        }
    (tmp_path / "control-evidence.json").write_text(json.dumps(report, indent=2))
    assert report["False"]["frames"] == report["True"]["frames"] == 1077
    assert report["False"]["bad_deltas"] == [1, 1, 3598]
    assert report["False"]["mux_clamps"]
    assert report["True"]["bad_deltas"] == report["True"]["mux_clamps"] == []
    assert len(report["False"]["discontinuities"]) == 3
    assert len(report["True"]["discontinuities"]) == 1


@pytest.mark.parametrize("kind", ["p25", "p50", "i25"])
@pytest.mark.parametrize("cut_mode", ["complete-video", "truncated-audio", "truncated-video"])
@pytest.mark.parametrize("bounded_video_pes", [False, True])
def test_qsv_moving_handoff_cadence(tmp_path, kind, cut_mode, bounded_video_pes):
    if os.environ.get("RUN_QSV_CADENCE") != "1":
        pytest.skip("explicit disposable QSV hardware sandbox required")
    import ts_canonicalizer as module
    inputs = []
    uncut_frames = []
    uncut_pes = []
    uncut_audio_pes = []
    for generation, tone in enumerate((440, 880)):
        source = tmp_path / f"{kind}-{generation}.ts"
        rate = 25 if kind == "p25" else 50
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
               f"testsrc2=size=320x180:rate={rate}:duration=3",
               "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=3"]
        vf = []
        if generation:
            vf.append("hue=h=90")
        if kind == "i25":
            vf.extend(("tinterlace=mode=interleave_top", "setfield=tff"))
            cmd += ["-flags", "+ilme+ildct", "-x264-params", "tff=1"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        if bounded_video_pes:
            # One-variable control: default zero-length video PES cannot be
            # flagged as truncated by the demuxer's PES-length check.
            cmd += ["-omit_video_pes_length", "0"]
        run(*cmd, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-threads", "1", "-bf", "0", "-g", "25",
            "-c:a", "aac", "-mpegts_pmt_start_pid", 1256 + generation * 256,
            "-mpegts_start_pid", 256 + generation * 256,
            "-output_ts_offset", 20 if not generation else 0, "-f", "mpegts", source)
        flags = frames(source)
        assert len(flags) == (75 if kind != "p50" else 150)
        assert {f["interlaced_frame"] for f in flags} == {int(kind == "i25")}
        uncut_frames.append([int(f["pts"]) for f in flags])
        uncut_pes.append(pes_headers(source.read_bytes(), 256 + generation * 256))
        uncut_audio_pes.append(pes_headers(source.read_bytes(), 257 + generation * 256))
        assert [p["pts"] for p in uncut_pes[-1]] == uncut_frames[-1]
        (tmp_path / f"uncut-{generation}.ts").write_bytes(source.read_bytes())
        inputs.append(source)
    # End immediately before a video PES start: every selected video access
    # unit is complete, while an in-flight interleaved audio PES may be cut.
    data = inputs[0].read_bytes()
    starts = [at for at in range(0, len(data), 188)
              if data[at + 1] & 64
              and ((data[at + 1] & 31) << 8 | data[at + 2]) == 256]
    first_packet = data[starts[0]:starts[0] + 188]
    payload_at = 5 + first_packet[4] if first_packet[3] & 32 else 4
    video_pes_length = int.from_bytes(first_packet[payload_at + 4:payload_at + 6], "big")
    assert bool(video_pes_length) == bounded_video_pes
    complete_cut = min(starts, key=lambda at: abs(at - len(data) * 2 / 3))
    if cut_mode == "truncated-audio":
        audio_starts = [at for at in range(0, len(data), 188)
                        if data[at + 1] & 64
                        and ((data[at + 1] & 31) << 8 | data[at + 2]) == 257]
        complete_cut = min(audio_starts, key=lambda at: abs(at - len(data) * 2 / 3)) + 188
        # FFmpeg writes these PES contiguously. All preceding video PES are
        # complete; the final audio PES deliberately has only one TS packet.
        next_packet = data[complete_cut:complete_cut + 188]
        assert ((next_packet[1] & 31) << 8 | next_packet[2]) == 257
        assert not next_packet[1] & 64
    complete_frames_a = sum(at < complete_cut for at in starts)
    assert len(starts) == (150 if kind == "p50" else 75)
    cut = complete_cut
    if cut_mode == "truncated-video":
        # Keep just the first TS packet of the next video PES. It cannot be
        # decoded intact and MUST be discarded, not counted as a saved frame.
        cut += 188
        assert cut < starts[complete_frames_a + 1]
    inputs[0].write_bytes(data[:cut])
    expected = (complete_frames_a + (150 if kind == "p50" else 75)) * (2 if kind == "i25" else 1)
    # Independent clean-video cut provides a non-corrupt visual reference.
    reference_a = tmp_path / "reference-a.ts"
    reference_a.write_bytes(data[:complete_cut])
    clean_adapter = module.TSCanonicalizer()
    clean = clean_adapter.feed(reference_a.read_bytes())
    clean_adapter.start_generation()
    clean += clean_adapter.feed(inputs[1].read_bytes()) + clean_adapter.finish()
    reference = tmp_path / "reference.ts"
    reference.write_bytes(clean)
    # Visual oracle comes from retained *uncut* inputs, not decoding the same
    # damaged stream or fitting neighbouring output images. Trim only after
    # field reconstruction, preserving the original source temporal context.
    visual_reference = tmp_path / "uncut-visual-reference.mkv"
    fields_a = complete_frames_a * (2 if kind == "i25" else 1)
    run("ffmpeg", "-v", "error", "-i", tmp_path / "uncut-0.ts",
        "-i", tmp_path / "uncut-1.ts", "-filter_complex",
        f"[0:v]bwdif=mode=send_field:parity=auto:deint=interlaced,trim=end_frame={fields_a},setpts=PTS-STARTPTS[a];"
        "[1:v]bwdif=mode=send_field:parity=auto:deint=interlaced,setpts=PTS-STARTPTS[b];"
        "[a][b]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-fps_mode", "passthrough",
        "-c:v", "ffv1", "-threads", "2", visual_reference)
    adapter = module.TSCanonicalizer()
    canonical = bytearray()
    for n, source in enumerate(inputs):
        if n:
            adapter.start_generation()
        data = source.read_bytes()
        for at in range(0, len(data), 997):
            canonical.extend(adapter.feed(data[at:at + 997]))
    canonical.extend(adapter.finish())
    joined = tmp_path / "canonical.ts"
    joined.write_bytes(canonical)
    target = tmp_path / "qsv.ts"
    output = normalize(joined, target)
    (tmp_path / "video-gate-evidence.json").write_text(json.dumps({
        "kind": kind, "cut_mode": cut_mode, "bounded_video_pes": bounded_video_pes,
        "first_video_pes_length": video_pes_length,
        "complete_source_a_frames": complete_frames_a,
        "expected_frames": expected, "actual_frames": len(output),
        "scope": "finite software decode/QSV encode gate, not product API",
    }, indent=2))
    assert len(output) == expected
    assert all(not f["interlaced_frame"] for f in output)
    step = 3600 if kind == "p25" else 1800
    pts = [int(f["pts"]) for f in output]
    assert all(b > a for a, b in zip(pts, pts[1:]))
    packets = json.loads(run("ffprobe", "-v", "error", "-show_packets", "-of", "json", target).stdout)["packets"]
    audio = [p for p in packets if p["codec_type"] == "audio"]
    source_period = 3600 if kind != "p50" else 1800
    damaged_slot = cut_mode == "truncated-video" and not bounded_video_pes
    last_demux_index = complete_frames_a - 1 + int(damaged_slot)
    expected_pts = source_positions(
        uncut_frames[0][:complete_frames_a], uncut_frames[1],
        fields=2 if kind == "i25" else 1,
        last_demux_pts=uncut_frames[0][last_demux_index],
        audio_epoch=uncut_audio_pes[0][0]["pts"],
        output_audio_epoch=int(audio[0]["pts"]),
        audio_delay=1920, source_period=source_period)
    assert int(audio[0]["duration"]) == 1920  # AAC encoder priming, 1024/48000 s
    assert int(audio[0]["pts"]) == 126000  # unchanged MPEG-TS mux epoch
    mismatches = [dict(index=i, expected=wanted, actual=actual)
                  for i, (wanted, actual) in enumerate(zip(expected_pts, pts))
                  if wanted != actual]
    policy_pts = standard_bwdif_positions(
        expected_pts, kind=kind, cut_mode=cut_mode,
        bounded_video_pes=bounded_video_pes, complete_frames_a=complete_frames_a)
    policy_mismatches = [dict(index=i, expected=wanted, actual=actual)
                         for i, (wanted, actual) in enumerate(zip(policy_pts, pts))
                         if wanted != actual]
    oracle = dict(
        standard_bwdif_policy_pts=policy_pts, policy_mismatches=policy_mismatches,
        uncut_frame_pts=uncut_frames, uncut_video_pes=uncut_pes,
        uncut_audio_pes=uncut_audio_pes, cut_byte_offset=cut,
        complete_cut_byte_offset=complete_cut, cut_frame_index=complete_frames_a,
        damaged_clock_slot=damaged_slot, expected_pts=expected_pts, actual_pts=pts,
        mismatches=mismatches,
        source_epoch=uncut_audio_pes[0][0]["pts"],
        output_audio_epoch=int(audio[0]["pts"]), aac_priming_ticks=1920,
        b_video_epoch=expected_pts[complete_frames_a * (2 if kind == "i25" else 1)],
        original_uniform_gate_passes=all(b - a == step for a, b in zip(pts, pts[1:])))
    (tmp_path / "source-timeline-oracle.json").write_text(json.dumps(oracle, indent=2))
    # All other cases retain their original exact uniform-cadence contract.
    if not damaged_slot:
        assert {b - a for a, b in zip(pts, pts[1:])} == {step}
    # Source-position gate is asserted after visual/audio measurements, so a
    # failing field position still retains complete diagnostic evidence.
    # No muxer-clamped timestamps may hide a decoder/filter regression.
    log = Path(str(target) + ".log").read_text()
    assert "Non-monotonic" not in log and "non monotonically" not in log
    assert len([line for line in log.splitlines() if "timestamp discontinuity" in line]) == 1
    # Independent B audio and video timestamps must share the same recovered
    # input epoch, not merely finish within a relaxed A/V endpoint tolerance.
    lines = log.splitlines()
    recovered = {}
    for stream, source_pts in (("video", uncut_frames[1][0]),
                               ("audio", uncut_audio_pes[1][0]["pts"])):
        index = next(i for i, line in enumerate(lines)
                     if "demuxer ->" in line and f"type:{stream} pkt_pts:{source_pts} " in line)
        fixed = next(line for line in lines[index + 1:]
                     if "demuxer+ffmpeg ->" in line and f"type:{stream} " in line)
        recovered[stream] = int(re.search(r"pkt_pts:(-?\d+)", fixed).group(1))
    assert recovered["audio"] - recovered["video"] == uncut_audio_pes[1][0]["pts"] - uncut_frames[1][0]
    assert recovered["video"] + int(audio[0]["pts"]) + 1920 == oracle["b_video_epoch"]
    oracle["recovered_b_input_epoch"] = recovered
    (tmp_path / "source-timeline-oracle.json").write_text(json.dumps(oracle, indent=2))
    if cut_mode == "truncated-audio":
        assert re.search(r"Packet corrupt \(stream = 1,.*dropping it", log)
    hashes = run("ffmpeg", "-v", "error", "-i", target, "-an", "-fps_mode", "passthrough", "-f", "framemd5", "-").stdout.decode()
    (tmp_path / "output.framemd5").write_text(hashes)
    hashes = [line.rsplit(",", 1)[-1].strip() for line in hashes.splitlines() if not line.startswith("#")]
    assert len(hashes) == len(set(hashes)) == expected
    # Independent SW decode/bwdif reference. Its timestamps are not used as the
    # cadence oracle: reset each stream to sequence indexes ONLY for comparison.
    comparison = run("ffmpeg", "-v", "info", "-i", visual_reference,
                     "-i", target, "-lavfi",
                     "[0:v]settb=1/90000,setpts=N[a];[1:v]settb=1/90000,setpts=N[b];[a][b]ssim=stats_file=" + str(tmp_path / "ssim.log"),
                     "-an", "-f", "null", "-")
    scores = [float(re.search(r"All:([0-9.]+)", line).group(1)) for line in (tmp_path / "ssim.log").read_text().splitlines()]
    assert len(scores) == expected
    assert min(scores) > .95
    assert float(re.search(rb"All:([0-9.]+)", comparison.stderr).group(1)) > .97
    # Every encoded frame must match its own input position more closely than
    # either neighbour: detects repeats/drops that aggregate SSIM can conceal.
    raws = []
    for path, vf in ((visual_reference, ""), (target, "")):
        raw = run("ffmpeg", "-v", "error", "-fflags", "+discardcorrupt", "-i", path, "-an", "-vf", vf + "scale=80:44", "-pix_fmt", "gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-").stdout
        raws.append([raw[i:i + 3520] for i in range(0, len(raw), 3520)])
    assert len(raws[0]) == len(raws[1]) == expected
    matches = []
    for i, actual in enumerate(raws[1]):
        distances = {j: sum(abs(a - b) for a, b in zip(actual, raws[0][j])) for j in range(max(0, i - 2), min(expected, i + 3))}
        assert min(distances, key=distances.get) == i, (i, distances)
        matches.append(dict(index=i, closest_reference_index=min(distances, key=distances.get),
                            distances=distances))
    (tmp_path / "source-image-positions.json").write_text(json.dumps(matches, indent=2))
    pcm = array.array("h", run("ffmpeg", "-v", "error", "-i", target, "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-").stdout)
    tones = [sum(a <= 0 < b for a, b in zip(s, s[1:])) for s in (pcm[24000:72000], pcm[-72000:-24000])]
    assert abs(tones[0] - 440) < 8 and abs(tones[1] - 880) < 8
    packets = json.loads(run("ffprobe", "-v", "error", "-show_packets", "-of", "json", target).stdout)["packets"]
    audio = [p for p in packets if p["codec_type"] == "audio"]
    audio_gaps = [float(b["pts_time"]) - float(a["pts_time"]) - float(a["duration_time"])
                  for a, b in zip(audio, audio[1:])]
    assert max(abs(gap) for gap in audio_gaps) < .00002
    av_head = float(audio[0]["pts_time"]) - pts[0] / 90000
    assert abs(av_head) < .12
    # Bound inserted silence anywhere, including the handoff, rather than
    # proving only the two steady-state tones. AAC padding is permitted.
    quiet_blocks = [max(abs(v) for v in pcm[i:i + 480]) < 100
                    for i in range(0, len(pcm) - 479, 480)]
    longest_quiet = quiet_run = 0
    for quiet in quiet_blocks:
        quiet_run = quiet_run + 1 if quiet else 0
        longest_quiet = max(longest_quiet, quiet_run)
    # TS muxing batches AAC into PES (~0.3 s). Cutting before the next
    # batch leaves a source-side audio gap, filled by the existing aresample.
    # This bound is not a claim of inaudible/seamless audio.
    assert longest_quiet <= 35
    av_tail = float(audio[-1]["pts_time"]) + float(audio[-1]["duration_time"]) - (pts[-1] + step) / 90000
    assert abs(av_tail) < .12
    evidence = dict(kind=kind, cut_mode=cut_mode, bounded_video_pes=bounded_video_pes,
                    complete_source_a_frames=complete_frames_a,
                    intentionally_discarded_incomplete_video_pes=int(cut_mode == "truncated-video"),
                    frames=expected, unique_frames=len(set(hashes)), nominal_pts_step=step,
                    actual_pts_deltas=sorted({b - a for a, b in zip(pts, pts[1:])}),
                    source_position_mismatches=mismatches,
                    max_audio_gap_seconds=max(abs(gap) for gap in audio_gaps),
                    max_quiet_seconds=longest_quiet * .01, av_head_seconds=av_head,
                    min_ssim=min(scores), tones=tones, av_tail_seconds=av_tail,
                    source_sha256=[hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs],
                    scope="finite real QSV profile + product adapter, NOT proxy API")
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print("QSV_CADENCE", json.dumps(evidence))
    assert pts == policy_pts, policy_mismatches
