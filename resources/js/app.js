// app.js - NewLyricsJukebox frontend.
// Polls /current-track + /lyrics (~100ms, backing off when idle), drives the
// flywheel clock for smooth 3-line lyric sync, renders now-playing + transport,
// and the player-select modal. No settings UI.

import { Flywheel, activeLineIndex } from './flywheel.js';

const POLL_INTERVAL = 100;
const IDLE_POLL_INTERVAL = 1000;
const IDLE_THRESHOLD = 20000;

const flywheel = new Flywheel();
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
  currentLines = lines;
  if (lines.length) {
    lyricStatus = '';
  } else if (data.is_instrumental) {
    lyricStatus = '♪ Instrumental ♪';
  } else if (data.pending) {
    lyricStatus = '…';
  } else {
    lyricStatus = '';            // no lyrics found -> blank (stale already cleared)
  }
  lyricProviders = data.providers || [];
  currentLyricProvider = data.provider || null;
  updateProviderCycle();
  $('provider').textContent = data.provider ? `via ${data.provider}` : '';
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
  if (currentLines.length) {
    const idx = activeLineIndex(currentLines, position);
    setLines({
      previous: idx - 1 >= 0 ? currentLines[idx - 1].text : '',
      current: idx >= 0 ? currentLines[idx].text : '',
      next: idx + 1 < currentLines.length ? currentLines[idx + 1].text : '',
    });
    if (idx !== lastActiveIdx) {
      lastActiveIdx = idx;
      nljLog('line', {
        idx, position: Number(position.toFixed(2)),
        text: idx >= 0 ? currentLines[idx].text : '(before first line)',
      });
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

// ---------- lyrics-provider cycle (+ / -) ----------

// Show the +/- buttons only when there's more than one provider to switch between.
function updateProviderCycle() {
  $('provider-cycle').classList.toggle('hidden', lyricProviders.length < 2);
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
  $('btn-players').onclick = openPlayerModal;
  $('player-modal').onclick = (e) => {
    if (e.target.id === 'player-modal') e.target.classList.remove('open');
  };
  requestAnimationFrame(renderFrame);
  loop();
}

document.addEventListener('DOMContentLoaded', init);
