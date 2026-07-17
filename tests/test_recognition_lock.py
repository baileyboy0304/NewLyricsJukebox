"""Recognition lock-cycle seam: the '3 attempts before locked' consensus."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recognition.engine import LockTracker  # noqa: E402
from recognition.result import RecognitionResult  # noqa: E402


def make(offset, capture_start, title="Song", artist="Artist"):
    return RecognitionResult(title=title, artist=artist, offset=offset,
                             capture_start_time=capture_start)


def test_locks_after_initial_plus_three_confirmations():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    # Same timeline: anchor = offset - capture_start = constant 10.0
    assert lt.offer(make(10, 0)) == "initial"   # first read accepted/displayed
    assert lt.offer(make(15, 5)) == "locking"   # confirmation 1 of 3
    assert lt.offer(make(20, 10)) == "locking"  # confirmation 2 of 3
    assert lt.offer(make(25, 15)) == "locked"   # confirmation 3 of 3 -> LOCKED
    assert lt.locked is True


def test_single_outlier_while_locking_is_held():
    """The bug from the Wham! log: one rogue read while converging must NOT reset
    the streak or move the position; the next in-timeline read keeps locking."""
    lt = LockTracker(lock_after=3, tolerance=3.0, relock_after=2)
    assert lt.offer(make(10, 0)) == "initial"    # baseline anchor 10
    assert lt.offer(make(16, 6)) == "locking"    # anchor 10, confirmation 1
    assert lt.offer(make(50, 12)) == "outlier"   # rogue (anchor 38) -> held
    assert lt.confirmations == 1                 # streak preserved, position held
    assert lt.offer(make(28, 18)) == "locking"   # anchor 10, confirmation 2
    assert lt.confirmations == 2


def test_sustained_shift_while_locking_rebases():
    """Two agreeing off-baseline reads while converging ARE a real shift."""
    lt = LockTracker(lock_after=3, tolerance=3.0, relock_after=2)
    lt.offer(make(10, 0))                        # initial, anchor 10
    lt.offer(make(16, 6))                         # locking 1, anchor 10
    assert lt.offer(make(50, 6)) == "outlier"     # anchor 44, drift 1 (held)
    assert lt.offer(make(56, 12)) == "relock"     # anchor 44, drift 2 -> re-base
    assert lt.confirmations == 1                  # streak restarts on new timeline


def test_single_outlier_after_lock_is_held():
    """A one-off bad match (chorus confusion) must NOT break the lock or move
    the position."""
    lt = LockTracker(lock_after=3, tolerance=3.0, relock_after=2)
    for off, cs in [(10, 0), (15, 5), (20, 10), (25, 15)]:
        lt.offer(make(off, cs))               # locked on anchor 10
    assert lt.offer(make(99, 0)) == "outlier"   # rogue (anchor 99) -> held
    assert lt.offer(make(35, 25)) == "ignored"  # back on anchor 10 -> in sync
    assert lt.locked


def test_sustained_shift_breaks_lock_and_reacquires():
    """A radio skip: a NEW, self-consistent timeline persisting for relock_after
    reads must break the lock and re-acquire (not be ignored forever). Default
    relock_after=2 auto-corrects after two recognitions."""
    lt = LockTracker(lock_after=3, tolerance=3.0, relock_after=2)
    for off, cs in [(10, 0), (15, 5), (20, 10), (25, 15)]:
        lt.offer(make(off, cs))               # locked on anchor 10
    # New timeline: anchor = offset - capture_start = constant 50.
    assert lt.offer(make(70, 20)) == "outlier"    # drift 1 of 2 (held)
    assert lt.offer(make(76, 26)) == "reacquire"  # drift 2 -> re-acquire
    assert lt.locked
    assert lt.offer(make(82, 32)) == "ignored"    # in sync on the new timeline


def test_relock_after_threshold_is_independent():
    """relock_after can require more reads than the default before re-acquiring."""
    lt = LockTracker(lock_after=3, tolerance=3.0, relock_after=3)
    for off, cs in [(10, 0), (15, 5), (20, 10), (25, 15)]:
        lt.offer(make(off, cs))
    assert lt.offer(make(70, 20)) == "outlier"    # drift 1 of 3 (held)
    assert lt.offer(make(76, 26)) == "outlier"    # drift 2 of 3 (held)
    assert lt.offer(make(82, 32)) == "reacquire"  # drift 3 -> re-acquire


def test_tracking_when_lock_disabled():
    lt = LockTracker(lock_after=3, tolerance=3.0, enabled=False)
    assert lt.offer(make(10, 0)) == "initial"
    # Never locks; always follows the latest recognition.
    assert lt.offer(make(15, 5)) == "tracking"
    assert lt.offer(make(99, 0)) == "tracking"
    assert lt.locked is False


def test_reset_clears_state():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))
    lt.reset()
    assert lt.confirmations == 0
    assert lt.initialized is False
    assert lt.result is None
    assert lt.locked is False


def test_position_interpolates_from_offset():
    import time
    r = make(30.0, time.time() - 2.0)  # captured 2s ago at 30s in
    pos = r.get_current_position()
    assert 31.5 < pos < 32.5


def test_recognition_call_times_out_and_loop_continues():
    """A hung Shazam/ACRCloud call must be abandoned (not block for minutes)."""
    import asyncio
    import time

    import numpy as np

    import recognition.engine as eng
    from recognition.engine import PlayerRecognizer
    from recognition.udp_capture import AudioChunk

    class _AlwaysAudio:
        """Capture stub that always has a fresh chunk available."""
        async def get_audio(self, duration, key, should_continue=None):
            return AudioChunk(
                data=(np.random.randn(16000) * 5000).astype(np.int16),
                sample_rate=16000, channels=1, duration=1.0,
                capture_start_time=time.time())

    async def scenario():
        orig = eng.RECOGNIZE_TIMEOUT
        eng.RECOGNIZE_TIMEOUT = 0.1
        try:
            rec = PlayerRecognizer("z", _AlwaysAudio())
            rec._interval = 0.02
            timed_out = {"n": 0}

            async def hang(_audio):
                timed_out["n"] += 1
                await asyncio.sleep(5)   # far longer than the timeout
                return None

            rec._recognize_once = hang
            rec.start()
            # If the timeout didn't work, this wait would hang on the 5s sleep.
            await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
            await rec.stop()
            assert timed_out["n"] >= 2   # multiple cycles ran despite the hang
        finally:
            eng.RECOGNIZE_TIMEOUT = orig

    asyncio.run(scenario())


def test_blank_threshold_is_context_aware_near_song_end():
    """Mid-song a no-match needs more failures to blank; within the song-end window
    (known duration) it blanks fast. Unknown duration falls back to the mid-song
    threshold."""
    import time

    from recognition.engine import PlayerRecognizer
    from recognition.result import RecognitionResult

    rec = PlayerRecognizer("z", capture=None)
    rec._blank_after = 3
    rec._blank_after_near_end = 1
    rec._song_end_window = 30.0

    # Mid-song: 200s track, 50s in -> 150s remaining -> mid-song threshold.
    rec._current = RecognitionResult(title="T", artist="A", offset=50.0,
                                     capture_start_time=time.time(), duration=200.0)
    assert rec._blank_threshold() == 3

    # Near the end: 200s track, 185s in -> 15s remaining -> near-end threshold.
    rec._current = RecognitionResult(title="T", artist="A", offset=185.0,
                                     capture_start_time=time.time(), duration=200.0)
    assert rec._blank_threshold() == 1

    # Unknown duration (radio) -> can't tell, use the tolerant mid-song threshold.
    rec._current = RecognitionResult(title="T", artist="A", offset=185.0,
                                     capture_start_time=time.time(), duration=None)
    assert rec._blank_threshold() == 3


def test_timeout_does_not_clear_held_track():
    """A hung Shazam call (timeout) is a hiccup, not a song-end: it must NOT clear
    the held track (the brief lyrics 'blip') or count as a blanking failure."""
    import asyncio
    import time

    import numpy as np

    import recognition.engine as eng
    from recognition.engine import PlayerRecognizer
    from recognition.result import RecognitionResult
    from recognition.udp_capture import AudioChunk

    class _AlwaysAudio:
        async def get_audio(self, duration, key, should_continue=None):
            return AudioChunk(
                data=(np.random.randn(16000) * 5000).astype(np.int16),
                sample_rate=16000, channels=1, duration=1.0,
                capture_start_time=time.time())

    async def scenario():
        orig = eng.RECOGNIZE_TIMEOUT
        eng.RECOGNIZE_TIMEOUT = 0.1
        try:
            rec = PlayerRecognizer("z", _AlwaysAudio())
            rec._interval = 0.02
            rec._blank_after = 1   # would clear after a single failure
            rec._current = RecognitionResult(
                title="T", artist="A", offset=10.0, capture_start_time=time.time())

            async def hang(_audio):
                await asyncio.sleep(5)   # forces the timeout every cycle
                return None

            rec._recognize_once = hang
            rec.start()
            await asyncio.wait_for(asyncio.sleep(0.5), timeout=2.0)
            await rec.stop()
            assert rec._current is not None   # held track survives the timeouts
            assert rec._failures == 0         # timeout not counted as a failure
        finally:
            eng.RECOGNIZE_TIMEOUT = orig

    asyncio.run(scenario())


def _acr_recognizer(priority=True, tolerance=5.0):
    """Build a PlayerRecognizer with stubbed Shazam/ACR for ACR-priority tests."""
    from recognition.engine import PlayerRecognizer

    rec = PlayerRecognizer("acr", capture=object())
    rec._acr_priority = priority
    rec._acr_tolerance = tolerance
    return rec


class _StubACR:
    def __init__(self, result, available=True):
        self._result = result
        self._available = available
        self.calls = 0

    def unavailable_reason(self):
        return None if self._available else "not configured"

    def is_available(self):
        return self._available

    async def recognize(self, audio):
        self.calls += 1
        return self._result


def test_same_recording_ignores_version_noise():
    """The George Benson case: same song, different variant labels must count as
    the same recording (so ACR's position + Spotify id are used)."""
    from recognition.engine import _same_recording
    a = make(10, 0, title="Lady Love Me (One More Time)", artist="George Benson")
    b = make(10, 0, title="Lady Love Me (2003 Remaster)", artist="George Benson")
    assert _same_recording(a, b) is True
    # A genuinely different song by the same artist is still rejected.
    c = make(10, 0, title="Give Me The Night", artist="George Benson")
    assert _same_recording(a, c) is False


def test_same_song_ignores_artist_punctuation():
    """The REM case: Shazam alternates "Rem" and "R.E.M." for the same audio.
    The punctuation-insensitive artist key must treat both as the same song."""
    from recognition.engine import _same_song
    a = make(10, 0, title="Near Wild Heaven", artist="Rem")
    b = make(10, 0, title="Near Wild Heaven", artist="R.E.M.")
    assert _same_song(a, b) is True
    assert _same_song(b, a) is True


def test_same_song_accepts_title_prefix_match():
    """The Céline Dion case: same recording labelled "Because You Loved Me"
    one cycle, "Because You Loved Me (Theme from "Up Close and Personal")"
    the next. Different ISRCs are sometimes assigned to each label
    (CAC222400016 vs CAC229600025), so the ISRC short-circuit can't merge
    them — instead the normalised title-prefix check has to. A genuinely
    different song by the same artist is still rejected."""
    from recognition.engine import _same_song
    short = make(10, 0, title="Because You Loved Me", artist="Céline Dion")
    long = make(10, 0,
                title='Because You Loved Me (Theme from "Up Close and Personal")',
                artist="Céline Dion")
    short.isrc = "CAC222400016"
    long.isrc = "CAC229600025"
    assert _same_song(short, long) is True
    assert _same_song(long, short) is True
    different = make(10, 0, title="My Heart Will Go On", artist="Céline Dion")
    assert _same_song(short, different) is False


def test_same_song_uses_isrc_when_present():
    """Matching ISRC is conclusive even if the artist/title strings drift."""
    from recognition.engine import _same_song
    a = make(10, 0, title="Whatever", artist="Some Artist")
    b = make(10, 0, title="Different Label", artist="Compilation Tag")
    a.isrc = "USRC12345678"
    b.isrc = "USRC12345678"
    assert _same_song(a, b) is True


def test_same_recording_ignores_artist_punctuation():
    """Stops ACR refinements being thrown away when Shazam says 'Rem' and
    ACRCloud says 'R.E.M.' for the same recording."""
    from recognition.engine import _same_recording
    a = make(10, 0, title="Near Wild Heaven", artist="Rem")
    b = make(10, 0, title="Near Wild Heaven", artist="R.E.M.")
    assert _same_recording(a, b) is True


def test_artist_mismatch_holds_held_track_when_title_matches():
    """The Michael Jackson / Haloca case: Shazam drops a garbage artist tag
    ('Lo Mejor Del Pop, Vol. 17', or — observed in the wild — 'Haloca' for one
    cycle of a Luther Vandross 'Dance With My Father' run) and the offset is
    from THAT match's reference, which doesn't line up with our locked
    baseline. Title alone is the strongest signal we have — once we hold a
    titled track, any same-title recognition stays on that track regardless
    of artist or anchor agreement. Artwork / metadata flap is unacceptable."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        await rec._handle_success(
            make(20, 1000.0, title="Man In the Mirror (2003 Edit)",
                 artist="Michael Jackson"))
        held = rec._current
        # Garbage artist, same title, anchor wildly off (Shazam matched a
        # different reference and reports its own offset).
        await rec._handle_success(
            make(120, 1000.0, title="Man In The Mirror",
                 artist="Lo Mejor Del Pop, Vol. 17 & Vol. 17"))
        assert rec._current.title == held.title
        assert rec._current.artist == held.artist

    asyncio.run(scenario())


def test_real_song_change_to_different_title_is_detected():
    """A real next-track by a different artist with a different title IS a
    song change — the title-match fallback only fires when titles agree.
    It takes two consecutive agreeing reads to commit (song-change debounce),
    matching a real track change persisting across recognition cycles."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        await rec._handle_success(
            make(20, 1000.0, title="Some Song", artist="Artist A"))
        # First read of the new track: held as a candidate, old track kept.
        await rec._handle_success(
            make(10, 1100.0, title="A Totally Different Song", artist="Artist B"))
        assert rec._current.artist == "Artist A"
        # Second agreeing read confirms the change.
        await rec._handle_success(
            make(14, 1104.0, title="A Totally Different Song", artist="Artist B"))
        assert rec._current.artist == "Artist B"
        assert rec._current.title == "A Totally Different Song"

    asyncio.run(scenario())


def test_single_bad_match_does_not_replace_held_track():
    """The Radio Ga Ga / 'Vj DjMarco' field case: a single Shazam fingerprint
    collision naming a different track must NOT replace the currently held
    (and correctly playing) track. Only a repeat of the SAME rogue candidate
    should ever displace it."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        await rec._handle_success(
            make(17, 1000.0, title="Radio Ga Ga", artist="Queen"))
        held = rec._current
        # One-off bad match (a remix/bootleg sampling the same recording).
        await rec._handle_success(
            make(194, 1004.0, title="Radio Ga Ga (feat. Freddie Mercury & Queen) [Special Long Remix]",
                 artist="Vj DjMarco"))
        assert rec._current is held
        # The real track is recognized again — the rogue candidate is dropped,
        # not confirmed by an unrelated later read.
        await rec._handle_success(
            make(120, 1004.0, title="Radio Ga Ga", artist="Queen"))
        assert rec._current.artist == "Queen"
        assert rec._pending_song is None

    asyncio.run(scenario())


def test_haloca_flap_holds_luther_held_track():
    """End-to-end regression: a Luther Vandross 'Dance With My Father' run
    is interrupted by a Shazam misidentification labelled 'Haloca - Dance
    With My Father' for one cycle, then 'Luther - Dance With My Father
    (5.1 Mix)' for another. Both must be merged into the held Luther track:
    no metadata flap, no album-art reload, no lyric refetch."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        # Lock the Luther track.
        await rec._handle_success(make(60, 1000.0,
                                       title="Dance With My Father",
                                       artist="Luther Vandross"))
        held_title = rec._current.title
        held_artist = rec._current.artist
        # Shazam flap to a different artist with the same title — must hold.
        await rec._handle_success(make(122, 1100.0,
                                       title="Dance With My Father",
                                       artist="Haloca"))
        assert rec._current.title == held_title
        assert rec._current.artist == held_artist
        # Shazam flap to a version-noise variant ('5.1 Mix') — also holds.
        await rec._handle_success(make(112, 1110.0,
                                       title="Dance With My Father (5.1 Mix)",
                                       artist="Luther Vandross"))
        assert rec._current.title == held_title
        assert rec._current.artist == held_artist

    asyncio.run(scenario())


def test_same_song_ignores_version_suffix_churn():
    """Shazam flip-flops the title for the same audio (Babylon / Babylon (UK Radio
    Mix), Angels / Angels (Remastered 2004), ...). Those must NOT count as a song
    change. Genuinely different songs still do."""
    from recognition.engine import _same_song
    dg = lambda t: make(10, 0, title=t, artist="David Gray")
    assert _same_song(dg("Babylon (UK Radio Mix)"), dg("Babylon")) is True
    assert _same_song(make(10, 0, title="Angels (Remastered 2004)", artist="Robbie Williams"),
                      make(10, 0, title="Angels", artist="Robbie Williams")) is True
    assert _same_song(dg("Babylon"), make(10, 0, title="Wonderwall", artist="Oasis")) is False
    assert _same_song(dg("Babylon"), None) is False


def test_title_variant_flip_is_not_a_new_song_and_spends_no_extra_acr():
    """The 'shudder': a version-suffix title flip must not reset the position or
    spend another ACR credit — ACR fires once for the real song only."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        acr = make(11, 1000.0, title="Babylon", artist="David Gray")
        acr.provider = "acrcloud"
        rec._acrcloud = _StubACR(acr)
        await rec._handle_success(
            make(10, 1000.0, title="Babylon", artist="David Gray"), audio=object())
        assert rec._acrcloud.calls == 1 and rec._acr_anchored is True
        frozen = rec._current
        # Shazam flips the title to a version variant of the SAME song.
        await rec._handle_success(
            make(40, 1030.0, title="Babylon (UK Radio Mix)", artist="David Gray"),
            audio=object())
        assert rec._acrcloud.calls == 1     # no extra ACR credit on the flip
        assert rec._current is frozen       # position not reset / no jump

    asyncio.run(scenario())


def test_variant_flip_keeps_first_title_but_refines_position():
    """The radio plays one version (UK Radio Mix) but Shazam flips the title; the
    served title/lyric variant must stay the FIRST-detected one (so the right
    lyric file is used) while the position still refines from later reads."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)   # ACR off -> pure lock behaviour
        await rec._handle_success(
            make(2, 1000.0, title="Babylon (UK Radio Mix)", artist="David Gray"))
        assert rec._current.title == "Babylon (UK Radio Mix)"
        # Same song, but Shazam flips the title to the album variant with a
        # refined (in-sync) position.
        await rec._handle_success(
            make(8, 1006.0, title="Babylon", artist="David Gray"))
        assert rec._current.title == "Babylon (UK Radio Mix)"  # variant preserved
        assert rec._current.offset == 8                        # position refined

    asyncio.run(scenario())


def test_retime_resets_lock_and_reacquires_position():
    """Long-press-the-dot retime: dropping the lock must make the very next
    recognition re-baseline the served position (status 'initial' again) while
    keeping the held track, so drifted lyrics can be rescued mid-song."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)   # ACR off -> pure lock behaviour
        # Lock a track on timeline anchor 10.
        for off, cs in [(10, 0), (15, 5), (20, 10), (25, 15)]:
            await rec._handle_success(make(off, cs, title="Drifting",
                                           artist="A"))
        assert rec._lock.locked is True
        assert rec.retiming is False
        held = rec._current

        rec.retime()
        assert rec.retiming is True
        assert rec._lock.locked is False        # lock dropped
        assert rec._acr_anchored is False

        # A fresh read on a NEW timeline (anchor 90) is now accepted immediately
        # instead of being ignored as an outlier — the position re-anchors.
        await rec._handle_success(make(120, 30, title="Drifting", artist="A"))
        assert rec.retiming is False            # retime complete
        assert rec._current.title == held.title  # same track kept
        assert rec._current.offset == 120        # position re-acquired

    asyncio.run(scenario())


def test_retiming_flag_self_heals_after_timeout():
    """If the stream goes silent right after a retime (no match to re-anchor
    on), the flag must clear itself so a polling display isn't stuck blue."""
    import recognition.engine as eng
    from recognition.engine import PlayerRecognizer

    rec = PlayerRecognizer("z", capture=None)
    rec.retime()
    assert rec.retiming is True
    rec._retime_at -= (eng.RETIME_TIMEOUT + 1.0)   # pretend the cap has elapsed
    assert rec.retiming is False


def test_retiming_flag_cleared_when_track_blanks():
    """Enough consecutive no-matches to blank the held track must also end any
    in-progress retime — there's nothing left to re-time."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        rec._blank_after = 1
        await rec._handle_success(make(10, 0, title="T", artist="A"))
        rec.retime()
        assert rec.retiming is True
        await rec._handle_failure()   # one no-match -> blanks (blank_after=1)
        assert rec._current is None
        assert rec.retiming is False

    asyncio.run(scenario())


def test_acrcloud_quota_resets_on_utc_day():
    """The daily counter must roll over on the UTC calendar day (ACRCloud's
    reset), not the server's local day."""
    from datetime import datetime, timezone
    from recognition.acrcloud import ACRCloudRecognizer
    r = ACRCloudRecognizer()
    r._requests_today = 50
    r._counter_date = "2000-01-01"          # stale
    r._reset_if_new_day()
    assert r._requests_today == 0
    assert r._counter_date == datetime.now(timezone.utc).date().isoformat()


def test_acr_priority_carries_spotify_id_when_adopted():
    """When ACR is adopted, its Spotify track id rides along for lyrics lookup."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        shazam = make(10, 1000.0)
        acr = make(12, 1000.0); acr.provider = "acrcloud"; acr.spotify_id = "sp123"
        rec._acrcloud = _StubACR(acr)
        await rec._handle_success(shazam, audio=object())
        assert rec._current.spotify_id == "sp123"

    asyncio.run(scenario())


def test_acr_priority_carries_spotify_id_even_when_position_rejected():
    """Same recording but the position disagrees beyond tolerance: keep Shazam's
    position, but still attach ACR's Spotify id so lyrics target the right
    variant."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        shazam = make(10, 1000.0)
        acr = make(40, 1000.0); acr.provider = "acrcloud"; acr.spotify_id = "sp999"
        rec._acrcloud = _StubACR(acr)
        await rec._handle_success(shazam, audio=object())
        assert rec._acr_anchored is False          # position NOT adopted
        assert rec._current is shazam              # Shazam position kept
        assert rec._current.spotify_id == "sp999"  # but Spotify id carried over

    asyncio.run(scenario())


def test_acr_priority_adopts_and_freezes_position():
    """ACR priority ON: a new Shazam track triggers exactly one ACRCloud lookup;
    an agreeing ACR position is adopted and frozen so later Shazam reads (even
    wildly different ones) never move the clock."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        shazam = make(10, 1000.0)                  # same capture window as ACR
        acr = make(13, 1000.0); acr.provider = "acrcloud"  # +3s, within tolerance
        rec._acrcloud = _StubACR(acr)

        await rec._handle_success(shazam, audio=object())
        assert rec._acrcloud.calls == 1            # exactly one ACR lookup
        assert rec._acr_anchored is True
        assert rec._current is acr                 # ACR position adopted

        # A later, very different Shazam read for the same song must be ignored.
        await rec._handle_success(make(90, 1000.0), audio=object())
        assert rec._acrcloud.calls == 1            # no second ACR spend
        assert rec._current is acr                 # position still frozen on ACR

    asyncio.run(scenario())


def test_acr_priority_rejects_disagreeing_position():
    """ACR priority ON but the ACR position disagrees beyond tolerance: keep the
    Shazam position and do NOT freeze (normal lock behaviour resumes)."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        shazam = make(10, 1000.0)
        acr = make(40, 1000.0); acr.provider = "acrcloud"  # +30s, beyond tolerance
        rec._acrcloud = _StubACR(acr)

        await rec._handle_success(shazam, audio=object())
        assert rec._acrcloud.calls == 1
        assert rec._acr_anchored is False
        assert rec._current is shazam              # kept Shazam, not ACR

    asyncio.run(scenario())


def test_acr_priority_off_never_calls_acrcloud_on_song_change():
    """ACR priority OFF (default): a new track does NOT spend an ACR lookup."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(priority=False)
        rec._acrcloud = _StubACR(make(13, 1000.0))
        await rec._handle_success(make(10, 1000.0), audio=object())
        assert rec._acrcloud.calls == 0
        assert rec._acr_anchored is False

    asyncio.run(scenario())


def test_acr_priority_unavailable_keeps_shazam():
    """ACR priority ON but ACRCloud is out of quota / cooling down: keep Shazam."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer()
        rec._acrcloud = _StubACR(make(13, 1000.0), available=False)
        shazam = make(10, 1000.0)
        await rec._handle_success(shazam, audio=object())
        assert rec._acrcloud.calls == 0            # not spent when unavailable
        assert rec._acr_anchored is False
        assert rec._current is shazam

    asyncio.run(scenario())


def test_acrcloud_never_used_as_shazam_fallback():
    """Routine recognition is Shazam only. A Shazam no-match must NEVER fall back
    to ACRCloud — regardless of the ACR-priority switch — so adverts / DJ talk /
    silence can never burn an ACRCloud credit. ACR is reserved exclusively for the
    single-shot refinement."""
    import asyncio

    async def scenario():
        for priority in (True, False):
            rec = _acr_recognizer(priority=priority)
            rec._shazam = _StubACR(None)           # Shazam never matches
            rec._acrcloud = _StubACR(make(5, 0))   # would match if (wrongly) asked
            result = await rec._recognize_once(object())
            assert result is None                  # no fallback to ACR
            assert rec._acrcloud.calls == 0        # ACR never spent on a no-match

    asyncio.run(scenario())


def test_acr_priority_accepts_title_variant():
    """ACR priority adopts the position even when ACRCloud labels the same
    recording with a cosmetic suffix (the Roxette '(From the Film ...)' case)."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer(tolerance=5.0)
        shazam = make(10, 1000.0, title="It Must Have Been Love", artist="Roxette")
        acr = make(11, 1000.0,
                   title="It Must Have Been Love (From the Film \"Pretty Woman\")",
                   artist="Roxette")
        acr.provider = "acrcloud"
        rec._acrcloud = _StubACR(acr)
        await rec._handle_success(shazam, audio=object())
        assert rec._acr_anchored is True
        assert rec._current is acr

    asyncio.run(scenario())


def test_acr_priority_rejects_genuinely_different_track():
    """A truly different ACR match (different artist) is still rejected."""
    import asyncio

    async def scenario():
        rec = _acr_recognizer()
        shazam = make(10, 1000.0, title="Grease", artist="Frankie Valli")
        acr = make(10, 1000.0, title="Greased Lightnin'", artist="John Travolta")
        acr.provider = "acrcloud"
        rec._acrcloud = _StubACR(acr)
        await rec._handle_success(shazam, audio=object())
        assert rec._acr_anchored is False
        assert rec._current is shazam

    asyncio.run(scenario())


def test_loop_does_not_starve_event_loop_when_no_stream():
    """Regression: a recognizer whose stream key doesn't exist must still yield
    each cycle, or it busy-loops and freezes the whole asyncio app (and the web
    server with it)."""
    import asyncio

    from recognition.engine import PlayerRecognizer
    from recognition.udp_capture import UdpAudioCapture

    async def scenario():
        capture = UdpAudioCapture()  # no socket started, no streams
        rec = PlayerRecognizer("does-not-exist", capture)
        rec._interval = 0.01
        rec.start()

        ticks = []

        async def other():
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks.append(1)

        # If the recognition loop busy-spun, this concurrent coroutine would
        # never get scheduled and wait_for would time out.
        await asyncio.wait_for(other(), timeout=2.0)
        await rec.stop()
        return ticks

    ticks = asyncio.run(scenario())
    assert len(ticks) == 5
