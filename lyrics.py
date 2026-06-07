"""Lyrics lookup + per-song JSON DB.

Keeps all the hard-won provider knowledge from the legacy app — every provider
(LRCLIB, Spotify, Musixmatch, NetEase, QQ), priorities, parallel fetch, two-pass
selection (line-sync by pure priority; word-sync independently with a boost) and
the per-song multi-provider DB schema.

What's rewritten: NO module-global state swapping per ``?player=`` request
(AUDIT.md cluster D). ``LyricsService`` is stateless about "current song"; the
selected ``LyricsData`` is returned to the caller, who owns per-player state.
"""

import asyncio
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

logger = logging.getLogger(__name__)

WORD_SYNC_BOOST = 10
_db_lock = threading.Lock()


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

    async def _run_provider(self, provider, artist, title, album, duration):
        try:
            if asyncio.iscoroutinefunction(provider.get_lyrics):
                return await provider.get_lyrics(artist, title, album, duration)
            return await asyncio.to_thread(provider.get_lyrics, artist, title, album, duration)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Provider %s failed: %s", provider.name, exc)
            return None

    async def fetch(self, artist: str, title: str, album: str = None,
                    duration: int = None) -> LyricsData:
        """Look up lyrics for a track. Returns the best selection.

        Uses the cache when present; otherwise queries all enabled providers in
        parallel, persists every result, then runs the two-pass selection.
        """
        cached = self._read_db(artist, title)
        if cached and cached.get("saved_lyrics"):
            return self._select(artist, title, cached)

        enabled = [p for p in self.providers if p.enabled]
        if FEATURES["parallel_provider_fetch"]:
            tasks = {asyncio.create_task(self._run_provider(p, artist, title, album, duration)): p
                     for p in enabled}
            for task in asyncio.as_completed(tasks):
                raw = await task
                provider = tasks[task]
                line, meta, word = _normalize_result(raw)
                if line or word:
                    self._write_provider(artist, title, provider.name, line, meta, word)
        else:
            for p in enabled:
                raw = await self._run_provider(p, artist, title, album, duration)
                line, meta, word = _normalize_result(raw)
                if line or word:
                    self._write_provider(artist, title, p.name, line, meta, word)

        db = self._read_db(artist, title) or {"saved_lyrics": {}, "word_synced_lyrics": {}, "metadata": {}}
        return self._select(artist, title, db)

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
