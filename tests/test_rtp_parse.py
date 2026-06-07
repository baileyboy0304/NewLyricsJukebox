"""RTP parsing seam: header decode + RFC 8285 MA identity extension."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recognition.udp_capture import RtpPacket, parse_rtp_ext_elements  # noqa: E402


def build_rtp(payload=b"\x01\x00\x02\x00", seq=100, ssrc=0xDEADBEEF,
              ext_elements=None):
    """Construct a minimal RTP v2 packet, optionally with a one-byte ext block."""
    has_ext = bool(ext_elements)
    byte0 = (2 << 6) | (1 << 4 if has_ext else 0)
    byte1 = 96  # payload type
    header = struct.pack("!BBHII", byte0, byte1, seq, 0, ssrc)
    ext = b""
    if has_ext:
        body = bytearray()
        for eid, data in ext_elements.items():
            body.append(((eid & 0x0F) << 4) | ((len(data) - 1) & 0x0F))
            body.extend(data)
        while len(body) % 4 != 0:
            body.append(0)
        ext = struct.pack("!HH", 0xBEDE, len(body) // 4) + bytes(body)
    return header + ext + payload


def test_parses_basic_rtp():
    pkt = RtpPacket(build_rtp(payload=b"abcd", seq=42, ssrc=0x11223344))
    assert pkt.sequence == 42
    assert pkt.ssrc == 0x11223344
    assert pkt.payload_type == 96
    assert pkt.payload == b"abcd"


def test_rejects_non_rtp_v2():
    bad = bytes([0x40]) + b"\x00" * 20  # version 1
    try:
        RtpPacket(bad)
        assert False, "should have raised"
    except ValueError:
        pass


def test_rejects_too_short():
    try:
        RtpPacket(b"\x80\x60")
        assert False
    except ValueError:
        pass


def test_decodes_ma_identity():
    pkt = RtpPacket(build_rtp(ext_elements={
        1: b"Living Room",
        2: b"player_abc",
    }, payload=b"pcm!"))
    name, pid = pkt.ma_identity()
    assert name == "Living Room"
    assert pid == "player_abc"
    assert pkt.payload == b"pcm!"


def test_no_identity_without_extension():
    pkt = RtpPacket(build_rtp())
    assert pkt.ma_identity() == (None, None)


def test_parse_ext_elements_one_byte():
    # element id 1, len 2 ("Hi"); id 2, len 1 ("X")
    body = bytes([(1 << 4) | 1, ord("H"), ord("i"), (2 << 4) | 0, ord("X"), 0, 0, 0])
    out = parse_rtp_ext_elements(0xBEDE, body)
    assert out[1] == b"Hi"
    assert out[2] == b"X"
