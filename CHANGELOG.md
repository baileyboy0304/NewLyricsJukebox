# Changelog

## 1.0.25

- **Radio with MA-enriched metadata is recognized, not trusted.** Smooth Radio
  now feeds MA enough info to resolve each song to a *library track*
  (album/duration), so the queue item looked like a plain track and radio
  detection missed it — the app stopped Shazam recognition and followed MA's
  (mismatched/lagging) station metadata, e.g. recognizer locked on "Emotion"
  while MA showed "Shallow". `get_player_state` now lets **radio win** across the
  queue `media_item` and the player's `current_media` (the player is still on a
  radio stream even when the now-playing is enriched), so it stays in recognition
  mode and the follow-playing-player step doesn't hijack it.
- **Added an `ma-class` diagnostic log** (`media_type` / `active_source` /
  `playing`, deduped per change) to make radio/Connect routing decisions traceable.

## 1.0.24

- **Fix switching Spotify Connect → radio leaving the app stuck on the old
  track.** When you change source, MA can leave the previous Spotify Connect
  player lingering as "playing" with its stale track. After 1.0.23 the
  "follow the actually-playing MA player" step latched onto that stale player and
  recognition for the new radio never started. Now that follow is skipped when the
  player we are actually listening to is itself on radio — recognition wins.
- **Migrate recognition off a dead selected stream.** The respeaker reconnects
  with a new RTP SSRC on a source change, so the previously selected stream goes
  silent but lingers in the table. `_resolve_stream_key` now prefers the exact
  stream only while it is live, otherwise migrates to the freshest stream for the
  same identity — so recognition binds to the stream that actually has audio.

## 1.0.23

- **Spotify Connect metadata/album art updates immediately, not after ~10s.** The
  "follow whichever MA player is actually playing" fallback — for grouped/external
  setups where the track lives on a coordinator/Connect player, not the player our
  RTP stream resolves to — was gated to *auto* mode only. Since the browser polls
  with a *selected* player, a natural Spotify Connect track change was missed and
  the app waited for the next recognition (~10s). It now runs for a selected
  player too, so MA metadata is used as soon as it changes. Radio is still
  excluded (`_is_radio`) and falls through to recognition as before.

## 1.0.22

- **A single rogue recognition no longer disturbs the lock or the lyrics.**
  Previously, one bad offset *while converging* (e.g. Wham! "Everything She Wants"
  reading 53.2s then a rogue 38.3s) reset the baseline and jumped the served
  position backwards, restarting the whole lock. Now a single off-baseline read —
  whether converging or locked — is `held` (`POSITION OUTLIER (held, awaiting
  confirmation)`): it doesn't move the position or break the lock streak. Only
  `relock_position_after` reads that agree with *each other* on a new timeline
  count as a real shift and re-base. So `53.2 → 38.3(rogue) → 65.2` now continues
  the lock as steps 1→2 instead of thrashing.

## 1.0.21

- **Configurable re-acquire threshold (`relock_position_after`, default 2).** The
  number of consecutive agreeing recognitions on a new timeline needed to break
  the lock and re-acquire is now its own add-on option, separate from
  `lock_position_after`. At the default of 2 (~12s at a 6s recognition cadence) a
  radio skip auto-corrects faster than the initial lock requires.

## 1.0.20

- **Break the position lock on a sustained timeline shift (radio skip/rebuffer).**
  Previously, once `POSITION LOCKED`, every later recognition was `IGNORED`
  forever — so when a radio stream jumped (e.g. Rod Stewart "Baby Jane" leaping
  from ~60s to ~136s and staying there), the lyrics stayed frozen on the stale
  position. A *single* off-baseline read is still ignored (chorus confusion), but
  `lock_position_after` consecutive reads that agree with each other on a NEW
  timeline now break the lock and re-acquire (logged `POSITION RE-ACQUIRED
  (sustained shift, position re-locked)`).

## 1.0.19

- **Log the current synced-lyric line the server is serving** (`lyric-line
  player=... pos=...s current='...'`), one line per transition. This makes the
  server's notion of the current line + position directly comparable to the
  browser/Chrome console and the recognized position, to debug sync.

## 1.0.18

- **Fix the recognizer freezing after a radio station change.** Switching
  stations makes the respeaker reconnect with a *new* RTP SSRC, so the previous
  stream goes silent but lingers in the table with the same MA id/name.
  `find_stream`/`first_active_stream` returned the first match in insertion order
  — the dead stream — so the recognizer stayed bound to it, got no audio, and
  paused (lyrics stopped). They now return the *freshest* matching stream, so the
  recognizer migrates to the live one automatically.
- **Restore the original position/lock logging.** Each recognition now logs a
  single line showing the engine, track, `Offset`, `Latency`, `Current`
  position, `Skew` (time/frequency) and the lock state — matching the original
  format so the "3 attempts before locked" cycle is visible:
  `POSITION LOCKED` (first read accepted) -> `LOCKING (1 of 3)` ->
  `LOCKING (2 of 3)` -> `LOCKING (3 of 3) - LOCKED` -> `IGNORED`. A song change
  logs `Song changed to: <artist> - <title> @ <pos>s`.
- **Lock cycle matches the original semantics.** `LockTracker` now accepts and
  displays the first recognition immediately, then requires `lock_position_after`
  (default 3) *consistent confirmations* before freezing the position; an outlier
  resets the streak (`RE-LOCKING`) instead of locking onto a bad reading. This is
  what corrects a wrong initial position (e.g. a chorus-confused offset) instead
  of letting it stick. When `lock_position` is off, position simply tracks the
  latest recognition (`TRACKING`).

## 1.0.17

- **Radio now uses UDP recognition, not the station name.** For radio sources MA
  reports the *station* as the title (e.g. "Smooth Radio (London, UK) 48k aac+")
  with the artist, which produced nonsense lyric lookups. metadata-first now
  excludes `media_type == "radio"`, so radio falls through to Shazam recognition
  to identify the actual song. Real tracks (queue, Spotify Connect) still use MA
  metadata instantly.

## 1.0.16

- **Stop piling up recognizers (the "bombarding"/jumping/"continues after pause"
  bugs).** The respeaker reconnects with a new RTP SSRC each time, and every new
  stream started a NEW recognizer while old ones kept running — many recognizers
  hammered Shazam in parallel, got rate-limited (`timed out after 10s`), and old
  stale results popped up out of order. Now AT MOST ONE recognizer runs (for the
  selected stream); switching streams or entering metadata mode stops the rest.
- **Never recognize stale audio.** `get_audio` now requires a full window of
  FRESH audio since the last read, so a dead/paused stream stops recognizing the
  same buffered clip (it returns no audio → the recognizer idles → is stopped).
- **Faster updates for grouped / Spotify Connect (the 7–8s lag).** In Auto mode,
  when the resolved player has no metadata, follow whichever MA player is
  actually playing (handles RESPEAKERGROUP / external sources), so MA metadata is
  used immediately instead of slow recognition.

## 1.0.15

- **Metadata-first: instant track updates (fixes the ~20s lag).** Music Assistant
  reports the playing track even for external sources (Spotify Connect), so the
  app now uses MA metadata immediately whenever MA has a title — instead of
  waiting for Shazam recognition. Recognition is now only a FALLBACK for sources
  MA can't describe (e.g. radio with no now-playing). Result: the app updates as
  fast as the MA player. Seeking stays queue-only; external streams are
  elapsed-only. Recognition no longer runs for players MA already describes.

## 1.0.14

- Console logging: removed the noisy ~1s `poll` heartbeat. Only change-based
  lines remain — `metadata` (track change), `lyrics` (availability), `line`
  (active lyric line), plus `playstate` and the startup `app started` line.
- Lowered the recognition network timeout from 20s to 10s.

## 1.0.13

- **Fix the multi-minute startup stall in recognition.** The first outbound
  Shazam API call (DNS + internet) could hang for minutes right after boot — the
  add-on connected to Music Assistant instantly (a LAN IP, no DNS) but the first
  internet call stalled until connectivity/DNS settled. Symptom: a single
  recognize() blocked ~5 min, then returned a result for the boot-time audio (a
  song that had already finished) and everything caught up. Recognition calls
  now have a 20s timeout, so a startup network hang can't block a cycle; the loop
  retries and self-heals as soon as the network is ready. (Pairs with the 1.0.12
  Shazam warmup and per-cycle outcome logging.)

## 1.0.12

- **Recognition visibility (diagnose "first boot doesn't detect").** The engine
  now logs each recognition outcome (throttled): `Recognize <key>: no match
  (audio level=…)`, `no audio (stream idle/too short)`, plus the existing
  `New song …` on success. Previously only a successful match logged, so a
  non-matching cold boot looked silent/broken. `current-track` also logs
  `mode=stream (recognizing, stream=…)` while waiting for the first match.
- **Faster first detection.** Shazam's core (~5s one-time init) is now warmed up
  at startup in the background, so the first real recognition isn't delayed by it.
- No changes to the recognition/detection logic itself — additive only.

## 1.0.11

- **Per-provider enable/disable in add-on options** (`provider_spotify`,
  `provider_lrclib`, `provider_musixmatch`, `provider_netease`, `provider_qq`).
  QQ defaults to OFF — its endpoint is currently returning HTTP 500 and only
  added log noise.
- **Fix "no console logging".** Static JS/CSS were browser-cached, so a rebuilt
  add-on could keep serving the old `app.js` (without logging). Asset URLs are
  now cache-busted per version (`?v=<version>`) and static responses send
  `Cache-Control: no-cache`. The page also sets `window.NLJ_VERSION`.
- **More useful console logs.** Added an `app started` line (shows the running
  version) and a ~1s `poll` heartbeat logging `source`, server position vs the
  local flywheel position, and `is_playing` — so browser/app timing can be
  compared directly (and a stalled timecode is visible as a non-advancing
  `server_pos`). Existing metadata / playstate / lyrics / line logs unchanged.

## 1.0.10

- **Fix regression in 1.0.9 that stopped recognition entirely.** RTP streams
  advertise an MA id via the RTP header extension, and the 1.0.9 transient-None
  guard treated any id as "MA-backed", so a stream got stuck in queue mode with
  no metadata and recognition never started. The guard now only keeps queue mode
  for a player that was ALREADY producing a real queue track; never-seen players
  / RTP streams correctly enter stream/recognition mode.

## 1.0.9

- **Lyrics now appear immediately (fixes the ~10s+ delay).** `fetch()` applied
  results only after every provider finished, so a slow/failing provider (QQ
  retrying a 500 for ~25s) blocked display even though Musixmatch/LRCLib had
  lyrics in <1s. Lyrics are now fetched in the background and applied
  incrementally — the first provider to return shows immediately, better
  providers upgrade it.
- **Stale lyrics cleared on track change.** The server clears lyrics the moment
  the track changes (returns `pending`), and the browser blanks the lines as
  soon as new metadata arrives — no more leftover lyrics from the previous song.
- **No recognition in queue/Spotify mode.** A transient Music Assistant read no
  longer flips an MA-backed player into stream/recognition mode (which started
  the respeaker recognizer and contaminated the timecode with a second
  timeline). This should fix the timecode freezing in Spotify mode.
- **Chrome console logging** (defect aid): the browser logs `[NLJ HH:MM:SS.mmm]`
  lines for metadata changes, play-state changes, lyric availability, and each
  active-line change — timestamped to line up with the add-on log. The server
  also logs `current-track player=… mode=… title=…` on each change.

## 1.0.8

- Add Home Assistant **ingress**: the UI now appears in the sidebar
  ("Lyrics Jukebox"). Removed `host_network` (it blocks the ingress proxy) now
  that the real cause of the earlier inaccessibility — the event-loop freeze —
  is fixed. Ports `9014/tcp` (direct access) and `6056/udp` (audio) stay mapped;
  player identity comes from the RTP SSRC/extension, so audio works without host
  networking. Reverts the host_network/firewall churn from 1.0.4/1.0.5.

## 1.0.7

- Fix `/current-track` 500 (`TypeError: unhashable type: 'PlayerQueue'`): this
  Music Assistant client version returns a `PlayerQueue` object from
  `get_active_queue()`, not an id string. Normalize to (queue, queue_id) in both
  state reads and seek.
- Fix `/lyrics` 500 (`KeyError` on an `as_completed` wrapper): parallel provider
  fetch no longer relies on identifying the originating task; each task returns
  `(provider, result)`. Regression test added.

## 1.0.6

- **Fix the web server freezing (the real cause of the unreachable UI).** The
  recognition loop could busy-loop and starve the asyncio event loop, so
  Hypercorn accepted TCP connections but never answered — `curl localhost:9014`
  hung. Two fixes: the loop now always `await`s/sleeps each cycle (never
  busy-loops, even with no stream/audio), and recognizers are only started for a
  real detected RTP stream (the MA player id is not a stream key).
- Demux RTP streams by SSRC (stable per session) so identity-bearing and plain
  packets from the same sender form one logical stream, with the MA name/id
  filled in when they arrive (fixes the same source showing as two streams).

## 1.0.5

- Web UI still timed out on 1.0.4 even with `host_network: true`. Re-add the
  `ports:` declaration (`9014/tcp`, `6056/udp`): on Home Assistant OS the host
  firewall only opens a port that is declared in `ports:`, even for host_network
  add-ons. Keep `host_network: true` so the UI is on the host IP and UDP keeps
  the real source IP.

## 1.0.4

- Fix web UI unreachable at `http://<host>:9014` (ERR_CONNECTION_TIMED_OUT).
  Restored the original working network model: `host_network: true` with no
  ingress/port-mapping. Removing host_network in 1.0.2/1.0.3 stopped exposing
  port 9014 on the host. The UI is again reachable directly at the host IP, and
  RTP audio arrives on UDP 6056 with the real source IP, exactly like the
  original add-on.

## 1.0.3

- Log the running version at startup (`=== NewLyricsJukebox version X.Y.Z ===`)
  and add a `/health` endpoint returning the version, so it's possible to confirm
  which build a Home Assistant install is actually running. (Add-on `config.yaml`
  network changes only take effect on Update/Rebuild, not a plain restart.)

## 1.0.2

- Fix inaccessible web UI (sidebar ingress and direct URL): removed
  `host_network`, which is incompatible with Home Assistant ingress. The add-on
  now runs on the standard add-on network with `9014/tcp` (UI) and `6056/udp`
  (RTP audio) mapped explicitly. UDP player identity comes from the RTP
  SSRC/extension, so port-mapped UDP works without the host network.

## 1.0.1

- Fix blank page under Home Assistant ingress: assets and API calls now use
  relative URLs so the UI works both behind the ingress path prefix and at the
  server root (direct `:9014`).

## 1.0.0

- Clean rebuild of NewLyricsJukebox (see `REBUILD_NOTES.md`).
- `classify_source_mode()` spine: queue-based (Music Assistant metadata) vs
  stream-based (UDP audio recognition).
- RTP-only UDP capture with per-player demux; Shazam → ACRCloud recognition with
  the "3 attempts before locked" cycle and persisted ACRCloud daily quota.
- All lyrics providers kept (LRCLIB, Spotify, Musixmatch, NetEase, QQ) with
  two-pass selection and the per-song JSON DB; per-player state (no module
  globals).
- Web UI on port 9014: player-select modal, now-playing, 3-line lyrics,
  transport bar with flywheel-clock sync.
