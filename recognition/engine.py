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


class LockTracker:
    """Converges on a stable song position over several recognitions.

    A recognition's "sync anchor" is ``offset - capture_start_time`` — invariant
    across captures of the same song on the same timeline. Two consecutive
    anchors within ``tolerance`` advance the streak; an outlier restarts it.
    Once the streak reaches ``lock_after``, position is considered locked and
    later anchors are ignored (avoids chorus-confusion jumps).
    """

    def __init__(self, lock_after: int = 3, tolerance: float = 3.0, enabled: bool = True):
        self.lock_after = lock_after
        self.tolerance = tolerance
        self.enabled = enabled
        self.reset()

    def reset(self):
        self._anchors = []
        self.consecutive_good = 0
        self.result: Optional[RecognitionResult] = None

    @property
    def locked(self) -> bool:
        return self.enabled and self.consecutive_good >= self.lock_after

    def offer(self, result: RecognitionResult) -> str:
        """Feed a same-song recognition. Returns one of:
        'locked' (now/already locked, position frozen),
        'locking' (accepted, still converging),
        'ignored' (already locked, position untouched)."""
        if self.locked:
            return "ignored"

        anchor = result.offset - result.capture_start_time
        if self._anchors:
            if abs(anchor - self._anchors[-1]) <= self.tolerance:
                self.consecutive_good += 1
            else:
                self.consecutive_good = 1  # outlier -> restart streak
                self._anchors = []
        else:
            self.consecutive_good = 1
        self._anchors.append(anchor)

        self.result = result
        return "locked" if self.locked else "locking"


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
        )
        self._interval = AUDIO_RECOGNITION["recognition_interval"]
        self._capture_duration = AUDIO_RECOGNITION["capture_duration"]
        self._latency_offset = AUDIO_RECOGNITION["latency_offset"]
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._failures = 0
        self._paused = False
        self._current: Optional[RecognitionResult] = None

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

    async def _loop(self):
        logger.info("Recognition loop started for player %s", self.key)
        while self._running:
            try:
                audio = await self._capture.get_audio(
                    self._capture_duration, self.key, lambda: self._running)
                if audio is None:
                    await self._handle_failure()
                else:
                    result = await self._recognize_once(audio)
                    if result is None:
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
            logger.info("New song on %s: %s", self.key, result)
            self._lock.reset()
            self._lock.offer(result)
            self._current = result
        else:
            outcome = self._lock.offer(result)
            if outcome in ("locking",):
                self._current = result  # refine position while converging
            # 'ignored'/'locked' -> keep the locked position
        if self._on_update:
            self._on_update(self)

    async def _handle_failure(self):
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES and not self._paused:
            self._paused = True
            logger.info("Player %s paused (no recognition)", self.key)
            if self._on_update:
                self._on_update(self)
