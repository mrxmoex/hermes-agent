"""A client-declared ClientCutText length is bounded at the header: the bridge must not buffer up to
2 GiB for a viewer that holds a ticket but no lease. An incomplete header must not grow the buffer
without a ceiling either — type-byte-then-flood never reached the declared-length check."""

import pytest

from tools.bot_desktop.rfb_filter import _MAX_BUF, _MAX_CUT_TEXT, RfbClientFilter

_HANDSHAKE = b"RFB 003.008\n\x01\x01"


def clipboard_header(length):
    return b"\x06\x00\x00\x00" + length.to_bytes(4, "big", signed=True)


@pytest.mark.parametrize("length", [_MAX_CUT_TEXT + 1, -_MAX_CUT_TEXT - 1, 2**31 - 1, -(2**31)])
@pytest.mark.parametrize("holder", [False, True])
def test_oversized_clipboard_is_rejected_at_header_without_waiting_for_payload(length, holder):
    parser = RfbClientFilter(lambda: holder)
    parser.feed(_HANDSHAKE)
    header = clipboard_header(length)
    for byte in header[:-1]:
        assert parser.feed(bytes([byte])) == b""
    with pytest.raises(ValueError, match="clipboard"):
        parser.feed(header[-1:])


@pytest.mark.parametrize("lead", [b"\x06", b"\x02", b"\xf8"])
def test_incomplete_header_cannot_grow_the_buffer_without_bound(lead):
    """A watcher that never finishes the 4/8/9-byte header used to make feed()
    return None from _message_length and retain every subsequent byte."""
    parser = RfbClientFilter(lambda: False)
    parser.feed(_HANDSHAKE)
    assert parser.feed(lead) == b""
    with pytest.raises(ValueError, match="buffer overflow"):
        parser.feed(b"A" * _MAX_BUF)


def test_legal_max_clipboard_still_frames_for_the_holder():
    parser = RfbClientFilter(lambda: True)
    parser.feed(_HANDSHAKE)
    body = b"x" * _MAX_CUT_TEXT
    out = parser.feed(clipboard_header(_MAX_CUT_TEXT) + body)
    assert out.endswith(body)
    assert len(out) == 8 + _MAX_CUT_TEXT
