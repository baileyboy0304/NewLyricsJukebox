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

SERVER = {
    "host": conf("server_host", "0.0.0.0"),
    "port": _as_int(conf("server_port", 9014), 9014),
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
    "port": _as_int(conf("udp_listen_port", 6056), 6056),
    "sample_rate": _as_int(conf("udp_audio_sample_rate", 16000), 16000),
    "channels": 1,
    "jitter_buffer_ms": _as_int(conf("udp_jitter_buffer_ms", 60), 60),
    "lock_position": _as_bool(conf("lock_position", True), True),
    "lock_position_after": _as_int(conf("lock_position_after", 3), 3),
    "lock_consensus_tolerance": _as_float(conf("lock_consensus_tolerance", 3.0), 3.0),
}

AUDIO_RECOGNITION = {
    "enabled": _as_bool(conf("recognition_enabled", True), True),
    "capture_duration": _as_float(conf("capture_duration", 6.0), 6.0),
    "recognition_interval": _as_float(conf("recognition_interval", 4.0), 4.0),
    "latency_offset": _as_float(conf("latency_offset", 0.0), 0.0),
    "silence_threshold": _as_int(conf("silence_threshold", 350), 350),
    "verification_cycles": _as_int(conf("verification_cycles", 2), 2),
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

LYRICS = {
    "display": {
        "update_interval": _as_float(conf("lyrics.update_interval", 0.1), 0.1),
        "idle_interval": _as_float(conf("lyrics.idle_interval", 1.0), 1.0),
        "smart_race_timeout": _as_float(conf("lyrics.smart_race_timeout", 3.0), 3.0),
        "latency_compensation": _as_float(conf("lyrics.latency_compensation", -0.1), -0.1),
        "word_sync_latency_compensation": _as_float(conf("lyrics.word_sync_latency_compensation", -0.1), -0.1),
    }
}

FEATURES = {
    "save_lyrics_locally": _as_bool(conf("features.save_lyrics_locally", True), True),
    "parallel_provider_fetch": _as_bool(conf("features.parallel_provider_fetch", True), True),
    "word_sync_auto_switch": _as_bool(conf("features.word_sync_auto_switch", False), False),
}

# --------------------------------------------------------------------------- #
# Lyrics providers (kept: LRCLIB, Spotify, Musixmatch, NetEase, QQ)
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
    "spotify": {
        "enabled": _provider_enabled("spotify"),
        "priority": _as_int(conf("providers.spotify.priority", 1), 1),
        "base_url": os.getenv("SPOTIFY_BASE_URL", "https://spotify-lyrics-api-azure.vercel.app"),
        "timeout": _as_int(conf("providers.spotify.timeout", 10), 10),
        "retries": _as_int(conf("providers.spotify.retries", 3), 3),
        "cache_duration": _as_int(conf("providers.spotify.cache_duration", 3600), 3600),
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


# Spotify API (used by the Spotify lyrics provider). Credentials optional.
SPOTIFY = {
    "client_id": conf("spotify_client_id", "") or os.getenv("SPOTIFY_CLIENT_ID", ""),
    "client_secret": conf("spotify_client_secret", "") or os.getenv("SPOTIFY_CLIENT_SECRET", ""),
    "redirect_uri": conf("spotify.redirect_uri", "http://127.0.0.1:9014/callback"),
    "scope": ["user-read-currently-playing", "user-read-playback-state"],
    "cache": {
        "enabled": _as_bool(conf("spotify.cache.enabled", True), True),
        "metadata_ttl": _as_float(conf("spotify.cache.metadata_ttl", 2.0), 2.0),
    },
    "polling": {
        "fast_interval": _as_float(conf("spotify.polling.fast_interval", 2.0), 2.0),
        "slow_interval": _as_float(conf("spotify.polling.slow_interval", 6.0), 6.0),
    },
}

# Album-art enhancement flags consulted by the Spotify provider. The full
# album-art subsystem is intentionally dropped; these defaults keep the
# provider import working without enabling any extra fetching.
ALBUM_ART = {
    "timeout": 5,
    "retries": 2,
    "enable_itunes": False,
    "enable_lastfm": False,
    "enable_spotify_enhanced": False,
    "min_resolution": 640,
}
