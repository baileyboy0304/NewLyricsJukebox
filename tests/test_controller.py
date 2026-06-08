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

    def find_playing_player_id(self):
        return None

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

        async def _fake_set_active(stream_key):
            return _Rec()

        c._set_active_recognizer = _fake_set_active
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

            def find_playing_player_id(self):
                return None

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


def test_radio_uses_recognition_not_ma_station_name():
    """For radio, MA's title is the station name — must NOT be shown; fall
    through to recognition instead."""
    from ma_models import PlayerState

    class _MA:
        connected = True
        preferred_player_id = None

        def list_players(self):
            return [{"player_id": "p1", "name": "Radio"}]

        def find_playing_player_id(self):
            return None

        async def get_player_state(self, pid):
            return PlayerState(player_id="p1", name="Radio", state="playing",
                               title="Smooth Radio (London, UK)",
                               artist="Luther Vandross", media_type="radio")

    async def scenario():
        c = Controller(ma=_MA(), capture=_Capture())

        class _Rec:
            current = None
            is_playing = False

            def get_position(self):
                return 0.0

        async def fake_set_active(stream_key):
            return _Rec()

        c._set_active_recognizer = fake_set_active
        track = await c.current_track(None)
        assert track["source"] == "stream"
        assert track["title"] != "Smooth Radio (London, UK)"

    asyncio.run(scenario())


def test_only_one_recognizer_runs_at_a_time():
    """Switching streams must stop the previous recognizer (no pile-up that
    bombards Shazam in parallel)."""
    class _Cap:
        async def get_audio(self, duration, key, should_continue=None):
            return None  # keep recognizers idle/cheap

    async def scenario():
        c = Controller(ma=None, capture=_Cap())
        await c._set_active_recognizer("aaaa")
        assert set(c.recognizers) == {"aaaa"}
        await c._set_active_recognizer("bbbb")
        assert set(c.recognizers) == {"bbbb"}      # aaaa stopped
        await c._stop_all_recognizers()
        assert c.recognizers == {}

    asyncio.run(scenario())


def test_get_audio_requires_fresh_data():
    """A full but STALE buffer must not be re-recognized; only fresh audio
    counts (fixes dead/paused streams 'recognizing' the same clip forever)."""
    import numpy as np
    from recognition.udp_capture import PlayerStream

    async def scenario():
        s = PlayerStream(key="x", sample_rate=16000, channels=1)
        chunk = (np.random.randn(16000 * 7) * 5000).astype("<i2").tobytes()
        s._buffer.extend(chunk)
        s._total_received = len(chunk)
        s.last_seen = time.time()

        first = await s.get_audio(6.0, lambda: True)
        assert first is not None            # fresh data -> returns a chunk

        # No new packets since; mark the stream dead so the wait can give up.
        s.last_seen = time.time() - 30
        second = await s.get_audio(6.0, lambda: True)
        assert second is None               # no fresh audio -> None (not stale replay)

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
