# NewLyricsJukebox — Code Audit

**Scope:** read-only review of SyncLyrics UDP v0.0.95.
**Purpose:** map the pipeline and external contracts, separate core from cruft,
and locate bugs/inconsistencies ahead of a clean rebuild.
**Root of all source:** `SyncUDP/SyncUDP/syncudp/` (51 Python files, ~29.6k LOC).
**No code was changed.**

## 1. Data flow — audio in to lyrics displayed

```
Music Assistant UDP players
        │  RTP/PCM datagrams, port 6056
        ▼
audio_recognition/udp_capture.py      ← UDP socket, RTP parse, jitter buffer,
        │  AudioChunk (16 kHz/16-bit/mono)   per-player demux via RFC 8285 ext
        ▼
audio_recognition/player_registry.py  ← IP/SSRC/MA-identity → logical player
        │
        ▼
audio_recognition/engine.py           ← capture→recognise loop, state machine,
        │  RecognitionResult                position interpolation + latency comp
        ▼
audio_recognition/{shazam,acrcloud,local}.py  ← recognise track (chain below)
        │  title/artist/offset
        ▼
system_utils/metadata.py              ← reconciles recognition with Music
        │  current track                   Assistant / Spotify / Windows / Reaper
        ▼
lyrics.py                             ← multi-provider fetch + cache + DB,
        │  line- & word-synced lyrics       instrumental detection, per-player state
        ▼
providers/{spotify_lyrics,lrclib,musixmatch,netease,qq}.py  ← lyrics sources
        │
        ▼
server.py  (Quart/Hypercorn ASGI)     ← REST endpoints (/lyrics, /current-track)
        │  JSON over HTTP, frontend polls ~100 ms
        ▼
resources/js/main.js + modules/{lineSync,wordSync,dom}.js   ← browser UI
           render + "flywheel clock" sync, line- and word-level highlighting
```

**Key timing detail:** there is **no server→client push** for lyrics. The
browser *polls* `/lyrics` and `/current-track` over HTTP (~100 ms, adaptive to
1 s when idle). Client-side a monotonic "flywheel clock"
(`resources/js/modules/lineSync.js`) interpolates between polls and snaps on
large drift; word-level animation lives in `wordSync.js`.

**Entry points:** `sync_lyrics.py` (876 LOC) is the primary process launcher
(logging, mDNS, watchdog, tray icon, Hypercorn). `run.sh` is the Home Assistant
add-on shim that maps `/data/options.json` to env vars and execs
`sync_lyrics.py`.

## 2. External contracts

### 2.1 UDP / Music Assistant audio interface
- **Transport:** UDP, default port **6056** (`UDP_AUDIO_PORT`), bound `0.0.0.0`.
  Handler: `audio_recognition/udp_capture.py`
  (`UdpAudioProtocol`/`UdpAudioCapture`).
- **Payload:** 16-bit signed LE PCM, mono, default 16 kHz
  (`UDP_AUDIO_SAMPLE_RATE`). Two framings auto-detected per packet: raw PCM
  (legacy) and **RTP v2** (RFC 3550).
- **Player identity:** RFC 8285 RTP header extensions carry MA player name
  (element id 1) and player id (element id 2). Resolution priority in
  `player_registry.py`: MA identity → SSRC → source IP.
- **Jitter buffer:** default 60 ms (`UDP_JITTER_BUFFER_MS`), ≤50 packets,
  reorders by sequence, inserts silence on loss, 30 s rolling buffer/player.
- **Consumer API:** `get_audio(duration, player_name) → AudioChunk`
  (`data:int16, sample_rate, channels, duration, capture_start_time`), used by
  the engine for latency compensation.
- **Separate MA control channel:** `system_utils/sources/music_assistant.py`
  (1780 LOC) is a *WebSocket* client to the MA server (`SYSTEM_MUSIC_ASSISTANT_*`
  env vars) for player state/metadata/playback control — distinct from the UDP
  audio path.

### 2.2 Recognition engine (3-stage chain, `engine.py` orchestrates)
- **Primary — Shazam** (`shazam.py`, ShazamIO): audio converted to WAV via
  stdlib `wave` + numpy (no ffmpeg); optional scipy resample (warns at
  `shazam.py:571` and falls back to `numpy.interp` if scipy missing). Rejects
  matches when `|timeskew|` or `|freqskew| > 0.08`.
- **Fallback — ACRCloud** (`acrcloud.py`): `POST https://{host}/v1/identify`,
  multipart WAV, HMAC-SHA1 signed; credentials via `ACRCLOUD_*` env vars; daily
  limit + per-request cooldown (in-memory, not persisted). Offset derived from
  `play_offset_ms` minus sample duration.
- **Optional — Local fingerprint** (`local.py` + `daemon.py` + `sfp-cli`):
  SoundFingerprinting daemon for sub-100 ms offline queries against a user FLAC
  DB; gated by `LOCAL_FP_ENABLED`; subprocess fallback intentionally disabled.
- **Output:** `RecognitionResult` with capture-start-relative offset; engine
  interpolates current position between recognitions.

### 2.3 Lyrics sources (`lyrics.py` orchestrates, priority-ordered)
| Provider | File | Endpoint | Sync level |
|---|---|---|---|
| Spotify (pri 1) | `providers/spotify_lyrics.py` | `spotify-lyrics-api-azure.vercel.app/?url=…&format=lrc` (track URL via Spotify API) | line (LRC) |
| LRCLIB (pri 2) | `providers/lrclib.py` | `https://lrclib.net/api` `/get` then `/search` | line (LRC) + plain |
| Musixmatch (pri 3) | `providers/musixmatch.py` | `apic-desktop.musixmatch.com/ws/1.1/` `macro.subtitles.get`, `track.richsync.get` | line + **word** (RichSync) |
| NetEase (pri 3) | `providers/netease.py` | `music.163.com/api/search/pc`, `/api/song/lyric` (`lv/yv/kv`) | line (LRC) + **word** (YRC) |
| QQ Music (pri 4) | `providers/qq.py` | `y.qq.com/…` | line only |

- **Fetch strategy:** parallel by default; first completed provider with best
  priority wins; a background `_backfill_missing_providers()` adds up to 3
  providers and prefers acquiring word-sync.
- **Persistence:** per-song JSON in the lyrics DB (`{artist} - {title}.json`)
  with `saved_lyrics` (line-sync per provider), `word_synced_lyrics` (per
  provider), `preferred_provider`, `preferred_word_sync_provider`,
  `is_instrumental_manual`, `word_sync_offset`.
- **Two-pass selection:** line-sync chosen by pure priority; word-sync chosen
  independently with a `WORD_SYNC_BOOST=10` — so providers can be mixed
  (e.g. Spotify line-sync + Musixmatch word-sync).
- **Sync formats parsed:** LRC `[MM:SS.MS]`; Musixmatch RichSync (char offsets →
  per-word duration); NetEase YRC (absolute ms with explicit per-word duration).

## 3. Core vs. cruft

**Essential to the pipeline**
- `sync_lyrics.py`, `server.py`, `lyrics.py`
- `audio_recognition/{udp_capture,engine,shazam,player_registry}.py`
- `system_utils/metadata.py`, `system_utils/sources/music_assistant.py`
- `providers/{base,lrclib,musixmatch,spotify_lyrics,spotify_api}.py`
- `config.py`, `logging_config.py`
- `resources/js/main.js`, `modules/{lineSync,wordSync,dom,api,state}.js`,
  `resources/templates/index.html`

**Auxiliary / support**
- `settings.py`, `state_manager.py`, `context.py`, `version.py`
- `ssl_utils.py`, `network_utils.py`, `font_scanner.py`
- `system_utils/{state,helpers,image,session_config}.py`,
  `system_utils/sources/{base,enrichment}.py`
- `audio_recognition/{capture,buffer,audio_buffer,player_manager}.py`
- `providers/{album_art,artist_image}.py`,
  `system_utils/{album_art,artist_image}.py`

**Experimental / niche**
- `system_utils/reaper.py` (960 LOC, Reaper DAW remote API — off the main path)
- `audio_recognition/local.py` + `daemon.py` + `sfp-cli` (offline FP, gated off
  by default; subprocess fallback disabled)
- `system_utils/windows.py` (Windows Media Session only)
- WebSocket `/ws/audio-stream` in `server.py` — disabled ("send audio over UDP")

**Dead / vestigial (inline, not whole files)**
- `wordSync.js` `lastServerSync` declared-unused (explicit `// DEAD CODE` note)
- `main.js` commented-out string-type lyrics check
- `lyrics.py` commented `LATENCY_COMPENSATION` line
- ACRCloud daily-counter logic that never persists (resets on restart)
- No orphaned `*_old.py`/`*_backup.py` files; every module is imported somewhere.

**Redundant-looking but actually layered (call out, don't cut blindly)**
- `providers/album_art.py` (remote fetch) vs `system_utils/album_art.py` (local
  DB); same split for artist images.
- Three Spotify modules: `providers/spotify_api.py` (auth/control singleton),
  `providers/spotify_lyrics.py` (lyrics), `system_utils/spotify.py` (playback
  state).
- Two audio buffers: `audio_buffer.py` (UI spectrum) vs `buffer.py` (frontend
  queue + latency).

## 4. Inconsistencies (same job done more than one way)

1. **Configuration is spread across four mechanisms:** `config.py`
   (env → settings.json → defaults), `settings.py` (typed persistent settings),
   `system_utils/session_config.py` (recognition session params), and direct env
   reads in `run.sh`. Overlapping responsibility, no single source of truth.
2. **State is split across two managers:** `state_manager.py` (theme/UI/global)
   and `system_utils/state.py` (locks/caches/trackers) with their own locking.
3. **Provider selection asymmetry:** line-sync uses pure priority but word-sync
   adds a `WORD_SYNC_BOOST`, so the two halves of one lyric can come from
   different providers by different rules — undocumented.
4. **Provider-count limits diverge:** backfill stops at 3 providers, but the
   word-sync backfill passes `skip_provider_limit=True`, so the cap is
   inconsistent.
5. **Word-sync "reload from DB after save" logic is duplicated** in two places
   in `lyrics.py` (background task ~L770 and backfill ~L873), both tagged `FIX:`.
6. **Instrumental detection has competing authorities:** manual flag,
   `_is_cached_instrumental()` evidence heuristic, and single-line placeholder
   detection — applied in different orders in `lyrics.py` vs `server.py`.
7. **Resampling paths:** scipy vs `numpy.interp` vs none, chosen at runtime with
   differing quality and only a warning to signal the degraded path.
8. **Two framings on one UDP socket** (raw PCM vs RTP), auto-detected per packet
   rather than negotiated.

## 5. Bugs — where they cluster and likely root causes

**Cluster A — UDP/RTP buffering (`udp_capture.py`).** Root cause: hand-rolled
jitter/sequence logic with private-attribute coupling.
- SSRC change calls `_jitter_buffer.reset()` without draining queued packets →
  audio loss on session restart.
- `_skip_to()` reorder after loss can drop packets that arrive past the gap.
- `max_gap` heuristic reaches into `_jitter_buffer._max_packets` (private),
  silently breaks if that constant changes.
- Unknown-player `get_audio()` can await packets forever (no timeout).

**Cluster B — recognition/position state (`engine.py`, `shazam.py`,
`acrcloud.py`).** Root cause: position is reconstructed from many interacting
offsets/locks and fallback paths.
- On skew rejection, a valid-but-skewed Shazam result is discarded even when the
  ACRCloud fallback returns nothing.
- Track-skip can re-lock the *previous* song's position, jumping backwards.
- ACRCloud offset assumes `sample_begin_ms` present; missing → silent position
  error. Daily quota counter not persisted.
- 2 s gap-based pause heuristic is fragile against jitter/RTP restarts.

**Cluster C — lyrics provider parsing/selection (`lyrics.py`, `musixmatch.py`,
`netease.py`).** Root cause: each provider re-implements sync parsing and
scoring; subtle format assumptions.
- Musixmatch word-duration derived from next char offset vs the comment's
  "space marks previous word end" — can mis-time words near spaces.
- NetEase YRC re-joins words with spaces, may not match original punctuation.
- LRCLIB returns a result even when instrumental flag set with no synced lyrics.

**Cluster D — multi-player state scoping (`lyrics.py`).** Root cause: a context
manager swaps *module globals* per `?player=` request, guarded by one lock; racy
under concurrent requests for different players.

**Cross-cutting:** **no automated tests anywhere**, so all of the above are
unverified by the codebase itself — the single biggest risk multiplier.

## 6. Worth keeping (solid enough to carry over largely as-is)

- **Provider abstraction** (`providers/base.py` + implementations): clean
  interface, retry/backoff, per-source scoring. The individual endpoint clients
  encode hard-won knowledge (Musixmatch token refresh + 12 s rate limit, NetEase
  scoring thresholds, ACRCloud offset math).
- **RTP/RFC-8285 identity parsing** in `udp_capture.py`: the MA-identity
  extension decoding is the right idea even if the buffering around it is buggy.
- **Client-side "flywheel clock"** (`lineSync.js`/`wordSync.js`): monotonic
  interpolation with drift snapping and EMA smoothing is a genuinely good sync
  design and largely self-contained.
- **Per-song multi-provider lyrics DB schema** (line + word sync, preferred
  provider, manual instrumental, word-sync offset): a good persistence model.
- **Word-sync format parsers** (RichSync, YRC) once isolated and test-covered.
- **Config precedence model** (env → JSON → defaults) is sound; the problem is
  duplication, not the idea.

## 7. Minimal viable core (smallest version that still works)

A single-player, single-recognizer, single-metadata-source slice:

- **Audio in:** `udp_capture.py` reduced to RTP-only, one player, simple
  reorder-by-sequence (drop the raw-PCM path and multi-player demux initially).
- **Recognition:** `engine.py` + `shazam.py` only (ShazamIO). Drop ACRCloud and
  local FP from the core; reintroduce as optional plugins behind one interface.
- **Track source:** `metadata.py` reduced to *either* recognition *or* Music
  Assistant WebSocket — not the 4-way Windows/Spotify/Reaper/MA reconciliation.
- **Lyrics:** `lyrics.py` + `providers/base.py` + **LRCLIB** (no auth, line-sync)
  as the only required provider; Musixmatch optional for word-sync. Keep the
  per-song JSON DB schema but one selection rule for both line and word sync.
- **Server/UI:** `server.py` trimmed to `/lyrics` and `/current-track`; keep
  `main.js` + `lineSync.js` + `wordSync.js` + `index.html` and the polling model.
- **Support kept:** `config.py` (single config path), `logging_config.py`,
  `version.py`.
- **Dropped from core:** Reaper, Windows Media, local fingerprinting, album-art
  and artist-image subsystems, tray/mDNS/SSL niceties, QQ/NetEase/Spotify-proxy
  providers, dual buffers, slideshow/fonts/settings UI — all reintroducible later
  as optional modules.

Net: roughly `udp_capture → engine+shazam → metadata(1 source) → lyrics+LRCLIB →
server(2 routes) → JS sync UI`, with a test harness added around each seam — far
smaller than the current ~30k-LOC, 51-module surface.
