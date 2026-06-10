"""ACRCloud recognizer (fallback).

HMAC-SHA1 signed multipart POST to /v1/identify. The daily-quota counter is
PERSISTED to disk (the legacy counter reset on restart — a bug) and rolls over
by calendar date.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests

from config import ACRCLOUD, STATE_DIR
from recognition.result import RecognitionResult
from recognition.shazam import audio_to_wav
from recognition.udp_capture import AudioChunk

logger = logging.getLogger(__name__)

_QUOTA_FILE = Path(STATE_DIR) / "acrcloud_quota.json"


class ACRCloudRecognizer:
    def __init__(self):
        self._host = ACRCLOUD["host"]
        self._access_key = ACRCLOUD["access_key"]
        self._access_secret = ACRCLOUD["access_secret"]
        self._daily_limit = ACRCLOUD["daily_limit"]
        self._cooldown = ACRCLOUD["cooldown"]
        self._enabled = bool(self._host and self._access_key and self._access_secret)
        self._last_request_time = 0.0
        self._requests_today, self._counter_date = self._load_quota()

    # -- quota persistence ------------------------------------------------- #

    def _load_quota(self):
        try:
            data = json.loads(_QUOTA_FILE.read_text())
            return int(data.get("requests_today", 0)), data.get("date", "")
        except (OSError, ValueError):
            return 0, ""

    def _save_quota(self):
        try:
            _QUOTA_FILE.write_text(json.dumps({
                "requests_today": self._requests_today,
                "date": self._counter_date,
            }))
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not persist ACRCloud quota: %s", exc)

    def _reset_if_new_day(self):
        today = date.today().isoformat()
        if self._counter_date != today:
            self._requests_today = 0
            self._counter_date = today
            self._save_quota()

    # -- availability ------------------------------------------------------ #

    def unavailable_reason(self) -> Optional[str]:
        """Why ACRCloud can't be used right now, or ``None`` if it can. Lets
        callers log a precise cause instead of a vague 'quota/cooldown'."""
        if not self._enabled:
            return "not configured (set acrcloud_host / access_key / access_secret)"
        self._reset_if_new_day()
        if self._requests_today >= self._daily_limit:
            return "daily quota reached (%d/%d)" % (self._requests_today, self._daily_limit)
        remaining = self._cooldown - (time.time() - self._last_request_time)
        if remaining > 0:
            return "cooling down (%.0fs left)" % remaining
        return None

    def is_available(self) -> bool:
        return self.unavailable_reason() is None

    def stats(self) -> dict:
        return {
            "enabled": self._enabled,
            "requests_today": self._requests_today,
            "daily_limit": self._daily_limit,
        }

    # -- signing ----------------------------------------------------------- #

    def _signature(self, timestamp: str) -> str:
        string_to_sign = "\n".join([
            "POST", "/v1/identify", self._access_key, "audio", "1", timestamp,
        ])
        digest = hmac.new(
            self._access_secret.encode("ascii"),
            string_to_sign.encode("ascii"),
            digestmod=hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    # -- recognition ------------------------------------------------------- #

    async def recognize(self, audio: AudioChunk) -> Optional[RecognitionResult]:
        if not self.is_available():
            return None
        wav = audio_to_wav(audio)
        timestamp = str(int(time.time()))
        signature = self._signature(timestamp)
        url = f"https://{self._host}/v1/identify"
        files = {"sample": ("audio.wav", wav, "audio/wav")}
        data = {
            "access_key": self._access_key,
            "data_type": "audio",
            "signature_version": "1",
            "signature": signature,
            "sample_bytes": len(wav),
            "timestamp": timestamp,
        }

        # Count usage before the request so cooldown/quota apply even on failure.
        self._reset_if_new_day()
        self._requests_today += 1
        self._last_request_time = time.time()
        self._save_quota()

        try:
            response = await asyncio.to_thread(
                requests.post, url, files=files, data=data, timeout=8)
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ACRCloud request failed: %s", exc)
            return None

        if payload.get("status", {}).get("code") != 0:
            return None
        try:
            track = payload["metadata"]["music"][0]
        except (KeyError, IndexError):
            return None

        offset_ms = track.get("play_offset_ms", 0)
        sample_begin_ms = track.get("sample_begin_time_offset_ms", 0)
        sample_end_ms = track.get("sample_end_time_offset_ms", 0)
        sample_duration_ms = sample_end_ms - sample_begin_ms
        offset = (offset_ms - sample_duration_ms) / 1000.0
        adjusted_capture_start = audio.capture_start_time + sample_begin_ms / 1000.0

        artists = track.get("artists", [])
        artist = artists[0]["name"] if artists else "Unknown"
        album = (track.get("album") or {}).get("name")
        isrc = (track.get("external_ids") or {}).get("isrc")
        score = track.get("score", 50)
        duration = track.get("duration_ms")
        # ACRCloud uniquely identifies the exact recording via a Spotify track id
        # (Shazam does not expose one). It's used downstream to pin lyrics to the
        # right variant — radio often plays an edit/remaster that fuzzy title
        # search mismatches.
        spotify = (track.get("external_metadata") or {}).get("spotify") or {}
        spotify_id = (spotify.get("track") or {}).get("id")

        return RecognitionResult(
            title=track.get("title", "Unknown"),
            artist=artist,
            offset=offset,
            capture_start_time=adjusted_capture_start,
            confidence=score / 100.0,
            album=album,
            isrc=isrc,
            duration=duration / 1000.0 if duration else None,
            provider="acrcloud",
            spotify_id=spotify_id,
        )
