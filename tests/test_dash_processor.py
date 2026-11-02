from stream_manager import DashProcessor
from url_signing import ROLE_DASH_FILE, sign_url
import logging

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


MPD_WITH_RELATIVE_BASEURL = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT60S">
  <Period>
    <BaseURL>dash/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000">
        <SegmentTemplate media="chunk-$RepresentationID$-$Number%05d$.m4s" initialization="init-$RepresentationID$.m4s" startNumber="1" duration="4" timescale="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

MPD_WITHOUT_BASEURL = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT60S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000">
        <SegmentTemplate media="chunk-$RepresentationID$-$Time$.m4s" initialization="init-$RepresentationID$.m4s" timescale="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

MPD_WITH_SEGMENT_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000">
        <SegmentList duration="10">
          <Initialization sourceURL="https://upstream.example.com/video/init.mp4"/>
          <SegmentURL media="https://upstream.example.com/video/seg1.m4s"/>
          <SegmentURL media="https://upstream.example.com/video/seg2.m4s"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

MPD_WITH_CONTENT_PROTECTION = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013" type="static" mediaPresentationDuration="PT60S">
  <Period>
    <BaseURL>dash/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc" cenc:default_KID="11111111-2222-3333-4444-555555555555"/>
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
        <cenc:pssh>SGVsbG8gV29ybGQ=</cenc:pssh>
      </ContentProtection>
      <Representation id="v0" bandwidth="500000">
        <SegmentTemplate media="chunk-$RepresentationID$-$Number%05d$.m4s" initialization="init-$RepresentationID$.m4s" startNumber="1" duration="4" timescale="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

MPD_MULTI_PERIOD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
  <Period id="p1">
    <BaseURL>period1/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000">
        <SegmentTemplate media="chunk-$Number%05d$.m4s" startNumber="1" duration="4" timescale="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
  <Period id="p2">
    <BaseURL>period2/</BaseURL>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000">
        <SegmentTemplate media="chunk-$Number%05d$.m4s" startNumber="1" duration="4" timescale="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


class TestDashProcessorBaseUrlRewriting:
    def test_relative_baseurl_is_rewritten_and_resolved_against_manifest_base(self):
        processor = DashProcessor(
            "http://proxy.com/dash/stream123", "cid-1", stream_id="stream123"
        )

        result = processor.process_manifest(
            MPD_WITH_RELATIVE_BASEURL,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/live/",
        )

        # The relative BaseURL "dash/" must resolve against the manifest base
        # before being encoded, i.e. https://upstream.example.com/live/dash/
        # Rewritten BaseURL shape: .../segment/{client_id}/{encoded_base}/
        assert "http://proxy.com/dash/stream123/segment/cid-1/" in result
        assert "<BaseURL>" in result

        token = result.split("/segment/cid-1/", 1)[1].split("/", 1)[0]
        assert (
            DashProcessor.verify_base(token, "stream123")
            == "https://upstream.example.com/live/dash/"
        )

        # SegmentTemplate placeholders must survive untouched - they are
        # resolved client-side against the rewritten BaseURL.
        assert "chunk-$RepresentationID$-$Number%05d$.m4s" in result
        assert "init-$RepresentationID$.m4s" in result

    def test_missing_baseurl_gets_one_injected(self):
        processor = DashProcessor(
            "http://proxy.com/dash/stream123", "cid-1", stream_id="stream123"
        )

        result = processor.process_manifest(
            MPD_WITHOUT_BASEURL,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/live/",
        )

        assert "<BaseURL>http://proxy.com/dash/stream123/segment/cid-1/" in result
        token = result.split("/segment/cid-1/", 1)[1].split("/", 1)[0]
        assert (
            DashProcessor.verify_base(token, "stream123")
            == "https://upstream.example.com/live/"
        )

    def test_multi_period_rewrites_each_period_baseurl_independently(self):
        processor = DashProcessor(
            "http://proxy.com/dash/stream123", "cid-1", stream_id="stream123"
        )

        result = processor.process_manifest(
            MPD_MULTI_PERIOD,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/live/",
        )

        bases = [
            DashProcessor.verify_base(chunk.split("/", 1)[0], "stream123")
            for chunk in result.split("http://proxy.com/dash/stream123/segment/cid-1/")[
                1:
            ]
        ]
        assert "https://upstream.example.com/live/period1/" in bases
        assert "https://upstream.example.com/live/period2/" in bases


class TestDashProcessorSegmentList:
    def test_absolute_segment_urls_rewritten_through_file_route(self):
        processor = DashProcessor("http://proxy.com/dash/stream123", "stream123")

        result = processor.process_manifest(
            MPD_WITH_SEGMENT_LIST,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/video/",
        )

        # Absolute SegmentList URLs are explicit URIs used verbatim, so the
        # rewrite matches HLS byte-for-byte: ?url=<encoded>&client_id=<id>.
        assert "http://proxy.com/dash/stream123/file?url=" in result
        # XML-escaped inside an attribute value (any DASH parser unescapes it).
        assert "&amp;client_id=stream123" in result

        from urllib.parse import unquote

        assert "https://upstream.example.com/video/init.mp4" in unquote(result)
        assert "https://upstream.example.com/video/seg1.m4s" in unquote(result)
        assert "https://upstream.example.com/video/seg2.m4s" in unquote(result)


class TestDashProcessorContentProtection:
    def test_content_protection_sets_encrypted_flag(self):
        processor = DashProcessor("http://proxy.com/dash/stream123", "stream123")
        assert processor.is_encrypted is False

        processor.process_manifest(
            MPD_WITH_CONTENT_PROTECTION,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/live/",
        )

        assert processor.is_encrypted is True

    def test_unencrypted_manifest_leaves_flag_false(self):
        processor = DashProcessor("http://proxy.com/dash/stream123", "stream123")

        processor.process_manifest(
            MPD_WITH_RELATIVE_BASEURL,
            "http://proxy.com/dash/stream123",
            "https://upstream.example.com/live/",
        )

        assert processor.is_encrypted is False

    def test_drm_fields_never_logged(self, caplog):
        """KID/PSSH/license material must never reach the logs, even as a byproduct
        of processing - only the boolean encrypted state is derived."""
        processor = DashProcessor("http://proxy.com/dash/stream123", "stream123")

        with caplog.at_level(logging.DEBUG):
            processor.process_manifest(
                MPD_WITH_CONTENT_PROTECTION,
                "http://proxy.com/dash/stream123",
                "https://upstream.example.com/live/",
            )

        log_text = caplog.text
        assert "11111111-2222-3333-4444-555555555555" not in log_text
        assert "SGVsbG8gV29ybGQ=" not in log_text


class TestDashProcessorEncoding:
    def test_encode_decode_base_roundtrip(self):
        url = "https://upstream.example.com/some/path/with?query=1&other=2"
        encoded = DashProcessor.encode_base(url)
        assert "/" not in encoded
        assert DashProcessor.decode_base(encoded) == url


class TestDashContentType:
    def test_get_content_type_mpd(self):
        from api import get_content_type

        assert (
            get_content_type("https://example.com/live/stream.mpd")
            == "application/dash+xml"
        )

    def test_is_dash_stream(self):
        from api import is_dash_stream, is_direct_stream

        assert is_dash_stream("https://example.com/live/stream.mpd") is True
        assert is_dash_stream("https://example.com/live/stream.m3u8") is False
        assert is_direct_stream("https://example.com/live/stream.mpd") is False

    def test_dash_file_content_type(self):
        from stream_manager import StreamManager

        assert StreamManager._dash_file_content_type("seg1.m4s") == "video/iso.segment"
        assert StreamManager._dash_file_content_type("init.mp4") == "video/mp4"
        assert (
            StreamManager._dash_file_content_type("unknown.bin")
            == "application/octet-stream"
        )


class TestDashSegmentRoutes:
    """The DASH segment/file routes mirror the HLS segment route: take the
    client_id off the request, register it against the parent stream, hand to
    proxy_dash_segment. HLS carries client_id as `&client_id=` on each segment
    line; DASH SegmentTemplate can't (RFC 3986 drops the BaseURL query), so it
    rides in the path (`/segment/{client_seg}/...`). SegmentList absolute URLs
    are explicit URIs and use the exact HLS `?client_id=` form."""

    def _make(self):
        from unittest.mock import Mock, AsyncMock, patch
        from fastapi.testclient import TestClient
        from starlette.responses import StreamingResponse
        import api

        async def _body():
            yield b"\x00\x00\x00\x18ftypmp42"

        mock = patch("api.stream_manager").start()
        mock.streams = {"stream123": Mock()}
        mock.clients = {}
        mock.register_client = AsyncMock()
        mock.proxy_dash_segment = AsyncMock(
            return_value=StreamingResponse(_body(), media_type="video/mp4")
        )
        return TestClient(api.app), mock, patch

    def test_segment_route_registers_path_client_and_proxies(self):
        client, mock, patcher = self._make()
        try:
            enc = DashProcessor.sign_base(
                "https://upstream.example.com/live/dash/", "stream123"
            )
            resp = client.get(f"/dash/stream123/segment/tab-B/{enc}/chunk-1.m4s")

            assert resp.status_code == 200
            assert mock.register_client.await_args.args[:2] == ("tab-B", "stream123")
            assert mock.proxy_dash_segment.call_args.args[1] == "tab-B"
            assert (
                mock.proxy_dash_segment.call_args.args[2]
                == "https://upstream.example.com/live/dash/chunk-1.m4s"
            )
        finally:
            patcher.stopall()

    def test_pooled_viewers_are_billed_separately_by_their_path_client_id(self):
        client, mock, patcher = self._make()
        try:
            enc = DashProcessor.sign_base(
                "https://upstream.example.com/live/", "stream123"
            )
            client.get(f"/dash/stream123/segment/tab-A/{enc}/s.m4s")
            billed_a = mock.proxy_dash_segment.call_args.args[1]
            client.get(f"/dash/stream123/segment/tab-B/{enc}/s.m4s")
            billed_b = mock.proxy_dash_segment.call_args.args[1]

            assert (billed_a, billed_b) == ("tab-A", "tab-B")
        finally:
            patcher.stopall()

    def test_file_route_matches_the_hls_segment_route_shape(self):
        client, mock, patcher = self._make()
        try:
            resp = client.get(
                "/dash/stream123/file",
                params={
                    "client_id": "tab-777",
                    "url": "https://upstream.example.com/video/seg1.m4s",
                    "sig": sign_url(
                        "https://upstream.example.com/video/seg1.m4s",
                        stream_id="stream123",
                        role=ROLE_DASH_FILE,
                    ),
                },
            )

            assert resp.status_code == 200
            assert mock.register_client.await_args.args[:2] == ("tab-777", "stream123")
            assert (
                mock.proxy_dash_segment.call_args.args[2]
                == "https://upstream.example.com/video/seg1.m4s"
            )
        finally:
            patcher.stopall()

    def test_file_route_requires_client_id_like_hls(self):
        client, mock, patcher = self._make()
        try:
            resp = client.get(
                "/dash/stream123/file",
                params={"url": "https://upstream.example.com/video/seg1.m4s"},
            )
            assert resp.status_code == 422
        finally:
            patcher.stopall()
