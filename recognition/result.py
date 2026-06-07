"""Shared recognition result type."""

import time
from dataclasses import dataclass, field
from typing import Optional


def _normalize(text: Optional[str]) -> str:
    return (text or "").strip().lower()


@dataclass
class RecognitionResult:
    title: str
    artist: str
    offset: float                 # position in the song at capture START (seconds)
    capture_start_time: float     # unix ts of capture start
    recognition_time: float = field(default_factory=time.time)
    confidence: float = 1.0
    time_skew: float = 0.0
    frequency_skew: float = 0.0
    album: Optional[str] = None
    album_art_url: Optional[str] = None
    isrc: Optional[str] = None
    duration: Optional[float] = None
    provider: str = "shazam"      # "shazam" | "acrcloud"

    def get_current_position(self) -> float:
        """Latency-compensated current position (offset + wall-clock elapsed)."""
        elapsed = time.time() - self.capture_start_time
        return max(0.0, self.offset + elapsed)

    def get_latency(self) -> float:
        return self.recognition_time - self.capture_start_time

    def is_same_song(self, other: Optional["RecognitionResult"]) -> bool:
        if other is None:
            return False
        return (_normalize(self.title) == _normalize(other.title)
                and _normalize(self.artist) == _normalize(other.artist))

    def __str__(self):
        return f"{self.artist} - {self.title} @ {self.offset:.1f}s ({self.provider})"
