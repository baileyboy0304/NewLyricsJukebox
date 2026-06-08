"""Shazam recognizer (primary), via ShazamIO.

Audio is converted to WAV with the stdlib ``wave`` module (no ffmpeg) — ShazamIO
downsamples internally. Matches with excessive time/frequency skew (radio pitch
shift, DJ speed-up) are rejected; the caller falls back to ACRCloud.
"""

import io
import logging
import wave
from typing import Optional

from recognition.result import RecognitionResult
from recognition.udp_capture import AudioChunk

logger = logging.getLogger(__name__)

SKEW_REJECT_THRESHOLD = 0.080
MIN_AUDIO_LEVEL = 100


def audio_to_wav(audio: AudioChunk) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(audio.channels)
        wf.setsampwidth(2)  # int16
        wf.setframerate(audio.sample_rate)
        wf.writeframes(audio.data.tobytes())
    return buffer.getvalue()


class ShazamRecognizer:
    def __init__(self):
        self._shazam = None

    def _client(self):
        if self._shazam is None:
            from shazamio import Shazam  # lazy: heavy optional dependency
            self._shazam = Shazam()
        return self._shazam

    def is_available(self) -> bool:
        try:
            import shazamio  # noqa: F401
            return True
        except ImportError:
            return False

    async def warmup(self):
        """Force the heavy ShazamIO/Rust core to initialize ahead of time so the
        first real recognition isn't delayed by ~5s of one-time setup. Best
        effort — a no-match on random noise is fine."""
        try:
            import numpy as np
            samples = (np.random.randn(16000 * 3) * 600).astype(np.int16)
            chunk = AudioChunk(data=samples, sample_rate=16000, channels=1,
                               duration=3.0, capture_start_time=0.0)
            await self.recognize(chunk)
            logger.info("Shazam recognizer warmed up")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Shazam warmup skipped: %s", exc)

    async def recognize(self, audio: AudioChunk) -> Optional[RecognitionResult]:
        if audio.is_silent(MIN_AUDIO_LEVEL):
            return None
        wav = audio_to_wav(audio)
        try:
            result = await self._client().recognize(wav)
        except Exception as exc:  # noqa: BLE001 - ShazamIO raises a variety
            logger.warning("Shazam recognize failed: %s", exc)
            return None

        matches = result.get("matches") if isinstance(result, dict) else None
        if not matches:
            return None

        track = result.get("track", {})
        match = matches[0]
        time_skew = float(match.get("timeskew", 0.0) or 0.0)
        freq_skew = float(match.get("frequencyskew", 0.0) or 0.0)

        if abs(time_skew) > SKEW_REJECT_THRESHOLD or abs(freq_skew) > SKEW_REJECT_THRESHOLD:
            logger.info("Shazam match rejected (skew time=%.3f freq=%.3f)", time_skew, freq_skew)
            return None

        offset = float(match.get("offset", 0.0) or 0.0)
        album = None
        for section in track.get("sections", []):
            if section.get("type") == "SONG":
                for meta in section.get("metadata", []):
                    if meta.get("title") == "Album":
                        album = meta.get("text")
        images = track.get("images", {}) or {}
        art = images.get("coverarthq") or images.get("coverart")

        return RecognitionResult(
            title=track.get("title", "Unknown"),
            artist=track.get("subtitle", "Unknown"),
            offset=offset,
            capture_start_time=audio.capture_start_time,
            confidence=1.0,
            time_skew=time_skew,
            frequency_skew=freq_skew,
            album=album,
            album_art_url=art,
            isrc=track.get("isrc"),
            provider="shazam",
        )
