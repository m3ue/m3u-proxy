from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def test_input_included_for_emby_url():
    cfg = BroadcastConfig(
        network_id="embytest",
        stream_url="http://192.168.11.22:8096/Videos/682/stream.ts?static=true&api_key=abc&StartTimeTicks=4290000000&AudioBitrate=192",
    )

    proc = NetworkBroadcastProcess(cfg, hls_base_dir="/tmp")
    cmd = proc._build_ffmpeg_command()

    # Ensure -i is present and the stream_url is the next argument
    assert "-i" in cmd
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == cfg.stream_url


def test_input_and_headers_ordering_when_headers_provided():
    cfg = BroadcastConfig(
        network_id="hdrtest",
        stream_url="http://example.com/start.m3u8",
        headers={"X-Test": "1"},
    )
    proc = NetworkBroadcastProcess(cfg, hls_base_dir="/tmp")
    cmd = proc._build_ffmpeg_command()

    assert "-headers" in cmd
    h_idx = cmd.index("-headers")
    assert "X-Test: 1" in cmd[h_idx + 1]
    # headers should be followed by -i and the URL
    assert cmd[h_idx + 2] == "-i"
    assert cmd[h_idx + 3] == cfg.stream_url


HLS_EXTENSION_OPTIONS = [
    "-allowed_extensions",
    "-allowed_segment_extensions",
    "-extension_picky",
]


def test_hls_input_relaxes_segment_extension_checks():
    cfg = BroadcastConfig(
        network_id="hlsext",
        stream_url="https://example.com/stream/index.m3u8?token=abc",
    )
    cmd = NetworkBroadcastProcess(cfg, hls_base_dir="/tmp")._build_ffmpeg_command()

    before_input = cmd[: cmd.index("-i")]
    for opt in HLS_EXTENSION_OPTIONS:
        assert opt in before_input
    assert before_input[before_input.index("-extension_picky") + 1] == "0"


def test_non_hls_input_has_no_hls_options():
    cfg = BroadcastConfig(
        network_id="nonhls",
        stream_url="http://192.168.11.22:8096/Videos/682/stream.ts?static=true",
    )
    cmd = NetworkBroadcastProcess(cfg, hls_base_dir="/tmp")._build_ffmpeg_command()

    for opt in HLS_EXTENSION_OPTIONS:
        assert opt not in cmd
