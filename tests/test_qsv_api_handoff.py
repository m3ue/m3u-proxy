"""Real product QSV API: one player GET across automatic upstream EOF.

Explicit sandbox opt-in. Only finite synthetic loopback sources; no providers.
A is cut on a TS packet boundary inside its finite AAC/video transport. B resets
PTS and changes both PMT and elementary PIDs. Capture ends intentionally while
B is active; exact end-frame drain is tested by the finite profile quality suite.
"""
import array
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.test_normalization_quality import probe, run


@pytest.mark.parametrize('audio_codec', ['aac', 'ac3'])
def test_qsv_real_api_automatic_changed_pmt_handoff(tmp_path, audio_codec):
    if os.environ.get('RUN_QSV_HANDOFF') != '1':
        pytest.skip('explicit disposable QSV handoff sandbox required')
    evidence = Path(os.environ.get('QSV_HANDOFF_EVIDENCE', str(tmp_path))) / audio_codec
    evidence.mkdir(parents=True, exist_ok=True)
    data = {}
    inputs = {}
    for label, color, tone, pmt, es in [('a', 'red', 440, 1256, 256), ('b', 'blue', 880, 1512, 512)]:
        file = evidence / (label + '.ts')
        run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:size=320x180:rate=25:duration=20',
            '-f', 'lavfi', '-i', f'sine=frequency={tone}:sample_rate=48000:duration=20',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-threads', '2',
            '-g', '25', '-bf', '0', '-c:a', audio_codec, '-mpegts_pmt_start_pid', str(pmt),
            '-mpegts_start_pid', str(es), '-mpegts_flags', '+system_b',
            '-output_ts_offset', '20' if label == 'a' else '0', '-f', 'mpegts', file)
        data[label] = file.read_bytes()
        description = json.loads(run('ffprobe', '-v', 'error', '-show_programs', '-of', 'json', file).stdout)['programs'][0]
        assert description['pmt_pid'] == pmt
        assert [int(s['id'], 16) for s in description['streams']] == [es, es + 1]
        assert description['streams'][1]['codec_name'] == audio_codec
        inputs[label] = dict(pmt_pid=pmt, elementary_pids=[es, es + 1],
                             start_pts=int(description['streams'][0]['start_pts']))
        if audio_codec == 'ac3':
            # Inspect actual generated DVB PMT, independently of codec metadata.
            packet = next(data[label][i:i + 188] for i in range(0, len(data[label]), 188)
                          if ((data[label][i + 1] & 31) << 8 | data[label][i + 2]) == pmt)
            entry = b'\x06' + (0xE000 | (es + 1)).to_bytes(2, 'big')
            at = packet.index(entry)
            length = ((packet[at + 3] & 15) << 8) | packet[at + 4]
            descriptors = packet[at + 5:at + 5 + length]
            tags = []
            at = 0
            while at < len(descriptors):
                tags.append(descriptors[at])
                at += 2 + descriptors[at + 1]
            assert at == len(descriptors) and 0x6A in tags
            inputs[label]['audio_stream_type'] = 6
            inputs[label]['audio_descriptors_hex'] = descriptors.hex()
    # FFmpeg's explicit positive output offset anchors A's video at 21.4 s;
    # default mux timing shifts B's video by codec-specific encoder priming.
    # Pin both actual epochs exactly rather than assuming offset=PTS delta.
    priming = 1920 if audio_codec == 'aac' else 480
    assert inputs['a']['start_pts'] == 20 * 90000 + 126000
    assert inputs['b']['start_pts'] == 126000 + priming
    # Simulated provider close, deliberately not an access-unit/PES aligned cut.
    data['a'] = data['a'][:int(len(data['a']) * 6 / 20) // 188 * 188]
    stop = threading.Event()
    requests = []
    snapshots = []
    errors = []
    process = None

    def snapshot(stage):
        try:
            for child in Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split():
                proc = Path('/proc') / child
                if b'h264_qsv' not in (proc / 'cmdline').read_bytes():
                    continue
                fds = [fd for fd in (proc / 'fd').iterdir() if str(fd.readlink()) == '/dev/dri/renderD128']
                assert fds
                engines = [int(line.split()[1]) for fd in fds for line in (proc / 'fdinfo' / fd.name).read_text().splitlines() if line.startswith('drm-engine-video:')]
                snapshots.append(dict(stage=stage, pid=int(child), start_ticks=(proc / 'stat').read_text().split()[21], render_fds=[fd.name for fd in fds], video_ns=max(engines)))
                return
            raise AssertionError('no owned h264_qsv child')
        except BaseException as exc:
            errors.append(repr(exc))

    class Source(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            label = self.path.rsplit('/', 1)[-1].split('.')[0]
            requests.append(label)
            if label not in data or requests.count(label) > 1:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            content = data[label]
            try:
                for offset in range(0, len(content), 1316):
                    if stop.is_set():
                        break
                    chunk = content[offset:offset + 1316]
                    self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    if label == 'b' and offset <= len(content) // 3 < offset + 1316:
                        snapshot('B-active')
                    stop.wait((6 if label == 'a' else 20) * len(chunk) / len(content) / 2)
                if label == 'a':
                    snapshot('A-EOF')
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'LIBVA_DRIVER_NAME', 'LIBVA_DRIVERS_PATH', 'LD_LIBRARY_PATH') if key in os.environ}
    env.update(PYTHONPATH=str(root / 'src'), ROOT_PATH='', REDIS_ENABLED='false',
               ENABLE_TRANSCODING_POOLING='true', ENABLE_SINGLE_PROCESS_TRANSCODING='true',
               LIVE_NORMALIZE_OUTPUT='false', WORKER_ID='', API_TOKEN='',
               STRICT_LIVE_TS_PREBUFFER_SIZE='0', DEFAULT_MAX_RETRIES='0',
               STREAM_RETRY_ATTEMPTS='0', LIVE_SILENT_CLOSE_RETRY_WINDOW_SECONDS='0',
               LOG_LEVEL='info', TEMP_DIR=str(tmp_path / 'streams'), LOG_DIR=str(tmp_path / 'logs'),
               HLS_TEMP_DIR=str(tmp_path / 'hls'), HLS_BROADCAST_DIR=str(tmp_path / 'broadcast'),
               DVR_RECORDING_DIR=str(tmp_path / 'dvr'))
    capture = evidence / 'capture.ts'
    operations = []
    with (evidence / 'proxy.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'api:app', '--fd', str(listener.fileno()), '--log-level', 'warning'],
                                   pass_fds=(listener.fileno(),), cwd=tmp_path, env=env, stdout=log, stderr=log)
        listener.close()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=25, trust_env=False) as client:
                for _ in range(100):
                    assert process.poll() is None
                    try:
                        if client.get('/health').status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    pytest.fail('owned API did not start')
                operations.append('POST /streams')
                created = client.post('/streams', json=dict(url=f'http://127.0.0.1:{server.server_port}/a.ts',
                                      failover_urls=[f'http://127.0.0.1:{server.server_port}/b.ts'],
                                      normalize_live_output=True, normalization_encoder='qsv'))
                assert created.status_code == 200, created.text
                info = created.json()
                assert info['metadata']['normalization_encoder'] == 'qsv'
                operations.append('GET player')
                started = time.monotonic()
                with client.stream('GET', info['stream_endpoint']) as response, capture.open('wb') as out:
                    assert response.status_code == 200
                    for chunk in response.iter_bytes():
                        out.write(chunk)
                        if len(snapshots) >= 2 and time.monotonic() - started > 9:
                            break
                state = client.get(f"/streams/{info['stream_id']}").json()['stream']
                (evidence / 'state.json').write_text(json.dumps(state, indent=2))
                assert client.delete(f"/streams/{info['stream_id']}").status_code == 200
                assert client.get('/streams').json()['streams'] == []
        finally:
            stop.set()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    assert not errors, errors
    assert requests == ['a', 'b'], requests
    assert len(snapshots) == 2, snapshots
    a, b = snapshots
    assert a['pid'] == b['pid'] and a['start_ticks'] == b['start_ticks']
    assert a['render_fds'] == b['render_fds']
    assert b['video_ns'] > a['video_ns']
    frames = probe(capture)['frames']
    pts = [int(frame['pts']) for frame in frames]
    steps = sorted(set(y - x for x, y in zip(pts, pts[1:])))
    # This is the strict product cadence gate, not merely monotonic timestamps.
    assert steps == [3600], steps
    assert all(frame['interlaced_frame'] == 0 for frame in frames)
    rgb = run('ffmpeg', '-v', 'error', '-i', capture, '-an', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-').stdout
    colors = ['a' if rgb[i] > rgb[i + 2] else 'b' for i in range(0, len(rgb), 3)]
    assert colors.count('a') >= 100 and colors.count('b') >= 100
    assert colors == sorted(colors), 'unexpected visual source reversal'
    ssim = {}
    for label, start in (('a', 0), ('b', colors.count('a'))):
        count = colors.count(label)
        stats = evidence / f'{label}-ssim.log'
        run('ffmpeg', '-v', 'error', '-i', capture, '-i', evidence / f'{label}.ts',
            '-lavfi',
            f'[0:v]trim=start_frame={start}:end_frame={start + count},settb=1/90000,setpts=N[a];'
            f'[1:v]trim=end_frame={count},settb=1/90000,setpts=N[b];'
            f'[a][b]ssim=stats_file={stats}', '-an', '-f', 'null', '-')
        scores = [float(line.split('All:')[1].split()[0]) for line in stats.read_text().splitlines()]
        assert len(scores) == count and min(scores) > .99
        ssim[label] = min(scores)
    pcm = run('ffmpeg', '-v', 'error', '-i', capture, '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-').stdout
    samples = array.array('f', pcm)
    def tone_at(seconds):
        window = samples[int(seconds * 48000):int((seconds + .5) * 48000)]
        assert len(window) == 24000
        return sum(x <= 0 < y for x, y in zip(window, window[1:])) * 2
    tones = [tone_at(2), tone_at(9)]
    assert abs(tones[0] - 440) <= 4 and abs(tones[1] - 880) <= 4, tones
    packets = json.loads(run('ffprobe', '-v', 'error', '-show_packets', '-of', 'json', capture).stdout)['packets']
    gaps = {}
    for stream in (0, 1):
        times = [int(p['dts']) for p in packets if p['stream_index'] == stream and 'dts' in p]
        assert all(y > x for x, y in zip(times, times[1:]))
        gaps[str(stream)] = max(y - x for x, y in zip(times, times[1:])) / 90000
    assert max(gaps.values()) < .15, gaps
    result = dict(scope='real product API, intentional capture cut while finite B active',
                  operations=operations, upstream_requests=requests, snapshots=snapshots,
                  frames=len(frames), frames_a=colors.count('a'), frames_b=colors.count('b'),
                  pts_steps=steps, min_ssim=ssim, tones_hz=tones, maximum_packet_gaps=gaps,
                  encoder='qsv', input_audio_codec=audio_codec, inputs=inputs,
                  automatic_a_eof=True, changed_pmt_and_es=True,
                  pts_reset_ticks=inputs['a']['start_pts'] - inputs['b']['start_pts'])
    (evidence / 'handoff.json').write_text(json.dumps(result, indent=2))
    print('API_QSV_HANDOFF', json.dumps(result))
