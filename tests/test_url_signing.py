"""
Regression tests for the unauthenticated file read / SSRF report: the segment,
file and ?url= routes must only fetch URLs the proxy signed while rewriting a
playlist or manifest, and local reads are limited to transcoder output dirs.

Signatures are also bound to the issuing stream and route family (#74): a URL
signed in one stream's playlist must not be replayable under another stream's
route, where it would be fetched with that stream's upstream headers.
"""

import sys
import os
import tempfile
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

import api
from stream_manager import DashProcessor, M3U8Processor, StreamManager
from url_signing import (
    ROLE_DASH_BASE,
    ROLE_DASH_FILE,
    ROLE_HLS_SEGMENT,
    hls_playlist_role,
    sign_url,
    verify_url,
)

UPSTREAM_SEGMENT = "https://upstream.example.com/live/seg1.ts"

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
seg1.ts
#EXTINF:4.0,
seg2.ts
"""

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000
high/index.m3u8
"""


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _sig(url: str, stream_id: str = "x", role: str = ROLE_HLS_SEGMENT) -> str:
    return sign_url(url, stream_id=stream_id, role=role)


class TestSigning:
    def test_signature_round_trip(self):
        assert verify_url(
            UPSTREAM_SEGMENT,
            _sig(UPSTREAM_SEGMENT),
            stream_id="x",
            role=ROLE_HLS_SEGMENT,
        )

    def test_missing_or_mismatched_signature_is_rejected(self):
        for signature in (None, "", _sig(UPSTREAM_SEGMENT), "n\u00e9-ascii"):
            assert not verify_url(
                "/etc/passwd", signature, stream_id="x", role=ROLE_HLS_SEGMENT
            )

    def test_signature_is_bound_to_stream(self):
        assert not verify_url(
            UPSTREAM_SEGMENT,
            _sig(UPSTREAM_SEGMENT, stream_id="a"),
            stream_id="b",
            role=ROLE_HLS_SEGMENT,
        )

    @pytest.mark.parametrize(
        "other_role", [ROLE_DASH_FILE, ROLE_DASH_BASE, hls_playlist_role("x")]
    )
    def test_signature_is_bound_to_route_family(self, other_role):
        assert not verify_url(
            UPSTREAM_SEGMENT, _sig(UPSTREAM_SEGMENT), stream_id="x", role=other_role
        )

    def test_media_playlist_segments_are_signed(self):
        processor = M3U8Processor("/hls/s1", "client-1", stream_id="s1")
        result = processor.process_playlist(
            MEDIA_PLAYLIST, "/hls/s1", "https://upstream.example.com/live/"
        )

        lines = [line for line in result.splitlines() if "/segment" in line]
        assert len(lines) == 2
        for line in lines:
            params = _query(line)
            assert verify_url(
                params["url"], params["sig"], stream_id="s1", role=ROLE_HLS_SEGMENT
            )

    def test_variant_playlist_links_are_signed_with_parent(self):
        processor = M3U8Processor(
            "/hls/s1", "client-1", parent_stream_id="s1", stream_id="s1"
        )
        result = processor.process_playlist(
            MASTER_PLAYLIST, "/hls/s1", "https://upstream.example.com/live/"
        )

        lines = [line for line in result.splitlines() if "playlist.m3u8" in line]
        assert len(lines) == 2
        for line in lines:
            params = _query(line)
            assert params["parent"] == "s1"
            assert verify_url(
                params["url"],
                params["sig"],
                stream_id="s1",
                role=hls_playlist_role("s1"),
            )
            assert not verify_url(
                params["url"],
                params["sig"],
                stream_id="s1",
                role=hls_playlist_role("s2"),
            )

    def test_dash_base_token_round_trip_and_tamper(self):
        base = "https://upstream.example.com/live/"
        token = DashProcessor.sign_base(base, "s1")

        assert DashProcessor.verify_base(token, "s1") == base
        assert DashProcessor.verify_base(token, "s2") is None
        assert DashProcessor.verify_base(DashProcessor.encode_base(base), "s1") is None
        forged = (
            DashProcessor.encode_base("http://127.0.0.1:8080/")
            + "."
            + token.split(".")[1]
        )
        assert DashProcessor.verify_base(forged, "s1") is None
        assert DashProcessor.verify_base("!!!not-base64!!!", "s1") is None


@pytest.fixture
def mocked_client():
    async def _body():
        yield b"segment-bytes"

    with patch("api.stream_manager") as sm, patch("api.event_manager") as em:
        sm.streams = {}
        sm.clients = {}
        sm.register_client = AsyncMock()
        sm.proxy_hls_segment = AsyncMock(
            side_effect=lambda *a, **k: StreamingResponse(
                _body(), media_type="video/mp2t"
            )
        )
        sm.proxy_dash_segment = AsyncMock(
            side_effect=lambda *a, **k: StreamingResponse(
                _body(), media_type="video/mp4"
            )
        )
        sm.get_or_create_stream = AsyncMock(return_value="created-stream")
        sm.get_playlist_content = AsyncMock(return_value="#EXTM3U\n")
        em.emit_event = AsyncMock()
        yield TestClient(api.app), sm


class TestHlsSegmentRoute:
    @pytest.mark.parametrize(
        "suffix", ["segment", "segment.ts", "segment.m4s", "segment.mp4"]
    )
    @pytest.mark.parametrize(
        "target",
        [
            "/etc/passwd",
            "file:///etc/hostname",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_unsigned_urls_are_rejected_before_any_fetch(
        self, mocked_client, suffix, target
    ):
        client, sm = mocked_client
        resp = client.get(f"/hls/x/{suffix}", params={"client_id": "a", "url": target})

        assert resp.status_code == 403
        sm.proxy_hls_segment.assert_not_awaited()
        sm.register_client.assert_not_awaited()

    def test_signature_for_another_url_is_rejected(self, mocked_client):
        client, sm = mocked_client
        resp = client.get(
            "/hls/x/segment",
            params={
                "client_id": "a",
                "url": "/etc/passwd",
                "sig": _sig(UPSTREAM_SEGMENT),
            },
        )

        assert resp.status_code == 403
        sm.proxy_hls_segment.assert_not_awaited()

    def test_signed_url_is_proxied(self, mocked_client):
        client, sm = mocked_client
        resp = client.get(
            "/hls/x/segment.ts",
            params={
                "client_id": "a",
                "url": UPSTREAM_SEGMENT,
                "sig": _sig(UPSTREAM_SEGMENT),
            },
        )

        assert resp.status_code == 200
        assert sm.proxy_hls_segment.await_args.args[2] == UPSTREAM_SEGMENT

    def test_signature_from_another_stream_is_rejected(self, mocked_client):
        client, sm = mocked_client
        resp = client.get(
            "/hls/b/segment.ts",
            params={
                "client_id": "a",
                "url": UPSTREAM_SEGMENT,
                "sig": _sig(UPSTREAM_SEGMENT, stream_id="a"),
            },
        )

        assert resp.status_code == 403
        sm.proxy_hls_segment.assert_not_awaited()
        sm.register_client.assert_not_awaited()


class TestDashRoutes:
    def test_file_route_rejects_hls_segment_signature(self, mocked_client):
        client, sm = mocked_client
        resp = client.get(
            "/dash/x/file",
            params={
                "client_id": "a",
                "url": UPSTREAM_SEGMENT,
                "sig": _sig(UPSTREAM_SEGMENT, stream_id="x", role=ROLE_HLS_SEGMENT),
            },
        )

        assert resp.status_code == 403
        sm.proxy_dash_segment.assert_not_awaited()

    def test_segment_route_rejects_base_from_another_stream(self, mocked_client):
        client, sm = mocked_client
        token = DashProcessor.sign_base("https://upstream.example.com/live/", "a")
        resp = client.get(f"/dash/b/segment/c/{token}/seg.m4s")

        assert resp.status_code == 403
        sm.proxy_dash_segment.assert_not_awaited()

    def test_file_route_rejects_unsigned_url(self, mocked_client):
        client, sm = mocked_client
        resp = client.get(
            "/dash/x/file", params={"client_id": "a", "url": "http://127.0.0.1:6379/"}
        )

        assert resp.status_code == 403
        sm.proxy_dash_segment.assert_not_awaited()

    def test_segment_route_rejects_unsigned_base(self, mocked_client):
        client, sm = mocked_client
        token = DashProcessor.encode_base("http://127.0.0.1:6379/")
        resp = client.get(f"/dash/x/segment/a/{token}/seg.m4s")

        assert resp.status_code == 403
        sm.proxy_dash_segment.assert_not_awaited()

    @pytest.mark.parametrize(
        "path", ["//evil.example/x.m4s", "http://evil.example/x.m4s"]
    )
    def test_segment_route_keeps_signed_host(self, mocked_client, path):
        client, sm = mocked_client
        token = DashProcessor.sign_base("https://upstream.example.com/live/", "x")
        resp = client.get(f"/dash/x/segment/a/{token}/{path}")

        assert resp.status_code == 403
        sm.proxy_dash_segment.assert_not_awaited()

    def test_segment_route_proxies_signed_base(self, mocked_client):
        client, sm = mocked_client
        token = DashProcessor.sign_base("https://upstream.example.com/live/", "x")
        resp = client.get(f"/dash/x/segment/a/{token}/video/seg-1.m4s")

        assert resp.status_code == 200
        assert (
            sm.proxy_dash_segment.await_args.args[2]
            == "https://upstream.example.com/live/video/seg-1.m4s"
        )


class TestRewrittenUrlsRoundTrip:
    """Players replay the rewritten URLs verbatim; they must pass verification."""

    def test_hls_segment_line_from_playlist_is_accepted(self, mocked_client):
        client, sm = mocked_client
        playlist = "#EXTM3U\n#EXTINF:4.0,\nseg1.ts?token=a%2Bb&exp=1\n"
        processor = M3U8Processor("/hls/s1", "client-1", stream_id="s1")
        result = processor.process_playlist(
            playlist, "/hls/s1", "https://upstream.example.com/live/"
        )
        line = next(line for line in result.splitlines() if "/segment" in line)

        resp = client.get(line)

        assert resp.status_code == 200
        sm.proxy_hls_segment.assert_awaited()

    def test_dash_segment_from_manifest_base_is_accepted(self, mocked_client):
        client, sm = mocked_client
        manifest = (
            '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011">'
            "<BaseURL>https://upstream.example.com/live/</BaseURL><Period/></MPD>"
        )
        processor = DashProcessor("/dash/s1", "client-1", stream_id="s1")
        result = processor.process_manifest(
            manifest, "/dash/s1", "https://upstream.example.com/"
        )
        base = result.split("<BaseURL>", 1)[1].split("</BaseURL>", 1)[0]

        resp = client.get(f"{base}chunk-1.m4s")

        assert resp.status_code == 200
        assert (
            sm.proxy_dash_segment.await_args.args[2]
            == "https://upstream.example.com/live/chunk-1.m4s"
        )


class TestUrlQueryStreamCreation:
    def test_unsigned_url_requires_api_token_when_configured(self, mocked_client):
        client, sm = mocked_client
        with patch.object(api.settings, "API_TOKEN", "configured-token"):
            for path in ("/stream/x", "/hls/x/playlist.m3u8", "/dash/x/manifest.mpd"):
                resp = client.get(path, params={"url": "http://ifconfig.me/all"})
                assert resp.status_code == 401, path
        sm.get_or_create_stream.assert_not_awaited()

    def test_signed_variant_url_is_accepted(self, mocked_client):
        client, sm = mocked_client
        variant = "https://upstream.example.com/live/low/index.m3u8"
        with patch.object(api.settings, "API_TOKEN", "configured-token"):
            resp = client.get(
                "/hls/x/playlist.m3u8",
                params={
                    "url": variant,
                    "parent": "x",
                    "sig": _sig(variant, role=hls_playlist_role("x")),
                },
            )

        assert resp.status_code == 200
        assert sm.get_or_create_stream.await_args.args[0] == variant

    @pytest.mark.parametrize(
        "path, parent",
        [
            ("/hls/other/playlist.m3u8", "x"),  # replayed under another stream
            ("/hls/x/playlist.m3u8", "other"),  # parent swapped to another stream
        ],
    )
    def test_variant_signature_is_bound_to_stream_and_parent(
        self, mocked_client, path, parent
    ):
        client, sm = mocked_client
        variant = "https://upstream.example.com/live/low/index.m3u8"
        with patch.object(api.settings, "API_TOKEN", "configured-token"):
            resp = client.get(
                path,
                params={
                    "url": variant,
                    "parent": parent,
                    "sig": _sig(variant, role=hls_playlist_role("x")),
                },
            )

        # Not a valid signature, so it falls back to the management token check
        assert resp.status_code == 401
        sm.get_or_create_stream.assert_not_awaited()

    def test_api_token_still_allows_direct_url(self, mocked_client):
        client, sm = mocked_client
        with patch.object(api.settings, "API_TOKEN", "configured-token"):
            resp = client.get(
                "/hls/x/playlist.m3u8",
                params={
                    "url": "https://upstream.example.com/a.m3u8",
                    "api_token": "configured-token",
                },
            )

        assert resp.status_code == 200
        sm.get_or_create_stream.assert_awaited()

    def test_open_mode_without_api_token_is_unchanged(self, mocked_client):
        client, sm = mocked_client
        with patch.object(api.settings, "API_TOKEN", None):
            resp = client.get(
                "/hls/x/playlist.m3u8",
                params={"url": "https://upstream.example.com/a.m3u8"},
            )

        assert resp.status_code == 200
        sm.get_or_create_stream.assert_awaited()


class TestLocalSegmentContainment:
    """Defense in depth: even a signed local URL must be transcoder output."""

    @pytest.fixture
    def hls_dir(self):
        path = tempfile.mkdtemp(prefix="m3u_proxy_hls_test_")
        with open(os.path.join(path, "seg1.ts"), "wb") as fh:
            fh.write(b"TS")
        yield path
        for name in os.listdir(path):
            os.unlink(os.path.join(path, name))
        os.rmdir(path)

    def test_transcoder_output_is_allowed(self, hls_dir):
        seg = os.path.join(hls_dir, "seg1.ts")
        assert StreamManager._local_segment_path(f"file://{seg}") == os.path.realpath(
            seg
        )

    @pytest.mark.parametrize(
        "make_url",
        [
            lambda d: "/etc/hosts",
            lambda d: "file:///etc/hosts",
            lambda d: f"file://{d}/../../../../etc/hosts",
            lambda d: f"file://{tempfile.gettempdir()}/not_a_transcoder_dir/x.ts",
        ],
    )
    def test_paths_outside_transcoder_output_are_refused(self, hls_dir, make_url):
        assert StreamManager._local_segment_path(make_url(hls_dir)) is None

    def test_symlink_out_of_transcoder_dir_is_refused(self, hls_dir):
        link = os.path.join(hls_dir, "escape.ts")
        os.symlink("/etc/hosts", link)
        assert StreamManager._local_segment_path(f"file://{link}") is None

    @pytest.mark.asyncio
    async def test_proxy_hls_segment_raises_403_for_outside_path(self):
        sm = StreamManager()
        with pytest.raises(HTTPException) as exc:
            await sm.proxy_hls_segment("x", "a", "/etc/hosts")
        assert exc.value.status_code == 403


class TestHeadersFollowIssuingStream:
    """End to end with a real StreamManager: the outbound headers on a segment
    fetch belong to the stream that issued the link, never the route it is
    replayed under (#74)."""

    @pytest.mark.asyncio
    async def test_link_from_stream_a_cannot_carry_stream_b_headers(self, monkeypatch):
        monkeypatch.setattr(api.settings, "API_TOKEN", "configured-token")
        monkeypatch.setattr(api.settings, "ROOT_PATH", "")
        captured = []

        def upstream(request):
            captured.append(dict(request.headers))
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(
                    200, text="#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg.ts\n"
                )
            return httpx.Response(200, content=b"media")

        manager = StreamManager()
        await manager.http_client.aclose()
        manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        monkeypatch.setattr(api, "stream_manager", manager)
        try:
            stream_a = await manager.get_or_create_stream(
                "http://attacker.test/index.m3u8"
            )
            stream_b = await manager.get_or_create_stream(
                "http://provider.test/index.m3u8",
                user_agent="provider-agent",
                headers={"Authorization": "Bearer provider-secret"},
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app), base_url="http://proxy"
            ) as client:
                playlist = await client.get(f"/hls/{stream_a}/playlist.m3u8")
                link = next(
                    line for line in playlist.text.splitlines() if "/segment" in line
                )

                transplanted = await client.get(
                    link.replace(f"/hls/{stream_a}/", f"/hls/{stream_b}/")
                )
                assert transplanted.status_code == 403

                replayed = await client.get(link)
                assert replayed.status_code == 200
                assert replayed.content == b"media"
        finally:
            await manager.stop()

        # Playlist + the issuing stream's own segment replay; never B's headers
        assert len(captured) == 2
        for headers in captured:
            assert "authorization" not in headers
            assert headers["user-agent"] != "provider-agent"
