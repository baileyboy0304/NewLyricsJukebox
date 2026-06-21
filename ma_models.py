"""Lightweight, dependency-free Music Assistant data models.

Kept separate from the MA client so the spine (``classify_source_mode``) and its
tests don't need the ``music_assistant_client`` package installed.
"""

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class PlayerState:
    player_id: str
    name: str
    state: str = "idle"             # "playing" | "paused" | "idle"
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    image_url: Optional[str] = None
    position: float = 0.0           # seconds into the track / stream
    duration_ms: Optional[int] = None
    media_type: Optional[str] = None  # e.g. "track", "radio"
    # Source discrimination (queue vs external stream):
    queue_id: Optional[str] = None
    active_source_id: Optional[str] = None
    active_source_name: Optional[str] = None
    position_last_updated: float = 0.0
    # MA URI of the currently-playing track ("spotify://track/..." or
    # "library://track/..."). Captured so /add-to-playlist can add the
    # current track to a Spotify playlist without re-searching.
    track_uri: Optional[str] = None

    @property
    def is_playing(self) -> bool:
        return self.state == "playing"


@dataclass
class DetectedPlayer:
    """A player offered for selection in the UI."""
    name: str
    ma_player_id: Optional[str] = None
    source_ip: Optional[str] = None
    ssrc: Optional[str] = None
    assigned: bool = False
    active: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ma_player_id": self.ma_player_id,
            "source_ip": self.source_ip,
            "ssrc": self.ssrc,
            "assigned": self.assigned,
            "active": self.active,
        }


def now() -> float:
    return time.time()
