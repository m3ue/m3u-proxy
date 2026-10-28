"""Strict, payload-preserving TS input normalization (no FFmpeg dependency)."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ts_canonicalizer import TSCanonicalizer, TSCanonicalizationError, crc32_mpeg


def section(table, body):
    size = len(body) + 4
    data = bytes([table, 0xB0 | (size >> 8), size & 255]) + body
    return data + crc32_mpeg(data).to_bytes(4, "big")


def pat(pmt=1256, program=7):
    return section(
        0,
        b"\x00\x09\xc1\x00\x00"
        + program.to_bytes(2, "big")
        + (0xE000 | pmt).to_bytes(2, "big"),
    )


def pmt(video=512, audio=513, program=7, audio_type=15, descriptors=b""):
    return section(
        2,
        program.to_bytes(2, "big")
        + b"\xc1\x00\x00"
        + (0xE000 | video).to_bytes(2, "big")
        + (0xF000 | len(descriptors)).to_bytes(2, "big")
        + descriptors
        + bytes([27])
        + (0xE000 | video).to_bytes(2, "big")
        + b"\xf0\x00"
        + bytes([audio_type])
        + (0xE000 | audio).to_bytes(2, "big")
        + b"\xf0\x00",
    )


def packets(pid, data, cc=0):
    data = b"\x00" + data
    result = []
    while data:
        chunk, data = data[:184], data[184:]
        result.append(
            bytes(
                [0x47, (0x40 if not result else 0) | (pid >> 8), pid & 255, 0x10 | cc]
            )
            + chunk.ljust(184, b"\xff")
        )
        cc = (cc + 1) & 15
    return b"".join(result)


def media(pid, value=42, cc=8):
    return (
        bytes([0x47, 0x40 | (pid >> 8), pid & 255, 0x10 | cc])
        + b"\x00\x00\x01\xe0"
        + bytes([value]) * 180
    )


def fixture(pmt_pid=1256, video=512, audio=513, **kw):
    return (
        packets(0, pat(pmt_pid))
        + packets(pmt_pid, pmt(video, audio, **kw))
        + media(video)
        + media(audio, 99)
    )


def split(data):
    return [data[i : i + 188] for i in range(0, len(data), 188)]


def pid(packet):
    return ((packet[1] & 31) << 8) | packet[2]


def test_changed_pmt_elementary_and_program_ids_are_stable():
    c = TSCanonicalizer()
    first = c.feed(fixture())
    c.start_generation()
    second = c.feed(fixture(1512, 768, 769))
    assert [pid(p) for p in split(first)] == [0, 4096, 256, 257]
    assert [pid(p) for p in split(second)] == [0, 4096, 256, 257]
    for before, after in zip(split(fixture())[2:], split(first)[2:]):
        assert before[4:] == after[4:]
    for a, b in zip(split(first), split(second)):
        assert (b[3] & 15) == ((a[3] + 1) & 15)
    assert c.finish() == b""


@pytest.mark.parametrize("seed", range(8))
def test_arbitrary_chunks_and_multi_packet_pmt(seed):
    # Valid private-data descriptors: intentionally NOT admitted by production.
    # Long ISO639 descriptor list makes a valid >188 byte PMT.
    descriptors = (b"\x0a\x04eng\x00") * 40
    data = fixture(descriptors=descriptors)
    expected = TSCanonicalizer().feed(data)
    c, rng, actual = TSCanonicalizer(), random.Random(seed), bytearray()
    while data:
        n = rng.randint(1, 211)
        actual.extend(c.feed(data[:n]))
        data = data[n:]
    actual.extend(c.finish())
    assert actual == expected
    # Reassemble emitted PMT and verify MPEG-2 CRC.
    psi = b"".join(
        p[5:] if p[1] & 64 else p[4:] for p in split(expected) if pid(p) == 4096
    )
    length = 3 + (((psi[1] & 15) << 8) | psi[2])
    assert crc32_mpeg(psi[:length]) == 0


@pytest.mark.parametrize(
    "change", ["crc", "codec", "ca", "multi", "scrambled", "truncated"]
)
def test_fail_closed(change):
    c = TSCanonicalizer()
    if change == "crc":
        data = bytearray(fixture())
        data[12] ^= 1
    elif change == "codec":
        data = fixture(audio_type=3)
    elif change == "ca":
        data = fixture(descriptors=b"\x09\x04\x00\x01\xe1\x00")
    elif change == "multi":
        data = packets(
            0, section(0, b"\x00\x09\xc1\x00\x00\x00\x07\xe4\xe8\x00\x08\xe5\xe8")
        )
    elif change == "scrambled":
        data = bytearray(fixture())
        data[-185] |= 0x80
    else:
        data = fixture()[:-1]
    with pytest.raises(TSCanonicalizationError):
        c.feed(data)
        c.finish()
    with pytest.raises(TSCanonicalizationError):
        c.feed(fixture())


def test_incompatible_generation_does_not_emit_backup():
    c = TSCanonicalizer()
    c.feed(fixture())
    c.start_generation()
    with pytest.raises(TSCanonicalizationError):
        c.feed(fixture(1512, 768, 769, audio_type=129))


def test_incomplete_generation_is_bounded():
    c = TSCanonicalizer(max_probe_bytes=188 * 4)
    with pytest.raises(TSCanonicalizationError):
        c.feed(media(512) * 5)


def test_continuation_gap_rejected():
    data = bytearray(fixture(descriptors=b"\x0a\x04eng\x00" * 40))
    data[188 * 2 + 3] ^= 3
    with pytest.raises(TSCanonicalizationError):
        TSCanonicalizer().feed(data)


def test_separate_pcr_and_adaptation_only_preserved():
    table = bytearray(pmt())
    table[8:10] = (0xE000 | 600).to_bytes(2, "big")
    table[-4:] = crc32_mpeg(table[:-4]).to_bytes(4, "big")
    packet = bytes([0x47, 600 >> 8, 600 & 255, 0x20, 183, 0x10]) + b"\x00" * 182
    data = packets(0, pat()) + packets(1256, table) + packet + media(512)
    result = split(TSCanonicalizer().feed(data))
    assert pid(result[2]) == 258
    assert result[2][4:] == packet[4:]


def test_generation_discards_incomplete_transport_packet():
    c = TSCanonicalizer()
    c.feed(fixture() + b"\x47\x00")
    c.start_generation()
    assert c.feed(fixture(1512, 768, 769))
    c.finish()


def test_mpeg2_crc_known_vector():
    assert crc32_mpeg(b"123456789") == 0x0376E6E7


def test_one_byte_chunks():
    data = fixture()
    c = TSCanonicalizer()
    result = b"".join(c.feed(bytes([value])) for value in data)
    assert result == TSCanonicalizer().feed(data)
    c.finish()


def test_pmt_program_mismatch():
    data = packets(0, pat()) + packets(1256, pmt(program=8))
    with pytest.raises(TSCanonicalizationError, match="program mismatch"):
        TSCanonicalizer().feed(data)


def test_source_program_number_change_is_normalized():
    c = TSCanonicalizer()
    first = c.feed(fixture())
    c.start_generation()
    data = packets(0, pat(1512, program=91)) + packets(1512, pmt(768, 769, program=91))
    second = c.feed(data + media(768) + media(769, 99))
    assert split(first)[0][4:] == split(second)[0][4:]
    assert split(first)[1][4:] == split(second)[1][4:]


def test_ac3_supported_but_cross_codec_rejected():
    c = TSCanonicalizer()
    assert c.feed(fixture(audio_type=129))
    c.start_generation()
    assert c.feed(fixture(1512, 768, 769, audio_type=129))
    c.start_generation()
    with pytest.raises(TSCanonicalizationError, match="incompatible generation"):
        c.feed(fixture(audio_type=15))


def test_adaptation_pointer_and_pcr_bytes_preserved():
    table = pmt()
    payload = b"\x03\xff\xff\xff" + table
    table_packet = (
        bytes([0x47, 0x40 | (1256 >> 8), 1256 & 255, 0x30, 7, 0])
        + b"\x00" * 6
        + payload
    )
    table_packet = table_packet.ljust(188, b"\xff")
    pes = (
        bytes([0x47, 0x40 | (512 >> 8), 512 & 255, 0x38, 7, 0x10])
        + b"\x19\x12\x23\x01\x7e\x44"
        + b"\x00\x00\x01\xe0"
        + bytes(range(172))
    )
    assert len(pes) == 188
    output = split(
        TSCanonicalizer().feed(packets(0, pat()) + table_packet + pes + media(513))
    )
    assert output[2][4:] == pes[4:]


def test_stable_table_repeat_has_continuous_output_cc():
    c = TSCanonicalizer()
    output = split(
        c.feed(
            fixture()
            + packets(0, pat(), cc=1)
            + packets(1256, pmt(), cc=1)
            + media(512, cc=9)
        )
    )
    previous = {}
    for packet in output:
        target, cc = pid(packet), packet[3] & 15
        if target in previous:
            assert cc == (previous[target] + 1) & 15
        previous[target] = cc


def test_unannounced_table_change_rejected():
    c = TSCanonicalizer()
    c.feed(fixture())
    with pytest.raises(TSCanonicalizationError, match="PAT changed"):
        c.feed(packets(0, pat(1512), cc=1))


@pytest.mark.parametrize("offset,value", [(0, 0), (1, 0x80), (3, 0x00), (3, 0x30)])
def test_invalid_transport_headers(offset, value):
    packet = bytearray(media(512))
    packet[offset] = value
    if value == 0x30:
        packet[4] = 184
    with pytest.raises(TSCanonicalizationError):
        TSCanonicalizer().feed(packet)


@pytest.mark.parametrize("source_pid,target", [(512, 256), (513, 257)])
def test_media_loss_survives_rebased_cc_and_wrap(source_pid, target):
    c = TSCanonicalizer()
    c.feed(packets(0, pat()) + packets(1256, pmt()))
    source = [
        media(source_pid, value=i, cc=cc) for i, cc in enumerate([13, 14, 1, 2, 2, 4])
    ]
    output = split(c.feed(b"".join(source)))
    assert [pid(p) for p in output] == [target] * len(source)
    # Missing 15/0 and missing 3 remain visible; same-CC different data is
    # not falsely made continuous either. Initial source CC is arbitrary.
    assert [p[3] & 15 for p in output] == [0, 1, 4, 5, 5, 7]
    assert [p[4:] for p in output] == [p[4:] for p in source]
    c.finish()


def test_payload_and_adaptation_only_output_cc_rules():
    adaptation = bytes([0x47, 512 >> 8, 512 & 255, 0x28, 183, 0]) + b"\xff" * 182
    output = split(TSCanonicalizer().feed(fixture() + adaptation + media(512, cc=9)))
    selected = [p for p in output if pid(p) == 256]
    assert [p[3] & 15 for p in selected] == [0, 0, 1]
    assert selected[1][4:] == adaptation[4:]
