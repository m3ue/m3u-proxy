"""Transport history must survive canonical identity rebasing."""
import pytest

from tests.test_ts_canonicalizer import fixture, media, packets, pat, pid, pmt, split
from ts_canonicalizer import TSCanonicalizer


def adaptation(cc, flags=0, payload=False):
    size = 7 if payload else 183
    return (bytes([0x47, 2, 0, (0x30 if payload else 0x20) | cc, size, flags])
            + b'\0' * (size - 1) + (b'P' * (183 - size) if payload else b''))


def video(data):
    return [p for p in split(data) if pid(p) == 256]


@pytest.mark.parametrize('pending', [False, True])
def test_exact_duplicate_keeps_identical_output_packet(pending):
    c = TSCanonicalizer()
    table = packets(0, pat()) + packets(1256, pmt())
    first, following = media(512, cc=14), media(512, value=43, cc=15)
    if pending:
        output = c.feed(first + first + table + following)
    else:
        output = c.feed(table + first) + c.feed(first + following)
    selected = video(output)
    assert selected[0] == selected[1]
    assert [p[3] & 15 for p in selected] == [0, 0, 1]
    c.finish()


@pytest.mark.parametrize('payload', [False, True])
def test_adaptation_preserves_loss_and_discontinuity(payload):
    c = TSCanonicalizer()
    prefix = c.feed(fixture())  # video source CC 8 -> output 0
    # Even an adaptation-only packet must not erase a source CC jump.
    source = adaptation(11, payload=payload)
    output = video(c.feed(source + media(512, cc=12)))
    assert [p[3] & 15 for p in output] == [3, 4]
    assert output[0][4:] == source[4:]
    # An explicit discontinuity permits an arbitrary source CC reset. Keep
    # its flag and the same per-generation offset, then resume normally.
    reset = adaptation(5, flags=0x80, payload=payload)
    output = video(c.feed(reset + media(512, cc=6)))
    assert [p[3] & 15 for p in output] == [13, 14]
    assert output[0][4:] == reset[4:]
    assert prefix
    c.finish()


def test_generation_resets_source_history_but_retains_output_history():
    c = TSCanonicalizer()
    first = video(c.feed(fixture() + media(512, cc=11)))
    assert [p[3] & 15 for p in first] == [0, 3]
    c.start_generation()
    # Reuse the source PID with a new source epoch and arbitrary initial CC.
    second = video(c.feed(packets(0, pat()) + packets(1256, pmt())
                          + media(512, cc=2) + media(512, cc=5)))
    assert [p[3] & 15 for p in second] == [4, 7]
    c.start_generation()
    third = video(c.feed(packets(0, pat(1512)) + packets(1512, pmt(768, 769))
                         + media(768, cc=15) + media(768, cc=0)))
    assert [p[3] & 15 for p in third] == [8, 9]
    c.finish()


def test_first_adaptation_has_unknown_initial_source_cc():
    c = TSCanonicalizer()
    source = adaptation(13)
    output = video(c.feed(packets(0, pat()) + packets(1256, pmt())
                          + source + media(512, cc=14)))
    assert [p[3] & 15 for p in output] == [0, 1]
    assert output[0][4:] == source[4:]
    c.finish()
