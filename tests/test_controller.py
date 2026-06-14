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

        c._ensure_recognizer = _fake_set_active
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


def test_selected_player_follows_playing_spotify_connect_player():
    """A SELECTED RTP stream whose own MA player has no track must follow the
    actually-playing MA player (Spotify Connect coordinator) immediately, instead
    of falling through to ~10s recognition."""
    from ma_models import PlayerState

    async def scenario():
        class _MA:
            connected = True
            preferred_player_id = None

            def list_players(self):
                return [{"player_id": "a0505ec6", "name": "Respeaker"}]

            def find_playing_player_id(self):
                return "coordinator"   # a different player holds the Connect track

            def players_related(self, a, b):
                # The respeaker IS grouped with the coordinator (it's the speaker
                # playing the Connect audio), so following its metadata is valid.
                return {a, b} == {"a0505ec6", "coordinator"}

            async def get_player_state(self, pid):
                if pid == "coordinator":
                    return PlayerState(
                        player_id="coordinator", name="Group", state="playing",
                        title="Candy", artist="Paolo Nutini",
                        active_source_name="Spotify Connect")
                return None  # the resolved respeaker player has no track itself

        c = Controller(ma=_MA(), capture=_Capture())
        # Select the RTP stream explicitly (param is a stream key, not "auto").
        track = await c.current_track("4efd289d")
        assert track["title"] == "Candy"
        assert track["from_ma"] is True
        assert track["source"] == "stream"
        assert track["player"] == "respeaker_lyrics"   # keeps the capture device name

    asyncio.run(scenario())


def test_selected_standalone_player_not_hijacked_to_unrelated_playing_player():
    """A selected standalone device (e.g. a mic) must NOT borrow the metadata of an
    unrelated MA player that happens to be playing. It should recognize its own
    audio instead of showing the other player's track/name."""
    from ma_models import PlayerState

    async def scenario():
        class _MA:
            connected = True
            preferred_player_id = None

            def list_players(self):
                return [{"player_id": "a0505ec6", "name": "Respeaker"}]

            def find_playing_player_id(self):
                return "cellar"   # a totally separate speaker is playing

            def players_related(self, a, b):
                return False       # the mic has nothing to do with it

            async def get_player_state(self, pid):
                if pid == "cellar":
                    return PlayerState(player_id="cellar", name="Cellar Speaker",
                                       state="playing", title="Watermelon Sugar",
                                       artist="Harry Styles")
                return None        # the selected mic's player has no track

        class _Rec:
            current = None
            is_playing = False

            def get_position(self):
                return 0.0

        c = Controller(ma=_MA(), capture=_Capture())

        async def _fake_set_active(stream_key):
            return _Rec()

        c._ensure_recognizer = _fake_set_active
        track = await c.current_track("4efd289d")
        # Not hijacked: no "Watermelon Sugar", falls through to recognition on the
        # selected stream, keeping the device's own name.
        assert track["title"] != "Watermelon Sugar"
        assert track["source"] == "stream"
        assert track["player"] == "respeaker_lyrics"

    asyncio.run(scenario())


def test_switching_spotify_connect_to_radio_uses_recognition_not_stale_connect():
    """Regression: after switching Spotify Connect -> radio, MA may still list the
    old Connect player as 'playing' with the stale track. The player we actually
    listen to is now on radio, so we must recognize, not follow the stale player."""
    from ma_models import PlayerState

    async def scenario():
        class _MA:
            connected = True
            preferred_player_id = None

            def list_players(self):
                return [{"player_id": "a0505ec6", "name": "respeaker_lyrics"}]

            def find_playing_player_id(self):
                return "spotify_connect"   # stale Connect player still 'playing'

            async def get_player_state(self, pid):
                if pid == "a0505ec6":
                    # Our resolved player is now on radio (group playing a station).
                    return PlayerState(player_id="a0505ec6", name="respeaker_lyrics",
                                       state="playing", title="Smooth Radio (London)",
                                       artist="Elton John", media_type="radio")
                if pid == "spotify_connect":
                    # Lingering Connect session with the OLD track.
                    return PlayerState(player_id="spotify_connect", name="Connect",
                                       state="playing", title="I Won't Let The Sun Go Down",
                                       artist="Nik Kershaw",
                                       active_source_name="Spotify Connect")
                return None

        c = Controller(ma=_MA(), capture=_Capture())

        class _Rec:
            current = None
            is_playing = False

            def get_position(self):
                return 0.0

        async def fake_set_active(stream_key):
            return _Rec()

        c._ensure_recognizer = fake_set_active
        track = await c.current_track("4efd289d")
        assert track["source"] == "stream"
        assert track["title"] != "I Won't Let The Sun Go Down"  # not the stale Connect track

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

        c._ensure_recognizer = fake_set_active
        track = await c.current_track(None)
        assert track["source"] == "stream"
        assert track["title"] != "Smooth Radio (London, UK)"

    asyncio.run(scenario())


def test_spotify_connect_to_radio_stale_key_recovers_to_recognition():
    """The reported lockup: selected key is a stale SSRC (respeaker reconnected),
    MA still lists the old Connect player as 'playing'. We must re-point at the
    live stream (now on radio) and recognize, not follow the stale Connect track."""
    from ma_models import PlayerState

    class _LiveRadio:
        key = "0369d034"            # the fresh stream now carrying radio audio
        ma_player_id = "respeaker"
        name = "respeaker_lyrics"
        active = True

    class _Cap:
        def get_stream(self, k):
            return None             # selected SSRC 73d34824 has been pruned (stale)

        def find_stream(self, **kw):
            return _LiveRadio() if kw.get("ma_player_id") == "respeaker" else None

        def first_active_stream(self):
            return _LiveRadio()

    async def scenario():
        class _MA:
            connected = True
            preferred_player_id = None

            def list_players(self):
                return [{"player_id": "respeaker", "name": "respeaker_lyrics"}]

            def find_playing_player_id(self):
                return "spotify_connect"   # lingering Connect player, still 'playing'

            async def get_player_state(self, pid):
                if pid == "respeaker":     # the live audio source is now radio
                    return PlayerState(player_id="respeaker", name="respeaker_lyrics",
                                       state="playing", title="Smooth Radio",
                                       artist="Elton John", media_type="radio")
                if pid == "spotify_connect":
                    return PlayerState(player_id="spotify_connect", name="Connect",
                                       state="playing", title="The Best of My Love",
                                       artist="Eagles",
                                       active_source_name="Spotify Connect")
                return None            # the stale selected key resolves to nothing

        c = Controller(ma=_MA(), capture=_Cap())

        class _Rec:
            current = None
            is_playing = False

            def get_position(self):
                return 0.0

        async def fake_set_active(stream_key):
            return _Rec()

        c._ensure_recognizer = fake_set_active
        track = await c.current_track("73d34824")   # stale selected SSRC
        assert track["source"] == "stream"
        assert track["title"] != "The Best of My Love"   # not the stale Connect track

    asyncio.run(scenario())


def test_resolve_stream_key_migrates_off_dead_selected_stream():
    """A selected stream that has gone dead (respeaker reconnected with a new
    SSRC) must resolve to the freshest live stream, not the dead one."""
    class _Dead:
        key = "73d34824"
        ma_player_id = "a0505ec6"
        active = False

    class _Live:
        key = "ae0d58dd"
        ma_player_id = "a0505ec6"
        active = True

    class _Cap:
        def get_stream(self, k):
            return _Dead() if k == "73d34824" else None

        def find_stream(self, **kw):
            return _Live() if kw.get("ma_player_id") == "a0505ec6" else None

        def first_active_stream(self):
            return _Live()

    c = Controller(ma=None, capture=_Cap())
    # Dead selected stream + matching identity -> migrate to the live stream.
    assert c._resolve_stream_key("73d34824", "a0505ec6") == "ae0d58dd"


def test_transport_migrates_stale_stream_key_to_live_ma_player():
    """A selected RTP stream key that has gone stale (respeaker reconnected with a
    new SSRC) must NOT be sent to MA as the player id — transport migrates to the
    live stream's MA player instead of erroring 'Player <ssrc> is not available'."""
    called = {}

    class _MA:
        connected = True
        preferred_player_id = None

        def list_players(self):
            return [{"player_id": "real-ma-id", "name": "respeaker"}]

        def find_playing_player_id(self):
            return None

        async def play_pause(self, pid):
            called["pid"] = pid
            return True

        async def play(self, pid): return True
        async def pause(self, pid): return True
        async def next(self, pid): return True
        async def previous(self, pid): return True
        async def seek(self, pid, pos): return True

    class _Live:
        key = "newssrc"
        ma_player_id = "real-ma-id"
        name = "respeaker"
        active = True

    class _Cap:
        def get_stream(self, k):
            return None                      # the stale SSRC is gone

        def first_active_stream(self):
            return _Live()

    async def scenario():
        c = Controller(ma=_MA(), capture=_Cap())
        res = await c.transport("78e8eb7a", "play_pause")   # stale selected SSRC
        assert res["ok"] is True
        assert called["pid"] == "real-ma-id"  # migrated, not the raw SSRC

    asyncio.run(scenario())


def test_transport_passes_through_valid_ma_id():
    """A valid MA player id is used as-is (no needless migration)."""
    called = {}

    class _MA:
        connected = True
        preferred_player_id = None

        def list_players(self):
            return [{"player_id": "p1", "name": "Kitchen"}]

        def find_playing_player_id(self):
            return None

        async def pause(self, pid):
            called["pid"] = pid
            return True

        async def play(self, pid): return True
        async def play_pause(self, pid): return True
        async def next(self, pid): return True
        async def previous(self, pid): return True
        async def seek(self, pid, pos): return True

    async def scenario():
        c = Controller(ma=_MA(), capture=None)
        res = await c.transport("p1", "pause")
        assert res["ok"] is True and called["pid"] == "p1"

    asyncio.run(scenario())


def test_set_lyric_offset_remembers_and_forgets():
    """memory ON stores the offset on the song; memory OFF resets to 0 and clears
    the stored value."""
    c = Controller(ma=None, capture=None)
    c.runtimes["auto"] = PlayerRuntime(
        key="auto", track={"title": "Song", "artist": "Artist",
                           "track_id": "Artist|Song"})
    stored = {}
    c.lyrics_service.set_timing_offset = lambda a, t, o: stored.__setitem__("v", o)
    c.lyrics_service.clear_timing_offset = lambda a, t: stored.pop("v", None)

    r1 = c.set_lyric_offset(None, 0.5, True)        # memory on -> remember
    assert r1["timing_offset"] == 0.5 and stored["v"] == 0.5
    assert c.runtimes["auto"].timing_offset == 0.5

    r2 = c.set_lyric_offset(None, 0.5, False)       # memory off -> forget + reset
    assert r2["timing_offset"] == 0.0 and "v" not in stored
    assert c.runtimes["auto"].timing_offset == 0.0


def test_set_lyric_offset_propagates_to_other_runtimes_on_same_song():
    """An offset change from one client is pushed to every runtime currently on the
    same song, so other clients (e.g. the ESP32 bridge) pick it up on their next
    /lyrics poll instead of only at the song's next play."""
    c = Controller(ma=None, capture=None)
    c.lyrics_service.set_timing_offset = lambda a, t, o: None
    c.lyrics_service.clear_timing_offset = lambda a, t: None
    # Two players on the same song; a third on a different song must NOT change.
    c.runtimes["web"] = PlayerRuntime(
        key="web", track={"title": "Song", "artist": "Artist"},
        lyrics_key="Artist|Song")
    c.runtimes["esp32"] = PlayerRuntime(
        key="esp32", track={"title": "Song", "artist": "Artist"},
        lyrics_key="Artist|Song")
    c.runtimes["other"] = PlayerRuntime(
        key="other", track={"title": "Else", "artist": "Artist"},
        lyrics_key="Artist|Else")

    c.set_lyric_offset("web", 0.75, True)
    assert c.runtimes["web"].timing_offset == 0.75
    assert c.runtimes["esp32"].timing_offset == 0.75   # propagated
    assert c.runtimes["other"].timing_offset == 0.0     # untouched


def test_set_suppress_lyrics_toggles_and_persists():
    """Thumbs-down sets the runtime flag and persists it; toggling off clears it."""
    c = Controller(ma=None, capture=None)
    c.runtimes["auto"] = PlayerRuntime(
        key="auto", track={"title": "Song", "artist": "Artist",
                           "track_id": "Artist|Song"})
    saved = {}
    c.lyrics_service.set_suppress_lyrics = lambda a, t, s: saved.__setitem__("v", s)

    r1 = c.set_suppress_lyrics(None, True)
    assert r1["suppress_lyrics"] is True and saved["v"] is True
    assert c.runtimes["auto"].suppress_lyrics is True

    r2 = c.set_suppress_lyrics(None, False)
    assert r2["suppress_lyrics"] is False and saved["v"] is False
    assert c.runtimes["auto"].suppress_lyrics is False


def test_list_players_dedupes_reconnected_streams():
    """A respeaker that reconnects gets a fresh SSRC, leaving stale sibling streams
    with the same name/MA id. The picker must show ONE entry per device (the active
    / freshest), not one per SSRC."""
    class _Cap:
        def list_streams(self):
            return [
                {"key": "aaa", "name": "respeaker_lyrics", "ma_player_id": "id1",
                 "source_ip": "1.2.3.4", "ssrc": "aaa", "active": False, "last_seen": 100},
                {"key": "bbb", "name": "respeaker_lyrics", "ma_player_id": "id1",
                 "source_ip": "1.2.3.4", "ssrc": "bbb", "active": True, "last_seen": 200},
                {"key": "ccc", "name": "respeaker_lyrics", "ma_player_id": "id1",
                 "source_ip": "1.2.3.4", "ssrc": "ccc", "active": False, "last_seen": 150},
            ]

    c = Controller(ma=None, capture=_Cap())
    out = c.list_players()
    assert len(out["players"]) == 1            # three SSRCs -> one device
    assert out["players"][0]["key"] == "bbb"   # the active stream wins


def test_list_players_keeps_distinct_devices_separate():
    """Two genuinely different devices (different MA ids) must NOT be merged."""
    class _Cap:
        def list_streams(self):
            return [
                {"key": "aaa", "name": "kitchen", "ma_player_id": "id1",
                 "source_ip": "1.2.3.4", "ssrc": "aaa", "active": True, "last_seen": 100},
                {"key": "bbb", "name": "lounge", "ma_player_id": "id2",
                 "source_ip": "1.2.3.5", "ssrc": "bbb", "active": True, "last_seen": 100},
            ]

    c = Controller(ma=None, capture=_Cap())
    out = c.list_players()
    assert len(out["players"]) == 2


def test_recognizer_is_deduped_per_stream():
    """Distinct streams each get a recognizer (mics recognize in parallel), but
    several players pointing at the SAME stream share ONE recognizer — they must
    not each spin up a Shazam loop on the same audio."""
    class _Cap:
        async def get_audio(self, duration, key, should_continue=None):
            return None  # keep recognizers idle/cheap

    async def scenario():
        c = Controller(ma=None, capture=_Cap())
        await c._ensure_recognizer("aaaa")
        await c._ensure_recognizer("aaaa")             # same stream again -> no dup
        await c._ensure_recognizer("bbbb")
        assert set(c.recognizers) == {"aaaa", "bbbb"}  # one per distinct stream
        await c._stop_stream_recognizer("aaaa")
        assert set(c.recognizers) == {"bbbb"}
        await c._stop_all_recognizers()
        assert c.recognizers == {}

    asyncio.run(scenario())


def test_supervisor_runs_only_leased_active_players_and_drops_expired():
    """The supervisor ticks the pipeline for every leased + active player and
    forgets expired leases (so the engine follows watchers, not HTTP polls)."""
    async def scenario():
        c = Controller(ma=None, capture=_Capture())
        calls = []

        async def fake_ct(key):
            calls.append(key)

        c.current_track = fake_ct
        c._player_active = lambda k: True
        c.leases = {"a": time.time() + 10, "b": time.time() + 10,
                    "old": time.time() - 1}
        await c.supervise_once()
        assert set(calls) == {"a", "b"}     # expired 'old' never ticked
        assert "old" not in c.leases

    asyncio.run(scenario())


def test_supervisor_tears_down_recognizer_when_unwatched():
    """A stream's recognizer is stopped once no leased player is recognizing on it
    (display slept / tab closed), instead of lingering forever."""
    class _Cap:
        async def get_audio(self, duration, key, should_continue=None):
            return None

    async def scenario():
        c = Controller(ma=None, capture=_Cap())

        async def noop(key):
            pass

        c.current_track = noop
        await c._ensure_recognizer("aaaa")
        assert "aaaa" in c.recognizers
        await c.supervise_once()            # no lease -> no wanted stream
        assert "aaaa" not in c.recognizers

    asyncio.run(scenario())


def test_supervisor_shares_one_recognizer_across_players_on_same_stream():
    """Two leased players resolving to the same stream must end up sharing ONE
    recognizer, not one each (the duplicate-id pile-up seen on real devices)."""
    class _Cap:
        async def get_audio(self, duration, key, should_continue=None):
            return None

    async def scenario():
        c = Controller(ma=None, capture=_Cap())
        c._player_active = lambda k: True

        async def fake_ct(key):
            # Both players recognize on the same physical stream "shared".
            rt = c.runtimes.setdefault(key, PlayerRuntime(key=key))
            rt.rec_stream = "shared"
            await c._ensure_recognizer("shared")

        c.current_track = fake_ct
        c.leases = {"web": time.time() + 10, "esp32": time.time() + 10}
        await c.supervise_once()
        assert set(c.recognizers) == {"shared"}     # one recognizer, not two
        await c._stop_all_recognizers()

    asyncio.run(scenario())


def test_track_snapshot_is_read_only_and_starts_no_recognition():
    """The HTTP read path returns last-computed state and never starts a
    recognizer — only the supervisor drives recognition now."""
    c = Controller(ma=_MANoPlayers(), capture=_Capture())
    c.touch_lease("4efd289d")
    snap = c.track_snapshot("4efd289d")
    assert snap["title"] is None            # nothing computed yet
    assert snap["player"] == "respeaker_lyrics"
    assert c.recognizers == {}              # read path never recognizes
    assert "4efd289d" in c.leases           # but the lease was renewed


def test_concurrent_ensure_never_leaves_two_recognizers_for_one_stream():
    """Concurrent ticks for the same stream must collapse to exactly one
    recognizer (the _rec_lock guards the create window)."""
    class _Cap:
        async def get_audio(self, duration, key, should_continue=None):
            return None  # keep recognizers idle/cheap

    async def scenario():
        c = Controller(ma=None, capture=_Cap())
        await asyncio.gather(*[
            c._ensure_recognizer("aaaa") for _ in range(24)
        ])
        assert len(c.recognizers) == 1      # never two for one stream
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
