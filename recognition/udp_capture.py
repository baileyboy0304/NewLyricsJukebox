"""Clean RTP-only UDP audio capture with per-player demux.

Music Assistant streams 16 kHz / 16-bit / mono PCM over RTP (RFC 3550). Player
identity travels in RFC 8285 header extensions: element id 1 = player name,
element id 2 = player id. We decode that to demux streams per player so the UI
can list every detected player, while only the selected player is actually read
for recognition.

This is a deliberate rewrite of the legacy jitter-buffer code (AUDIT.md cluster
A): RTP only (no raw-PCM auto-detect), simple sequence reorder, an idle timeout
on unknown players, and no reaching into private attributes.
"""

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

RTP_VERSION = 2
RTP_HEADER_MIN_SIZE = 12
RTP_EXT_PROFILE_ONE_BYTE = 0xBEDE
RTP_EXT_PROFILE_TWO_BYTE = 0x1000
RTP_EXT_ID_MA_PLAYER_NAME = 1
RTP_EXT_ID_MA_PLAYER_ID = 2

STREAM_ACTIVE_WINDOW = 10.0   # seconds since last packet to count as "live"
STREAM_STALE_TIMEOUT = 300.0  # drop detected streams unseen this long
DEAD_STREAM_TIMEOUT = 10.0    # get_audio gives up if no packets for this long


@dataclass
class AudioChunk:
    data: np.ndarray            # int16 samples
    sample_rate: int
    channels: int
    duration: float
    capture_start_time: float

    def get_max_amplitude(self) -> int:
        if self.data.size == 0:
            return 0
        return int(np.max(np.abs(self.data)))

    def is_silent(self, threshold: int = 100) -> bool:
        return self.get_max_amplitude() < threshold


# --------------------------------------------------------------------------- #
# RTP parsing
# --------------------------------------------------------------------------- #


def parse_rtp_ext_elements(profile: int, body: bytes) -> Dict[int, bytes]:
    """Decode RFC 8285 header-extension elements into {element_id: bytes}."""
    out: Dict[int, bytes] = {}
    i = 0
    n = len(body)
    if profile == RTP_EXT_PROFILE_ONE_BYTE:
        while i < n:
            b = body[i]
            i += 1
            if b == 0x00:  # padding
                continue
            eid = (b >> 4) & 0x0F
            length = (b & 0x0F) + 1
            if eid == 15:  # reserved stop marker
                break
            if i + length > n:
                return {}
            out[eid] = bytes(body[i:i + length])
            i += length
    elif profile == RTP_EXT_PROFILE_TWO_BYTE:
        while i + 1 < n:
            eid = body[i]
            length = body[i + 1]
            i += 2
            if eid == 0:  # padding
                continue
            if i + length > n:
                return {}
            out[eid] = bytes(body[i:i + length])
            i += length
    return out


class RtpPacket:
    """A parsed RTP v2 packet. Raises ValueError on anything that is not RTP v2."""

    __slots__ = ("sequence", "timestamp", "ssrc", "payload_type", "marker",
                 "ext_elements", "payload")

    def __init__(self, data: bytes):
        if len(data) < RTP_HEADER_MIN_SIZE:
            raise ValueError("too short for RTP")
        byte0 = data[0]
        byte1 = data[1]
        if (byte0 >> 6) & 0x03 != RTP_VERSION:
            raise ValueError("not RTP v2")
        padding = bool((byte0 >> 5) & 0x01)
        extension = bool((byte0 >> 4) & 0x01)
        cc = byte0 & 0x0F
        self.marker = bool((byte1 >> 7) & 0x01)
        self.payload_type = byte1 & 0x7F
        self.sequence, self.timestamp, self.ssrc = struct.unpack_from("!HII", data, 2)

        header_len = RTP_HEADER_MIN_SIZE + cc * 4
        self.ext_elements: Dict[int, bytes] = {}
        if extension and len(data) >= header_len + 4:
            profile, ext_words = struct.unpack_from("!HH", data, header_len)
            ext_body_start = header_len + 4
            ext_body_end = ext_body_start + ext_words * 4
            if ext_body_end <= len(data):
                self.ext_elements = parse_rtp_ext_elements(
                    profile, data[ext_body_start:ext_body_end])
            header_len = ext_body_end

        payload_end = len(data)
        if padding and payload_end > header_len:
            payload_end -= data[-1]
        self.payload = data[header_len:payload_end]

    def ma_identity(self) -> Tuple[Optional[str], Optional[str]]:
        """Return (player_name, player_id) from MA header extensions, or (None, None)."""
        if not self.ext_elements:
            return (None, None)
        name_bytes = self.ext_elements.get(RTP_EXT_ID_MA_PLAYER_NAME)
        id_bytes = self.ext_elements.get(RTP_EXT_ID_MA_PLAYER_ID)
        try:
            name = name_bytes.decode("utf-8").strip() if name_bytes else None
            pid = id_bytes.decode("utf-8").strip() if id_bytes else None
        except UnicodeDecodeError:
            return (None, None)
        return (name or None, pid or None)


# --------------------------------------------------------------------------- #
# Per-player stream state
# --------------------------------------------------------------------------- #


@dataclass
class PlayerStream:
    """Audio buffer and identity for one detected player."""

    key: str                      # stable id: ma player id, or ssrc, or ip
    sample_rate: int
    channels: int
    name: Optional[str] = None
    ma_player_id: Optional[str] = None
    source_ip: Optional[str] = None
    ssrc: Optional[int] = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = 0.0
    packet_count: int = 0
    _expected_seq: Optional[int] = None
    _buffer: bytearray = field(default_factory=bytearray)
    _max_bytes: int = 0
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        # Keep ~30 s of audio per player.
        self._max_bytes = self.sample_rate * self.channels * 2 * 30

    @property
    def frame_size(self) -> int:
        return 2 * self.channels

    @property
    def active(self) -> bool:
        return (time.time() - self.last_seen) < STREAM_ACTIVE_WINDOW

    def add_packet(self, pkt: RtpPacket):
        now = time.time()
        # SSRC change => new session; reset sequence tracking, keep identity.
        if self.ssrc is not None and pkt.ssrc != self.ssrc:
            self._expected_seq = None
        self.ssrc = pkt.ssrc

        # Simple in-order acceptance: drop clearly old/duplicate sequences,
        # accept the rest. (16-bit wrap handled by treating large backward
        # jumps as a reset rather than reordering far-past packets.)
        if self._expected_seq is not None:
            delta = (pkt.sequence - self._expected_seq) & 0xFFFF
            if delta > 0x8000:  # packet older than expected -> drop
                return
        self._expected_seq = (pkt.sequence + 1) & 0xFFFF

        self._buffer.extend(pkt.payload)
        if len(self._buffer) > self._max_bytes:
            del self._buffer[:len(self._buffer) - self._max_bytes]
        self.packet_count += 1
        self.last_seen = now
        self._event.set()

    async def get_audio(self, duration: float, should_continue) -> Optional[AudioChunk]:
        needed = int(duration * self.sample_rate * self.frame_size)
        while should_continue():
            if len(self._buffer) >= needed:
                break
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                if not should_continue():
                    return None
                if self.last_seen and (time.time() - self.last_seen) > DEAD_STREAM_TIMEOUT:
                    return None
        else:
            return None

        audio_bytes = bytes(self._buffer[-needed:])
        data = np.frombuffer(audio_bytes, dtype=np.int16)
        if self.channels > 1:
            data = data.reshape(-1, self.channels)
        return AudioChunk(
            data=data,
            sample_rate=self.sample_rate,
            channels=self.channels,
            duration=duration,
            capture_start_time=self.last_seen - duration,
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "ma_player_id": self.ma_player_id,
            "source_ip": self.source_ip,
            "ssrc": format(self.ssrc, "08x") if self.ssrc is not None else None,
            "packet_count": self.packet_count,
            "active": self.active,
        }


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, capture: "UdpAudioCapture"):
        self._capture = capture

    def datagram_received(self, data, addr):
        self._capture.receive(data, addr)

    def error_received(self, exc):  # pragma: no cover - network noise
        logger.warning("UDP socket error: %s", exc)


class UdpAudioCapture:
    """Listens for RTP audio, demuxing into one PlayerStream per detected player."""

    def __init__(self, port: int = 6056, sample_rate: int = 16000, channels: int = 1):
        self._port = port
        self._sample_rate = sample_rate
        self._channels = channels
        self._streams: Dict[str, PlayerStream] = {}
        self._transport = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self),
            local_addr=("0.0.0.0", self._port),
        )
        logger.info("UDP audio capture listening on 0.0.0.0:%d", self._port)

    def stop(self):
        if self._transport:
            self._transport.close()
            self._transport = None

    # -- packet handling --------------------------------------------------- #

    def receive(self, data: bytes, addr):
        try:
            pkt = RtpPacket(data)
        except ValueError:
            return  # RTP-only: silently ignore anything else
        name, ma_id = pkt.ma_identity()
        source_ip = addr[0]
        key = ma_id or format(pkt.ssrc, "08x") or source_ip

        stream = self._streams.get(key)
        if stream is None:
            stream = PlayerStream(
                key=key,
                sample_rate=self._sample_rate,
                channels=self._channels,
                name=name,
                ma_player_id=ma_id,
                source_ip=source_ip,
                ssrc=pkt.ssrc,
            )
            self._streams[key] = stream
            logger.info("Detected new RTP stream: key=%s name=%s ip=%s ssrc=%08x",
                        key, name, source_ip, pkt.ssrc)
        else:
            if name and not stream.name:
                stream.name = name
            stream.source_ip = source_ip

        stream.add_packet(pkt)
        self._prune()

    def _prune(self):
        now = time.time()
        stale = [k for k, s in self._streams.items()
                 if s.last_seen and (now - s.last_seen) > STREAM_STALE_TIMEOUT]
        for k in stale:
            logger.info("Dropping stale stream %s", k)
            del self._streams[k]

    # -- consumer API ------------------------------------------------------ #

    def list_streams(self) -> List[dict]:
        return [s.to_dict() for s in self._streams.values()]

    def get_stream(self, key: str) -> Optional[PlayerStream]:
        return self._streams.get(key)

    def find_stream(self, *, ma_player_id=None, name=None, ssrc=None, source_ip=None) -> Optional[PlayerStream]:
        for s in self._streams.values():
            if ma_player_id and s.ma_player_id == ma_player_id:
                return s
            if name and s.name == name:
                return s
            if ssrc is not None and s.ssrc == ssrc:
                return s
            if source_ip and s.source_ip == source_ip:
                return s
        return None

    def first_active_stream(self) -> Optional[PlayerStream]:
        for s in self._streams.values():
            if s.active:
                return s
        return None

    async def get_audio(self, duration: float, key: str, should_continue=None) -> Optional[AudioChunk]:
        stream = self._streams.get(key)
        if stream is None:
            return None
        if should_continue is None:
            should_continue = lambda: True
        return await stream.get_audio(duration, should_continue)
