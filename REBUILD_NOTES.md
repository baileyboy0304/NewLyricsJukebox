# NewLyricsJukebox — Rebuild Notes

Traceable old→new map for the clean rebuild. The old tree lived under
`legacy/SyncUDP/syncudp/` during the rebuild and was removed once finished
(`main` remains the fallback).

## Architecture (the spine)

```
Music Assistant (WS)               UDP RTP audio (per-player demux)
        │                                   │
        ▼                                   ▼
classify_source_mode(player)  ──────────────┤
   queue-based                          stream-based
   MA metadata + position               Shazam → ACRCloud, 3-attempt lock
        └───────────────┬───────────────────┘
                        ▼
            lyrics lookup (all providers, two-pass) → per-song JSON DB
                        ▼
            server.py  /players /current-track /lyrics /transport
                        ▼
            browser: polling (~100 ms) + flywheel clock, 3-line UI (port 9014)
```

## File map

| New file | Status | Derived from (legacy) |
|---|---|---|
| `config.py` | rewritten | `config.py` + `settings.py` (single path: options.json→env→defaults; no settings UI) |
| `logging_config.py` | rewritten | `logging_config.py` (trimmed) |
| `classify.py` | new | discriminator logic extracted from `system_utils/sources/music_assistant.py` |
| `ma_models.py` | new | lightweight `PlayerState`/`DetectedPlayer` (no heavy deps) |
| `music_assistant.py` | rewritten | `system_utils/sources/music_assistant.py` (1780 LOC → trimmed client) |
| `recognition/udp_capture.py` | rewritten | `audio_recognition/udp_capture.py` + `player_registry.py` (RTP-only, clean demux) |
| `recognition/result.py` | rewritten | `RecognitionResult` from `audio_recognition/shazam.py` |
| `recognition/shazam.py` | rewritten | `audio_recognition/shazam.py` (kept WAV conv + skew reject) |
| `recognition/acrcloud.py` | rewritten | `audio_recognition/acrcloud.py` (kept signing/offset; **quota now persisted**) |
| `recognition/engine.py` | rewritten | `audio_recognition/engine.py` (57k→clean; `LockTracker` + per-player state) |
| `lyrics.py` | rewritten | `lyrics.py` (kept providers/two-pass/DB; **removed module-global swapping**) |
| `providers/*` | kept as-is | `providers/{base,lrclib,musixmatch,netease,qq,spotify_lyrics,spotify_api}.py` |
| `server.py` | rewritten | `server.py` + `sync_lyrics.py` (4 endpoints, per-player runtime, Quart/Hypercorn) |
| `main.py` | new | entry point (replaces `sync_lyrics.py` 876 LOC) |
| `resources/js/flywheel.js` | rewritten | flywheel clock algorithm from `resources/js/modules/lineSync.js` |
| `resources/js/app.js` | new | replaces `main.js` + `modules/{api,state,dom,controls,playerSelector}.js` |
| `resources/templates/index.html`, `resources/css/app.css` | new | minimal 3-line UI |
| `config.yaml`, `Dockerfile`, `run.sh`, `requirements.txt`, `repository.yaml` | rewritten | HA add-on packaging, renamed + port 9014 |

## Dropped (per brief / CLAUDE.md)

Local fingerprinting (`local.py`, `daemon.py`, `sfp-cli`) — permanently excluded.
Reaper, Windows Media, album-art/artist-image subsystems, tray/mDNS/SSL, slideshow,
fonts, settings UI, dual audio buffers, word-level animation (later), `state_manager.py`,
`context.py`, `session_config.py`.

## Bugs fixed from AUDIT.md

- **Cluster A** (UDP): RTP-only capture, no private-attribute coupling, idle/stale
  timeouts, SSRC change resets sequence (not draining mid-flight).
- **Cluster B** (recognition): single `LockTracker` consensus; new-song resets lock so
  a skip can't re-lock the previous song.
- **Cluster D** (multi-player): per-player `PlayerRuntime`/`PlayerRecognizer` objects —
  no module-global swapping.
- **Config sprawl**: one `config.py`, one path.
- **ACRCloud quota**: persisted to `state/acrcloud_quota.json` (survives restart).

## Tests (one per seam)

`tests/test_rtp_parse.py`, `tests/test_classify.py`, `tests/test_recognition_lock.py`,
`tests/test_providers.py` — run with `python3 -m pytest -q`.

## Run

- Dev: `python3 main.py` (serves http://localhost:9014).
- Add-on: `run.sh` maps `/data/options.json` and execs `main.py`.
