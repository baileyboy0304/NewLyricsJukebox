# NewLyricsJukebox — Project Rules

NewLyricsJukebox (NLJ) is a **Home Assistant add-on** that displays time-synced
lyrics for audio playing through Music Assistant. This is a clean rebuild of an
app that had accumulated bugs and inconsistent patterns from multiple AI
authors. The guiding principle is: **keep it simple.** See `AUDIT.md` for the
old code's structure and bugs.

## The ecosystem

NLJ is the **recognition + lyrics engine** at the centre of a four-project
ecosystem. Its job is to turn "some audio is playing on some speaker" into
"these lyrics, on this line, at this millisecond" and serve that to whatever
display device wants it. The aim is **synchronised lyrics, metadata and
artwork to any speaker for any source.**

Repos / projects:

1. **`baileyboy0304/newlyricsjukebox`** — this repo. HA add-on. Receives UDP
   audio on port `6056`, runs recognition (Shazam → ACRCloud) when needed,
   talks to Music Assistant over WebSocket for queue metadata, fetches lyrics
   from LRCLIB / Musixmatch / NetEase / QQ, and serves a browser UI + REST API
   on port **9014**. Logs at logger names `recognition.*`, `lyrics`,
   `music_assistant`, `newlyricsjukebox`.

2. **`baileyboy0304/ha-udp-lyrics-player`** — HA custom integration. Creates
   **dummy "sendspin" media_player entities** that can be added to Music
   Assistant / sendspin groups. When MA pushes audio to one of these dummies,
   the integration resamples it to 16 kHz mono PCM and forwards it to NLJ over
   RTP/UDP, with RFC 8285 header extensions carrying `player_name` and
   `player_id`. This is how arbitrary MA-routed audio reaches the recogniser
   without needing a physical mic.

3. **`baileyboy0304/guition-v1-sendspin-groupvol-mic`** — ESPHome firmware
   bundle for physical "sendspin" devices:
   - **`sendspin.yaml`** (Guition JC3636W518, 1.8" round 360×360, ESP32-S3) —
     display + mic + rotary encoder, sends UDP mic audio.
   - **`waveshare_34/`** (Waveshare ESP32-P4 3.4" round 800×800) — bigger
     display, no encoder, ES7210 ADC mic.
   - **`atom_echo/`** (M5 Atom Echo) — mic-only; advertises as a sendspin
     media_player so MA grouping works, but has no screen.
   - **`companion-chip/`** — secondary ESP32 acting as a transparent I2S
     repeater between the S3 and an external DAC (PCM5100A).
   - **`lyrics-mic-bridge/`** — HA custom integration. Bridges a real speaker
     (e.g. a Sonos Move on MA, or a speaker started via Alexa outside MA) to
     one or more ESPHome mic devices. It tells the mic when to start/stop
     streaming so we don't flood the network with silent UDP, and tells NLJ
     "this mic is listening to that speaker" via `source_player=` on the REST
     API so NLJ can prefer the speaker's MA metadata over the mic's recognition.

Data path for a typical "Sonos Move plays Spotify Connect" case:
```
Sonos Move (audio)
  ├─ sound waves → ESP32 mic (sendspin / atom_echo)
  │     → mic_udp_streamer.h → RTP/UDP 16 kHz mono → NLJ :6056
  └─ MA state (title/artist/position) ─ WebSocket ─→ NLJ
                                          │
lyrics-mic-bridge: "mic X is listening to speaker Y"
                                          ↓
                NLJ classify_source_mode(Y) decides:
                  queue   → use MA metadata (no recognition)
                  stream  → use UDP recognition
                                          ↓
                Lyrics providers (parallel, two-pass select)
                                          ↓
                REST → browser / ESP display (poll + flywheel clock)
```

## How this app works (the spine)

One decision drives everything — `classify_source_mode(player)`
(`classify.py:24`):

- **Queue-based** (Spotify-from-MA, Apple Music, local media, Spotify Connect
  *when MA exposes the track*): no recognition. Use Music Assistant metadata +
  position for immediate lyrics lookup and sync.
- **Stream-based** (radio, anything MA can't describe): run UDP audio
  recognition through the Shazam → ACRCloud chain using the "3 attempts before
  locked" method; target sync + metadata within ~4–6 s.

The discriminator (strongest signal first):
1. `media_type == "radio"` → stream.
2. `active_source_name` ends with "Queue" → queue, else stream.
3. `active_source_id` == queue/player id → queue, else stream.
4. Stale (not playing, position not updated ≥ 5 s) → stream.
5. Default → queue.

**Important nuance**: in `server.py current_track()` the
metadata-first branch (line ~478) uses MA's track **even when classify returns
"stream"**, as long as `state.title` is set and it's not radio. Recognition is
only started when MA has no usable metadata. So a properly-reporting Spotify
Connect track must take the metadata path, not the recogniser. The `mode`
field on the runtime track reflects classify's verdict (so the UI knows whether
seek is allowed), but `rec_stream` will be `None` for any metadata-driven
track.

Pipeline: `MA / UDP-recognition → metadata → lyrics lookup (all providers) →
per-song JSON DB → server (REST) → browser (polling + flywheel clock)`.

## Module map

| File | Role |
|------|------|
| `main.py` | Entry point. Init UDP capture, MA client, controller; start Hypercorn on 9014. |
| `server.py` | `Controller` + Quart HTTP API. Player selection, lease-driven supervisor, recogniser lifecycle, lyrics fetch orchestration. |
| `classify.py` | The spine — `classify_source_mode()`. |
| `config.py` | Single config source: `/data/options.json` → env → defaults. |
| `lyrics.py` | `LyricsService` — parallel provider fetch, two-pass selection, per-song JSON DB. |
| `music_assistant.py` | Trimmed MA WebSocket client. `get_player_state()` → `PlayerState`. |
| `ma_models.py` | `PlayerState`, `DetectedPlayer` dataclasses. |
| `text_clean.py` | Title/artist normalisation (strip version noise, features). |
| `logging_config.py` | One-shot logger setup. Console always; optional rotating file via `NLJ_LOGS_DIR`. |
| `providers/` | `lrclib.py`, `musixmatch.py`, `netease.py`, `qq.py` + `base.py`. |
| `recognition/` | `engine.py` (`PlayerRecognizer` + `LockTracker`), `shazam.py`, `acrcloud.py`, `udp_capture.py`, `result.py`. |
| `tests/` | `test_classify.py`, `test_recognition_lock.py`, `test_providers.py`, `test_controller.py`, `test_rtp_parse.py`. |

REST endpoints (server.py): `/`, `/health`, `/players`, `/current-track`,
`/lyrics`, `/bad-match`, `/lyric-offset`, `/suppress-lyrics`, `/transport`,
`/players/<key>/rename`.

## Hard rules

- **Keep it simple.** The simplest thing that works. No abstraction until
  something is repeated 3+ times.
- **No new dependencies without asking.**
- **One way to do each job.** If you're about to write a second variant of
  something that already exists, stop and ask. (The old code's mess was
  duplicate config/state/selection paths — don't recreate that.)
- **Test each seam as you build it.** The old code had zero tests. Add a
  minimal test around: RTP parse, recognition lock cycle,
  `classify_source_mode()`, provider lookup.
- **No settings or config UI.** All configuration comes from the Home
  Assistant add-on options (`/data/options.json` → env → defaults) via a
  single `config.py`.
- **This must stay a working Home Assistant add-on.** Never break the add-on
  packaging (`config.yaml`/`config.json`, `Dockerfile`, `run.sh`, options
  schema). Web UI on port **9014**. Every user-facing setting must appear in
  the add-on options schema.

## Recognition

- Cloud engines only: **Shazam** (primary) + **ACRCloud** (fallback /
  optional priority refinement). Persist the ACRCloud daily-quota counter
  (the old one reset on restart — a bug).
- **Local fingerprinting is permanently excluded.** Never bring over or
  reintroduce `local.py`, `daemon.py`, or `sfp-cli`.

## Lyrics

- Providers: LRCLIB, Musixmatch, NetEase, QQ — with the existing priorities,
  parallel fetch, and two-pass selection. (**Spotify was removed**: its
  lyrics endpoint is locked behind a rotating TOTP anti-bot, and Musixmatch —
  which is what Spotify's lyrics come from — covers the same content. Don't
  reintroduce it.)
- Keep the per-song JSON DB schema.

## Players & UI

- **Multi-player:** detect all incoming RTP streams for the selection UI,
  but only run the recognition/lyrics pipeline for the selected player. Use
  proper per-player state objects — never swap module globals per request
  (that was a race-condition bug).
- **UI, no settings:** player-select modal (list detected players),
  now-playing (track, artist, thumbnail only), 3 lyric lines (previous /
  current / next), transport controls + progress bar with a play/pause
  toggle that reflects playback state. Word-level animation and full album
  art come later — keep it to 3 lines for now.

## Source-of-truth precedence

Authoritative ranking when choosing a metadata source:
1. **MA queue metadata** (best — seekable, exact position).
2. **MA external-source metadata** (Spotify Connect track surfaced by MA —
   not seekable from our side, but title/artist/position trusted).
3. **`source_player=` association** from `lyrics-mic-bridge` (read THAT
   speaker's MA state).
4. **Audio recognition** (Shazam → ACRCloud) — last resort. Used only when
   MA has nothing usable, or `media_type == "radio"`.

Recognition is **not preferred**: it has API quotas, network latency, and
sync drift. Prefer MA metadata whenever available.

## Build / run

- `pip install -r requirements.txt`
- `python main.py` (reads `/data/options.json` if present, else env / defaults)
- HA add-on: `Dockerfile` + `run.sh` + `config.yaml`. Port `9014` exposed.
- Tests: `pytest tests/`.
