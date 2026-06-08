# Changelog

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
