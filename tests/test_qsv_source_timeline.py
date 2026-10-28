"""Fault-aware timeline contract: missing input is not arbitrary output slack."""

import pytest

from tests.test_qsv_cadence_handoff import source_positions, standard_bwdif_positions


@pytest.mark.parametrize("kind", ["p25", "p50", "i25"])
@pytest.mark.parametrize(
    "cut_mode", ["complete-video", "truncated-audio", "truncated-video"]
)
@pytest.mark.parametrize("bounded_video_pes", [False, True])
def test_standard_policy_is_one_explicit_field_tradeoff(
    kind, cut_mode, bounded_video_pes
):
    fields = 2 if kind == "i25" else 1
    source = source_positions(
        [0, 3600],
        [1000, 4600],
        fields=fields,
        last_demux_pts=7200,
        audio_epoch=0,
        output_audio_epoch=0,
        audio_delay=0,
        source_period=3600,
    )
    original = source.copy()
    policy = standard_bwdif_positions(
        source,
        kind=kind,
        cut_mode=cut_mode,
        bounded_video_pes=bounded_video_pes,
        complete_frames_a=2,
    )
    assert source == original  # Original source-field vector remains authoritative.
    special_cut = (
        kind == "i25" and cut_mode == "truncated-video" and not bounded_video_pes
    )
    changes = [
        (i, new - old) for i, (old, new) in enumerate(zip(source, policy)) if old != new
    ]
    assert changes == ([(3, 1800)] if special_cut else [])
    if special_cut:
        assert source[3] == 5400
        assert policy[3] == (source[2] + source[4]) // 2 == 7200
        assert (
            source != policy
        )  # Accepted presentation policy is NOT source-PTS preservation.


@pytest.mark.parametrize("fields", [1, 2])
def test_source_oracle_retains_each_valid_position(fields):
    expected = source_positions(
        [0, 3600],
        [1000, 4600],
        fields=fields,
        last_demux_pts=7200,
        audio_epoch=0,
        output_audio_epoch=0,
        audio_delay=0,
        source_period=3600,
    )
    assert (
        expected
        == (
            {
                1: [0, 3600, 10800, 14400],
                2: [0, 1800, 3600, 5400, 10800, 12600, 14400, 16200],
            }[fields]
        )
    )
    # Lose/repeat a valid frame, or insert a gap outside the proven cut: rejected.
    assert expected[1:] != expected
    assert [expected[0], *expected] != expected
    changed = expected.copy()
    changed[-1] += 1800
    assert changed != expected


def test_interlaced_midpoint_is_not_a_source_field_position():
    expected = source_positions(
        [0, 3600],
        [1000, 4600],
        fields=2,
        last_demux_pts=7200,
        audio_epoch=0,
        output_audio_epoch=0,
        audio_delay=0,
        source_period=3600,
    )
    actual_midpoint = expected.copy()
    # bwdif's midpoint to the next *decoded* picture shifts a surviving A field.
    actual_midpoint[3] = (expected[2] + expected[4]) // 2
    assert actual_midpoint != expected
    assert actual_midpoint[4:] == expected[4:]


def test_audio_epoch_is_not_fitted_to_video_output():
    options = dict(
        fields=1,
        last_demux_pts=3600,
        audio_epoch=100,
        output_audio_epoch=126000,
        audio_delay=1920,
        source_period=3600,
    )
    first = source_positions([100, 3700], [1000, 4600], **options)
    options["audio_epoch"] += 90000
    shifted = source_positions([100, 3700], [1000, 4600], **options)
    assert [a - b for a, b in zip(first, shifted)] == [90000] * len(first)
