"""Private QSV response boundary; no provider or hardware access."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import StreamingResponse

from tests import test_live_normalization as normalization_tests
from tests.test_ts_canonicalizer import (
    fixture,
    packets,
    pat,
    pmt,
    section,
    media,
    split,
    pid,
)
from stream_manager import StreamInfo
from ts_canonicalizer import TSCanonicalizer, TSCanonicalizationError

# Register the shared fixture without an unused-import/parameter name collision.
manager = normalization_tests.manager


def owned(manager, encoder="qsv"):
    now = datetime.now(timezone.utc)
    inner = StreamInfo("inner", "http://fixture/a.ts", now, now)
    outer = StreamInfo("outer", "http://127.0.0.1/inner", now, now)
    inner.normalization_parent_id = outer.stream_id
    outer.normalization_input_id = inner.stream_id
    outer.metadata = {"normalization_encoder": encoder}
    manager.streams.update(inner=inner, outer=outer)
    return inner, outer


@pytest.mark.asyncio
async def test_wrapper_epoch_partial_discovery_and_close(manager, monkeypatch):
    inner, _ = owned(manager)
    closed = []

    async def chunks():
        try:
            a = fixture()
            yield a[:188]  # bounded PAT discovery may produce no bytes
            yield a[188:] + b"old partial"
            inner.direct_generation += 1
            yield fixture(1512, 768, 769)
        finally:
            closed.append(True)

    monkeypatch.setattr(
        manager,
        "_stream_continuous_direct_original",
        AsyncMock(return_value=StreamingResponse(chunks())),
    )
    response = await manager.stream_continuous_direct("inner", "player")
    chunks_out = [c async for c in response.body_iterator]
    assert len(chunks_out) == 2
    assert [[pid(p) for p in split(c)] for c in chunks_out] == [[0, 4096, 256, 257]] * 2
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["early_close", "adapter_error", "terminal_fragment"])
async def test_wrapper_closes_source_without_retry_or_raw_fallback(
    manager, monkeypatch, mode
):
    owned(manager)
    closed = []

    async def chunks():
        try:
            yield fixture()
            if mode == "adapter_error":
                yield b"!" * 188
            else:
                yield b"partial"
        finally:
            closed.append(True)

    source = chunks()
    factory = AsyncMock(return_value=StreamingResponse(source))
    monkeypatch.setattr(manager, "_stream_continuous_direct_original", factory)
    response = await manager.stream_continuous_direct("inner", "player")
    it = response.body_iterator
    assert await anext(it)
    if mode == "early_close":
        await it.aclose()
    else:
        with pytest.raises(TSCanonicalizationError):
            await anext(it)
    assert closed == [True]
    factory.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["cpu", "vaapi", "spoof", "missing_outer", "wrong_backlink"]
)
async def test_only_reciprocal_qsv_owned_input_is_adapted(manager, monkeypatch, mode):
    inner, outer = owned(manager, encoder=mode)
    if mode == "spoof":
        inner.normalization_parent_id = None
        inner.metadata = {
            "normalization_encoder": "qsv",
            "normalization_parent_id": "outer",
        }
    elif mode == "missing_outer":
        manager.streams.pop("outer")
    elif mode == "wrong_backlink":
        outer.metadata["normalization_encoder"] = "qsv"
        outer.normalization_input_id = "not-inner"

    async def raw():
        yield b"not TS, byte-exact passthrough"

    response = StreamingResponse(raw())
    original = response.body_iterator
    monkeypatch.setattr(
        manager, "_stream_continuous_direct_original", AsyncMock(return_value=response)
    )
    result = await manager.stream_continuous_direct("inner", "player")
    assert result.body_iterator is original
    assert [x async for x in result.body_iterator] == [
        b"not TS, byte-exact passthrough"
    ]


def dvb_fixture(descriptor):
    table = pmt(audio_type=6)
    # Insert descriptors into the audio ES loop, regenerating length and CRC.
    body = table[3:-4]
    body = body[:-2] + (0xF000 | len(descriptor)).to_bytes(2, "big") + descriptor
    return packets(0, pat()) + packets(1256, section(2, body)) + media(512) + media(513)


@pytest.mark.parametrize(
    "descriptor", [b"\x6a\x01\x00", b"\x05\x04AC-3", b"\x05\x04AC-3\x6a\x01\x00"]
)
def test_dvb_ac3_preserves_descriptors_pes_and_crc(descriptor):
    data = dvb_fixture(descriptor)
    c = TSCanonicalizer()
    output = c.feed(data)
    c.finish()
    assert split(output)[-1][4:] == split(data)[-1][4:]
    assert descriptor in output
    # Reparse the generated PAT/PMT through the same strict ABI/CRC gate.
    assert TSCanonicalizer().feed(output)


@pytest.mark.parametrize(
    "descriptor",
    [b"", b"\x05\x04EAC3", b"\x7a\x01\x00", b"\x6a\x00", b"\x09\x04\x00\x01\xe1\x00"],
)
def test_private_audio_unknown_eac3_and_ca_fail_closed(descriptor):
    with pytest.raises(TSCanonicalizationError):
        TSCanonicalizer().feed(dvb_fixture(descriptor))
