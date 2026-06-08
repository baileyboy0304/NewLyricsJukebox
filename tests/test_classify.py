"""classify_source_mode seam: the spine decision."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from classify import classify_source_mode  # noqa: E402
from ma_models import PlayerState  # noqa: E402
from music_assistant import _coalesce_media_type  # noqa: E402


def test_coalesce_media_type_radio_wins():
    # MA enriches a radio's now-playing into a library track: queue item looks
    # like a track, but the player's current_media is still a radio stream.
    assert _coalesce_media_type("track", "radio") == "radio"
    assert _coalesce_media_type("radio", "track") == "radio"
    assert _coalesce_media_type("radio", None) == "radio"
    # No radio signal -> first concrete type.
    assert _coalesce_media_type("track", None) == "track"
    assert _coalesce_media_type(None, "track") == "track"
    assert _coalesce_media_type("idle", "track") == "track"
    assert _coalesce_media_type(None, None) is None


def test_radio_is_stream():
    p = PlayerState(player_id="p1", name="Kitchen", media_type="radio",
                    active_source_name="Kitchen Queue")
    assert classify_source_mode(p) == "stream"


def test_queue_source_name_is_queue():
    p = PlayerState(player_id="p1", name="Kitchen", state="playing",
                    active_source_name="Kitchen Queue", media_type="track")
    assert classify_source_mode(p) == "queue"


def test_external_source_name_is_stream():
    p = PlayerState(player_id="p1", name="Kitchen", state="playing",
                    active_source_name="Spotify Connect", media_type="track")
    assert classify_source_mode(p) == "stream"


def test_source_id_matches_queue():
    p = PlayerState(player_id="p1", name="Kitchen", state="playing",
                    queue_id="q1", active_source_id="q1")
    assert classify_source_mode(p) == "queue"


def test_source_id_external():
    p = PlayerState(player_id="p1", name="Kitchen", state="playing",
                    queue_id="q1", active_source_id="spotify_connect")
    assert classify_source_mode(p) == "stream"


def test_stale_idle_is_stream():
    p = PlayerState(player_id="p1", name="Kitchen", state="idle",
                    position_last_updated=time.time() - 30)
    assert classify_source_mode(p) == "stream"


def test_default_is_queue():
    p = PlayerState(player_id="p1", name="Kitchen", state="playing",
                    position_last_updated=time.time())
    assert classify_source_mode(p) == "queue"
