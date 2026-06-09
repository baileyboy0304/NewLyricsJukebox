"""Per-player recognition engine: Shazam -> ACRCloud chain + lock cycle.

Each selected player gets its own ``PlayerRecognizer`` (proper per-player state,
not swapped module globals — AUDIT.md cluster D). The "3 attempts before locked"
position consensus lives in ``LockTracker`` so it can be unit-tested in isolation.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from config import AUDIO_RECOGNITION, UDP_AUDIO
from recognition.acrcloud import ACRCloudRecognizer
from recognition.result import RecognitionResult
from recognition.shazam import ShazamRecognizer

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5
# Display names for the recognition log line (result.provider is lowercase).
_PROVIDER_LABELS = {"shazam": "Shazam", "acrcloud": "ACRCloud"}
# Safety net so a hung Shazam/ACRCloud network call can never stall a recognition
# cycle. Shazam is normally <3s and the whole chain well under this.
RECOGNIZE_TIMEOUT = 10.0


class LockTracker:
    """Converges on a stable song position over several recognitions — the
    "3 attempts before locked" method, with the same state progression the
    original code logged (POSITION LOCKED -> LOCKING (n of N) -> LOCKED ->
    IGNORED).

    A recognition's "sync anchor" is ``offset - capture_start_time`` — invariant
    across captures of the same song on the same timeline. The first read of a
    new song is *accepted* immediately (status ``initial``) and becomes the
    baseline. Each later read within ``tolerance`` of the baseline counts as a
    confirmation and refines the baseline. After ``lock_after`` confirmations the
    position is *locked* and in-sync anchors are ignored.

    A single off-baseline read — whether converging or locked — is treated as a
    rogue (status ``outlier``): it does NOT move the served position or disturb
    the lock streak. Only ``relock_after`` reads that agree with EACH OTHER on a
    NEW timeline are a real shift (e.g. a radio skip/rebuffer); they then re-base —
    restarting the lock streak while converging (status ``relock``) or breaking a
    lock to re-acquire (status ``reacquire``). ``relock_after`` is separate from
    ``lock_after`` so re-acquisition can be quicker than the initial lock.

    ``offer`` returns a status string consumed by the engine for logging:
    ``initial`` | ``locking`` | ``locked`` | ``ignored`` | ``outlier`` |
    ``relock`` | ``reacquire`` | ``tracking`` (the last when locking is disabled).
    """

    def __init__(self, lock_after: int = 3, tolerance: float = 3.0,
                 enabled: bool = True, relock_after: int = 2):
        self.lock_after = lock_after
        self.relock_after = relock_after
        self.tolerance = tolerance
        self.enabled = enabled
        self.reset()

    def reset(self):
        self._baseline: Optional[float] = None  # anchor of the accepted position
        self.confirmations = 0                  # consistent reads after the initial
        self.initialized = False
        self.result: Optional[RecognitionResult] = None
        self._drift_anchor: Optional[float] = None  # candidate new timeline while locked
        self._drift_count = 0

    @property
    def locked(self) -> bool:
        return self.enabled and self.confirmations >= self.lock_after

    def offer(self, result: RecognitionResult) -> str:
        """Feed a same-song recognition; returns its lock status (see class doc)."""
        anchor = result.offset - result.capture_start_time

        # First read of a new song: accept and display immediately.
        if not self.initialized:
            self._baseline = anchor
            self.confirmations = 0
            self.initialized = True
            self.result = result
            return "initial"

        # Locking disabled: always follow the latest recognition.
        if not self.enabled:
            self._baseline = anchor
            self.result = result
            return "tracking"

        # In sync with the established baseline. This is the only path that
        # moves the served position (refine while converging; frozen once locked).
        if abs(anchor - self._baseline) <= self.tolerance:
            self._drift_anchor = None   # in sync -> drop any pending drift streak
            self._drift_count = 0
            if self.locked:
                return "ignored"
            self.confirmations += 1
            self._baseline = anchor
            self.result = result
            return "locked" if self.locked else "locking"

        # Off baseline. A SINGLE rogue read (chorus confusion, bad match) must not
        # move the position or disturb the lock streak — hold it and wait. Only
        # ``relock_after`` reads that agree with EACH OTHER on a new timeline are a
        # real shift (radio skip), at which point we re-base on it.
        if self._drift_anchor is not None and abs(anchor - self._drift_anchor) <= self.tolerance:
            self._drift_count += 1
        else:
            self._drift_count = 1
        self._drift_anchor = anchor
        if self._drift_count >= self.relock_after:
            self._baseline = anchor          # confirmed new timeline -> re-base
            self._drift_anchor = None
            self._drift_count = 0
            self.result = result
            if self.locked:
                return "reacquire"
            self.confirmations = 1           # restart the lock streak on it
            return "relock"
        return "outlier"                     # held, position unchanged


class PlayerRecognizer:
    """Owns the recognition loop and current position for one player."""

    def __init__(self, key: str, capture, on_update: Optional[Callable] = None):
        self.key = key
        self._capture = capture
        self._on_update = on_update
        self._shazam = ShazamRecognizer()
        self._acrcloud = ACRCloudRecognizer()
        self._lock = LockTracker(
            lock_after=UDP_AUDIO["lock_position_after"],
            tolerance=UDP_AUDIO["lock_consensus_tolerance"],
            enabled=UDP_AUDIO["lock_position"],
            relock_after=UDP_AUDIO["relock_position_after"],
        )
        self._interval = AUDIO_RECOGNITION["recognition_interval"]
        self._capture_duration = AUDIO_RECOGNITION["capture_duration"]
        self._latency_offset = AUDIO_RECOGNITION["latency_offset"]
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._failures = 0
        self._paused = False
        self._current: Optional[RecognitionResult] = None
        self._last_outcome = ""
        self._last_outcome_at = 0.0

    # -- lifecycle --------------------------------------------------------- #

    def start(self):
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -- public state ------------------------------------------------------ #

    @property
    def current(self) -> Optional[RecognitionResult]:
        return self._current

    @property
    def is_playing(self) -> bool:
        return not self._paused and self._current is not None

    def get_position(self) -> Optional[float]:
        if self._current is None:
            return None
        return self._current.get_current_position() + self._latency_offset

    # -- recognition chain ------------------------------------------------- #

    async def _recognize_once(self, audio) -> Optional[RecognitionResult]:
        result = await self._shazam.recognize(audio)
        if result is None and self._acrcloud.is_available():
            result = await self._acrcloud.recognize(audio)
        return result

    def _log_outcome(self, msg, *args):
        """INFO-log a recognition outcome, throttled so a long run of the same
        outcome (e.g. silence / no match) doesn't spam the log."""
        import time as _t
        rendered = msg % args if args else msg
        now = _t.time()
        if rendered != self._last_outcome or (now - self._last_outcome_at) > 30:
            logger.info("Recognize %s: %s", self.key, rendered)
            self._last_outcome = rendered
            self._last_outcome_at = now

    async def _loop(self):
        logger.info("Recognition loop started for player %s", self.key)
        while self._running:
            try:
                audio = await self._capture.get_audio(
                    self._capture_duration, self.key, lambda: self._running)
                if audio is None:
                    self._log_outcome("no audio (stream idle/too short)")
                    await self._handle_failure()
                else:
                    # Purely informational — recognition logic is unchanged.
                    level = audio.get_max_amplitude()
                    try:
                        result = await asyncio.wait_for(
                            self._recognize_once(audio), timeout=RECOGNIZE_TIMEOUT)
                    except asyncio.TimeoutError:
                        self._log_outcome("timed out after %.0fs", RECOGNIZE_TIMEOUT)
                        result = None
                    if result is None:
                        hint = " (likely silence)" if level < 100 else ""
                        self._log_outcome("no match (audio level=%d)%s", level, hint)
                        await self._handle_failure()
                    else:
                        self._handle_success(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Recognition loop error: %s", exc)
            # ALWAYS yield/sleep each cycle — never busy-loop, even when there is
            # no stream/audio (otherwise the event loop starves and the web
            # server stops responding).
            await asyncio.sleep(self._interval)

    def _handle_success(self, result: RecognitionResult):
        self._failures = 0
        self._paused = False
        if not result.is_same_song(self._current):
            self._lock.reset()
            status = self._lock.offer(result)  # "initial"
            self._current = result
            logger.info("Song changed to: %s - %s @ %.1fs",
                        result.artist, result.title, result.get_current_position())
        else:
            status = self._lock.offer(result)
            if status not in ("ignored", "outlier"):
                # refine while converging, or jump to a re-acquired timeline.
                # 'ignored'/'outlier' -> hold the current position unchanged.
                self._current = result
        self._log_recognition(result, status)
        if self._on_update:
            self._on_update(self)

    def _log_recognition(self, result: RecognitionResult, status: str):
        """Per-recognition position/lock log line (matches the original format,
        so the lock cycle is visible: LOCKED -> LOCKING (n of N) -> LOCKED ->
        IGNORED)."""
        n = self._lock.lock_after
        if status == "initial":
            pos = "LOCKED"
        elif status == "locking":
            pos = "LOCKING (%d of %d)" % (self._lock.confirmations, n)
        elif status == "locked":
            pos = "LOCKING (%d of %d) - LOCKED" % (n, n)
        elif status == "outlier":
            pos = "OUTLIER (held, awaiting confirmation)"
        elif status == "relock":
            pos = "RE-LOCKING (new timeline confirmed, streak restarted)"
        elif status == "reacquire":
            pos = "RE-ACQUIRED (sustained shift, position re-locked)"
        elif status == "tracking":
            pos = "TRACKING (lock disabled)"
        else:  # ignored
            pos = "IGNORED"
        logger.info(
            "%s Recognized: %s - %s | Offset: %.1fs | Latency: %.1fs | "
            "Current: %.1fs | Skew: t=%.6f, f=%.4f | POSITION %s",
            _PROVIDER_LABELS.get(result.provider, result.provider.title()),
            result.artist, result.title, result.offset, result.get_latency(),
            result.get_current_position(), result.time_skew,
            result.frequency_skew, pos)

    async def _handle_failure(self):
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES and not self._paused:
            self._paused = True
            # The song has ended (advert / DJ talk / silence between tracks). Drop
            # the held result so current-track reports no track and the UI can fade
            # the now-playing metadata + lyrics away until the next recognition.
            self._current = None
            self._lock.reset()
            logger.info("Player %s paused (no recognition)", self.key)
            if self._on_update:
                self._on_update(self)
