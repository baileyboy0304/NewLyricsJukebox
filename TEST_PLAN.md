# NLJ Source-Routing Test Plan

Goal: confirm that **only true streams** (radio, sources MA can't describe)
trigger audio recognition, and that **everything MA can describe** —
including Spotify Connect — takes the metadata path with no recognition.

Working hypothesis from the user: Spotify Connect currently triggers
recognition even when MA has the track. We need to either confirm and fix,
or refute and document.

## Where to look in the code (so the test results map to lines)

| File | Lines | What it does |
|------|-------|--------------|
| `classify.py` | 24–48 | `classify_source_mode(player)` — returns `queue` or `stream`. |
| `server.py` | 469–473 | **Diagnostic log** `ma-class player=… mt=… source=… playing=… title=…` — deduped per change. **This is the key signal.** |
| `server.py` | 475–485 | (1) **Metadata-first**: if MA has title and it's not radio → use MA, no recognition. |
| `server.py` | 487–517 | (1b) **Grouped/external fallback**: follow whichever MA player is actually playing. |
| `server.py` | 525–562 | (3) **Recognition fallback** — only entered if (1) and (1b) found nothing. |
| `server.py` | 401–417 | `_ma_track()` — sets `source` from classify and `seekable = (mode=="queue")`. |
| `server.py` | 389–393 | `_log_mode()` — emits `current-track player=… mode=… title=…` on every change. |
| `music_assistant.py` | 159–259 | `get_player_state()` — how `media_type`, `active_source_name`, `title` are populated. |

## Logging to enable

Before each test, set log level to **INFO** (or DEBUG for the
music_assistant.py read path). The lines to capture per test:

```
ma-class player=<name> mt=<media_type> source=<active_source_name> playing=<bool> title=<title>
current-track player=<name> mode=<queue|stream> title=<title> [ (recognizing, stream=…) ]
```

Useful extra log namespaces:
- `recognition.engine` — emits whenever a recognizer is **started/stopped**;
  the absence of these lines is evidence that recognition is **not** running.
- `recognition.udp_capture` — emits when a new RTP stream is seen
  (player_name / player_id from RFC 8285).
- `lyrics` — provider fetches; confirms metadata was good enough to query.

If `NLJ_LOGS_DIR` is set, a rotating file copy is written there too — easier
to attach to bug reports than scrollback.

### One-time log helper (no code change, just env)

```bash
export NLJ_LOGS_DIR=/share/nlj-logs        # HA add-on share folder
# inside the add-on container, or via the add-on's options:
#   log_level: info
```

### If you need MORE detail than the current logging gives

Two **one-line tweaks** that are worth proposing only after the first pass
confirms the hypothesis:

1. `server.py` ~line 530 — log the **reason** we fell through to recognition:
   ```python
   logger.info("fallthrough-to-recognition player=%s state=%s title=%r is_radio=%s",
               name, "none" if state is None else "got", state.title if state else None,
               self._is_radio(state) if state else False)
   ```
2. `music_assistant.py` ~line 244 (before the `return PlayerState(...)`) — log
   the exact values:
   ```python
   logger.info("ma-state id=%s name=%s title=%r media_type=%s source_id=%s source_name=%s",
               player_id, getattr(player, "display_name", None), title, media_type,
               active_source_id, active_source_name)
   ```

Don't add these now — only if Stage A below is ambiguous.

---

## Test environment

- One HA instance with Music Assistant configured.
- At least one MA player that can be driven by:
  - **A.** MA-native Spotify (queue), and
  - **B.** Spotify Connect (direct from the Spotify app to the speaker), and
  - **C.** A real radio station (TuneIn / Radio Browser), and
  - **D.** A UDP mic device (sendspin / atom_echo / `ha-udp-lyrics-player`
        dummy) attached to that speaker via `lyrics-mic-bridge`.
- NLJ add-on running on port 9014, UDP listener on 6056.
- Browser open at `http://<ha>:9014/` with the player selected.
- ACRCloud key configured (so quota burn is visible — a strong "did we
  recognize?" signal).

---

## Stage A — Spotify Connect (the suspected bug)

**Setup:** start playback from the Spotify app *directly to the speaker*
(use the Devices menu → pick the speaker → play a track). Do **not** route
via the MA queue.

**Procedure (do each step, capture logs between steps):**

| # | Action | Watch for |
|---|--------|-----------|
| A1 | Confirm playback. Note the actual track. | Speaker plays Spotify track. |
| A2 | Open NLJ UI, select the player. | Single `ma-class` line. |
| A3 | Let it run 30 s. | `current-track mode=…`, lyrics show. |
| A4 | Skip to next track on Spotify. | Fresh `ma-class` + `current-track` lines. |
| A5 | Pause / resume on Spotify. | `playing=` toggles in `ma-class`. |

**Capture for each step:**
- The most recent `ma-class` line — **expected**: `mt=track`,
  `source=<something not ending in "Queue">` (e.g. "Spotify Connect" or
  the speaker name), `playing=True`, `title=<actual track>`.
- The most recent `current-track` line — **expected**: `mode=stream`
  (classify's verdict) **but no `(recognizing, stream=…)` suffix**.
- Whether any `recognition.engine` lines fire in the same window —
  **expected**: none, because the metadata-first branch took it.
- Whether ACRCloud quota incremented during the test — **expected**: no
  change.

**Pass criteria:** title in UI tracks the Spotify Connect track within ~1 s
of MA reporting it, **no recognizer is started**, ACRCloud quota does not
move.

**Failure signature → root cause map:**

| Symptom in logs | Likely cause | Where to look |
|-----------------|--------------|---------------|
| `ma-class … title=None …` while Spotify is clearly playing | MA isn't surfacing the Connect track on this player. The metadata-first branch fails because `state.title is None`. | `music_assistant.py get_player_state()` lines 198–223 — `current_item.media_item` vs `player.current_media`. Add the ma-state log from above. |
| `ma-class … mt=radio …` while Spotify is playing | `_coalesce_media_type()` is mis-classifying the Connect source as radio. | `music_assistant.py:_coalesce_media_type` plus item/current_media types. |
| `ma-class` looks correct (`mt=track`, title present) but `current-track` still shows `(recognizing, stream=…)` | We're not selecting the right player — `state` is `None` on the resolved id and the grouped fallback (1b) doesn't fire. | `server.py current_track()` lines 444–464 (stale SSRC migration), 487–517 (fallback). |
| Recognition starts intermittently | Transient MA disconnects. | `server.py` line 521 "transient MA read failure" — currently keeps last track; check if the runtime ever flips. |
| ACRCloud quota burns | Same as "recognition starts" — proves it. | `recognition/acrcloud.py` quota counter. |

---

## Stage B — Spotify from MA

**Setup:** queue a track from MA itself (Music Assistant → Library →
Spotify → play to the speaker). The queue is owned by MA.

| # | Action | Expected `ma-class` | Expected `current-track` |
|---|--------|---------------------|--------------------------|
| B1 | Play track from MA. | `mt=track source=<Player> Queue playing=True title=…` | `mode=queue title=<track>` |
| B2 | Skip / seek. | Track changes; `playing` stays true. | Position seek allowed (UI shows seek bar interactable). |
| B3 | Pause. | `playing=False`. | `mode=queue` unchanged. |

**Pass criteria:** `mode=queue`, `seekable=true` in `/current-track`
response, no recognizer started, ACRCloud quota unchanged.

---

## Stage C — UDP dummy player (`ha-udp-lyrics-player`)

**Setup:** add a `udp_lyrics_player` integration entry pointing at the NLJ
host. Add it to an MA group with a real speaker, play Spotify via MA to the
group.

| # | Action | Expected |
|---|--------|----------|
| C1 | Group is playing. | `recognition.udp_capture` logs a new RTP stream with the configured `player_name`. |
| C2 | NLJ UI auto-detects the dummy in the player list. | `/players` returns the dummy under `unassigned_streams` or as a known player. |
| C3 | Select the dummy. | Without `source_player=` association: `current-track mode=stream` and a recognizer **does** start (this is expected — we have no MA metadata for the dummy itself). |
| C4 | Bind it to the real speaker via `lyrics-mic-bridge` (so NLJ receives `source_player=<real_speaker_id>`). | `current-track` switches to the speaker's track; recognizer for the dummy's stream is **stopped** (supervisor tears it down because `runtime.rec_stream` becomes `None`). |

**Pass criteria for C4:** title matches the real speaker's MA track within
1–2 s; `recognition.engine` logs a recognizer stop; ACRCloud quota does
not move.

This is the strongest end-to-end test that the "metadata beats
recognition" rule is honoured.

---

## Stage D — Live radio

**Setup:** play a real radio station (TuneIn) on the speaker via MA.

| # | Action | Expected |
|---|--------|----------|
| D1 | Play radio. | `ma-class … mt=radio …` |
| D2 | Selected in NLJ. | `current-track mode=stream (recognizing, stream=…)`. |
| D3 | Wait up to ~6 s. | `recognition.engine` logs a lock cycle (`initial` → `locking` → `locked`). |
| D4 | Title and artist appear. | Lyrics fetched; UI shows 3 lines syncing to audio. |
| D5 | Station ad break / unknown content. | After failure threshold, metadata blanks (expected). |

**Pass criteria:** recognition runs (this is the one case where it should),
matches within ~4–6 s, and quota usage is bounded (Shazam only — ACRCloud
should not be called per match unless ACR-priority is on).

---

## What "results" should look like when you send them back

For each stage, please paste:

1. The `ma-class` line(s) seen during the test.
2. The `current-track` line(s) seen during the test.
3. Any `recognition.engine` start/stop lines.
4. Any `recognition.udp_capture` new-stream lines.
5. ACRCloud quota before / after the stage (the persisted counter).
6. A one-line note on what the **UI showed** vs what was actually playing.

A minimal log capture command from inside the NLJ container:

```bash
# tail the live log and filter to the lines we care about
journalctl -u nlj 2>/dev/null | tail -n +0 -f \
  | grep -E 'ma-class|current-track|recognition\.engine|recognition\.udp_capture|ACRCloud'
```

(Or `tail -f $NLJ_LOGS_DIR/nlj.log | grep -E '…'` if file logging is on.)

That's enough to fingerprint exactly which branch in `current_track()` the
player went through, on every transition.

---

## After results come back

Most likely outcomes and the fix in each case:

- **Spotify Connect MA reports no title** → fix is in
  `music_assistant.py:get_player_state()` — pull title/artist from
  `player.current_media` when the queue's `current_item.media_item` is
  empty (lines 215–221 already attempt this; the bug is probably one
  field name).
- **Spotify Connect mis-typed as radio** → fix is in
  `_coalesce_media_type()`; ensure radio wins **only** when both the item
  and the current_media agree.
- **Resolved player is a group child** → fix is in
  `current_track()` step 1b: relax `players_related()` so a Connect
  source player counts as related to its target speaker.
- **Recognition starts then stops on every MA blip** → fix is in step 2
  (the transient-MA branch) — extend the "keep last track" window.

Don't change code until results are in.
