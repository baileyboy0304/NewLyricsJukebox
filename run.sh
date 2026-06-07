#!/bin/bash
# NewLyricsJukebox - Home Assistant add-on entrypoint.
# Configuration is read directly from /data/options.json by config.py (single
# config path). Here we only set persistent storage locations and exec the app.

set -e

echo "============================================"
echo "  NewLyricsJukebox - HA Add-on Starting"
echo "============================================"

# Persistent storage under the addon_config mount (/config).
export NLJ_OPTIONS_FILE="/data/options.json"
export NLJ_DATA_DIR="/config"
export NLJ_LYRICS_DB="/config/lyrics_database"
export NLJ_CACHE_DIR="/config/cache"
export NLJ_STATE_DIR="/config/state"
export NLJ_LOGS_DIR="/config/logs"
export PYTHONUNBUFFERED=1

mkdir -p "$NLJ_LYRICS_DB" "$NLJ_CACHE_DIR" "$NLJ_STATE_DIR" "$NLJ_LOGS_DIR"

exec python3 main.py
