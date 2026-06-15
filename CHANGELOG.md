# Changelog

## 1.0.63

- **Ecosystem documentation** — `CLAUDE.md` expanded with the full
  four-project map (NLJ, `ha-udp-lyrics-player`, sendspin ESPHome firmware,
  `lyrics-mic-bridge`), module map, REST endpoint catalogue, and the
  source-of-truth precedence (MA queue > MA external > bridge association
  > recognition).
- **`TEST_PLAN.md`** — Spotify Connect / MA Spotify / UDP dummy / radio
  walkthrough with the exact log lines to capture and a failure-signature
  → code-location map.
- **Diagnostic log for source-association** — `current_track()` step 0
  was silently falling through to recognition when MA had the track on
  the associated source player. Now emits one deduped `source-assoc` line
  per change showing exactly what MA returned (`src=none` / `title=…` /
  `mt=…` / `source=…` / `playing=…`), so the failure mode is no longer
  invisible.

## 1.0.62

- **Mic ↔ speaker association: use the speaker's metadata instead of recognising.**
  A microphone listening to a Spotify Connect / Spotify / Apple Music / local-queue
  speaker now shows that speaker's now-playing **directly from Music Assistant** —
  instant, no Shazam, no ACRCloud — when the consumer tells NLJ which speaker the
  mic is hearing. Pass `source_player=<MA player id>` alongside `player=` on
  `/current-track` and `/lyrics`; NLJ reads that player's metadata for the mic
  (keeping the mic's own display name). When the speaker is on **radio** (MA has
  only the station name) or has no metadata, it falls back to **recognising the
  mic's audio** as before. This is opt-in per device — players without an
  association are unaffected (still governed by the grouping check from 1.0.56), so
  there's no risk of a mic being hijacked onto an unrelated speaker.

## 1.0.61

- **Context-aware lyric blanking — fewer false fades on microphone input.** A single
  no-match used to clear the held track immediately (`blank_after_failures` default
  1), so a transient miss mid-song (a quiet passage, or a brief gap when a mic
  restarts its stream) faded the artwork/lyrics and made the next match look like a
  brand-new song — a constant fade/art-reload churn. Blanking is now position-aware:
  - **Mid-song:** tolerate `blank_after_failures` consecutive misses (new default **3**).
  - **Near a song's end** (within `song_end_window` of a known duration, default
    **30 s**): blank after `blank_after_failures_near_end` misses (default **1**), so
    genuine song-ends still fade promptly.
  - Unknown duration (e.g. radio with no length) falls back to the tolerant mid-song
    threshold. All three values are add-on options.

## 1.0.60

- **Faster lyrics with multiple clients: coalesce concurrent fetches for the same
  song.** When two clients watch one player (a phone + an ESP32 display, or
  duplicate/stale player ids), each runtime fetched lyrics independently — querying
  every provider twice in parallel, which tripped rate limits (Musixmatch captcha,
  LRCLib timeouts) and delayed lyrics for the second client by 10–15 s even though
  the first already had them. Same-song fetches now share a single provider sweep:
  the first triggers it, later ones subscribe and receive the same incremental
  results. Rescue/cleaned-search (bad-match) fetches use a different query and are
  never shared.

## 1.0.59

- **Fix lyric "shudder" introduced by the lease engine (1.0.57).** The supervisor
  recomputes a player's position only ~once/second, but the browser polls ~10x/s and
  re-anchors its flywheel on every poll — so it kept getting yanked back to the stale
  per-second position, jittering the active lyric line back and forth. `/current-track`
  now reports a **live position**, extrapolated from when it was computed
  (`PlayerRuntime.position_at`), so each poll sees a smoothly-advancing value (as the
  old per-poll recompute did). Paused tracks are not extrapolated.

## 1.0.58

- **Fix slow recognition start: one recognizer per *stream*, not per *player*.**
  1.0.57 keyed recognizers by player, so several ids that resolve to the same
  device (a configured name + stale SSRCs from earlier reconnects + the live
  stream) each spun up their own Shazam loop on the *same* audio — tripling the
  recognition load and making the first lock take far longer (repeated `timed out
  after 10s`). Recognizers are now deduped by stream: many players pointing at one
  RTP stream share a single recognizer, while genuinely distinct mics still
  recognize in parallel. The supervisor keeps only the streams a leased player is
  actually recognizing on (`PlayerRuntime.rec_stream`) and tears down the rest.

## 1.0.57

- **Lyrics no longer require an open web page — the engine is now lease-driven.**
  Recognition used to run only while a browser polled `/current-track`, so an
  ESPHome display couldn't get lyrics on its own. A server-owned **supervisor** now
  drives the pipeline: any consumer (web page *or* ESPHome display) polling
  `/current-track` or `/lyrics` renews a short **lease** for that player, and the
  supervisor runs recognition for every leased player whose stream is active —
  tearing it down a few seconds after the last poll. The HTTP layer is now a pure
  reader of computed state.
  - **Multiple players recognize in parallel:** recognizers are now per-player
    (keyed by player, not "at most one"), so several mics/displays work at once.
  - **Self-cleaning:** fixes the lingering recognizer after a tab closes, and two
    browsers fighting over a single recognizer.
  - Cost stays bounded: only *watched* + *stream-active* players recognize;
    metadata-only players (Spotify/Connect/grouped) stay cheap.
  - Trade-off: a just-woken display sees a brief (~4–6 s) recognition warm-up.

## 1.0.56

- **An explicitly-selected player is no longer hijacked onto an unrelated speaker.**
  Selecting a standalone device (e.g. the Atom Echo mic) immediately bounced to
  whatever else Music Assistant was playing (e.g. "Cellar Speaker"), and the name
  shown was that other player's. The "follow the player actually playing" step —
  needed for grouped / Spotify-Connect setups — now only follows when it's Auto, or
  when the playing player is the **same as, or grouped/synced with**, the one you
  selected (new `players_related()` check). A standalone pick now recognizes its own
  audio. The displayed name also stays the **selected capture device** rather than
  the borrowed coordinator. An explicitly-selected, still-live stream is likewise no
  longer silently migrated to another stream.

## 1.0.55

- **Blank lyric lines no longer blank the display.** Providers include timestamped
  empty lines for the gaps between sung lines, which were rendered as an empty
  "current" line — the lyrics flashed blank mid-song. Those blank entries are now
  ignored, so the previous line stays shown until the next real line is due.
  Applied at the source, so both the web UI and poll-based clients (ESP32 bridge)
  benefit.

## 1.0.53

- **Lyric-timing offset changes propagate live to all clients on the same song.**
  A +/- nudge in one client is now pushed to every player runtime currently on
  that song, so other clients (e.g. the ESP32 bridge) pick it up on their next
  `/lyrics` poll instead of only at the song's next play — no extra DB read on the
  poll hot path.

## 1.0.52

- **Keep the first-detected lyric variant for a song (fixes wrong/desynced lyrics
  on radio mixes).** Shazam flips the title between e.g. `Babylon` and `Babylon
  (UK Radio Mix)` for the same audio. Since 1.0.50 those correctly count as the
  same song (no position shudder), but whichever flip happened to win the lock
  also became the displayed title — and `Babylon` (album) vs `Babylon (UK Radio
  Mix)` (radio edit) have **different lyric files with different timings**, so
  freezing the wrong one showed lyrics that didn't match the audio even though the
  position was right. The song now keeps its **first-detected** title and lyric
  variant for its whole duration; later reads still refine the position but no
  longer swap the lyric file.

## 1.0.51

- **A Shazam timeout no longer briefly blanks the lyrics (the "blip").** When a
  Shazam recognition call hung and hit the 10s safety timeout, it was treated as a
  no-match; with `blank_after_failures=1` that immediately cleared the held track,
  fading the lyrics/artwork out for a few seconds before the song was re-detected.
  A timeout is a recognition hiccup, not evidence the song ended, so it now holds
  the current track and simply retries next cycle (only genuine no-matches count
  toward blanking).

## 1.0.50

- **Fixed the lyric "shudder" on radio — and the ACR-credit burn behind it.**
  Shazam frequently returns two titles for the *same* audio on alternating cycles
  (e.g. `Babylon` ↔ `Babylon (UK Radio Mix)`, `Angels` ↔ `Angels (Remastered
  2004)`, `Live It Up (Remastered)` ↔ `(Acoustic)`). The strict song-change check
  treated every flip as a brand-new song, so it **reset the position lock every
  few seconds** — the lyrics jumped back and forth — and **spent an ACR credit on
  each flip** (a single thrashing song could burn ~120/hour, which is what
  exhausted the 100/day quota). Song-change detection now ignores version-suffix
  churn (same artist + same title once mix/edit/remaster/live/... noise is
  stripped), so the position stays locked and ACR fires **once per real track**.
- **ACR quota now resets on the UTC day.** ACRCloud's daily counter rolls over at
  00:00 UTC (its usage dashboard is bucketed by UTC date); the local-date reset
  could resume ACR hours early/late. ACR is left alone once the daily limit is hit
  and resumes exactly at the UTC rollover.

## 1.0.49

- **Thumbs-down: hide lyrics for a song with no good match.** A new thumbs-down
  button sits next to the sad-face (bad-match) control. Pressing it means "no good
  lyrics were found" and **hides the lyrics for this song**; the choice is
  persisted. When the song plays again the lyrics stay hidden and the thumbs-down
  is **highlighted**, so you can toggle it off to re-check whether a better option
  has since become available (then toggle it back on if not). Persistence is the
  per-song JSON DB (`suppress_lyrics`) via a new `POST /suppress-lyrics`; the
  +/- provider cycle still works while hidden, so other options remain reachable.

## 1.0.48

- **Musixmatch no longer discards word-synced-only lyrics.** It previously
  returned lyrics ONLY when line-synced *subtitles* were present; if a track had
  RichSync (word-synced) lyrics but no subtitles — common on Spotify-id matches,
  e.g. Madonna – "Cherish" — it logged "No synced lyrics available" and returned
  nothing, so the provider appeared to have no option. Line-synced lyrics are now
  derived from the per-line RichSync timings in that case, so Musixmatch surfaces
  (and the +/- cycle can pick) its lyrics.

## 1.0.47

- **Lyric-sync nudge direction reversed.** `+` now **advances** the lyrics (shows
  them earlier) and `−` **delays** them (later) — the opposite of 1.0.46. (This
  reversal was made for 1.0.46 but missed that merge.)
- **Fixed the PWA manifest icon 404.** The manifest's icon `src` was
  `static/images/icon-192.png`, but a manifest icon path is resolved relative to
  the manifest's own URL (`static/manifest.json`), producing the doubled
  `static/static/images/...` 404 seen in the console. Changed to `images/...` so
  it resolves correctly (also works behind Home Assistant ingress).

## 1.0.46

- **Manual lyric-sync nudge with per-song memory.** A new control sits on the left
  of the transport row (mirroring the provider +/- on the right): a −/value/+
  adjuster that shifts the lyrics relative to the audio in **0.25 s** steps, shown
  as `+0.25s` / `-0.50s` / `1.00s`. `+` delays the lyrics (shows them later), `−`
  advances them (earlier). A **memory** checkbox next to it:
  - When ticked, changing the timing stores the offset on that song.
  - When the song plays again, the remembered offset is reapplied automatically.
  - Unticking memory reverts the song to default timing and deletes the stored
    value.
  The offset is applied to lyric rendering in the browser; persistence uses the
  per-song JSON DB via a new `POST /lyric-offset` endpoint. The memory tick state
  is remembered in the browser (localStorage).

> Note: this release also carries the transport stale-stream fix that narrowly
> missed the 1.0.45 (#43) merge — play/pause no longer errors with "Player <ssrc>
> is not available" after the respeaker reconnects with a new SSRC.

## 1.0.45

- **Player picker no longer shows the same device several times.** A respeaker
  reconnects with a fresh RTP SSRC, leaving stale sibling streams behind, so the
  "Select player" list showed `respeaker_lyrics` once per SSRC. Streams that share
  an identity (Music Assistant player id, or name + source IP) are now collapsed to
  a single entry — the active one, else the most-recently-seen. Genuinely
  different devices stay separate.
- **Play/pause icon now reflects the real state.** When playback stopped or the
  track cleared (paused / between songs), `current-track` returns no title and the
  poll returned early *before* updating the transport icon, so it was stuck showing
  the pause symbol forever. The icon is now reset to "play" in that path.
- **Transport commands no longer fail on a stale stream.** When the respeaker
  reconnects with a new SSRC, the selected stream key goes stale; `transport` was
  then passing that raw SSRC to Music Assistant as the player id ("Player <ssrc>
  is not available"). It now validates the id against MA's player list and, if
  stale, migrates to the live stream's MA player (or whatever MA reports as
  playing) — the same migration `current-track` already did.

## 1.0.44

- **🧪 Smarter same-recording match (ACR priority).** Shazam and ACRCloud often
  label the same recording with different variant suffixes — e.g. Shazam "Lady
  Love Me (One More Time)" vs ACRCloud "Lady Love Me (2003 Remaster)" — which the
  old prefix check rejected as a *different track*, throwing away ACR's position
  and Spotify id. Version noise (remaster / edit / live / radio edit / ...) is now
  stripped from both titles before comparing, so genuine variants match and the
  refinement is used. Genuinely different songs are still rejected. The
  title/artist cleaning is shared (`text_clean.py`) between this check and the
  'bad match' re-search so they always agree.
- **🧪 ISRC-guided lyrics (Shazam).** Shazam doesn't expose a Spotify URI, but it
  usually returns an **ISRC** (the recording's unique code), which we already
  capture. It's now passed to Musixmatch (`track_isrc`) alongside any Spotify id,
  pinning lyrics to the right recording on *every* Shazam match — no ACRCloud
  needed. Providers that don't support ISRC ignore it.

## 1.0.43

- **🧪 "Bad match" button — reject the wrong lyrics version and re-search (Phase 2).**
  A new sad-face button sits next to the +/- lyrics-provider controls. When the
  engine finds the wrong variant of a song (common on radio — an edit/remaster
  matching the album cut's lyrics), press it to re-search. Each press advances to
  the next, more-aggressively-cleaned title variant — stripping version noise
  (`(Radio Edit)`, `- Remastered 2011`, `(Live ...)`, `(From the Film ...)`) and
  then featured artists (`feat. ...`, secondary artists) — and swaps in the
  alternate. The chosen level is remembered per song (persisted), so a recall
  reuses it. When there are no cleaner variants left, the UI briefly shows "No
  other version found". New `POST /bad-match` endpoint; one INFO log line per
  rescue with the cleaned search terms.

## 1.0.42

- **🧪 ACRCloud Spotify track id → exact-recording lyrics (Phase 1).** When ACR
  priority is on, ACRCloud now also returns the matched track's **Spotify id**
  (Shazam doesn't expose one). That id is carried onto the served track and passed
  to Musixmatch as `track_spotify_id`, so lyrics are pinned to the *exact*
  recording instead of a fuzzy artist/title search — which on radio often lands on
  the wrong variant (radio edit / remaster / live). The id is attached whenever
  ACRCloud matched the same recording, even if its position isn't adopted, so the
  lyrics benefit doesn't depend on the position passing tolerance. Providers that
  don't support Spotify-id lookup (LRCLIB, NetEase, QQ) simply ignore it. No new
  options; piggybacks on the existing ACR-priority switch.

## 1.0.41

- **ACRCloud is no longer a Shazam fallback at all — it is used exclusively for
  the single-shot "ACR priority" refinement.** Routine recognition is now Shazam
  only; a Shazam no-match simply reports no track and never calls ACRCloud, so
  adverts / DJ talk / silence can never burn an ACRCloud credit. ACRCloud is spent
  only when ACR priority is on, once per newly-detected track, to refine the
  position. (Supersedes 1.0.40, which only suppressed the fallback while ACR
  priority was on.) `_recognize_once` is reduced to a single Shazam call and the
  engine docstring updated to match.

## 1.0.40

- **🧪 ACR priority — stop wasting ACRCloud on non-music (fixes the high
  no-result rate).** While ACR priority is on, ACRCloud is now reserved purely for
  the one-shot per-track refinement and is no longer used as the blind Shazam
  *fallback*. The fallback was firing on every Shazam no-match — i.e. on adverts,
  DJ talk and the silence between songs — which burned the daily quota on
  non-music (most of the "No Result" lookups on the ACRCloud dashboard) and left
  ACRCloud in its 30s cooldown right when the next real song change wanted its
  refinement (hence the frequent "ACRCloud unavailable (quota/cooldown)"). With
  this change, ACR priority spends roughly one lookup per *track*, on
  Shazam-confirmed music. When ACR priority is **off**, the original Shazam →
  ACRCloud fallback chain is unchanged.
- **🧪 ACR priority — accept cosmetic title variants.** ACRCloud and Shazam often
  label the same recording slightly differently (e.g. Shazam "It Must Have Been
  Love" vs ACRCloud "It Must Have Been Love (From the Film 'Pretty Woman')"). The
  refinement now treats them as the same recording when the artist matches and one
  title is a prefix of the other, so a valid position isn't discarded over a
  suffix. Genuinely different tracks (different artist/title) are still rejected.

## 1.0.39

- **🧪 TEST FEATURE — "ACR priority" (one ACRCloud lookup per track).** New
  add-on option **`acr_priority`** (default **off**). Background: ACRCloud is
  assumed more accurate for sync than Shazam but is limited to ~100 lookups/day,
  so it can't run every cycle on a radio stream. When **on**, Shazam still does
  all the routine recognition, but each time a *new* track is detected it triggers
  **exactly one** ACRCloud lookup to refine the position. If the ACRCloud position
  agrees with Shazam within **`acr_priority_tolerance`** seconds (default 5) it is
  adopted and **frozen** for the rest of the track — subsequent Shazam reads only
  re-confirm the track and never move the clock (the served position keeps
  advancing on its own). This guarantees at most one ACRCloud lookup per song
  (none wasted). If ACRCloud is unavailable (quota/cooldown), times out, matches a
  different track, or disagrees beyond tolerance, the Shazam position is kept and
  normal lock behaviour resumes. When **off**, the app behaves exactly as before
  (ACRCloud is only the fallback when Shazam fails). This is a **test feature**;
  it is self-contained and can be reverted by turning the option off or reverting
  the PR.

## 1.0.38

- **Removed the Spotify lyrics provider and all its plumbing.** Spotify locked its
  lyrics endpoint behind a rotating TOTP anti-bot (the public proxy is dead and a
  direct `sp_dc` fetch needs a secret Spotify keeps rotating), and Musixmatch —
  which is where Spotify's lyrics come from anyway — already covers the same
  content. Deleted the Spotify provider + Spotify API client, the
  `spotify_client_id` / `spotify_client_secret` / `spotify_sp_dc` /
  `provider_spotify` options, the `SPOTIFY`/`ALBUM_ART` config, and the now-unused
  `spotipy` / `python-dotenv` dependencies. Remaining lyrics providers: LRCLIB,
  Musixmatch, NetEase, QQ. (Spotify *Connect* source detection for recognition is
  unaffected.)

## 1.0.37

- **Spotify lyrics fetched directly with an `sp_dc` cookie — no third-party
  proxy.** The old public proxy (`spotify-lyrics-api-azure.vercel.app`) is dead
  (persistent 404s). Spotify has no client-id/secret or OAuth path to lyrics —
  its lyrics endpoint only accepts a web-player token from the **`sp_dc` cookie**
  (the same method syrics / librelyrics use). New option **`spotify_sp_dc`**: with
  it set, the provider fetches synced lyrics straight from Spotify's
  `color-lyrics` endpoint (token cached until expiry); without it, it falls back
  to the proxy. The cookie is valid ~1 year. Spotify lyrics need the client
  ID/secret (to find the track) **and** the sp_dc cookie (to read the lyrics).

## 1.0.36

- **+/- cycles through every provider, fetching on demand.** The buttons now step
  through **all enabled** lyrics providers (not only the ones already cached). If a
  provider has no lyrics yet for the song it is fetched on the spot; if it has none
  at all, the screen shows "No lyrics from <provider>" and the outcome is logged.
- **Picked provider is remembered per song.** Choosing a provider with +/- persists
  it (`preferred_provider`) in the song's DB, so when that song is recalled from
  cache its lyrics come from the chosen provider — building up a per-song provider
  preference over time.

## 1.0.35

- **Spotify lyrics now work with just the client ID + secret.** Previously the
  Spotify provider required a user OAuth sign-in for which this build has no
  `/callback` route, so it could never authenticate. It now falls back to the
  app-level **Client Credentials** flow (all the lyrics provider needs is track
  *search*), so configuring the client ID + secret is enough — no sign-in.
- **Spotify status is logged at boot.** The Spotify client was created lazily on
  the first cache-miss fetch, so with everything cached you'd see no Spotify log
  at all. It's now warmed up at startup, logging whether it initialised.
- **Cache hits are logged.** Per-provider logging only ran on a fresh fetch, so
  already-cached songs logged nothing. A cache hit now logs the chosen provider
  and which providers have lyrics for that song.

## 1.0.34

- **Cycle lyrics providers from the UI.** Added `+` / `−` buttons on the right of
  the transport row (same Lucide stroke style, 25% smaller) that step through
  every provider that returned lyrics for the current song. The lyrics and the
  "via …" label update live to show the chosen source. The buttons appear only
  when more than one provider has lyrics, and the choice resets to the
  auto-selected best on each new song. New `/lyrics` returns a `providers` list
  and accepts `?provider=` to serve a specific one.
- **Per-provider lyrics logging.** Each provider's outcome is now logged
  (`lyrics-provider <name> OK/ERROR/no lyrics for <artist> - <title>`), so failed
  or empty fetches are visible.

## 1.0.33

- **Grouped the options by category.** Each setting's name is now prefixed with
  its group — **Music Assistant —**, **Recognition —**, **UDP audio —**,
  **Cloud API —**, **Lyrics provider —**, **General —** — and the options are
  ordered to match, so they cluster visually. (HA add-on forms have no real
  section-heading construct, so this prefix approach is the closest available.)
- **UDP port is no longer listed twice.** `udp_listen_port` has been removed from
  Options — the UDP audio port's single home is the `6056/udp` row in the add-on's
  Network panel (the host side is the editable knob), exactly like the web port.
  The app's bind stays 6056 to match the mapping's container side.

## 1.0.32

- **Friendly option names + descriptions in the add-on UI.** Added
  `translations/en.yaml` so every setting shows a plain-English name and an
  explanation instead of the raw key — e.g. `blank_after_failures` now reads
  "Fade lyrics after N failed recognitions". Options are reordered in
  `config.yaml` into logical groups (Music Assistant, recognition engine, cloud
  APIs, lyrics providers, general). Note: HA add-on options forms don't support
  section headings, so grouping is by order + clear labels, not dividers.
- **Grouped the UDP audio port with the other UDP settings.** `udp_listen_port`
  now sits next to the sample-rate and jitter options (and has a friendly name +
  description), so the UDP port is a single setting in one place.

## 1.0.31

- **Removed the redundant `server_port` option — the web UI port is now fixed at
  9014.** The port lived in three places (`ingress_port`, the `ports` mapping, and
  the `server_port` option) that all had to agree. Setting only `server_port`
  (e.g. to 9015) moved the app's bind but not `ingress_port`, so the HA
  sidebar/ingress kept hitting 9014, no request reached the app, and recognition
  (which starts on the first UI poll) never ran — the log looked stuck. The
  internal bind is now pinned to 9014 to match `ingress_port`; to expose direct
  access on a different host port, change only the host side of the `ports:`
  mapping. (For a HA add-on `ingress_port` is static YAML and can't follow an
  option, so a single user-editable web port isn't possible.)

## 1.0.30

- **Fix duplicate recognition + wrong lyrics (two recognizers for one player).**
  The browser polls `current-track` ~10×/s, so `_set_active_recognizer` raced
  itself: its stop step awaits, and a concurrent poll could create a recognizer in
  that window, leaving **two** recognizer loops running for one speaker. With the
  respeaker reconnecting under a new RTP SSRC (two "active" streams for one
  device), the orphan was never stopped — so every recognition and lyric line was
  emitted twice and the two loops fought, scrambling the synced position. Recognizer
  start/stop is now serialized under a lock, so exactly one recognizer ever runs
  per player. Added a concurrency regression test.

- **Fade the screen between songs.** On a stream, when a song ends and recognition
  stops matching (adverts / DJ talk / silence), the recognizer now drops its held
  result instead of leaving the finished track on screen. The UI fades the
  artwork, artist/title and lyrics away — leaving just the transport controls —
  and fades back up when the next song is recognised.
- **New option `blank_after_failures` (default `1`).** Number of consecutive
  failed recognitions before the metadata is cleared. Default `1` blanks the
  screen immediately when a song ends; raise it to ride out brief mid-song
  recognition glitches before fading.
- **Selecting a player gives immediate feedback.** The chip is updated the moment
  a player is chosen, and on every poll — even while a stream is still being
  recognised (`title=None`). Previously the poll bailed out before updating the
  chip whenever no track was identified yet, so picking a radio player that hadn't
  matched looked like nothing happened.

## 1.0.28

- **Transport controls now use Music Assistant's exact icons.** MA's player uses
  the **Lucide** icon set (`SkipBack` / `Play` / `Pause` / `SkipForward`) — thin
  outline/stroke glyphs — not the filled Material Design Icons shipped in 1.0.27.
  Swapped our SVGs for the exact Lucide geometry and stroke styling so the
  controls match MA. The existing dark theme and accent colour are unchanged.
- **The player chip shows the friendly name, not the raw SSRC.** When a stream
  arrives with a fresh SSRC and no name extension, the chip used to display the
  hex key (e.g. `73D34824`). It now borrows the friendly name of a sibling stream
  on the same device (matching `source_ip` / `ma_player_id`), so it shows e.g.
  `respeaker_lyrics`.

## 1.0.27

- **Transport controls and the player picker restyled to the Music Assistant
  player.** Replaced the emoji glyphs with crisp SVG icons and moved the
  speaker/player selector into a rounded chip below the controls that shows the
  current player name (à la MA's speaker chip). The dark theme, accent colour, and
  functionality (previous / play-pause / next / seek) are unchanged.

## 1.0.26

- **Fix the Spotify Connect → radio lockup.** Switching from Spotify Connect to
  radio left the app frozen on the last Connect track with no recognition. Root
  cause: the selected player key is a *stale SSRC* (the respeaker reconnects with
  a new SSRC on every source change), so it no longer resolved to a live MA
  player — `state` came back `None`, the radio guard couldn't fire, and the
  "follow the actually-playing MA player" step latched onto the lingering Connect
  player (still reported as "playing" the old track). `current_track` now
  re-points an unresolved selection at the stream actually delivering audio, so it
  classifies the **current** source: the live stream is on radio → recognition
  runs instead of following the ghost Connect track.

## 1.0.25

- **Radio detection is robust to MA-enriched metadata.** `get_player_state` now
  lets **radio win** across the queue `media_item` and the player's
  `current_media` — the player is still on a radio stream even when MA enriches
  the now-playing into track metadata (album/duration) — so radio stays in
  recognition mode and the follow-playing-player step can't hijack it.
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
