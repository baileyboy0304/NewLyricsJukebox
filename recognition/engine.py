"""Per-player recognition engine: Shazam recognition + lock cycle.

Each selected player gets its own ``PlayerRecognizer`` (proper per-player state,
not swapped module globals — AUDIT.md cluster D). The "3 attempts before locked"
position consensus lives in ``LockTracker`` so it can be unit-tested in isolation.

Routine recognition is **Shazam only**. ACRCloud is used solely for the optional
single-shot "ACR priority" refinement: when enabled, each newly-detected track
gets exactly one ACRCloud lookup to refine the Shazam position, after which the
position is frozen for the rest of the track. ACRCloud is never used as a Shazam
fallback, so a no-match (advert / DJ talk / silence) never spends a credit.
"""

import asyncio
import logging
import re
import time
from dataclasses import replace
from typing import Callable, Optional

from config import AUDIO_RECOGNITION, UDP_AUDIO
from recognition.acrcloud import ACRCloudRecognizer
from recognition.result import RecognitionResult
from recognition.shazam import ShazamRecognizer
from text_clean import strip_version_noise

logger = logging.getLogger(__name__)

# Display names for the recognition log line (result.provider is lowercase).
_PROVIDER_LABELS = {"shazam": "Shazam", "acrcloud": "ACRCloud"}
# Safety net so a hung Shazam/ACRCloud network call can never stall a recognition
# cycle. Shazam is normally <3s and the whole chain well under this.
RECOGNIZE_TIMEOUT = 10.0

# Anchor-continuity window (seconds) for the title-only fallback in _same_song:
# a same-titled read whose sync anchor sits this close to the locked baseline is
# treated as the same song even when the artist string differs wildly (compilation
# metadata, etc.). Different songs effectively never share a continuous anchor.
SAME_SONG_ANCHOR_TOLERANCE = 10.0

_NON_ALNUM_RE = re.compile(r"[^\w]+", re.UNICODE)


def _norm_artist(s: Optional[str]) -> str:
    """Punctuation-insensitive artist key: "R.E.M.", "R E M" and "Rem" all collapse
    to "rem", so a Shazam flip between those labels is no longer a song change."""
    return _NON_ALNUM_RE.sub("", (s or "").strip().lower())


def _norm_title(s: Optional[str]) -> str:
    """Version-noise + punctuation stripped title key."""
    return _NON_ALNUM_RE.sub("", strip_version_noise((s or "").strip().lower()))


def _same_recording(a: RecognitionResult, b: RecognitionResult) -> bool:
    """Looser track equality used only by the ACR refinement. Shazam and ACRCloud
    routinely label the same recording differently — e.g. "It Must Have Been Love"
    vs "... (From the Film 'Pretty Woman')", or "Lady Love Me (One More Time)" vs
    "Lady Love Me (2003 Remaster)". A matching ISRC is conclusive; otherwise
    artists are compared punctuation-insensitively (so "Rem" / "R.E.M." agree) and
    the cleaned titles must be equal or a prefix of one another — so a valid
    position + Spotify id isn't thrown away over a variant label."""
    if a.isrc and b.isrc and a.isrc == b.isrc:
        return True
    na, nb = _norm_artist(a.artist), _norm_artist(b.artist)
    if not na or na != nb:
        return False
    ta, tb = _norm_title(a.title), _norm_title(b.title)
    return bool(ta and tb) and (ta == tb or ta.startswith(tb) or tb.startswith(ta))


def _same_song(a: RecognitionResult, b: Optional[RecognitionResult]) -> bool:
    """Song-change detection that ignores Shazam's metadata churn for the same
    audio. Three classes of flap merged here:
      * Punctuation: "Rem" vs "R.E.M." (collapsed by _norm_artist).
      * Strippable version suffix: "Babylon" vs "Babylon (UK Radio Mix)"
        (handled by strip_version_noise inside _norm_title).
      * Title-only expansion / subtitle that *isn't* in the version-noise list,
        e.g. "Because You Loved Me" vs "Because You Loved Me (Theme from "Up
        Close and Personal")" — both Shazam labels for the same recording.
        A different ISRC is sometimes assigned to each label (CAC222400016 vs
        CAC229600025), so the ISRC short-circuit doesn't save us; instead we
        accept the case where one normalised title is a prefix of the other.

    The artist-mismatch case — e.g. a compilation tag like "Lo Mejor Del Pop,
    Vol. 17" appearing for one cycle of Michael Jackson — still needs the
    position-continuity guard in _handle_success."""
    if b is None:
        return False
    if a.isrc and b.isrc and a.isrc == b.isrc:
        return True
    na, nb = _norm_artist(a.artist), _norm_artist(b.artist)
    if not na or na != nb:
        return False
    ta, tb = _norm_title(a.title), _norm_title(b.title)
    if not ta or not tb:
        return False
    return ta == tb or ta.startswith(tb) or tb.startswith(ta)


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

    @property
    def baseline(self) -> Optional[float]:
        """Current accepted sync anchor (offset - capture_start_time), or None
        before the first read."""
        return self._baseline

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
        # TEST FEATURE — ACR priority: one ACRCloud refinement per track, then the
        # position is frozen and Shazam only confirms the track (see config.py).
        self._acr_priority = AUDIO_RECOGNITION["acr_priority"]
        self._acr_tolerance = AUDIO_RECOGNITION["acr_priority_tolerance"]
        self._acr_anchored = False   # is the current track's position ACR-locked?
        # Clear the held track after this many consecutive no-matches so the UI
        # can fade the metadata away between songs. Context-aware: fewer failures
        # needed near a song's end (it probably really ended), more mid-song (a
        # single miss there is usually a transient quiet passage / mic gap).
        self._blank_after = max(1, AUDIO_RECOGNITION.get("blank_after_failures", 3))
        self._blank_after_near_end = max(
            1, AUDIO_RECOGNITION.get("blank_after_failures_near_end", 1))
        self._song_end_window = max(
            0.0, AUDIO_RECOGNITION.get("song_end_window", 30.0))
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

    @property
    def failures(self) -> int:
        """Consecutive no-match recognitions on this stream. Reset to 0
        on every successful match. Exposed so the lyrics layer can blank
        the served line earlier than the metadata-blank threshold —
        silence on the mic shouldn't keep yesterday's lyric on screen."""
        return self._failures

    def get_position(self) -> Optional[float]:
        if self._current is None:
            return None
        return self._current.get_current_position() + self._latency_offset

    # -- recognition chain ------------------------------------------------- #

    async def _recognize_once(self, audio) -> Optional[RecognitionResult]:
        # Routine recognition is Shazam ONLY. ACRCloud is never used as a fallback
        # here — it is reserved exclusively for the single-shot per-track position
        # refinement (see ``_apply_acr_priority``), so a Shazam no-match on an
        # advert / DJ talk / silence never burns an ACRCloud credit.
        return await self._shazam.recognize(audio)

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
                        # A hung Shazam call is a recognition hiccup, NOT evidence
                        # the song ended. Treating it as a no-match cleared the held
                        # track (with blank_after_failures=1) and faded the lyrics
                        # out for a few seconds — the "blip". Hold the current track
                        # and just try again next cycle.
                        self._log_outcome("timed out after %.0fs (held)", RECOGNIZE_TIMEOUT)
                        result = None
                        timed_out = True
                    else:
                        timed_out = False
                    if result is not None:
                        await self._handle_success(result, audio)
                    elif not timed_out:
                        hint = " (likely silence)" if level < 100 else ""
                        self._log_outcome("no match (audio level=%d)%s", level, hint)
                        await self._handle_failure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Recognition loop error: %s", exc)
            # ALWAYS yield/sleep each cycle — never busy-loop, even when there is
            # no stream/audio (otherwise the event loop starves and the web
            # server stops responding).
            await asyncio.sleep(self._interval)

    async def _handle_success(self, result: RecognitionResult, audio=None):
        self._failures = 0
        self._paused = False
        new_song = not _same_song(result, self._current)
        if new_song and self._current is not None and self._lock.baseline is not None:
            # Artist string differs (Shazam sometimes returns a compilation tag like
            # "Lo Mejor Del Pop, Vol. 17" for one cycle in the middle of a Michael
            # Jackson run). Fall back to title + position continuity: if the cleaned
            # titles agree AND the new read's sync anchor sits right on the locked
            # baseline, it's the same song with bad metadata, not a track change.
            if (_norm_title(result.title) == _norm_title(self._current.title)
                    and abs((result.offset - result.capture_start_time)
                            - self._lock.baseline) <= SAME_SONG_ANCHOR_TOLERANCE):
                logger.info(
                    "Treating %s - %s as same song (title + position match; "
                    "artist '%s' differs from held '%s')",
                    result.artist, result.title, result.artist, self._current.artist)
                new_song = False
        if new_song:
            self._lock.reset()
            self._acr_anchored = False
            status = self._lock.offer(result)  # "initial"
            self._current = result
            logger.info("Song changed to: %s - %s @ %.1fs",
                        result.artist, result.title, result.get_current_position())
        elif self._acr_anchored:
            # ACR priority: this track's position is frozen to the ACRCloud anchor;
            # Shazam reads only re-confirm the track and never move the clock. The
            # served position keeps advancing on its own (offset + wall-clock).
            logger.debug("ACR priority: Shazam re-confirmed %s - %s; position held at %.1fs",
                         result.artist, result.title, self._current.get_current_position())
            return
        else:
            status = self._lock.offer(result)
            if status not in ("ignored", "outlier"):
                # Refine the position from this read, but KEEP the song's
                # first-detected title and lyric variant. Shazam flips e.g.
                # "Babylon" <-> "Babylon (UK Radio Mix)" for the same audio, and
                # those have different lyric files/timings — letting a later flip
                # overwrite the title swaps in the wrong (desynced) lyrics for the
                # rest of the song. Only the position fields move.
                self._current = replace(
                    result,
                    title=self._current.title,
                    artist=self._current.artist,
                    album=self._current.album,
                    album_art_url=self._current.album_art_url or result.album_art_url,
                    spotify_id=self._current.spotify_id or result.spotify_id,
                    isrc=self._current.isrc or result.isrc,
                    duration=self._current.duration or result.duration,
                )
        self._log_recognition(result, status)
        if self._on_update:
            self._on_update(self)
        # ACR priority: on a song change, spend ONE ACRCloud lookup to refine the
        # Shazam position. Done after the Shazam line is logged/served so the UI
        # shows lyrics immediately and the refinement (if any) follows.
        if new_song and self._acr_priority and audio is not None:
            await self._apply_acr_priority(audio)

    async def _apply_acr_priority(self, audio):
        """One ACRCloud lookup for the freshly-detected track. If ACR agrees with
        Shazam (same track, position within tolerance) adopt ACR's position and
        freeze it for the rest of the track. Otherwise keep Shazam and behave
        normally. Exactly one ACR request is spent per track — never wasted."""
        shazam = self._current
        if shazam is None:
            return
        reason = self._acrcloud.unavailable_reason()
        if reason:
            logger.info("ACR priority: ACRCloud unavailable (%s) — "
                        "keeping Shazam position for this track", reason)
            return
        try:
            acr = await asyncio.wait_for(
                self._acrcloud.recognize(audio), timeout=RECOGNIZE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.info("ACR priority: ACRCloud timed out — keeping Shazam position")
            return
        # A late song change mid-lookup means this ACR result is for the old audio.
        if shazam is not self._current:
            return
        if acr is None:
            logger.info("ACR priority: no ACRCloud match — keeping Shazam position")
            return
        if not _same_recording(acr, shazam):
            logger.info("ACR priority: ACRCloud matched a different track "
                        "(%s - %s) — keeping Shazam position", acr.artist, acr.title)
            return
        # Same recording: carry ACRCloud's Spotify track id onto the served result
        # so lyrics lookup can pin the exact variant — independent of whether the
        # position is adopted below.
        if acr.spotify_id and not shazam.spotify_id:
            shazam.spotify_id = acr.spotify_id
            logger.info("ACR priority: Spotify track id %s from ACRCloud "
                        "(lyrics will target the exact recording)", acr.spotify_id)
        delta = ((acr.offset - acr.capture_start_time)
                 - (shazam.offset - shazam.capture_start_time))
        if abs(delta) > self._acr_tolerance:
            logger.info("ACR priority: ACRCloud position differs by %+.1fs "
                        "(> %.1fs tolerance) — keeping Shazam position",
                        delta, self._acr_tolerance)
            return
        # Adopt ACR's position; keep Shazam's richer artwork if ACR lacks it.
        if not acr.album_art_url and shazam.album_art_url:
            acr.album_art_url = shazam.album_art_url
        self._current = acr
        self._acr_anchored = True
        logger.info("ACR priority: refined position by %+.1fs via ACRCloud "
                    "(now %.1fs) — locked for this track",
                    delta, acr.get_current_position())
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

    def _blank_threshold(self) -> int:
        """How many consecutive no-matches to tolerate before blanking. Near the
        end of a known-duration song a no-match likely means it really ended (blank
        fast); mid-song it's more likely a transient miss (tolerate more)."""
        result = self._current
        duration = getattr(result, "duration", None) if result is not None else None
        if duration:
            remaining = duration - result.get_current_position()
            if remaining <= self._song_end_window:
                return self._blank_after_near_end
        return self._blank_after

    async def _handle_failure(self):
        self._failures += 1
        # A no-match means the song may have ended (advert / DJ talk / silence) — or
        # it's just a transient miss mid-song. Once enough consecutive no-matches
        # accumulate (threshold depends on how close we are to the song's end), drop
        # the held result so current-track reports no track and the UI fades away.
        if self._failures >= self._blank_threshold() and self._current is not None:
            self._current = None
            self._lock.reset()
            self._paused = True
            logger.info("Player %s: %d no-match -> cleared track (fade out)",
                        self.key, self._failures)
            if self._on_update:
                self._on_update(self)
