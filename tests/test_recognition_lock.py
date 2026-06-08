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


def test_outlier_resets_streak():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))                    # initial, anchor 10
    assert lt.offer(make(15, 5)) == "locking"   # anchor 10 -> confirmation 1
    assert lt.offer(make(50, 5)) == "relock"    # anchor 45 -> outlier, reset
    assert lt.locked is False
    assert lt.confirmations == 0


def test_ignores_after_locked():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))
    lt.offer(make(15, 5))
    lt.offer(make(20, 10))
    lt.offer(make(25, 15))
    assert lt.locked
    # A wildly different anchor after lock is ignored, not applied.
    assert lt.offer(make(100, 0)) == "ignored"
    assert lt.locked


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
