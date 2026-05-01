"""
Unit tests for the ZPL `~HS` parser and zpl_zt411_host_status tool.

Pure parser tests — no socket, no fixture replay. Phase 4.0 captured the
canonical idle response from the lab printer:

    030,0,0,0308,000,0,0,0,000,0,0,0
    001,0,0,0,1,2,6,0,00000000,1,000
    0000,0

Field positions follow Zebra's ZPL Programming Guide for ~HS. We mutate
single fields to exercise paused / fault / framed / malformed cases.
"""
from __future__ import annotations

import pytest

from zt411_agent.agent.tools import _parse_host_status


IDLE = (
    "030,0,0,0308,000,0,0,0,000,0,0,0\n"
    "001,0,0,0,1,2,6,0,00000000,1,000\n"
    "0000,0"
)


class TestIdle:
    def test_no_flags_set(self):
        f = _parse_host_status(IDLE)
        assert f["paused"] is False
        assert f["head_open"] is False
        assert f["media_out"] is False
        assert f["ribbon_out"] is False
        assert f["buffer_full"] is False

    def test_paused_is_user_initiated_is_none_when_not_paused(self):
        assert _parse_host_status(IDLE)["paused_is_user_initiated"] is None

    def test_label_length_parsed(self):
        assert _parse_host_status(IDLE)["label_length_dots"] == 308

    def test_raw_response_preserved_verbatim(self):
        assert _parse_host_status(IDLE)["raw_response"] == IDLE


class TestUserPaused:
    """User pressed PAUSE: paused=True, no fault flags."""

    response = (
        "030,0,1,0308,000,0,0,0,000,0,0,0\n"
        "001,0,0,0,1,2,6,0,00000000,1,000\n"
        "0000,0"
    )

    def test_paused_true(self):
        assert _parse_host_status(self.response)["paused"] is True

    def test_user_initiated_when_no_fault(self):
        assert _parse_host_status(self.response)["paused_is_user_initiated"] is True

    def test_no_fault_flags(self):
        f = _parse_host_status(self.response)
        assert not (f["head_open"] or f["media_out"] or f["ribbon_out"])


class TestAutoPausedFromFault:
    """Printer paused itself due to a physical fault."""

    head_open_response = (
        "030,0,1,0308,000,0,0,0,000,0,0,0\n"
        "001,0,1,0,1,2,6,0,00000000,1,000\n"
        "0000,0"
    )
    media_out_response = (
        "030,1,1,0308,000,0,0,0,000,0,0,0\n"
        "001,0,0,0,1,2,6,0,00000000,1,000\n"
        "0000,0"
    )
    ribbon_out_response = (
        "030,0,1,0308,000,0,0,0,000,0,0,0\n"
        "001,0,0,1,1,2,6,0,00000000,1,000\n"
        "0000,0"
    )

    def test_head_open_flag_set(self):
        f = _parse_host_status(self.head_open_response)
        assert f["paused"] is True
        assert f["head_open"] is True
        assert f["paused_is_user_initiated"] is False

    def test_media_out_flag_set(self):
        f = _parse_host_status(self.media_out_response)
        assert f["paused"] is True
        assert f["media_out"] is True
        assert f["paused_is_user_initiated"] is False

    def test_ribbon_out_flag_set(self):
        f = _parse_host_status(self.ribbon_out_response)
        assert f["paused"] is True
        assert f["ribbon_out"] is True
        assert f["paused_is_user_initiated"] is False


class TestFraming:
    """The printer may wrap each line in STX/ETX with CR LF terminators."""

    framed = (
        "\x02 030,0,0,0308,000,0,0,0,000,0,0,0 \x03\r\n"
        "\x02 001,0,0,0,1,2,6,0,00000000,1,000 \x03\r\n"
        "\x02 0000,0 \x03\r\n"
    )

    def test_stx_etx_cr_stripped(self):
        f = _parse_host_status(self.framed)
        assert f["paused"] is False
        assert f["label_length_dots"] == 308


class TestMalformedRaisesValueError:
    def test_single_line_raises(self):
        with pytest.raises(ValueError):
            _parse_host_status("030,0,0,0308,000,0,0,0")

    def test_too_few_fields_raises(self):
        with pytest.raises(ValueError):
            _parse_host_status("030,0\n001,0\n0000,0")


class TestBackwardCompatKeys:
    """device_specialist.py:165 reads `raw_bitmask`. Must not be missing."""

    def test_raw_bitmask_present(self):
        assert "raw_bitmask" in _parse_host_status(IDLE)

    def test_keys_match_snmp_physical_flags_shape(self):
        f = _parse_host_status(IDLE)
        for k in ("paused", "head_open", "media_out", "ribbon_out",
                  "paused_is_user_initiated", "raw_bitmask"):
            assert k in f, f"missing back-compat key: {k}"
