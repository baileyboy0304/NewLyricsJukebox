"""Lyrics lookup + per-song JSON DB.

Keeps all the hard-won provider knowledge from the legacy app — every provider
(LRCLIB, Musixmatch, NetEase, QQ), priorities, parallel fetch, two-pass
selection (line-sync by pure priority; word-sync independently with a boost) and
the per-song multi-provider DB schema.

What's rewritten: NO module-global state swapping per ``?player=`` request
(AUDIT.md cluster D). ``LyricsService`` is stateless about "current song"; the
selected ``LyricsData`` is returned to the caller, who owns per-player state.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import DATABASE_DIR, FEATURES, LYRICS
from providers import available_providers
from text_clean import strip_features, strip_version_noise

logger = logging.getLogger(__name__)

WORD_SYNC_BOOST = 10
_db_lock = threading.Lock()


def _accepts(fn, name: str) -> bool:
    """Does ``fn`` accept a keyword argument called ``name``? Lets us pass the
    optional ``spotify_id`` only to providers that support it (Musixmatch),
    without changing every provider's signature."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# --- "bad match" rescue: progressively cleaned search variants --------------- #

# Title/artist cleaning lives in text_clean so recognition (same-recording check)
# and lyrics (re-search variants) agree on what counts as "version noise".
_strip_version_noise = strip_version_noise
_strip_features = strip_features


def alternate_queries(artist: str, title: str) -> List[Tuple[str, str]]:
    """Ordered, de-duplicated (artist, title) search variants for the 'bad match'
    rescue — from no cleaning to most aggressive. Index 0 is always the original;
    each later entry strips more noise so a different recording can be found."""
    variants: List[Tuple[str, str]] = [(artist, title)]

    def add(a, t):
        pair = (a.strip(), t.strip())
        if pair not in variants and pair[1]:
            variants.append(pair)

    t1 = _strip_version_noise(title)
    add(artist, t1)                                  # level 1: drop version noise
    add(_strip_features(artist), _strip_features(t1))  # level 2: drop features
    return variants


@dataclass
class LyricsData:
    artist: str
    title: str
    line_synced: List[Tuple[float, str]] = field(default_factory=list)
    line_provider: Optional[str] = None
    word_synced: List[dict] = field(default_factory=list)
    word_provider: Optional[str] = None
    is_instrumental: bool = False
    plain: Optional[str] = None

    @property
    def has_lyrics(self) -> bool:
        return bool(self.line_synced) or bool(self.plain)

    @property
    def has_word_sync(self) -> bool:
        return bool(self.word_synced)


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w \-_]", "", text or "").strip()


def _db_path(artist: str, title: str) -> Path:
    return Path(DATABASE_DIR) / f"{_safe_name(artist)} - {_safe_name(title)}.json"


def _normalize_result(raw) -> Tuple[List[Tuple[float, str]], dict, List[dict]]:
    """Provider output -> (line_synced, metadata, word_synced)."""
    if raw is None:
        return [], {}, []
    if isinstance(raw, list):
        return [tuple(x) for x in raw], {}, []
    if isinstance(raw, dict):
        line = [tuple(x) for x in raw.get("lyrics", []) or []]
        word = raw.get("word_synced_lyrics") or []
        meta = {k: v for k, v in raw.items()
                if k not in ("lyrics", "word_synced_lyrics")}
        meta.setdefault("is_instrumental", False)
        return line, meta, word
    return [], {}, []


class LyricsService:
    def __init__(self):
        self.providers = sorted(
            (p() for p in available_providers),
            key=lambda p: p.priority,
        )
        self._by_name = {p.name: p for p in self.providers}

    # -- DB ---------------------------------------------------------------- #

    def _read_db(self, artist: str, title: str) -> Optional[dict]:
        path = _db_path(artist, title)
        with _db_lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def read_db(self, artist: str, title: str) -> Optional[dict]:
        """Public read of the per-song DB (used by the controller to expose the
        set of providers that returned lyrics, for the +/- provider cycle)."""
        return self._read_db(artist, title)

    # -- provider cycling (manual +/- selection) -------------------------- #

    def enabled_provider_names(self) -> List[str]:
        """All enabled providers, by priority — the full list the +/- buttons
        cycle through (even providers that have no lyrics for the current song)."""
        return [p.name for p in self.providers if p.enabled]

    def provider_names_in(self, db: dict) -> List[str]:
        """Providers that have line-synced lyrics in this song's DB, ordered by
        priority."""
        saved = (db or {}).get("saved_lyrics", {})
        return [p.name for p in self.providers if p.name in saved]

    def set_preferred(self, artist: str, title: str, provider: str) -> None:
        """Persist the user's provider pick for this song so it's reused whenever
        the song is recalled from cache (the provider/song correlation)."""
        if not FEATURES["save_lyrics_locally"]:
            return
        path = _db_path(artist, title)
        with _db_lock:
            try:
                db = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                db = {"artist": artist, "title": title, "saved_lyrics": {},
                      "word_synced_lyrics": {}, "metadata": {}}
            db["preferred_provider"] = provider
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:  # pragma: no cover
                logger.warning("Could not persist preferred provider: %s", exc)
        logger.info("lyrics preferred provider for %s - %s -> %s", artist, title, provider)

    def set_bad_match_index(self, artist: str, title: str, index: int) -> None:
        """Persist the 'bad match' rescue level chosen for this song, so a recall
        starts from the same cleaned-search variant the user settled on."""
        if not FEATURES["save_lyrics_locally"]:
            return
        path = _db_path(artist, title)
        with _db_lock:
            try:
                db = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                db = {"artist": artist, "title": title, "saved_lyrics": {},
                      "word_synced_lyrics": {}, "metadata": {}}
            db["bad_match_index"] = int(index)
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:  # pragma: no cover
                logger.warning("Could not persist bad-match index: %s", exc)

    async def fetch_provider(self, artist: str, title: str, provider: str,
                             album: str = None, duration: int = None,
                             on_update=None, spotify_id: str = None,
                             isrc: str = None) -> Optional[LyricsData]:
        """On-demand fetch from ONE provider (when the user cycles to a provider
        not yet in the song's cache, e.g. a provider enabled after the song was
        first cached). Logs the outcome and caches any lyrics. ``on_update(data,
        db)`` is called with the result (data is None when the provider has none)."""
        p = self._by_name.get(provider)
        if p is None or not p.enabled:
            return None
        raw, err = await self._run_provider(p, artist, title, album, duration,
                                            spotify_id, isrc)
        line, meta, word = _normalize_result(raw)
        self._log_provider_outcome(provider, artist, title, line, word, err)
        if line or word:
            self._write_provider(artist, title, provider, line, meta, word)
        db = self._read_db(artist, title) or {}
        data = self.lyrics_for_provider(artist, title, db, provider)
        if on_update:
            on_update(data, db)
        return data

    def lyrics_for_provider(self, artist: str, title: str, db: dict,
                            provider: str) -> Optional[LyricsData]:
        """Build LyricsData for ONE specific provider from the song's DB (so the
        user can step through each provider's lyrics), or None if absent."""
        saved = (db or {}).get("saved_lyrics", {})
        if provider not in saved:
            return None
        data = LyricsData(artist=artist, title=title)
        data.line_synced = [tuple(x) for x in saved[provider]]
        data.line_provider = provider
        meta = (db.get("metadata") or {}).get(provider, {})
        data.is_instrumental = bool(meta.get("is_instrumental"))
        data.plain = meta.get("plain_lyrics")
        ws = (db.get("word_synced_lyrics") or {}).get(provider)
        if ws:
            data.word_synced = ws
            data.word_provider = provider
        return data

    def _clear_lyrics(self, artist, title):
        """Empty a song's cached lyrics (keeping non-lyric keys like
        ``bad_match_index``) ahead of a forced re-search. Drops any
        ``preferred_provider`` since the user just rejected that selection."""
        if not FEATURES["save_lyrics_locally"]:
            return
        path = _db_path(artist, title)
        with _db_lock:
            try:
                db = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            db["saved_lyrics"] = {}
            db["word_synced_lyrics"] = {}
            db["metadata"] = {}
            db.pop("preferred_provider", None)
            db.pop("preferred_word_sync_provider", None)
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:  # pragma: no cover
                logger.warning("Could not clear lyrics DB: %s", exc)

    def _write_provider(self, artist, title, provider, line, meta, word):
        if not FEATURES["save_lyrics_locally"]:
            return
        path = _db_path(artist, title)
        with _db_lock:
            try:
                db = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                db = {"artist": artist, "title": title, "saved_lyrics": {},
                      "word_synced_lyrics": {}, "metadata": {}}
            db.setdefault("saved_lyrics", {})
            db.setdefault("word_synced_lyrics", {})
            db.setdefault("metadata", {})
            if line:
                db["saved_lyrics"][provider] = [list(x) for x in line]
            if word:
                db["word_synced_lyrics"][provider] = word
            if meta:
                db["metadata"][provider] = meta
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:  # pragma: no cover
                logger.warning("Could not write lyrics DB: %s", exc)

    # -- selection (two-pass) --------------------------------------------- #

    def _select(self, artist: str, title: str, db: dict) -> LyricsData:
        saved = db.get("saved_lyrics", {})
        word = db.get("word_synced_lyrics", {})
        meta = db.get("metadata", {})
        data = LyricsData(artist=artist, title=title)

        # PASS 1 — line-sync by pure priority (or user preference).
        best_priority = 999
        preferred = db.get("preferred_provider")
        if preferred and preferred in saved:
            data.line_synced = [tuple(x) for x in saved[preferred]]
            data.line_provider = preferred
        else:
            for p in self.providers:
                if p.name in saved and p.priority < best_priority:
                    best_priority = p.priority
                    data.line_synced = [tuple(x) for x in saved[p.name]]
                    data.line_provider = p.name

        # PASS 2 — word-sync independently, with a boost.
        best_ws = 999
        preferred_ws = db.get("preferred_word_sync_provider")
        if preferred_ws and word.get(preferred_ws):
            data.word_synced = word[preferred_ws]
            data.word_provider = preferred_ws
        else:
            for p in self.providers:
                ws = word.get(p.name)
                if ws:
                    effective = p.priority - WORD_SYNC_BOOST
                    if effective < best_ws:
                        best_ws = effective
                        data.word_synced = ws
                        data.word_provider = p.name

        if db.get("is_instrumental_manual") is not None:
            data.is_instrumental = bool(db["is_instrumental_manual"])
        else:
            prov_meta = meta.get(data.line_provider, {}) if data.line_provider else {}
            data.is_instrumental = bool(prov_meta.get("is_instrumental"))
            data.plain = prov_meta.get("plain_lyrics")
        return data

    # -- fetch ------------------------------------------------------------- #

    async def _run_named(self, provider, artist, title, album, duration,
                         spotify_id=None, isrc=None):
        raw, err = await self._run_provider(provider, artist, title, album, duration,
                                            spotify_id, isrc)
        return provider, raw, err

    async def _run_provider(self, provider, artist, title, album, duration,
                            spotify_id=None, isrc=None):
        """Run one provider, returning (raw_result, error). The error is surfaced
        (not swallowed) so the caller can log per-provider failures. ``spotify_id``
        and ``isrc`` are passed only to providers whose ``get_lyrics`` accepts them
        (Musixmatch) — exact-recording match hints."""
        kwargs = {}
        if spotify_id and _accepts(provider.get_lyrics, "spotify_id"):
            kwargs["spotify_id"] = spotify_id
        if isrc and _accepts(provider.get_lyrics, "isrc"):
            kwargs["isrc"] = isrc
        try:
            if asyncio.iscoroutinefunction(provider.get_lyrics):
                return await provider.get_lyrics(artist, title, album, duration, **kwargs), None
            return await asyncio.to_thread(
                provider.get_lyrics, artist, title, album, duration, **kwargs), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    @staticmethod
    def _log_provider_outcome(name, artist, title, line, word, err):
        """One INFO line per provider so failures/empties are visible in the log."""
        if err is not None:
            logger.info("lyrics-provider %s ERROR for %s - %s: %s", name, artist, title, err)
        elif line or word:
            logger.info("lyrics-provider %s OK for %s - %s (%d lines%s)",
                        name, artist, title, len(line), ", word-sync" if word else "")
        else:
            logger.info("lyrics-provider %s no lyrics for %s - %s", name, artist, title)

    async def fetch(self, artist: str, title: str, album: str = None,
                    duration: int = None, on_update=None,
                    spotify_id: str = None, search_artist: str = None,
                    search_title: str = None, force: bool = False,
                    isrc: str = None) -> LyricsData:
        """Look up lyrics for a track. Returns the best selection.

        Uses the cache when present; otherwise queries all enabled providers in
        parallel. Results are applied INCREMENTALLY: ``on_update(LyricsData)`` is
        called each time a provider returns and improves the selection, so the
        UI can show lyrics from the first fast provider (~1s) without waiting for
        slow/failing ones (e.g. QQ retrying a 500 for ~25s).

        ``search_artist`` / ``search_title`` override the terms sent to providers
        (the DB is always keyed by the original ``artist``/``title``) — used by the
        'bad match' rescue to re-query with a cleaned title. ``force`` bypasses the
        cache so a rescue always re-queries and overwrites the cached lyrics.
        """
        cached = self._read_db(artist, title)
        if not force and cached and cached.get("saved_lyrics"):
            data = self._select(artist, title, cached)
            # Cached song: the per-provider fetch loop below is skipped, so log the
            # cache hit here (otherwise nothing is logged for already-seen songs).
            logger.info("lyrics from cache for %s - %s: using %s (have %s)",
                        artist, title, data.line_provider,
                        ", ".join(self.provider_names_in(cached)) or "none")
            if on_update:
                on_update(data)
            return data

        sa = search_artist or artist
        st = search_title or title
        if force:
            # Wipe the rejected lyrics first so only the fresh re-search results
            # remain (a provider that returns nothing on the cleaned query must not
            # leave its old, rejected lyrics behind).
            self._clear_lyrics(artist, title)
            logger.info("lyrics re-search for %s - %s using '%s - %s' (bad-match rescue)",
                        artist, title, sa, st)

        enabled = [p for p in self.providers if p.enabled]
        data = LyricsData(artist=artist, title=title)

        def apply(provider_name, line, meta, word):
            nonlocal data
            if not (line or word):
                return
            self._write_provider(artist, title, provider_name, line, meta, word)
            db = self._read_db(artist, title) or {
                "saved_lyrics": {}, "word_synced_lyrics": {}, "metadata": {}}
            data = self._select(artist, title, db)
            if on_update:
                on_update(data)

        if FEATURES["parallel_provider_fetch"]:
            # Each task returns (provider, raw) so we don't rely on identifying
            # the originating task (as_completed yields wrappers, not the tasks).
            tasks = [asyncio.create_task(
                        self._run_named(p, sa, st, album, duration, spotify_id, isrc))
                     for p in enabled]
            for fut in asyncio.as_completed(tasks):
                provider, raw, err = await fut
                line, meta, word = _normalize_result(raw)
                self._log_provider_outcome(provider.name, artist, title, line, word, err)
                apply(provider.name, line, meta, word)
        else:
            for p in enabled:
                raw, err = await self._run_provider(p, sa, st, album, duration,
                                                    spotify_id, isrc)
                line, meta, word = _normalize_result(raw)
                self._log_provider_outcome(p.name, artist, title, line, word, err)
                apply(p.name, line, meta, word)

        return data

    # -- line helpers (for the 3-line view) ------------------------------- #

    @staticmethod
    def lines_around(data: LyricsData, position: float) -> Dict[str, str]:
        """Return previous / current / next line text for a given position."""
        lines = data.line_synced
        if not lines:
            return {"previous": "", "current": "", "next": ""}
        idx = -1
        for i, (start, _) in enumerate(lines):
            end = lines[i + 1][0] if i + 1 < len(lines) else start + 10
            if start <= position < end:
                idx = i
                break
        if idx == -1 and position >= lines[-1][0]:
            idx = len(lines) - 1
        return {
            "previous": lines[idx - 1][1] if idx - 1 >= 0 else "",
            "current": lines[idx][1] if idx >= 0 else "",
            "next": lines[idx + 1][1] if 0 <= idx + 1 < len(lines) else "",
        }
