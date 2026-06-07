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
let currentTrackId = null;
let seekable = false;
let durationMs = null;
let isScrubbing = false;

// URL ?player= pins the selection.
const urlPlayer = new URLSearchParams(location.search).get('player');
if (urlPlayer) selectedPlayer = urlPlayer;
else selectedPlayer = localStorage.getItem('selectedPlayer') || null;

function withPlayer(url) {
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
  const data = await fetchJSON(withPlayer('/current-track'));
  if (!data || !data.title) {
    flywheel.isPlaying = false;
    return false;
  }
  // Track changed -> reset lyrics + flywheel.
  if (data.track_id !== currentTrackId) {
    currentTrackId = data.track_id;
    currentLines = [];
    flywheel.reset();
  }
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
  const data = await fetchJSON(withPlayer('/lyrics'));
  if (!data) return;
  if (data.track_id && data.track_id !== currentTrackId) return; // stale
  currentLines = data.line_synced || [];
  if (!data.has_lyrics) {
    setLines({ previous: '', current: data.is_instrumental ? '♪ Instrumental ♪' : '', next: '' });
  }
  $('provider').textContent = data.provider ? `via ${data.provider}` : '';
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
      if (!window._fetchingLyrics) {
        window._fetchingLyrics = true;
        pollLyrics().finally(() => { window._fetchingLyrics = false; });
      }
    } else {
      if (!idleSince) idleSince = Date.now();
      if (Date.now() - idleSince > IDLE_THRESHOLD) interval = IDLE_POLL_INTERVAL;
      setLines({ previous: '', current: '', next: '' });
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

function updatePlayPause(isPlaying) {
  $('btn-playpause').textContent = isPlaying ? '⏸' : '▶';
}

// ---------- transport ----------

async function transport(action, extra) {
  await fetchJSON(withPlayer('/transport'), {
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
  const data = await fetchJSON('/players');
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
  use.onclick = () => selectPlayer(isAuto ? null : p.key);
  row.appendChild(use);
  return row;
}

async function doRename(key, current) {
  const name = prompt('Rename player', current);
  if (name) {
    await fetchJSON(`/players/${encodeURIComponent(key)}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    openPlayerModal();
  }
}

function selectPlayer(key) {
  selectedPlayer = key;
  if (key) localStorage.setItem('selectedPlayer', key);
  else localStorage.removeItem('selectedPlayer');
  $('player-modal').classList.remove('open');
  currentTrackId = null;
  flywheel.reset();
}

// ---------- init ----------

function init() {
  wireTransport();
  $('btn-players').onclick = openPlayerModal;
  $('player-modal').onclick = (e) => {
    if (e.target.id === 'player-modal') e.target.classList.remove('open');
  };
  requestAnimationFrame(renderFrame);
  loop();
}

document.addEventListener('DOMContentLoaded', init);
