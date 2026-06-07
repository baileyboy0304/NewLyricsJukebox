// flywheel.js - monotonic "flywheel clock" for smooth lyric sync.
//
// Ported from the legacy lineSync.js clock (AUDIT.md §6 calls this out as a
// genuinely good design): a monotonic position advanced every frame that chases
// the server's reported position via an EMA-filtered drift, snapping instantly
// on large jumps (seek/track change) and deadbanding tiny drift. Decoupled here
// from the old pixel-scroll DOM so it can drive the simple 3-line view.

const DRIFT_SMOOTHING = 0.3;

export class Flywheel {
  constructor() {
    this.anchorPosition = 0;     // server position at anchor (seconds)
    this.anchorTimestamp = 0;    // performance.now() when anchor was set
    this.isPlaying = false;
    this.visualPosition = 0;
    this.visualSpeed = 1.0;
    this.filteredDrift = 0;
    this.lastFrameTime = 0;
  }

  // Set from each /current-track poll.
  setAnchor(position, isPlaying) {
    this.anchorPosition = position;
    this.anchorTimestamp = performance.now();
    if (isPlaying !== null && isPlaying !== undefined) {
      this.isPlaying = isPlaying;
    }
    // Seed on first anchor.
    if (this.visualPosition === 0) this.visualPosition = position;
  }

  // Call once per animation frame; returns the smoothed current position.
  tick(timestamp) {
    const dt = this.lastFrameTime ? (timestamp - this.lastFrameTime) / 1000 : 0;
    this.lastFrameTime = timestamp;
    if (!this.isPlaying) return this.visualPosition;

    const elapsed = (performance.now() - this.anchorTimestamp) / 1000;
    const serverPosition = this.anchorPosition + elapsed;
    const rawDrift = serverPosition - this.visualPosition;

    // Large jump (seek / new track) - snap immediately.
    if (Math.abs(rawDrift) > 0.5) {
      this.visualPosition = serverPosition;
      this.visualSpeed = 1.0;
      this.filteredDrift = 0;
      return this.visualPosition;
    }

    // EMA-filter the drift, then rubber-band within +/-10% to chase it.
    this.filteredDrift = this.filteredDrift * (1 - DRIFT_SMOOTHING) + rawDrift * DRIFT_SMOOTHING;
    if (Math.abs(this.filteredDrift) < 0.03) {
      this.visualSpeed = 1.0;
    } else {
      this.visualSpeed = Math.max(0.9, Math.min(1.1, 1.0 + this.filteredDrift * 0.8));
    }
    this.visualPosition += dt * this.visualSpeed;
    return this.visualPosition;
  }

  reset() {
    this.visualPosition = 0;
    this.visualSpeed = 1.0;
    this.filteredDrift = 0;
    this.lastFrameTime = 0;
  }
}

// Given line-synced lyrics [{start, text}] and a position, return the active
// index (-1 before the first line).
export function activeLineIndex(lines, position) {
  if (!lines || lines.length === 0) return -1;
  let idx = -1;
  for (let i = 0; i < lines.length; i++) {
    const start = lines[i].start || 0;
    const end = lines[i + 1] ? lines[i + 1].start : start + 10;
    if (position >= start && position < end) { idx = i; break; }
  }
  if (position < (lines[0].start || 0)) return -1;
  if (idx === -1 && position >= (lines[lines.length - 1].start || 0)) {
    idx = lines.length - 1;
  }
  return idx;
}
