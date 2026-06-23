"""Single source of configuration for NewLyricsJukebox.

Precedence: Home Assistant add-on options (``/data/options.json``) -> environment
variables -> hardcoded defaults. This is the ONLY config path in the app — there
is no settings UI and no settings.json. Every user-facing setting is exposed in
the add-on options schema (config.yaml).
"""

import json
import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Raw option loading
# --------------------------------------------------------------------------- #

OPTIONS_FILE = os.getenv("NLJ_OPTIONS_FILE", "/data/options.json")

try:
    with open(OPTIONS_FILE, "r", encoding="utf-8") as fh:
        _OPTIONS = json.load(fh) or {}
except (OSError, ValueError):
    _OPTIONS = {}


def _read_version() -> str:
    """Read the add-on version from config.yaml (shipped in the image) so we can
    log which build is actually running. Avoids a yaml dependency."""
    try:
        for line in (Path(__file__).parent / "config.yaml").read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "unknown"


VERSION = _read_version()


def conf(key, default=None):
    """Resolve a config value: options.json -> env var -> default.

    ``key`` may be dotted (e.g. ``providers.lrclib.priority``). The dotted form
    is mapped to an UPPER_SNAKE env var; for options.json the dotted key, then
    its final segment, are tried.
    """
    if key in _OPTIONS and _OPTIONS[key] not in (None, ""):
        return _OPTIONS[key]
    last = key.split(".")[-1]
    if last in _OPTIONS and _OPTIONS[last] not in (None, ""):
        return _OPTIONS[last]

    env_val = os.getenv(key.upper().replace(".", "_"))
    if env_val is not None and env_val.strip():
        return env_val
    return default


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Storage paths (persisted under /config in the add-on)
# --------------------------------------------------------------------------- #

DATA_DIR = Path(os.getenv("NLJ_DATA_DIR", str(Path(__file__).parent / "data")))
DATABASE_DIR = Path(os.getenv("NLJ_LYRICS_DB", str(DATA_DIR / "lyrics_database")))
CACHE_DIR = Path(os.getenv("NLJ_CACHE_DIR", str(DATA_DIR / "cache")))
STATE_DIR = Path(os.getenv("NLJ_STATE_DIR", str(DATA_DIR / "state")))
RESOURCES_DIR = Path(__file__).parent / "resources"

for _d in (DATABASE_DIR, CACHE_DIR, STATE_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        pass

# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

# The web UI port is FIXED at 9014 to match the add-on's ingress_port and the
# container port in config.yaml. It is intentionally NOT a user option: ingress_port
# is static YAML and can't follow an option, so changing only the internal bind would
# silently break the HA sidebar/ingress (it always targets 9014). To expose direct
# access on a different HOST port, change the host side of the `ports:` mapping in
# config.yaml. The env override below exists only for local development.
SERVER = {
    "host": conf("server_host", "0.0.0.0"),
    "port": _as_int(os.getenv("SERVER_PORT", "9014"), 9014),
}

LOG_LEVEL = conf("log_level", "INFO")

# --------------------------------------------------------------------------- #
# Music Assistant
# --------------------------------------------------------------------------- #

MUSIC_ASSISTANT = {
    "server_url": conf("music_assistant_base_url", "") or conf("system.music_assistant.server_url", ""),
    "token": conf("music_assistant_token", "") or conf("system.music_assistant.token", ""),
    "player_id": conf("music_assistant_player_id", "") or conf("system.music_assistant.player_id", ""),
}

# --------------------------------------------------------------------------- #
# UDP audio capture + recognition
# --------------------------------------------------------------------------- #

UDP_AUDIO = {
    "enabled": _as_bool(conf("recognition_enabled", True), True),
    # Fixed at 6056 (the container side of the `6056/udp` ports mapping). Like the
    # web port, the single user-facing knob is the host side of that mapping in the
    # add-on's Network panel — so the UDP port isn't duplicated as an option. Env
    # override is for local development only.
    "port": _as_int(os.getenv("UDP_LISTEN_PORT", "6056"), 6056),
    "sample_rate": _as_int(conf("udp_audio_sample_rate", 16000), 16000),
    "channels": 1,
    "jitter_buffer_ms": _as_int(conf("udp_jitter_buffer_ms", 60), 60),
    "lock_position": _as_bool(conf("lock_position", True), True),
    "lock_position_after": _as_int(conf("lock_position_after", 3), 3),
    "relock_position_after": _as_int(conf("relock_position_after", 2), 2),
    "lock_consensus_tolerance": _as_float(conf("lock_consensus_tolerance", 3.0), 3.0),
}

AUDIO_RECOGNITION = {
    "enabled": _as_bool(conf("recognition_enabled", True), True),
    "capture_duration": _as_float(conf("capture_duration", 6.0), 6.0),
    "recognition_interval": _as_float(conf("recognition_interval", 4.0), 4.0),
    "latency_offset": _as_float(conf("latency_offset", 0.0), 0.0),
    "silence_threshold": _as_int(conf("silence_threshold", 350), 350),
    "verification_cycles": _as_int(conf("verification_cycles", 2), 2),
    # Consecutive failed recognitions before the now-playing metadata + lyrics are
    # cleared (the UI then fades to just the transport controls). Context-aware: a
    # no-match NEAR the end of a known-duration song probably means the song really
    # ended, so blank fast; MID-song a no-match is more likely a transient miss
    # (quiet passage, mic gap), so tolerate more before blanking.
    "blank_after_failures": _as_int(conf("blank_after_failures", 3), 3),
    "blank_after_failures_near_end": _as_int(conf("blank_after_failures_near_end", 1), 1),
    "song_end_window": _as_float(conf("song_end_window", 30.0), 30.0),
    # ACR priority: one ACRCloud refinement per track. When ON, each newly-
    # detected track gets exactly ONE ACRCloud lookup to refine the Shazam
    # position (ACR is assumed more accurate). If the ACR position agrees
    # with Shazam within ``acr_priority_tolerance`` seconds it is adopted
    # and FROZEN for the rest of the track — subsequent Shazam reads no
    # longer move the clock. When OFF the app behaves exactly as before
    # (ACRCloud is only the fallback when Shazam fails to match).
    "acr_priority": _as_bool(conf("acr_priority", False), False),
    "acr_priority_tolerance": _as_float(conf("acr_priority_tolerance", 5.0), 5.0),
    # Whether room-mic audio is allowed into the recognition pipeline. Off by
    # default — recognition routes captured room audio through Shazam /
    # ACRCloud (third-party cloud services), so it's privacy-sensitive. When
    # off, the lyrics-mic-bridge keeps the ESP mics silent (no green dot,
    # no UDP streamed) and NLJ skips recognition for any incoming mic UDP
    # as defence in depth. Stream-recognition for audio routed THROUGH
    # Music Assistant (ha-udp-lyrics-player) is independent of this and
    # controlled by ``recognition_enabled`` above.
    "mic_recognition_enabled": _as_bool(conf("mic_recognition_enabled", False), False),
}

ACRCLOUD = {
    "host": conf("acrcloud_host", "") or os.getenv("ACRCLOUD_HOST", ""),
    "access_key": conf("acrcloud_access_key", "") or os.getenv("ACRCLOUD_ACCESS_KEY", ""),
    "access_secret": conf("acrcloud_access_secret", "") or os.getenv("ACRCLOUD_ACCESS_SECRET", ""),
    "daily_limit": _as_int(conf("acrcloud_daily_limit", 100), 100),
    "cooldown": _as_int(conf("acrcloud_cooldown", 30), 30),
}

# --------------------------------------------------------------------------- #
# Players
# --------------------------------------------------------------------------- #


def _parse_players():
    raw = _OPTIONS.get("players")
    if raw is None:
        env_raw = os.getenv("PLAYERS_JSON")
        if env_raw:
            try:
                raw = json.loads(env_raw)
            except ValueError:
                raw = []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        out.append({
            "name": entry.get("name"),
            "source_ip": entry.get("source_ip") or None,
            "rtp_ssrc": entry.get("rtp_ssrc") or None,
            "music_assistant_player_id": entry.get("music_assistant_player_id") or None,
            "description": entry.get("description") or None,
        })
    return out


PLAYERS = {
    "auto_discover": _as_bool(conf("players_auto_discover", True), True),
    "configured": _parse_players(),
}

# --------------------------------------------------------------------------- #
# Lyrics display / sync
# --------------------------------------------------------------------------- #

def _quantise_quarter(value: float) -> float:
    return round(value * 4) / 4


LYRICS = {
    "display": {
        "update_interval": _as_float(conf("lyrics.update_interval", 0.1), 0.1),
        "idle_interval": _as_float(conf("lyrics.idle_interval", 1.0), 1.0),
        "smart_race_timeout": _as_float(conf("lyrics.smart_race_timeout", 3.0), 3.0),
        "latency_compensation": _as_float(conf("lyrics.latency_compensation", -0.1), -0.1),
        "word_sync_latency_compensation": _as_float(conf("lyrics.word_sync_latency_compensation", -0.1), -0.1),
    },
    # NTP-anchored preload: deliver lyric lines this many seconds AHEAD of
    # their display moment. Each line's display_at_epoch_ms in /lyrics is
    # `track_anchor_ms + line.start*1000 + timing_offset_ms` (NOT minus
    # preload — preload is a delivery lead, not a display shift). Clients
    # render when their NTP clock reaches display_at_epoch_ms.
    "preload_time": _quantise_quarter(
        max(0.0, _as_float(conf("preload_time", 1.0), 1.0))
    ),
}

FEATURES = {
    "save_lyrics_locally": _as_bool(conf("features.save_lyrics_locally", True), True),
    "parallel_provider_fetch": _as_bool(conf("features.parallel_provider_fetch", True), True),
    "word_sync_auto_switch": _as_bool(conf("features.word_sync_auto_switch", False), False),
}

# --------------------------------------------------------------------------- #
# Lyrics providers (LRCLIB, Musixmatch, NetEase, QQ — Spotify removed: its lyrics
# endpoint is locked behind a rotating TOTP anti-bot, and Musixmatch covers the
# same lyrics).
# --------------------------------------------------------------------------- #

def _provider_enabled(name: str) -> bool:
    """Per-provider on/off. The flat HA add-on option ``provider_<name>`` takes
    precedence; falls back to the dotted form, then defaults to enabled."""
    val = conf(f"provider_{name}", None)
    if val is None:
        val = conf(f"providers.{name}.enabled", True)
    return _as_bool(val, True)


PROVIDERS = {
    "lrclib": {
        "enabled": _provider_enabled("lrclib"),
        "priority": _as_int(conf("providers.lrclib.priority", 2), 2),
        "base_url": "https://lrclib.net/api",
        "timeout": _as_int(conf("providers.lrclib.timeout", 10), 10),
        "retries": _as_int(conf("providers.lrclib.retries", 3), 3),
        "cache_duration": _as_int(conf("providers.lrclib.cache_duration", 86400), 86400),
    },
    "musixmatch": {
        "enabled": _provider_enabled("musixmatch"),
        "priority": _as_int(conf("providers.musixmatch.priority", 3), 3),
        "timeout": _as_int(conf("providers.musixmatch.timeout", 15), 15),
        "retries": _as_int(conf("providers.musixmatch.retries", 3), 3),
        "cache_duration": _as_int(conf("providers.musixmatch.cache_duration", 86400), 86400),
    },
    "netease": {
        "enabled": _provider_enabled("netease"),
        "priority": _as_int(conf("providers.netease.priority", 4), 4),
        "timeout": _as_int(conf("providers.netease.timeout", 10), 10),
        "retries": _as_int(conf("providers.netease.retries", 3), 3),
        "cache_duration": _as_int(conf("providers.netease.cache_duration", 86400), 86400),
    },
    "qq": {
        "enabled": _provider_enabled("qq"),
        "priority": _as_int(conf("providers.qq.priority", 5), 5),
        "timeout": _as_int(conf("providers.qq.timeout", 10), 10),
        "retries": _as_int(conf("providers.qq.retries", 3), 3),
        "cache_duration": _as_int(conf("providers.qq.cache_duration", 86400), 86400),
    },
}


def get_provider_config(name: str) -> dict:
    return PROVIDERS.get(name, {"enabled": False, "priority": 0})


def is_provider_enabled(name: str) -> bool:
    return PROVIDERS.get(name, {}).get("enabled", False)


def get_provider_priority(name: str) -> int:
    return PROVIDERS.get(name, {}).get("priority", 100)
