# NewLyricsJukebox — Project Rules

NewLyricsJukebox is a **Home Assistant add-on** that displays time-synced lyrics for
audio playing through Music Assistant. This is a clean rebuild of an app that had
accumulated bugs and inconsistent patterns from multiple AI authors. The guiding
principle is: **keep it simple.** See `AUDIT.md` for the old code's structure and bugs.

## How this app works (the spine)

One decision drives everything — `classify_source_mode(player)`:

- **Queue-based** (Spotify, local media): no recognition. Use Music Assistant metadata
  + position for immediate lyrics lookup and sync.
- **Stream-based** (radio, Spotify Connect): run UDP audio recognition through the
  Shazam → ACRCloud chain using the "3 attempts before locked" method; target sync +
  metadata within ~4–6 s.

Pipeline: `MA / UDP-recognition → metadata → lyrics lookup (all providers) →
per-song JSON DB → server (REST) → browser (polling + flywheel clock)`.

## Hard rules

- **Keep it simple.** The simplest thing that works. No abstraction until something is
  repeated 3+ times.
- **No new dependencies without asking.**
- **One way to do each job.** If you're about to write a second variant of something that
  already exists, stop and ask. (The old code's mess was duplicate config/state/selection
  paths — don't recreate that.)
- **Test each seam as you build it.** The old code had zero tests. Add a minimal test
  around: RTP parse, recognition lock cycle, `classify_source_mode()`, provider lookup.
- **No settings or config UI.** All configuration comes from the Home Assistant add-on
  options (`/data/options.json` → env → defaults) via a single `config.py`.
- **This must stay a working Home Assistant add-on.** Never break the add-on packaging
  (`config.yaml`/`config.json`, `Dockerfile`, `run.sh`, options schema). Web UI on
  port **9014**. Every user-facing setting must appear in the add-on options schema.

## Recognition

- Cloud engines only: **Shazam** (primary) + **ACRCloud** (fallback). Persist the
  ACRCloud daily-quota counter (the old one reset on restart — a bug).
- **Local fingerprinting is permanently excluded.** Never bring over or reintroduce
  `local.py`, `daemon.py`, or `sfp-cli`.

## Lyrics

- Keep **all** providers: LRCLIB, Musixmatch, Spotify, NetEase, QQ — with the existing
  priorities, parallel fetch, and two-pass selection.
- Keep the per-song JSON DB schema.

## Players & UI

- **Multi-player:** detect all incoming RTP streams for the selection UI, but only run
  the recognition/lyrics pipeline for the selected player. Use proper per-player state
  objects — never swap module globals per request (that was a race-condition bug).
- **UI, no settings:** player-select modal (list detected players), now-playing (track,
  artist, thumbnail only), 3 lyric lines (previous / current / next), transport controls
  + progress bar with a play/pause toggle that reflects playback state. Word-level
  animation and full album art come later — keep it to 3 lines for now.

## Project layout

- (Fill in build/run commands and the module map here as the rebuild settles, so future
  sessions don't have to rediscover them.)
