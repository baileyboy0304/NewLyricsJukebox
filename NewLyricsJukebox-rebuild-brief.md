# NewLyricsJukebox — Rebuild Implementation Brief (for Claude Code)

You have `AUDIT.md` describing the existing app and the old source tree, and `CLAUDE.md`
with the standing project rules. This brief tells you what to build for the simplified
rebuild. **Read `AUDIT.md` and the old code, then build straight through the phases below —
no approval gate, just go.**

---

## 0. Strategy & ground rules

- **Fresh build, old code as reference.** Work on a new `rebuild` branch of this repo.
  Move the existing code into a `legacy/` directory at the start so you can read it while
  building the new clean tree at the repo root; delete `legacy/` before the rebuild branch
  is finalised. `main` stays untouched as the fallback. Bring over only the modules listed
  under *Keep* below, largely unchanged. Do **not** copy the modules flagged as buggy in
  `AUDIT.md` §5 — rewrite those.
- **No approval gate — just build.** Don't stop to get a plan signed off. Keep a running
  old→new file map in your rebuild notes (kept / rewritten / new, and the old source each
  derives from) so the work stays traceable, but proceed straight through the phases.
- **Guardrails (these are the CLAUDE.md rules — honour them throughout):**
  - Simplest thing that works. No abstractions until something is repeated 3+ times.
  - No new dependencies without asking.
  - One way to do each job. If you find yourself writing a second variant of something,
    stop and ask.
  - Add a minimal test around each seam as you build it (see §6). The old code had **no
    tests** — that is the single biggest risk we are fixing.
  - No settings/config UI. Config lives only in the Home Assistant add-on options.

---

## 1. Target architecture

The branch on playback type is the spine of the app:

```
              Music Assistant (WebSocket: player state, metadata, transport control)
                          │
                          ▼
              classify_source_mode(player)  ── single decision function ──┐
                          │                                               │
        queue-based (Spotify / local media)            stream-based (radio / Spotify Connect)
                          │                                               │
            METADATA MODE  │                                 RECOGNITION MODE
   title/artist/position/thumbnail from MA      UDP audio → Shazam → 3-attempt lock
   immediate lyrics lookup, sync from MA pos    derive title/artist/offset, target 4–6 s
                          │                                               │
                          └───────────────┬───────────────────────────────┘
                                          ▼
                        lyrics lookup (existing providers, priorities)
                                          ▼
                        per-song JSON DB (keep existing schema)
                                          ▼
                        server.py  →  /players /current-track /lyrics /transport
                                          ▼
                        browser: polling (~100 ms) + flywheel clock
                        player-select modal · now-playing · 3-line lyrics · transport
```

`classify_source_mode()` must be one small, well-named function. Determine the right MA
signals (media type / source / whether a seekable queue with reliable position exists)
from the existing `music_assistant.py` client and `AUDIT.md` §2.1. Make it easy to fix
in one place.

---

## 2. Keep / rewrite / drop

**Keep largely as-is** (from `AUDIT.md` §6 — these encode hard-won knowledge):
- `providers/base.py` + the lyrics provider implementations and the priority-ordered
  lookup engine. *I want the current lyrics lookup kept — all providers, priorities,
  parallel fetch, two-pass selection.* (Word-sync data may still be fetched/stored; the
  UI just won't render it yet — see §4.)
- RTP / RFC-8285 player-identity parsing from `udp_capture.py` (the identity decoding,
  not the jitter buffer).
- The client-side flywheel clock (`lineSync.js`) — reuse for sync even though only 3
  lines are shown.
- The per-song multi-provider lyrics DB schema.
- The config precedence idea (env → JSON → defaults), unified to **one** path.

**Rewrite** (from `AUDIT.md` §5 — do not copy these):
- UDP jitter/sequence buffering (Cluster A): clean RTP-only capture with per-player
  demux for detection/selection, sequence reorder, timeout on unknown players, no
  reaching into private attributes.
- Recognition/position state (Cluster B): one clear position model; keep the
  "3 attempts before locked" cycle but make it robust to skips and skew-rejection.
- Per-player state (Cluster D): replace module-global swapping with proper per-player
  state objects. **Required** — we have multiple players (see screenshot).

**Recognition engines — include all cloud recognizers from the start:**
- **Shazam (ShazamIO)** primary + **ACRCloud** fallback, via the existing chain in
  `engine.py` (keep the priority/fallback ordering, skew rejection, and ACRCloud offset
  math). Persist the ACRCloud daily-quota counter this time (the old one reset on restart).
- **Local fingerprinting is permanently excluded** — do not bring over `local.py`,
  `daemon.py`, or sfp-cli, and do not add it later.

**Lyrics providers — keep all of them:** LRCLIB, Musixmatch, Spotify, NetEase, QQ, with the
existing priorities, parallel fetch, and two-pass selection.

**Drop from the core** (reintroducible later as optional modules, except where noted):
- Reaper, Windows Media Session, full album-art & artist-image subsystems, tray icon,
  mDNS, slideshow, fonts, the settings/state UI, and the dual audio buffers (keep one).
- Local fingerprinting — permanently, see above.

---

## 3. Backend requirements

1. **Source-mode branching** via `classify_source_mode()` as in §1.
2. **Metadata mode** (queue-based): pull title, artist, album-art thumbnail URL, position
   and duration from MA; look up lyrics immediately; sync using MA position + flywheel
   clock. No recognition runs.
3. **Recognition mode** (stream-based): on track start, run UDP recognition for the
   selected player through the Shazam→ACRCloud recognition chain; use the existing
   **3-attempts-before-locked** method to converge and lock; derive title/artist/offset
   from recognition; then run the normal lyrics lookup.
   Target sync + metadata within **~4–6 s**.
4. **Multi-player detection & selection:** detect all incoming RTP streams and expose them
   for selection (id, friendly/MA name, IP, SSRC, assigned vs unassigned). "Auto" picks
   the first live player. `?player=<name>` pins a selection. Only the selected player runs
   the recognition/lyrics pipeline.
5. **Transport control** via the MA WebSocket: play, pause, next, previous, and seek.
   Expose play/pause/next/prev/seek through a `/transport` endpoint (or similar). Report
   current playback state so the UI can toggle the play/pause icon. **Seek/scrub only
   applies in queue mode** — in stream mode there is nothing to seek, so the progress bar
   is elapsed-only and scrub is disabled.
6. **Config:** single path only — HA add-on options (`/data/options.json`) → env →
   defaults via one `config.py`. **No UX settings, no `settings.py`/`session_config.py`
   sprawl.** Every user-facing setting must be exposed in the add-on `options`/`schema`
   (since there is no settings UI). Default web port **9014**. Keep Quart/Hypercorn.
7. **Persistence:** keep the per-song JSON DB schema (line-sync per provider, preferred
   provider, manual instrumental, word-sync offset).
8. **Endpoints (minimum):** `/players`, `/current-track`, `/lyrics`, `/transport`. Keep
   the polling model (frontend polls ~100 ms, adaptive to ~1 s when idle).
9. **Home Assistant add-on — must remain a working plugin.** The output ships as a HA
   add-on, exactly as the current code does. Carry over and *update* (don't rewrite from
   scratch) the add-on packaging: the `config.yaml`/`config.json` manifest, the
   `Dockerfile`, and `run.sh` (which maps `/data/options.json` → env and execs the app).
   Requirements:
   - Expose **all** configuration in the add-on `options` + `schema` (there is no settings
     UI, so anything tunable lives here).
   - Map/expose port **9014** in the manifest, and keep the ingress/panel configuration as
     the current add-on has it so the UI appears inside Home Assistant.
   - Propagate the **NewLyricsJukebox** name to the add-on name and slug, and bump the
     version.
   - As the final smoke test, verify the add-on still builds and starts via `run.sh`
     against a sample `options.json`.

---

## 4. Frontend requirements

Keep the current hosting model and the polling + flywheel-clock sync. Simple, clean UX —
**no settings option anywhere.**

- **Player-select modal** matching the supplied screenshot: an "Auto" option (select first
  live player), then a list of detected players showing name, IP and SSRC, each with a
  rename (pencil) and a "Use" action, plus an "Unassigned streams" section for detected-
  but-unnamed SSRCs. Nothing else.
- **Now playing:** current track title, artist, and a small **album-art thumbnail** (from
  metadata only — no full album-art display yet).
- **Lyrics:** three lines only — previous, current, next. No word-level animation yet
  (that's a later addition); render the line-synced current line driven by the flywheel
  clock.
- **Transport bar:** play/pause (icon toggles with playback state), previous, next, a
  timecode/position progress bar that reflects position vs duration and supports scrub in
  queue mode (elapsed-only in stream mode), all wired to the `/transport` endpoint so they
  actually control the player.
- Served on port **9014**.

---

## 5. Rename

Rename the project to **NewLyricsJukebox** and propagate through the new code where
relevant: package/module identity, server name, HA add-on name/slug, user-agent strings,
log/config paths and any DB directory naming. Don't rename third-party endpoints.

---

## 6. Phased execution plan (checkpoint after each phase)

Build incrementally, run after each phase, commit per phase, and confirm before moving on.

- **Phase 0 — Scaffold.** Create the `rebuild` branch and move existing code into
  `legacy/`. Confirm `CLAUDE.md` is in place and lay out the new directory structure. Keep
  the old→new file map in your notes as you go. No approval gate — continue into Phase 1.
- **Phase 1 — Backend skeleton.** `config.py` (single path, port 9014), `server.py` with
  the four endpoints stubbed, and a trimmed MA WebSocket client (player state + metadata +
  transport control only).
- **Phase 2 — Source-mode + metadata path.** Implement `classify_source_mode()` and the
  full metadata-mode flow end to end (MA metadata → lyrics lookup → sync). This is the
  simpler path and proves the spine.
- **Phase 3 — Recognition path.** Clean RTP `udp_capture` with multi-player demux + the
  Shazam engine + 3-attempt lock + per-player state. Verify the 4–6 s target.
- **Phase 4 — Lyrics integration.** Wire in the kept provider lookup and the per-song DB;
  serve line-synced lyrics to `/lyrics`.
- **Phase 5 — Frontend.** Player-select modal, now-playing, 3-line lyrics, transport +
  progress bar with play/pause toggle, polling + flywheel clock.
- **Phase 6 — Rename & smoke test.** Propagate the NewLyricsJukebox rename, remove anything
  unused, and run an end-to-end check on both a queue source and a stream source.

Add a minimal test at each seam as you build it: RTP parse, recognition lock cycle,
`classify_source_mode()`, and provider lookup.

---

## 7. Settled decisions (do not re-litigate)

These are decided — build to them:
- Recognition includes both cloud engines (Shazam primary + ACRCloud fallback) from the
  start; local fingerprinting is permanently excluded.
- All lyrics providers are retained (LRCLIB, Musixmatch, Spotify, NetEase, QQ).
- Multi-player demux is retained (for detection/selection) even though only the selected
  player runs the pipeline.
- Seek/scrub is queue-mode only; stream mode is elapsed-only.
- Output ships as a working Home Assistant add-on (see §3.9).
- The rebuild happens on a new `rebuild` branch with old code moved to `legacy/`, not an
  in-place strip of `main`.
