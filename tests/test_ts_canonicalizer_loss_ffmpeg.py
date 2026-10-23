"""Real CPU demux/decode regression; synthetic media only, no GPU/provider."""
import re
import shutil
import subprocess

import pytest

from tests.test_ts_canonicalizer import pid, split
from ts_canonicalizer import TSCanonicalizer


def run_ffmpeg(*args):
    return subprocess.run(['ffmpeg', '-nostdin', '-v', 'warning', *args],
                          capture_output=True, check=True, timeout=60)


def missing_slice(data):
    """Remove a complete NAL from frame 5 but retain a transport loss CC gap.

    An unbounded PES has no length/CRC clue: the CC gap is the only demux
    corruption signal. A complete missing NAL can still decode successfully.
    """
    packets = split(data)
    starts = [i for i, p in enumerate(packets) if pid(p) == 256 and p[1] & 64]
    begin, end = starts[5:7]
    selected = [p for p in packets[begin:end] if pid(p) == 256]
    pes = b''.join(p[5 + p[4]:] if p[3] & 32 else p[4:] for p in selected)
    assert pes[:3] == b'\0\0\1' and pes[4:6] == b'\0\0'
    header_len = 9 + pes[8]
    es = pes[header_len:]
    nals = list(re.finditer(b'\0\0\1', es))
    slices = [(m.start(), nals[i + 1].start() if i + 1 < len(nals) else len(es))
              for i, m in enumerate(nals) if es[m.end()] & 31 == 1]
    assert len(slices) == 8
    a, b = slices[2]
    modified = pes[:header_len] + es[:a] + es[b:]
    cc = selected[0][3] & 15
    new = []
    for i in range(0, len(modified), 184):
        chunk = modified[i:i + 184]
        padding = 184 - len(chunk)
        if padding:
            adapt = bytes([padding - 1]) + (b'\0' + b'\xff' * (padding - 2)
                                                  if padding >= 2 else b'')
            control = 0x30
        else:
            adapt, control = b'', 0x10
        packet_cc = (cc + (i != 0)) % 16
        new.append(bytes([0x47, 0x41 if i == 0 else 1, 0, control | packet_cc])
                   + adapt + chunk)
        cc = (cc + 1) % 16
    result = packets[:begin] + new + [p for p in packets[begin:end] if pid(p) != 256] + packets[end:]
    # Resume after the deliberately marked gap, not another accidental gap
    # caused by the different number of repacketized packets.
    cc = ((new[-1][3] & 15) + 1) % 16
    for i in range(begin + len(new), len(result)):
        p = result[i]
        if pid(p) == 256:
            result[i] = p[:3] + bytes([(p[3] & 0xf0) | cc]) + p[4:]
            if p[3] & 16:
                cc = (cc + 1) % 16
    return b''.join(result)


def decode(path):
    result = run_ffmpeg('-fflags', '+genpts+discardcorrupt', '-err_detect:v', 'explode',
                        '-ec:v', '0', '-threads', '1', '-i', str(path), '-an',
                        '-fps_mode', 'passthrough', '-f', 'framemd5', '-')
    return [line for line in result.stdout.decode().splitlines() if not line.startswith('#')]


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='CPU ffmpeg required')
def test_complete_missing_slice_retains_demux_discard_and_pixel_hashes(tmp_path):
    clean = tmp_path / 'clean.ts'
    run_ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25',
               '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000', '-t', '1',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-threads:v', '1',
               '-x264-params', 'slices=8:keyint=25:scenecut=0:bframes=0',
               '-c:a', 'aac', '-f', 'mpegts', str(clean))
    raw = tmp_path / 'loss.ts'
    canonical = tmp_path / 'canonical.ts'
    raw.write_bytes(missing_slice(clean.read_bytes()))
    c = TSCanonicalizer()
    canonical.write_bytes(c.feed(raw.read_bytes()) + c.finish())
    clean_rows, raw_rows, canonical_rows = decode(clean), decode(raw), decode(canonical)
    assert len(clean_rows) == 25
    assert len(raw_rows) == 24
    # Exact PTS and decoded pixels: no extra concealed frame and no extra
    # loss of otherwise retained frames compared with the raw demux policy.
    assert canonical_rows == raw_rows
    clean_c = TSCanonicalizer()
    canonical.write_bytes(clean_c.feed(clean.read_bytes()) + clean_c.finish())
    assert decode(canonical) == clean_rows
