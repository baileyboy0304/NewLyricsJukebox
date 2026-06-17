// app.js - NewLyricsJukebox frontend.
// Polls /current-track + /lyrics (~100ms, backing off when idle), drives the
// flywheel clock for smooth 3-line lyric sync, renders now-playing + transport,
// and the player-select modal. No settings UI.

import { Flywheel, activeLineIndex, activeLineIndexByTime, ServerClock } from './flywheel.js';

const POLL_INTERVAL = 100;
const IDLE_POLL_INTERVAL = 1000;
const IDLE_THRESHOLD = 20000;

const flywheel = new Flywheel();
const serverClock = new ServerClock();
const SERVER_CLOCK_RESYNC_MS = 30000;   // re-sync every 30s to absorb drift
let selectedPlayer = null;      // null = Auto
let currentLines = [];          // [{start, text}]
let lyricStatus = '';           // shown on the center line when no synced lyrics
let currentTrackId = null;
let lastActiveIdx = -2;         // for logging line changes
let lastIsPlaying = null;
let seekable = false;
let durationMs = null;
let isScrubbing = false;
let lyricProviders = [];         // providers that returned lyrics for this song
let currentLyricProvider = null; // provider currently shown
let chosenProvider = null;       // user pick via +/- (null = auto best)
let hasLyrics = false;           // whether the current song has any lyrics
let recognitionSilent = false;   // mic isn't hearing audio -> blank lyric line
let flashUntil = 0;              // suppress provider-text updates until this ts (ms)

// Manual lyric-timing offset (seconds). +offset ADVANCES lyrics (shows earlier),
// -offset DELAYS them (later). 0.25s steps. "memory" remembers it per song.
const SYNC_STEP = 0.25;
let lyricOffset = 0;
let memoryOn = (localStorage.getItem('nlj_memory') !== '0');  // default on
let offsetTrackId = null;        // track we've applied the remembered offset for

// Thumbs-down: hide lyrics for a song judged to have no good lyrics. Persisted
// per song; the icon is highlighted on replay so the user can toggle it off to
// re-check for a better option.
let suppressLyrics = false;
let suppressTrackId = null;      // track we've applied the stored suppress flag for

// ---------- console logging (compare timing with the add-on log) ----------
// Logs use wall-clock HH:MM:SS.mmm to line up with the server's timestamps.
function nljLog(kind, detail) {
  const t = new Date();
  const ts = t.toTimeString().slice(0, 8) + '.' + String(t.getMilliseconds()).padStart(3, '0');
  // eslint-disable-next-line no-console
  console.log(`[NLJ ${ts}] ${kind}`, detail !== undefined ? detail : '');
}

// URL ?player= pins the selection.
const urlPlayer = new URLSearchParams(location.search).get('player');
if (urlPlayer) selectedPlayer = urlPlayer;
else selectedPlayer = localStorage.getItem('selectedPlayer') || null;

// Resolve API paths against the document base so the app works both at the
// server root and behind a Home Assistant ingress path prefix. The browser
// resolves a relative URL against document.baseURI, which already includes the
// ingress prefix (HA serves the page under a trailing-slash path).
function api(path) {
  return new URL(path, document.baseURI).toString();
}

function withPlayer(path) {
  const url = api(path);
  if (!selectedPlayer || selectedPlayer === 'auto') return url;
  return url + (url.includes('?') ? '&' : '?') + 'player=' + encodeURIComponent(selectedPlayer);
}

const $ = (id) => document.getElementById(id);

// ---------- polling loop ----------

// Sync the local <-> server clock offset. Cheap: one GET /time, sampled
// every SERVER_CLOCK_RESYNC_MS so drift can't accumulate.
let lastClockSync = 0;
async function syncServerClock() {
  const now = Date.now();
  if (serverClock.synced && now - lastClockSync < SERVER_CLOCK_RESYNC_MS) return;
  const sentAt = Date.now();
  try {
    const res = await fetch(api('time'), { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    const receivedAt = Date.now();
    if (data && typeof data.server_time_ms === 'number') {
      serverClock.applySample(sentAt, receivedAt, data.server_time_ms);
      lastClockSync = receivedAt;
    }
  } catch (e) { /* ignore — next loop tick retries */ }
}

async function fetchJSON(url, options) {
  try {
    const res = await fetch(url, options);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

async function pollTrack() {
  const data = await fetchJSON(withPlayer('current-track'));
  // Reflect the resolved player in the chip even while recognising (title=None),
  // otherwise selecting a player on a not-yet-identified stream looks like a no-op.
  if (data && data.player) updateSpeakerName(data.player);
  if (!data || !data.title) {
    flywheel.isPlaying = false;
    // No playing track (paused / stopped / cleared between songs) — reset the
    // transport icon to "play", otherwise it stays stuck on the pause symbol.
    updatePlayPause(!!(data && data.is_playing));
    return false;
  }
  // Track changed -> clear stale lyrics IMMEDIATELY (metadata drives this) and
  // reset the flywheel.
  if (data.track_id !== currentTrackId) {
    currentTrackId = data.track_id;
    currentLines = [];
    lyricStatus = '…';            // loading until lyrics arrive
    lastActiveIdx = -2;
    chosenProvider = null;        // new song -> back to the auto-selected provider
    lyricProviders = [];
    currentLyricProvider = null;
    hasLyrics = false;
    $('btn-bad-match').classList.remove('active');  // reset wrong-version state
    lyricOffset = 0;                 // new song -> default timing until remembered value loads
    offsetTrackId = null;
    updateSyncDisplay();
    suppressLyrics = false;          // new song -> show lyrics until stored flag loads
    suppressTrackId = null;
    $('btn-suppress').classList.remove('active');
    flywheel.reset();
    nljLog('metadata', {
      title: data.title, artist: data.artist, source: data.source,
      position: Number((data.position || 0).toFixed(2)),
      is_playing: data.is_playing, track_id: data.track_id,
    });
  } else if (data.is_playing !== lastIsPlaying) {
    nljLog('playstate', { is_playing: data.is_playing, position: Number((data.position || 0).toFixed(2)) });
  }
  lastIsPlaying = data.is_playing;

  seekable = !!data.seekable;
  durationMs = data.duration_ms;
  flywheel.setAnchor(data.position || 0, data.is_playing);

  $('np-title').textContent = data.title || '';
  $('np-artist').textContent = data.artist || '';
  const art = $('np-art');
  if (data.album_art_url) { art.src = data.album_art_url; art.style.visibility = 'visible'; }
  else { art.style.visibility = 'hidden'; }

  updatePlayPause(data.is_playing);
  $('scrub').disabled = !seekable;
  return true;
}

async function pollLyrics() {
  let url = withPlayer('lyrics');
  if (chosenProvider) {
    url += (url.includes('?') ? '&' : '?') + 'provider=' + encodeURIComponent(chosenProvider);
  }
  const data = await fetchJSON(url);
  if (!data) return;
  if (data.track_id && data.track_id !== currentTrackId) return; // stale
  const lines = data.line_synced || [];
  const had = currentLines.length;
  const wasSilent = recognitionSilent;
  recognitionSilent = !!data.recognition_silent;
  currentLines = lines;
  if (recognitionSilent) {
    // Mic isn't hearing the song — server has blanked line_synced; we
    // also wipe the on-screen status string so nothing stale lingers.
    lyricStatus = '';
    if (!wasSilent) nljLog('silent', { reason: 'recognition_silent' });
  } else if (lines.length) {
    lyricStatus = '';
  } else if (data.is_instrumental) {
    lyricStatus = '♪ Instrumental ♪';
  } else if (data.no_lyrics) {
    lyricStatus = data.provider ? `No lyrics from ${data.provider}` : '';
  } else if (data.pending) {
    lyricStatus = '…';
  } else {
    lyricStatus = '';            // no lyrics found -> blank (stale already cleared)
  }
  lyricProviders = data.providers || [];
  currentLyricProvider = data.provider || null;
  hasLyrics = !!data.has_lyrics;
  applyRememberedOffset(data.timing_offset, data.track_id);
  applySuppress(data.suppress_lyrics, data.track_id);
  updateProviderCycle();
  // Don't clobber a brief "no other version" flash from the bad-match button.
  if (Date.now() > flashUntil) {
    $('provider').textContent = data.provider ? `via ${data.provider}` : '';
  }
  // Log only when the lyric availability actually changes, not every poll.
  if (!!had !== !!lines.length || lines.length === 0) {
    nljLog('lyrics', {
      provider: data.provider, lines: lines.length,
      word_sync: data.has_word_sync, instrumental: data.is_instrumental,
      pending: data.pending, track_id: data.track_id,
    });
  }
}

let lastCheck = 0;
let idleSince = 0;
let interval = POLL_INTERVAL;

async function loop() {
  while (true) {
    const now = Date.now();
    if (now - lastCheck < interval) {
      await new Promise((r) => setTimeout(r, interval - (now - lastCheck)));
      continue;
    }
    lastCheck = Date.now();
    syncServerClock();   // fire-and-forget; cheap, gated by resync interval
    const playing = await pollTrack();
    if (playing) {
      idleSince = 0;
      interval = POLL_INTERVAL;
      setStageVisible(true);
      if (!window._fetchingLyrics) {
        window._fetchingLyrics = true;
        pollLyrics().finally(() => { window._fetchingLyrics = false; });
      }
    } else {
      if (!idleSince) idleSince = Date.now();
      if (Date.now() - idleSince > IDLE_THRESHOLD) interval = IDLE_POLL_INTERVAL;
      // No current track (between songs / stopped): fade the stage out and reset
      // the progress bar, leaving just the transport controls.
      setStageVisible(false);
      resetProgress();
      currentLines = [];
      lyricStatus = '';
      currentTrackId = null;
    }
  }
}

// ---------- rendering ----------

function setLines({ previous, current, next }) {
  $('line-prev').textContent = previous || '';
  $('line-current').textContent = current || '';
  $('line-next').textContent = next || '';
}

function renderFrame(ts) {
  const position = flywheel.tick(ts);
  // Lyric line lookup uses the manually-nudged position; the progress bar/time
  // below stay on the true audio position. +offset ADVANCES the lyrics (looks
  // ahead in the song), -offset delays them.
  const lyricPos = position + lyricOffset;
  if (suppressLyrics || recognitionSilent) {
    // Lyrics hidden either because the user thumbs-down'd the song or
    // because the mic isn't hearing audio (server-flagged silence).
    setLines({ previous: '', current: '', next: '' });
  } else if (currentLines.length) {
    // Prefer NTP-anchored display_at_epoch_ms (the spec): a line becomes
    // active the moment the server's wall clock reaches its display_at.
    // Falls back to position-based picking if the payload is older or the
    // ServerClock hasn't synced yet.
    let idx = -1;
    if (serverClock.synced && currentLines[0].display_at_epoch_ms != null) {
      idx = activeLineIndexByTime(currentLines, serverClock.nowMs());
    } else {
      idx = activeLineIndex(currentLines, lyricPos);
    }
    setLines({
      previous: idx - 1 >= 0 ? currentLines[idx - 1].text : '',
      current: idx >= 0 ? currentLines[idx].text : '',
      next: idx + 1 < currentLines.length ? currentLines[idx + 1].text : '',
    });
    if (idx !== lastActiveIdx) {
      lastActiveIdx = idx;
      // Render-time diagnostics. `shown_at` is wall-clock when the line
      // hits the screen (matches the [NLJ HH:MM:SS.mmm] prefix). When
      // the absolute-time picker drove the swap we also log the line's
      // scheduled display_at and the skew (shown_at - display_at) so
      // it can be compared against the bridge / NLJ server logs and the
      // ESPHome device's own SNTP-fired log line.
      const line = idx >= 0 ? currentLines[idx] : null;
      const shownAt = new Date();
      const shownStr = shownAt.toISOString().slice(11, 23);  // HH:MM:SS.mmm
      const detail = {
        idx,
        position: Number(position.toFixed(2)),
        text: line ? line.text : '(before first line)',
        shown_at: shownStr,
        shown_at_ms: shownAt.getTime(),
      };
      if (line && line.display_at_epoch_ms != null) {
        detail.display_at_ms = line.display_at_epoch_ms;
        detail.display_at = new Date(line.display_at_epoch_ms)
          .toISOString().slice(11, 23);
        // serverClock.nowMs() is our best estimate of the server's wall
        // clock right now; the skew below is therefore how late/early
        // we rendered relative to the SCHEDULED instant on server time.
        detail.skew_ms = Math.round(serverClock.nowMs() - line.display_at_epoch_ms);
        detail.picker = 'time';
      } else {
        detail.picker = 'position';
      }
      nljLog('line', detail);
    }
  } else {
    // No synced lyrics: show the status (loading / instrumental / blank). This
    // also guarantees stale lines are cleared the moment the track changes.
    setLines({ previous: '', current: lyricStatus, next: '' });
  }
  if (!isScrubbing && durationMs) {
    const pct = Math.min(100, (position * 1000 / durationMs) * 100);
    $('scrub').value = pct;
    $('time-pos').textContent = fmt(position);
    $('time-dur').textContent = fmt(durationMs / 1000);
  }
  requestAnimationFrame(renderFrame);
}

function fmt(sec) {
  if (!sec || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// Lucide icon geometry (matches Music Assistant's lucide-vue-next icons).
const ICON_PLAY =
  '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z" />';
const ICON_PAUSE =
  '<rect x="14" y="3" width="5" height="18" rx="1" /><rect x="5" y="3" width="5" height="18" rx="1" />';

function updatePlayPause(isPlaying) {
  $('playpause-icon').innerHTML = isPlaying ? ICON_PAUSE : ICON_PLAY;
}

function updateSpeakerName(name) {
  $('speaker-name').textContent = name || 'Select player';
}

// Fade the now-playing metadata + lyrics in/out. The transport controls live in
// the footer and stay visible. Used to blank the screen between songs.
function setStageVisible(visible) {
  document.querySelector('.stage').classList.toggle('hidden', !visible);
}

function resetProgress() {
  durationMs = null;
  if (!isScrubbing) {
    $('scrub').value = 0;
    $('time-pos').textContent = '0:00';
    $('time-dur').textContent = '0:00';
  }
}

// ---------- lyrics-provider cycle (+ / -) and bad-match ----------

// The cycle row shows whenever the song has lyrics. The +/- pair is shown only
// when there's more than one provider to switch between; the bad-match (wrong
// version) button shows whenever there are lyrics to reject.
function updateProviderCycle() {
  const show = hasLyrics || lyricProviders.length > 0 || suppressLyrics;
  $('provider-cycle').classList.toggle('hidden', !show);
  $('prov-pm').classList.toggle('hidden', lyricProviders.length < 2);
  $('btn-bad-match').classList.toggle('hidden', !hasLyrics);
  // Keep the thumbs-down available while suppressed so the user can toggle it off.
  $('btn-suppress').classList.toggle('hidden', !(hasLyrics || suppressLyrics));
}

// "Bad match": tell the server the served lyrics are the wrong version; it
// re-searches with a cleaned title and swaps in an alternate (or reports none).
async function badMatch() {
  const res = await fetchJSON(withPlayer('bad-match'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  if (!res || !res.ok) return;
  nljLog('bad-match', { index: res.index, search: res.search, none: !!res.no_alternate });
  if (res.no_alternate) {
    $('provider').textContent = 'No other version found';
    flashUntil = Date.now() + 2500;
    return;
  }
  $('btn-bad-match').classList.add('active');
  chosenProvider = null;          // serve the re-search's auto-selected provider
  lyricStatus = '…';
  if (!window._fetchingLyrics) {   // refresh now, don't wait for the next poll
    window._fetchingLyrics = true;
    pollLyrics().finally(() => { window._fetchingLyrics = false; });
  }
}

// ---------- lyric-timing offset (sync nudge) + memory ----------

function fmtOffset(v) {
  const sign = v > 0 ? '+' : v < 0 ? '-' : '';
  return `${sign}${Math.abs(v).toFixed(2)}s`;
}

function updateSyncDisplay() {
  $('sync-value').textContent = fmtOffset(lyricOffset);
}

// POST the current offset for persistence. remember=true stores it on the song;
// remember=false forgets it (server resets the stored value to default).
function persistOffset(remember) {
  fetchJSON(withPlayer('lyric-offset'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ offset: lyricOffset, remember }),
  });
}

function adjustOffset(dir) {
  lyricOffset = Math.max(-30, Math.min(30,
    Math.round((lyricOffset + dir * SYNC_STEP) * 100) / 100));
  updateSyncDisplay();
  offsetTrackId = currentTrackId;     // user override wins over any remembered value
  if (memoryOn) persistOffset(true);  // changing the timing stores it (when remembering)
  nljLog('lyric-offset', { offset: lyricOffset, memory: memoryOn });
}

function onMemoryToggle() {
  memoryOn = $('sync-memory').checked;
  localStorage.setItem('nlj_memory', memoryOn ? '1' : '0');
  if (memoryOn) {
    persistOffset(true);              // start remembering the current offset
  } else {
    lyricOffset = 0;                  // unticked -> default timing, forget the cached value
    updateSyncDisplay();
    persistOffset(false);
  }
}

// Apply a song's remembered offset once per track (from the /lyrics payload).
function applyRememberedOffset(serverOffset, trackId) {
  if (!memoryOn || trackId == null || trackId === offsetTrackId) return;
  lyricOffset = Number(serverOffset) || 0;
  offsetTrackId = trackId;
  updateSyncDisplay();
}

// Thumbs-down: toggle hiding lyrics for the current song (persisted per song).
function toggleSuppress() {
  suppressLyrics = !suppressLyrics;
  suppressTrackId = currentTrackId;
  $('btn-suppress').classList.toggle('active', suppressLyrics);
  fetchJSON(withPlayer('suppress-lyrics'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ suppress: suppressLyrics }),
  });
  nljLog('suppress', { suppress: suppressLyrics });
}

// Apply a song's stored suppress flag once per track (from the /lyrics payload).
function applySuppress(serverSuppress, trackId) {
  if (trackId == null || trackId === suppressTrackId) return;
  suppressLyrics = !!serverSuppress;
  suppressTrackId = trackId;
  $('btn-suppress').classList.toggle('active', suppressLyrics);
}

function cycleProvider(dir) {
  if (lyricProviders.length < 2) return;
  let idx = lyricProviders.indexOf(chosenProvider || currentLyricProvider);
  if (idx < 0) idx = 0;
  idx = (idx + dir + lyricProviders.length) % lyricProviders.length;
  chosenProvider = lyricProviders[idx];
  nljLog('provider-pick', { provider: chosenProvider, of: lyricProviders });
  if (!window._fetchingLyrics) {              // refresh immediately, don't wait a poll
    window._fetchingLyrics = true;
    pollLyrics().finally(() => { window._fetchingLyrics = false; });
  }
}

// ---------- transport ----------

async function transport(action, extra) {
  await fetchJSON(withPlayer('transport'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ action }, extra || {})),
  });
}

function wireTransport() {
  $('btn-playpause').onclick = () => transport('play_pause');
  $('btn-next').onclick = () => transport('next');
  $('btn-prev').onclick = () => transport('previous');
  const scrub = $('scrub');
  scrub.oninput = () => { if (seekable) isScrubbing = true; };
  scrub.onchange = () => {
    if (seekable && durationMs) {
      const positionMs = Math.round((scrub.value / 100) * durationMs);
      transport('seek', { position_ms: positionMs });
    }
    isScrubbing = false;
  };
}

// ---------- player modal ----------

async function openPlayerModal() {
  const data = await fetchJSON(api('players'));
  const modal = $('player-modal');
  const list = $('player-list');
  list.innerHTML = '';

  list.appendChild(playerRow({ name: 'Auto', key: 'auto' }, true));
  (data.players || []).forEach((p) => list.appendChild(playerRow(p, false)));

  if ((data.unassigned_streams || []).length) {
    const h = document.createElement('div');
    h.className = 'section-title';
    h.textContent = 'Unassigned streams';
    list.appendChild(h);
    data.unassigned_streams.forEach((p) => list.appendChild(playerRow(p, false, true)));
  }
  modal.classList.add('open');
}

function playerRow(p, isAuto, unassigned) {
  const row = document.createElement('div');
  row.className = 'player-row';
  const info = document.createElement('div');
  info.className = 'player-info';
  const name = document.createElement('div');
  name.className = 'player-name';
  name.textContent = p.name;
  info.appendChild(name);
  if (!isAuto) {
    const meta = document.createElement('div');
    meta.className = 'player-meta';
    const bits = [];
    if (p.source_ip) bits.push('IP ' + p.source_ip);
    if (p.ssrc) bits.push('SSRC ' + p.ssrc);
    meta.textContent = bits.join(' · ');
    info.appendChild(meta);
  }
  row.appendChild(info);

  if (!isAuto && !unassigned) {
    const rename = document.createElement('button');
    rename.className = 'icon-btn';
    rename.textContent = '✎';
    rename.onclick = (e) => { e.stopPropagation(); doRename(p.key, p.name); };
    row.appendChild(rename);
  }
  const use = document.createElement('button');
  use.className = 'use-btn';
  use.textContent = 'Use';
  use.onclick = () => selectPlayer(isAuto ? null : p.key, isAuto ? null : p.name);
  row.appendChild(use);
  return row;
}

async function doRename(key, current) {
  const name = prompt('Rename player', current);
  if (name) {
    await fetchJSON(api(`players/${encodeURIComponent(key)}/rename`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    openPlayerModal();
  }
}

function selectPlayer(key, name) {
  selectedPlayer = key;
  if (key) localStorage.setItem('selectedPlayer', key);
  else localStorage.removeItem('selectedPlayer');
  // Immediate feedback; the next poll confirms the server-resolved name.
  updateSpeakerName(name);
  $('player-modal').classList.remove('open');
  currentTrackId = null;
  flywheel.reset();
}

// ---------- init ----------

function init() {
  nljLog('app started', { version: window.NLJ_VERSION || 'unknown', player: selectedPlayer || 'auto' });
  wireTransport();
  $('btn-prov-prev').onclick = () => cycleProvider(-1);
  $('btn-prov-next').onclick = () => cycleProvider(1);
  $('btn-bad-match').onclick = badMatch;
  $('btn-suppress').onclick = toggleSuppress;
  $('btn-sync-minus').onclick = () => adjustOffset(-1);  // lyrics later (delay)
  $('btn-sync-plus').onclick = () => adjustOffset(1);    // lyrics earlier (advance)
  $('sync-memory').checked = memoryOn;
  $('sync-memory').onchange = onMemoryToggle;
  updateSyncDisplay();
  $('btn-players').onclick = openPlayerModal;
  $('player-modal').onclick = (e) => {
    if (e.target.id === 'player-modal') e.target.classList.remove('open');
  };
  requestAnimationFrame(renderFrame);
  loop();
}

document.addEventListener('DOMContentLoaded', init);
