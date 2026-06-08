"""Controller: lyrics must be non-blocking (defect: 10s+ delay from slow QQ)."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NLJ_DATA_DIR", tempfile.mkdtemp(prefix="nlj_ctl_"))
os.environ.setdefault("NLJ_OPTIONS_FILE", "/tmp/nlj_no_options.json")

from server import Controller, PlayerRuntime  # noqa: E402


class _Fast:
    name = "lrclib"
    priority = 2
    enabled = True

    def get_lyrics(self, artist, title, album=None, duration=None):
        return {"lyrics": [(0.0, "hello"), (5.0, "world")]}


class _SlowFail:
    name = "qq"
    priority = 5
    enabled = True

    def get_lyrics(self, artist, title, album=None, duration=None):
        time.sleep(1.0)   # simulates QQ's slow retries
        return None


class _Stream:
    key = "4efd289d"
    ma_player_id = "a0505ec6"
    name = "respeaker_lyrics"
    source_ip = "192.168.1.137"
    ssrc = 0x4EFD289D
    active = True


class _Capture:
    def list_streams(self):
        return []

    def get_stream(self, k):
        return _Stream() if k == "4efd289d" else None

    def find_stream(self, **kw):
        return _Stream() if kw.get("ma_player_id") == "a0505ec6" else None

    def first_active_stream(self):
        return _Stream()


class _MANoPlayers:
    connected = True
    preferred_player_id = None

    def list_players(self):
        return []

    async def get_player_state(self, pid):
        return None


def test_rtp_stream_enters_stream_mode_not_stuck_in_queue():
    """Regression: an RTP stream advertises an MA id, but MA has no such player.
    It must enter stream/recognition mode, not get stuck in queue mode."""
    async def scenario():
        c = Controller(ma=_MANoPlayers(), capture=_Capture())

        class _Rec:
            current = None
            is_playing = False

            def get_position(self):
                return 0.0

        c._ensure_recognizer = lambda key: _Rec()
        track = await c.current_track(None)
        assert track["source"] == "stream"

    asyncio.run(scenario())


def test_working_queue_player_survives_transient_ma_none():
    """A player already showing MA metadata keeps its track when MA briefly
    returns None (rather than flipping to recognition)."""
    async def scenario():
        prev = {"source": "queue", "from_ma": True, "player": "Kitchen",
                "title": "Mamma Mia", "artist": "ABBA",
                "track_id": "ABBA|Mamma Mia", "is_playing": True}

        class _MA:
            connected = True
            preferred_player_id = "p1"

            def list_players(self):
                return [{"player_id": "p1", "name": "Kitchen"}]

            async def get_player_state(self, pid):
                return None  # transient blip

        c = Controller(ma=_MA(), capture=None)
        c.runtimes["p1"] = PlayerRuntime(key="p1", mode="queue", track=dict(prev))
        track = await c.current_track(None)
        assert track["source"] == "queue"
        assert track["title"] == "Mamma Mia"

    asyncio.run(scenario())


def test_metadata_first_uses_ma_even_for_external_spotify_connect():
    """MA has the track even for an external Spotify Connect source — use it
    immediately instead of slow recognition. seekable=False (can't seek)."""
    from ma_models import PlayerState

    async def scenario():
        class _MA:
            connected = True
            preferred_player_id = "p1"

            def list_players(self):
                return [{"player_id": "p1", "name": "Respeaker"}]

            async def get_player_state(self, pid):
                return PlayerState(
                    player_id="p1", name="Respeaker", state="playing",
                    title="Candy", artist="Paolo Nutini", album="Sunny Side Up",
                    position=12.0, duration_ms=298000,
                    active_source_name="Spotify Connect",  # external -> classify "stream"
                )

        c = Controller(ma=_MA(), capture=None)
        track = await c.current_track(None)
        assert track["title"] == "Candy"
        assert track["from_ma"] is True
        assert track["source"] == "stream"     # external source
        assert track["seekable"] is False       # can't seek a Spotify Connect stream

    asyncio.run(scenario())


def test_lyrics_response_is_non_blocking():
    async def scenario():
        c = Controller(ma=None, capture=None)
        c.lyrics_service.providers = [_Fast(), _SlowFail()]
        c.lyrics_service._by_name = {p.name: p for p in c.lyrics_service.providers}
        c.runtimes["auto"] = PlayerRuntime(
            key="auto",
            track={"title": "Mamma Mia", "artist": "ABBA",
                   "track_id": "ABBA|Mamma Mia", "position": 0.0},
        )

        # First call must return immediately (pending), NOT wait on the slow provider.
        t0 = time.monotonic()
        r1 = await c.lyrics(None)
        assert (time.monotonic() - t0) < 0.3
        assert r1["pending"] is True
        assert r1["has_lyrics"] is False

        # The fast provider should populate lyrics well before the slow one finishes.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if c.runtimes["auto"].lyrics is not None:
                break
        r2 = await c.lyrics(None)
        assert r2["has_lyrics"] is True
        assert r2["provider"] == "lrclib"

        # Clean up the background task.
        task = c.runtimes["auto"].lyrics_task
        if task:
            task.cancel()

    asyncio.run(scenario())
