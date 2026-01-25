import pytest
from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def test_custom_headers_in_ffmpeg_command():
    config = BroadcastConfig(
        network_id="testnet",
        stream_url="http://example.com/start.m3u8",
        headers={"X-Test-Header": "hello", "User-Agent": "m3u-proxy-test"}
    )

    proc = NetworkBroadcastProcess(config, hls_base_dir="/tmp")
    cmd = proc._build_ffmpeg_command()

    # Ensure -headers and the header values exist in the command
    assert "-headers" in cmd
    header_index = cmd.index("-headers")
    header_value = cmd[header_index + 1]
    assert "X-Test-Header: hello" in header_value
    assert "User-Agent: m3u-proxy-test" in header_value
