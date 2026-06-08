"""RTP parsing seam: header decode + RFC 8285 MA identity extension."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recognition.udp_capture import (  # noqa: E402
    PlayerStream, RtpPacket, UdpAudioCapture, parse_rtp_ext_elements)


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


# --- stream migration on reconnect (new SSRC, same player identity) ---------- #


def _stream(cap, key, ssrc, last_seen, name="respeaker_lyrics",
            ma_id="a0505ec6", ip="192.168.1.137"):
    s = PlayerStream(key=key, sample_rate=16000, channels=1, name=name,
                     ma_player_id=ma_id, source_ip=ip, ssrc=ssrc)
    s.last_seen = last_seen
    cap._streams[key] = s
    return s


def test_find_stream_prefers_freshest_on_reconnect():
    """A station change makes the respeaker reconnect with a new SSRC; the old,
    now-silent stream lingers with the same ma_id/name. find_stream must return
    the live (freshest) stream so the recognizer migrates instead of sticking to
    the dead one."""
    import time
    cap = UdpAudioCapture()
    now = time.time()
    dead = _stream(cap, "73d34824", 0x73D34824, last_seen=now - 120)
    live = _stream(cap, "e6ba4a5a", 0xE6BA4A5A, last_seen=now - 0.5)

    assert cap.find_stream(ma_player_id="a0505ec6") is live
    assert cap.find_stream(name="respeaker_lyrics") is live
    assert cap.first_active_stream() is live
    # Exact SSRC still resolves the specific (even dead) stream.
    assert cap.find_stream(ssrc=0x73D34824) is dead
