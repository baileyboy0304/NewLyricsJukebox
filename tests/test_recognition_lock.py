"""Recognition lock-cycle seam: the '3 attempts before locked' consensus."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recognition.engine import LockTracker  # noqa: E402
from recognition.result import RecognitionResult  # noqa: E402


def make(offset, capture_start, title="Song", artist="Artist"):
    return RecognitionResult(title=title, artist=artist, offset=offset,
                             capture_start_time=capture_start)


def test_locks_after_three_consistent():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    # Same timeline: anchor = offset - capture_start = constant 10.0
    assert lt.offer(make(10, 0)) == "locking"
    assert lt.offer(make(15, 5)) == "locking"   # anchor 10.0
    assert lt.offer(make(20, 10)) == "locked"   # anchor 10.0, 3rd good
    assert lt.locked is True


def test_outlier_resets_streak():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))           # anchor 10
    lt.offer(make(15, 5))           # anchor 10 -> good (2)
    lt.offer(make(50, 5))           # anchor 45 -> outlier, reset to 1
    assert lt.locked is False
    assert lt.consecutive_good == 1


def test_ignores_after_locked():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))
    lt.offer(make(15, 5))
    lt.offer(make(20, 10))
    assert lt.locked
    # A wildly different anchor after lock is ignored, not applied.
    assert lt.offer(make(100, 0)) == "ignored"
    assert lt.locked


def test_reset_clears_state():
    lt = LockTracker(lock_after=3, tolerance=3.0)
    lt.offer(make(10, 0))
    lt.reset()
    assert lt.consecutive_good == 0
    assert lt.result is None
    assert lt.locked is False


def test_position_interpolates_from_offset():
    import time
    r = make(30.0, time.time() - 2.0)  # captured 2s ago at 30s in
    pos = r.get_current_position()
    assert 31.5 < pos < 32.5
