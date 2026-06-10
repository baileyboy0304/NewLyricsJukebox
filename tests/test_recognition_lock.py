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
