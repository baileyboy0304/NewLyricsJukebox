"""HTTP server + controller wiring the spine together.

Endpoints (the brief's minimum four): /players, /current-track, /lyrics,
/transport. The browser polls /current-track and /lyrics (~100 ms, backing off
when idle) and runs the flywheel clock client-side.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from quart import Quart, jsonify, render_template, request

from classify import classify_source_mode
from config import LYRICS, PLAYERS, RESOURCES_DIR, SERVER, VERSION
from lyrics import LyricsData, LyricsService, visible_lines, alternate_queries
from ma_models import PlayerState

logger = logging.getLogger(__name__)

# How long a player's engine keeps running after its last poll. Any consumer
# (web page or ESPHome display) polling within this window keeps recognition alive;
# once nothing polls for this long, the supervisor tears the pipeline down.
LEASE_TTL = 6.0


# How many consecutive recognizer no-matches before we serve the lyric
# stream as "silent" — empty lines + a recognition_silent flag. Tuned to
# blank lyrics WELL BEFORE we blank the now-playing metadata (controlled
# by blank_after_failures). With the default 4 s recognition interval
# two misses ≈ 8 s of silence on the mic, which is long enough that a
# transient miss won't blank the lyrics but short enough that an actual
# pause / DJ talk doesn't leave the previous line stale on screen.
LYRIC_SILENCE_THRESHOLD = 2


def _track_duration_seconds(track) -> Optional[int]:
    """Round a track's duration_ms to whole seconds for LRCLIB / other
    providers that take seconds. Rounded (not floored) so 237501 -> 238,
    matching what providers' exact-match endpoints actually compare
    against. Returns None when the duration is unknown."""
    ms = (track or {}).get("duration_ms")
    if not ms:
        return None
    return int(round(int(ms) / 1000))


def _track_anchor_ms(runtime) -> int:
    """Wall-clock epoch-ms at which the current track's playback position
    was 0. Used to compute absolute display_at times for each lyric line:
    `display_at_ms = anchor_ms + (line.start - timing_offset) * 1000`
    (positive timing_offset advances the lyrics, so it subtracts).

    Returns the current time if no position has been captured yet, so the
    payload still includes a sane (if conservative) anchor."""
    if not runtime.position_at:
        return int(time.time() * 1000)
    position = float(runtime.track.get("position") or 0.0)
    return int((runtime.position_at - position) * 1000)


@dataclass
class PlayerRuntime:
    """Per-player state — never module globals (AUDIT.md cluster D)."""
    key: str
    mode: str = "queue"             # "queue" | "stream"
    track: dict = field(default_factory=dict)
    lyrics: Optional[LyricsData] = None
    lyrics_key: Optional[str] = None  # "artist|title" the lyrics belong to
    lyrics_db: Optional[dict] = None  # per-song DB (all providers) for +/- cycling
    lyrics_task: object = None        # background fetch task for the current track
    preferred_provider: Optional[str] = None  # persisted +/- pick for this song
    lyrics_attempts: set = field(default_factory=set)  # providers tried on demand
    lyrics_empty: set = field(default_factory=set)     # providers tried, no lyrics
    bad_match_index: int = 0          # "bad match" rescue level (cleaned-search variant)
    timing_offset: float = 0.0        # manual lyric-timing offset (s), remembered per song
    suppress_lyrics: bool = False     # user flagged "no good lyrics" -> hide for this song
    rec_stream: Optional[str] = None  # RTP stream this player recognizes on (None = metadata-driven)
    position_at: float = 0.0          # wall-clock when track["position"] was computed (for live extrapolation)
    log_key: Optional[str] = None     # last (mode,title) we logged
    log_line: Optional[str] = None    # last current-lyric line we logged
    log_class: Optional[str] = None   # last MA classification we logged
    log_assoc: Optional[str] = None   # last source-association state we logged


class Controller:
    def __init__(self, ma=None, capture=None):
        self.ma = ma
        self.capture = capture
        self.lyrics_service = LyricsService()
        # player key -> (PlayerRecognizer, stream_key). One recognizer per player.
        self.recognizers: Dict[str, tuple] = {}
        # Serializes recognizer start/stop so a concurrent call can't leave two
        # recognizers running for one player.
        self._rec_lock = asyncio.Lock()
        self.runtimes: Dict[str, PlayerRuntime] = {}
        # display-name overrides set via the rename UI
        self.renames: Dict[str, str] = {}
        # Engine leases: player key -> expiry timestamp. Any consumer (web page or
        # ESPHome display) polling /current-track or /lyrics renews the lease; the
        # supervisor runs the pipeline only for players with a live lease. This is
        # what lets an ESPHome display drive its own lyrics without a web page open.
        self.leases: Dict[str, float] = {}
        self._supervisor_task: Optional[asyncio.Task] = None
        # Explicit mic -> speaker association (set by the bridge). Maps a player key
        # (a mic) to the MA player id whose audio it's hearing, so metadata-first
        # reads the speaker's now-playing instead of recognising the mic. Radio /
        # no-metadata still falls back to recognising the mic's own audio.
        self.source_players: Dict[str, str] = {}
        # Direct source metadata pushed by the bridge (NLJ ≥ 1.0.64). Maps player
        # key -> (recv_time, track_dict). Used when the bridge polls with
        # source_title=… etc — built straight from the speaker's HA media_player
        # entity, so no MA round-trip and no player-id resolution is needed.
        # Entries older than SOURCE_META_TTL are treated as absent (covers the
        # bridge-crash case) so a stale title can't keep recognition off forever.
        self.source_meta: Dict[str, tuple] = {}
        # Last (key, title, aliases) we logged for set-source-meta, so the line
        # only fires on a real change instead of on every 1 s bridge poll.
        self._source_meta_log: Dict[str, tuple] = {}

    # -- player discovery / resolution ------------------------------------ #

    def list_players(self) -> dict:
        configured = PLAYERS["configured"]
        streams = self.capture.list_streams() if self.capture else []
        ma_players = self.ma.list_players() if (self.ma and self.ma.connected) else []

        # Collapse multiple RTP streams from the SAME device to one entry. A
        # respeaker reconnects with a fresh SSRC, leaving stale siblings behind
        # (same name + IP / MA id) that would otherwise each show in the picker.
        # Keep the active one, else the most-recently-seen.
        def identity(s):
            return s.get("ma_player_id") or (s.get("name"), s.get("source_ip"))
        best = {}
        for s in streams:
            # Only de-dupe streams that have an identity to share; keep anonymous
            # ones (no MA id and no name) separate so distinct devices aren't merged.
            if not s.get("ma_player_id") and not s.get("name"):
                best[s["key"]] = s
                continue
            ident = identity(s)
            cur = best.get(ident)
            if (cur is None
                    or (s.get("active") and not cur.get("active"))
                    or (s.get("active") == cur.get("active")
                        and s.get("last_seen", 0) > cur.get("last_seen", 0))):
                best[ident] = s
        streams = list(best.values())

        assigned_keys = set()
        players = []
        for c in configured:
            players.append({
                "name": self.renames.get(c["name"], c["name"]),
                "key": c["name"],
                "ma_player_id": c.get("music_assistant_player_id"),
                "source_ip": c.get("source_ip"),
                "ssrc": c.get("rtp_ssrc"),
                "assigned": True,
                "active": True,
            })
        unassigned = []
        for s in streams:
            if s.get("name") and any(c.get("name") == s["name"] for c in configured):
                continue
            entry = {
                "name": self.renames.get(s["key"], s.get("name") or s["key"]),
                "key": s["key"],
                "ma_player_id": s.get("ma_player_id"),
                "source_ip": s.get("source_ip"),
                "ssrc": s.get("ssrc"),
                "assigned": bool(s.get("name")),
                "active": s.get("active", False),
            }
            (players if s.get("name") else unassigned).append(entry)

        return {
            "players": players,
            "unassigned_streams": unassigned,
            "ma_players": ma_players,
        }

    def _friendly_name(self, key: str, name: Optional[str]) -> str:
        """Best display name for a player/stream — never the raw SSRC key.

        Renames win; then the stream's own MA name. When a stream carries no name
        (e.g. the respeaker reconnected with a fresh SSRC and skipped the name
        extension) we borrow the friendly name of a sibling stream on the same
        device (same source_ip / ma_player_id) so the UI shows e.g.
        "respeaker_lyrics" instead of "73D34824"."""
        if key in self.renames:
            return self.renames[key]
        if name and name in self.renames:
            return self.renames[name]
        if name and name != key:
            return name
        if self.capture:
            this = self.capture.get_stream(key)
            if this:
                for s in self.capture.list_streams():
                    sname = s.get("name")
                    if not sname:
                        continue
                    same_device = (
                        (this.source_ip and s.get("source_ip") == this.source_ip)
                        or (this.ma_player_id and s.get("ma_player_id") == this.ma_player_id)
                    )
                    if same_device:
                        return self.renames.get(sname, sname)
        return name or key

    def _resolve(self, param: Optional[str]):
        """Resolve a ?player= value to (key, ma_player_id, stream_key, name)."""
        if not param or param == "auto":
            return self._resolve_auto()

        for c in PLAYERS["configured"]:
            if c["name"] == param:
                stream = self._find_stream(c)
                return (c["name"], c.get("music_assistant_player_id"),
                        stream.key if stream else None,
                        self.renames.get(c["name"], c["name"]))
        # A detected stream key
        if self.capture and self.capture.get_stream(param):
            stream = self.capture.get_stream(param)
            return (param, stream.ma_player_id, param, stream.name or param)
        # An MA player id / name
        if self.ma and self.ma.connected:
            for p in self.ma.list_players():
                if param in (p["player_id"], p["name"]):
                    return (param, p["player_id"], None, p["name"])
        return (param, param, None, param)

    def _resolve_auto(self):
        # Prefer a playing MA player; else the first active RTP stream.
        if self.ma and self.ma.connected:
            players = self.ma.list_players()
            if self.ma.preferred_player_id:
                for p in players:
                    if p["player_id"] == self.ma.preferred_player_id:
                        return (p["player_id"], p["player_id"], None, p["name"])
            if players:
                p = players[0]
                return (p["player_id"], p["player_id"], None, p["name"])
        if self.capture:
            stream = self.capture.first_active_stream()
            if stream:
                return (stream.key, stream.ma_player_id, stream.key, stream.name or stream.key)
        return ("auto", None, None, "Auto")

    def _find_stream(self, configured: dict):
        if not self.capture:
            return None
        ssrc = configured.get("rtp_ssrc")
        ssrc_int = None
        if ssrc:
            try:
                ssrc_int = int(ssrc, 16) if isinstance(ssrc, str) else int(ssrc)
            except ValueError:
                ssrc_int = None
        return self.capture.find_stream(
            ma_player_id=configured.get("music_assistant_player_id"),
            name=configured.get("name"),
            ssrc=ssrc_int,
            source_ip=configured.get("source_ip"),
        )

    # -- recognition management ------------------------------------------- #

    def _resolve_stream_key(self, stream_key, ma_id):
        """Map a candidate key to an actual detected RTP stream.

        The MA player id (e.g. 'player-3') is NOT an RTP stream key (streams are
        keyed by SSRC), so never start a recognizer on it. Prefer the exact stream
        IF it is still live; otherwise migrate to the freshest stream for this
        identity. The respeaker reconnects with a NEW SSRC on a source change
        (e.g. Spotify Connect -> radio), leaving the selected stream dead but still
        in the table for a while — binding it would get no audio.
        """
        if not self.capture:
            return None
        s = self.capture.get_stream(stream_key) if stream_key else None
        if s is not None and getattr(s, "active", True):
            return stream_key
        if ma_id:
            s2 = self.capture.find_stream(ma_player_id=ma_id)
            if s2:
                return s2.key
        s2 = self.capture.first_active_stream()
        if s2:
            return s2.key
        return stream_key if s is not None else None

    async def _ensure_recognizer(self, stream_key):
        """Ensure exactly ONE recognizer per STREAM. Several players (web tabs, an
        ESPHome display, stale/duplicate ids that resolve to the same device) can
        all point at one RTP stream — they must SHARE a single recognizer, not each
        spin up their own and triple the Shazam load. Different streams still get
        their own recognizer, so genuinely distinct mics recognize in parallel.

        Non-destructive: never stops another stream's recognizer (the supervisor
        owns teardown). Held under ``_rec_lock`` so concurrent ticks can't create
        two recognizers for one stream."""
        async with self._rec_lock:
            if not stream_key or not self.capture:
                return None
            rec = self.recognizers.get(stream_key)
            if rec is None:
                from recognition.engine import PlayerRecognizer
                rec = PlayerRecognizer(stream_key, self.capture)
                rec.start()
                self.recognizers[stream_key] = rec
                logger.info("Started recognizer for stream %s", stream_key)
            return rec

    async def _stop_stream_recognizer(self, stream_key):
        """Stop a single stream's recognizer (no player needs it any more)."""
        async with self._rec_lock:
            rec = self.recognizers.pop(stream_key, None)
        if rec is not None:
            await rec.stop()
            logger.info("Stopped recognizer for stream %s", stream_key)

    async def _stop_all_recognizers(self):
        async with self._rec_lock:
            items = list(self.recognizers.items())
            self.recognizers.clear()
        for stream_key, rec in items:
            await rec.stop()
            logger.info("Stopped recognizer for stream %s", stream_key)

    # -- engine supervisor (lease-driven) --------------------------------- #

    def touch_lease(self, param: Optional[str]) -> str:
        """Renew (or create) the engine lease for the player ``param`` resolves to.
        Called on every /current-track and /lyrics poll, by any consumer."""
        key = self._resolve(param)[0]
        self.leases[key] = time.time() + LEASE_TTL
        return key

    # Treat pushed source metadata as stale after this long without a refresh.
    # Matches the engine lease TTL — a bridge that stops polling is reasonably
    # considered offline within the same window.
    SOURCE_META_TTL = 6.0

    def _meta_aliases(self, key: str) -> set:
        """Return every id that could legitimately point at the same physical
        device as ``key``. MA can expose one device under multiple player ids
        (e.g. a sendspin slug ``waveshare-mic-e20b1c`` AND a MAC-address
        wrapper ``30:ED:A0:E2:0B:1C`` for the same mic), so we store and look
        up source_meta under all of them: the literal key, the resolved MA id,
        the friendly name, and any active RTP stream whose name or
        ma_player_id agrees with what we resolved."""
        _k, ma_id, stream_key, name_resolved = self._resolve(key)
        name = self._friendly_name(_k, name_resolved)
        aliases = {key, name}
        if ma_id:
            aliases.add(ma_id)
        if stream_key:
            aliases.add(stream_key)
        if self.capture:
            for s in self.capture.list_streams():
                sname = s.get("name") or ""
                sma = s.get("ma_player_id")
                if (sname and sname == name) or (sma and sma == ma_id):
                    aliases.add(s["key"])
                    if sma:
                        aliases.add(sma)
                    if sname:
                        aliases.add(sname)
        return {a for a in aliases if a}

    def set_source_metadata(self, key: str, args) -> None:
        """Receive direct now-playing metadata from the bridge (Cast / HA
        media_player attrs forwarded straight through). Only acts when the
        caller actually included ``source_title`` in the query — the browser
        polls without any source_* params, and we must NOT clear the bridge's
        cache on its behalf. The bridge can pass an empty ``source_title`` to
        clear the entry explicitly (e.g. on radio); otherwise the 6 s TTL
        handles eventual cleanup."""
        if "source_title" not in args:
            return
        aliases = self._meta_aliases(key)
        title = (args.get("source_title") or "").strip()
        if not title:
            for a in aliases:
                self.source_meta.pop(a, None)
            return
        try:
            pos = float(args.get("source_position") or 0.0)
        except (TypeError, ValueError):
            pos = 0.0
        try:
            dur_ms = int(args.get("source_duration_ms") or 0) or None
        except (TypeError, ValueError):
            dur_ms = None
        is_playing = (args.get("source_is_playing") or "").lower() == "true"
        track = {
            "source": "stream",          # external Cast/Connect — not seekable from us
            "from_ma": False,
            "title": title,
            "artist": (args.get("source_artist") or "").strip() or None,
            "album": (args.get("source_album") or "").strip() or None,
            "album_art_url": (args.get("source_art") or "").strip() or None,
            "position": pos,
            "duration_ms": dur_ms,
            "is_playing": is_playing,
            "seekable": False,
        }
        track["track_id"] = f"{track['artist']}|{track['title']}"
        entry = (time.time(), track)
        for a in aliases:
            self.source_meta[a] = entry
        sig = (title, tuple(sorted(aliases)))
        if self._source_meta_log.get(key) != sig:
            self._source_meta_log[key] = sig
            logger.info("set-source-meta key=%r aliases=%r title=%r",
                        key, sorted(aliases), title)

    def set_source_player(self, key: str, source_player: Optional[str]) -> None:
        """Associate a player (a mic) with the MA player whose audio it's hearing
        (its speaker). ``""`` clears it; ``None`` leaves it unchanged (consumers
        that don't manage associations, e.g. the web UI, simply omit it)."""
        if source_player is None:
            return
        if source_player:
            self.source_players[key] = source_player
        else:
            self.source_players.pop(key, None)

    def track_snapshot(self, param: Optional[str]) -> dict:
        """Read-only view of a player's current track. The HTTP layer never drives
        recognition any more — the supervisor does — so this just returns whatever
        the pipeline last computed (or a 'recognizing' placeholder)."""
        key, _ma_id, _stream_key, name = self._resolve(param)
        name = self._friendly_name(key, name)
        rt = self.runtimes.get(key)
        now_ms = int(time.time() * 1000)
        if rt is None or not rt.track:
            return {"source": rt.mode if rt else "stream", "player": name,
                    "title": None, "artist": None, "is_playing": False,
                    "seekable": False, "server_time_ms": now_ms}
        track = rt.track
        # The supervisor only recomputes the position ~once/second, but the browser
        # polls ~10x/s and re-anchors its flywheel each time — anchoring on a frozen
        # value yanks the clock backwards (a visible lyric shudder). Advance the
        # position by the time elapsed since it was computed so each poll sees a
        # smoothly-progressing value, like the old per-poll recompute did.
        if track.get("is_playing") and track.get("position") is not None and rt.position_at:
            track = {**track, "position": track["position"] + (time.time() - rt.position_at)}
        # NTP-anchor metadata so clients can render lyric lines on absolute
        # wall-clock time (see _lyrics_payload / _track_anchor_ms).
        track = {
            **track,
            "server_time_ms": now_ms,
            "track_anchor_ms": _track_anchor_ms(rt),
            "preload_time": LYRICS["preload_time"],
        }
        return track

    def _player_active(self, key: str) -> bool:
        """Worth running the pipeline for? An RTP stream player must have a live
        stream (no point recognizing silence); players without a gaugeable stream
        (pure MA / Spotify Connect) are cheap (metadata-first, no recognition) so
        they stay desired while leased."""
        if key in self.source_players:
            return True   # metadata comes from the associated speaker, not the mic
        _k, _ma_id, stream_key, _n = self._resolve(key)
        if self.capture and stream_key:
            s = self.capture.get_stream(stream_key)
            if s is not None:
                return bool(getattr(s, "active", False))
        return True

    async def supervise_once(self):
        """One reconcile tick: run the pipeline for every leased+active player and
        tear down recognizers for everyone else."""
        now_ts = time.time()
        for k in [k for k, exp in self.leases.items() if exp <= now_ts]:
            del self.leases[k]
        desired = {k for k in self.leases if self._player_active(k)}
        for k in desired:
            try:
                await self.current_track(k)
            except Exception:  # noqa: BLE001
                logger.exception("pipeline tick failed for %s", k)
        # Keep only the recognizers some desired player is actually recognizing on
        # (several players can share one stream). Streams nobody needs are stopped.
        wanted_streams = {rt.rec_stream for k in desired
                          if (rt := self.runtimes.get(k)) and rt.rec_stream}
        for stream_key in list(self.recognizers):
            if stream_key not in wanted_streams:
                await self._stop_stream_recognizer(stream_key)

    async def run_supervisor(self, interval: float = 1.0):
        while True:
            try:
                await self.supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("supervisor tick failed")
            await asyncio.sleep(interval)

    def start_supervisor(self):
        if self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(self.run_supervisor())
            logger.info("Engine supervisor started")

    async def stop_supervisor(self):
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            self._supervisor_task = None
        await self._stop_all_recognizers()

    # -- current track ----------------------------------------------------- #

    def _log_mode(self, runtime, name, mode, title, extra=""):
        key = f"{mode}|{title}"
        if runtime.log_key != key:
            runtime.log_key = key
            logger.info("current-track player=%s mode=%s title=%s%s", name, mode, title, extra)

    @staticmethod
    def _is_radio(state: PlayerState) -> bool:
        # For radio, MA's "title" is the STATION name (e.g. "Smooth Radio"), not
        # the song — so we must recognize the audio instead of trusting it.
        return (getattr(state, "media_type", "") or "").lower() == "radio"

    def _ma_track(self, state: PlayerState, name: str) -> dict:
        mode = classify_source_mode(state)
        track = {
            "source": mode,
            "from_ma": True,
            "player": name,
            "title": state.title,
            "artist": state.artist,
            "album": state.album,
            "album_art_url": state.image_url,
            "position": state.position,
            "duration_ms": state.duration_ms,
            "is_playing": state.is_playing,
            "seekable": mode == "queue",
            "uri": state.track_uri,
        }
        track["track_id"] = f"{track.get('artist')}|{track.get('title')}"
        return track

    def _try_source_meta(self, runtime, ma_id, key, name,
                         stream_key=None, tag=" (source-meta)"):
        """Return a fresh track dict if the bridge has pushed metadata for
        this device under ANY alias that matches a candidate id for the
        current poll. We try ma_id, key, name, and stream_key — MA can
        expose a single device under multiple player ids (e.g. a sendspin
        slug plus a MAC-address wrapper), so we have to look across all of
        them rather than rely on one canonical key.

        Last-resort fallback: if no candidate matches BUT there's exactly
        one fresh entry in source_meta, return that. This rescues the case
        where a browser holds a SSRC that was dropped from udp_capture
        after the mic stopped streaming (mic_required gate) — the entry is
        still being refreshed by the bridge under its stable aliases, just
        not under the SSRC the browser is asking for. Single-bridged-device
        setups recover transparently; multi-bridge setups don't apply the
        fallback (ambiguous) and the browser sees the placeholder until
        the user repicks.
        """
        candidates = []
        seen = set()
        for cid in (ma_id, key, name, stream_key):
            if cid and cid not in seen:
                seen.add(cid)
                candidates.append(cid)
        now = time.time()
        for cid in candidates:
            meta = self.source_meta.get(cid)
            if meta is None:
                continue
            if (now - meta[0]) > self.SOURCE_META_TTL:
                continue
            return self._emit_source_meta(runtime, meta[1], name, tag)
        # Last-resort: exactly one fresh entry → use it.
        fresh = {id(meta): meta for meta in self.source_meta.values()
                 if (now - meta[0]) <= self.SOURCE_META_TTL}
        if len(fresh) == 1:
            (meta,) = fresh.values()
            return self._emit_source_meta(runtime, meta[1], name,
                                          tag + " single-fallback")
        return None

    def _emit_source_meta(self, runtime, base_track, name, tag):
        track = {**base_track, "player": name}
        runtime.mode = "stream"
        runtime.track = track
        runtime.position_at = time.time()
        runtime.rec_stream = None
        self._log_mode(runtime, name, "stream", track["title"], tag)
        return track

    async def current_track(self, param: Optional[str]) -> dict:
        key, ma_id, stream_key, name = self._resolve(param)
        name = self._friendly_name(key, name)
        runtime = self.runtimes.setdefault(key, PlayerRuntime(key=key))
        # An explicit pick (not Auto) must be honoured: we don't silently migrate
        # it to another live stream, nor borrow an unrelated player's metadata.
        is_auto = not param or param == "auto"

        # 0a) DIRECT SOURCE METADATA (from the bridge, NLJ ≥ 1.0.64). The bridge
        #     reads the speaker's HA media_player attrs (Cast / Sonos / AirPlay /
        #     MA wrapper alike) and forwards them on every poll. We use them as-is
        #     — no MA round-trip and no player-id resolution. Radio is signalled
        #     by absence (the bridge omits source_title when content_type=radio),
        #     so a missing entry naturally falls through to recognition.
        track = self._try_source_meta(runtime, ma_id, key, name,
                                      stream_key=stream_key)
        if track is not None:
            return track

        # 0b) EXPLICIT SOURCE ASSOCIATION (from the bridge, legacy ≤ 1.0.63): this
        #    mic told us which speaker it's listening to. Read THAT speaker's
        #    now-playing — if MA can describe it (Spotify Connect / Spotify /
        #    Apple / local queue), use it and skip recognition entirely. Radio
        #    (station name only) or no metadata falls through to recognising the
        #    mic's own audio below.
        source_id = self.source_players.get(key)
        if source_id and self.ma and self.ma.connected:
            src = await self.ma.get_player_state(source_id)
            # Diagnostic: log what MA returned for the associated source player so a
            # silent step-0 miss can be traced (src=None, title missing, radio, etc.).
            desc = (
                "src=none" if src is None else
                "title=%r artist=%r mt=%s source=%s playing=%s pos=%s"
                % (src.title, src.artist, src.media_type, src.active_source_name,
                   src.is_playing, src.position)
            )
            if runtime.log_assoc != desc:
                runtime.log_assoc = desc
                logger.info("source-assoc player=%s source_id=%s %s",
                            name, source_id, desc)
            if src is not None and src.title and not self._is_radio(src):
                track = self._ma_track(src, name)   # keep the mic's display name
                runtime.mode = track["source"]
                runtime.track = track
                runtime.position_at = time.time()
                runtime.rec_stream = None            # speaker describes it; no recognition
                self._log_mode(runtime, name, track["source"], track["title"])
                return track

        state: Optional[PlayerState] = None
        if self.ma and self.ma.connected and ma_id:
            state = await self.ma.get_player_state(ma_id)

        # The selected key can be a STALE SSRC: the respeaker reconnects with a
        # new SSRC on every source change, so the originally-selected stream key
        # no longer resolves to a live player. Re-point at the stream actually
        # delivering our audio now, so we classify the CURRENT source (radio vs
        # Spotify Connect) instead of a ghost id — otherwise a switch to radio
        # gets stuck behind a lingering "still playing" Connect player (1b below).
        sel_stream = self.capture.get_stream(stream_key) if (self.capture and stream_key) else None
        sel_live = sel_stream is not None and getattr(sel_stream, "active", True)
        # Skip the migration when an explicitly-selected stream is still live —
        # only Auto (or a dead/stale selected stream) re-points to another stream.
        if state is None and self.capture and not (not is_auto and sel_live):
            live = self.capture.first_active_stream()
            if live is not None and live.ma_player_id and live.ma_player_id != ma_id:
                ma_id, stream_key = live.ma_player_id, live.key
                name = self._friendly_name(live.key, live.name)
                # Re-check source_meta with the migrated ma_id: a stale-SSRC
                # poll (browser holding an old SSRC after NLJ restart) maps
                # to the canonical mic id only after this migration, so step
                # 0a above couldn't find the bridge's entry. Without this
                # re-check we'd fall through to step 3 and start recognition
                # for a device the bridge is already feeding metadata for.
                track = self._try_source_meta(runtime, ma_id, key, name,
                                              stream_key=stream_key,
                                              tag=" (source-meta after migrate)")
                if track is not None:
                    return track
                if self.ma and self.ma.connected:
                    state = await self.ma.get_player_state(ma_id)

        if state is not None:
            # Diagnostic: how MA classifies the resolved player (so a misrouted
            # radio/Connect decision can be traced). Deduped per change.
            desc = "mt=%s source=%s playing=%s" % (
                state.media_type, state.active_source_name, state.is_playing)
            if runtime.log_class != desc:
                runtime.log_class = desc
                logger.info("ma-class player=%s %s title=%r", name, desc, state.title)

        # 1) METADATA-FIRST: if MA knows the real track (queue or Spotify
        #    Connect), use it immediately. Radio is excluded — MA's title there is
        #    the station name, so radio falls through to recognition.
        if state is not None and state.title and not self._is_radio(state):
            track = self._ma_track(state, name)
            runtime.mode = track["source"]
            runtime.track = track
            runtime.position_at = time.time()
            runtime.rec_stream = None               # MA describes it; no recognition
            self._log_mode(runtime, name, track["source"], track["title"])
            return track

        # 1b) Grouped / external (Spotify Connect) players: the resolved player may
        #     not be the one MA shows the track on (e.g. a group coordinator or the
        #     Connect source player). When the resolved player gave us no usable
        #     (non-radio) metadata above, follow whichever MA player is actually
        #     playing with known non-radio media. This runs for a SELECTED player
        #     too (not just auto) so a natural Spotify Connect track change is
        #     picked up from MA immediately instead of waiting ~10s for recognition.
        #
        #     BUT skip this when the player we are ACTUALLY listening to is itself
        #     on radio: switching Spotify Connect -> radio can leave the old Connect
        #     player lingering in MA as "playing" with the stale track, and we must
        #     not latch onto it — our player is on radio, so recognition wins.
        resolved_is_radio = state is not None and self._is_radio(state)
        if self.ma and self.ma.connected and not resolved_is_radio:
            playing_id = self.ma.find_playing_player_id()
            # Only follow another player's metadata if it's Auto, or the playing
            # player is the SAME as / grouped with the one we resolved to. A
            # standalone selected device (e.g. a mic unrelated to whatever else is
            # playing) must not be hijacked onto it.
            if (playing_id and playing_id != ma_id
                    and (is_auto or self.ma.players_related(ma_id, playing_id))):
                state2 = await self.ma.get_player_state(playing_id)
                if state2 is not None and state2.title and not self._is_radio(state2):
                    # Keep the selected capture device's name; borrow only the track.
                    track = self._ma_track(state2, name)
                    runtime.mode = track["source"]
                    runtime.track = track
                    runtime.position_at = time.time()
                    runtime.rec_stream = None
                    self._log_mode(runtime, track["player"], track["source"], track["title"])
                    return track

        # 2) Transient MA read failure (None) on a player that was just showing MA
        #    metadata — keep the last track rather than flipping to recognition.
        if state is None and runtime.track.get("from_ma") and runtime.track.get("title"):
            self._log_mode(runtime, name, runtime.mode, runtime.track.get("title"), " (ma-none)")
            return runtime.track

        # 3) FALLBACK: recognition, for streams MA can't describe (e.g. radio with
        #    no now-playing metadata). One recognizer per stream, shared by every
        #    player pointing at it.
        runtime.mode = "stream"
        stream_key = self._resolve_stream_key(stream_key, ma_id)
        runtime.rec_stream = stream_key
        rec = await self._ensure_recognizer(stream_key)
        result = rec.current if rec else None
        if result is None:
            self._log_mode(runtime, name, "stream", None,
                           f" (recognizing, stream={stream_key})")
            runtime.track = {"source": "stream", "player": name, "title": None,
                             "artist": None, "is_playing": False, "seekable": False}
            return runtime.track
        track = {
            "source": "stream",
            "player": name,
            "title": result.title,
            "artist": result.artist,
            "album": result.album,
            "album_art_url": result.album_art_url,
            "position": rec.get_position() or 0.0,
            "duration_ms": int(result.duration * 1000) if result.duration else None,
            "is_playing": rec.is_playing,
            "seekable": False,
            # ACRCloud (ACR priority) may supply a Spotify track id — used to pin
            # lyrics to the exact recording (radio edits/remasters).
            "spotify_id": getattr(result, "spotify_id", None),
            # Shazam usually supplies an ISRC (the recording's unique code) — a
            # second way to pin lyrics to the right recording, available on every
            # Shazam match (no ACR needed).
            "isrc": getattr(result, "isrc", None),
        }
        track["track_id"] = f"{track.get('artist')}|{track.get('title')}"
        runtime.track = track
        runtime.position_at = time.time()
        self._log_mode(runtime, name, "stream", track.get("title"))
        return track

    # -- lyrics ------------------------------------------------------------ #

    def _empty_lyrics(self, track_id=None, pending=False, provider=None,
                      providers=None, no_lyrics=False, timing_offset=0.0,
                      suppress_lyrics=False):
        return {
            "track_id": track_id,
            "has_lyrics": False,
            "is_instrumental": False,
            "provider": provider,
            "providers": providers or [],
            "no_lyrics": no_lyrics,
            "word_sync_provider": None,
            "has_word_sync": False,
            "pending": pending,
            "lines": {"previous": "", "current": "", "next": ""},
            "line_synced": [],
            "word_synced": [],
            "timing_offset": timing_offset,
            "suppress_lyrics": suppress_lyrics,
            "server_time_ms": int(time.time() * 1000),
            "preload_time": LYRICS["preload_time"],
            "track_anchor_ms": None,
            "recognition_silent": False,
        }

    def _recognition_silent(self, runtime) -> bool:
        """True when the player is in stream (mic-recognition) mode and the
        recognizer has missed enough consecutive cycles to suggest the mic
        isn't actually hearing the song. Clients use this to clear the
        on-screen lyric line so silence doesn't leave a stale lyric up."""
        if runtime is None or runtime.mode != "stream" or not runtime.rec_stream:
            return False
        rec = self.recognizers.get(runtime.rec_stream)
        return bool(rec) and rec.failures >= LYRIC_SILENCE_THRESHOLD

    def _lyrics_payload(self, track_id, data, providers, lines, timing_offset=0.0,
                        suppress_lyrics=False, track_anchor_ms=None,
                        recognition_silent=False):
        preload = LYRICS["preload_time"]
        now_ms = int(time.time() * 1000)
        if track_anchor_ms is None:
            # Fallback: anchor is "now minus 0 position" — line scheduling
            # will be approximate. Callers should pass the real anchor.
            track_anchor_ms = now_ms
        # When the mic isn't hearing the song, serve the lines empty AND
        # blank the 3-line view so clients (browser + ESP via bridge)
        # clear the stale line immediately. Keep `has_lyrics`/providers
        # truthful so the +/- and bad-match controls don't disappear.
        if recognition_silent:
            lines = {"previous": "", "current": "", "next": ""}
            line_synced = []
        else:
            line_synced = list(visible_lines(data.line_synced))
        return {
            "track_id": track_id,
            "has_lyrics": data.has_lyrics,
            "is_instrumental": data.is_instrumental,
            "provider": data.line_provider,
            "providers": providers,
            "no_lyrics": False,
            "word_sync_provider": data.word_provider,
            "has_word_sync": data.has_word_sync,
            "pending": False,
            "lines": lines,
            "line_synced": [
                {
                    "start": s,
                    "text": t,
                    # Absolute NTP wall-clock instant at which the device
                    # should render this line. Positive `timing_offset`
                    # ADVANCES the lyrics (shows them earlier — matches the
                    # legacy browser semantics where lyricPos = position +
                    # offset advanced the lookup), so we SUBTRACT it from
                    # the display instant. preload_time is the delivery
                    # lead, NOT subtracted from display_at.
                    "display_at_epoch_ms": int(
                        track_anchor_ms + (s - timing_offset) * 1000
                    ),
                }
                for s, t in line_synced
            ],
            "word_synced": data.word_synced,
            "timing_offset": timing_offset,
            "suppress_lyrics": suppress_lyrics,
            # NTP-sync metadata for the client. The client subtracts its
            # local clock from server_time_ms to estimate the offset, then
            # gates each line on (display_at_epoch_ms - now_server_ms).
            "server_time_ms": now_ms,
            "preload_time": preload,
            "track_anchor_ms": track_anchor_ms,
            "recognition_silent": bool(recognition_silent),
        }

    async def _fetch_lyrics_bg(self, runtime, lyrics_key, artist, title, album,
                               duration, spotify_id=None, search_artist=None,
                               search_title=None, force=False, isrc=None):
        def on_update(data):
            if runtime.lyrics_key == lyrics_key:   # ignore if track changed
                runtime.lyrics = data
                # Refresh the per-song DB so /lyrics can list every provider that
                # returned (for the +/- cycle). Fires only when a provider lands,
                # not per poll, so the disk read is cheap.
                runtime.lyrics_db = self.lyrics_service.read_db(artist, title)
        try:
            await self.lyrics_service.fetch(artist, title, album, duration,
                                            on_update=on_update, spotify_id=spotify_id,
                                            search_artist=search_artist,
                                            search_title=search_title, force=force,
                                            isrc=isrc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Lyrics fetch failed for %s - %s", artist, title)

    async def _fetch_provider_bg(self, runtime, lyrics_key, artist, title, provider):
        """On-demand fetch when the user cycles to a provider not in the cache."""
        def on_update(data, db):
            if runtime.lyrics_key != lyrics_key:
                return
            runtime.lyrics_db = db
            if data is None:                     # tried, no lyrics -> remember it
                runtime.lyrics_empty.add(provider)
        try:
            await self.lyrics_service.fetch_provider(
                artist, title, provider,
                runtime.track.get("album"),
                _track_duration_seconds(runtime.track),
                on_update=on_update,
                spotify_id=runtime.track.get("spotify_id"),
                isrc=runtime.track.get("isrc"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Lyrics provider %s fetch failed for %s - %s",
                             provider, artist, title)

    def _log_line(self, runtime, name, position, line):
        """Log the current synced-lyric line the server is serving, so it can be
        compared against what the browser is actually displaying (and against the
        recognized position). One line per transition."""
        if runtime.log_line != line:
            runtime.log_line = line
            logger.info("lyric-line player=%s pos=%.1fs current=%r", name, position, line)

    async def lyrics(self, param: Optional[str], provider: Optional[str] = None) -> dict:
        key, _, _, name = self._resolve(param)
        runtime = self.runtimes.get(key)
        if runtime is None or not runtime.track.get("title"):
            return self._empty_lyrics()

        artist = runtime.track.get("artist") or ""
        title = runtime.track.get("title") or ""
        track_id = runtime.track.get("track_id")
        lyrics_key = f"{artist}|{title}"

        # New track: clear stale lyrics IMMEDIATELY and kick off a background
        # fetch. The response never blocks on providers; polls pick up lyrics as
        # soon as the first provider returns (see LyricsService.fetch on_update).
        if runtime.lyrics_key != lyrics_key:
            runtime.lyrics_key = lyrics_key
            runtime.lyrics = None
            runtime.lyrics_db = None
            runtime.preferred_provider = None
            runtime.lyrics_attempts = set()
            runtime.lyrics_empty = set()
            # Resume the 'bad match' rescue level the user previously settled on for
            # this song (the cached lyrics already reflect that cleaned search).
            persisted = self.lyrics_service.read_db(artist, title) or {}
            runtime.bad_match_index = int(persisted.get("bad_match_index", 0) or 0)
            runtime.timing_offset = float(persisted.get("timing_offset", 0.0) or 0.0)
            runtime.suppress_lyrics = bool(persisted.get("suppress_lyrics", False))
            if runtime.lyrics_task and not runtime.lyrics_task.done():
                runtime.lyrics_task.cancel()
            # If this song was previously rescued, search with the same cleaned
            # variant (a cache hit still short-circuits, so this only matters when
            # nothing is cached yet).
            sa = st = None
            if runtime.bad_match_index > 0:
                variants = alternate_queries(artist, title)
                if runtime.bad_match_index < len(variants):
                    sa, st = variants[runtime.bad_match_index]
            runtime.lyrics_task = asyncio.create_task(self._fetch_lyrics_bg(
                runtime, lyrics_key, artist, title,
                runtime.track.get("album"),
                _track_duration_seconds(runtime.track),
                runtime.track.get("spotify_id"),
                search_artist=sa, search_title=st,
                isrc=runtime.track.get("isrc"),
            ))

        # The +/- buttons cycle through ALL enabled providers (not only those with
        # lyrics) so any provider can be tried.
        providers = self.lyrics_service.enabled_provider_names()

        # A specific provider was picked. Persist it (remembered on recall), and
        # serve its lyrics — fetching on demand if it isn't cached yet.
        if provider and provider in providers:
            if provider != runtime.preferred_provider:
                runtime.preferred_provider = provider
                self.lyrics_service.set_preferred(artist, title, provider)
            db = runtime.lyrics_db or {}
            cached = self.lyrics_service.lyrics_for_provider(artist, title, db, provider)
            if cached is not None:
                position = runtime.track.get("position", 0.0)
                lines = LyricsService.lines_around(cached, position)
                self._log_line(runtime, name, position, lines.get("current", ""))
                return self._lyrics_payload(track_id, cached, providers, lines,
                                            runtime.timing_offset, runtime.suppress_lyrics,
                                            track_anchor_ms=_track_anchor_ms(runtime),
                                            recognition_silent=self._recognition_silent(runtime))
            if provider in runtime.lyrics_empty:        # tried, has none -> blank
                return self._empty_lyrics(track_id, provider=provider,
                                          providers=providers, no_lyrics=True,
                                          timing_offset=runtime.timing_offset,
                                          suppress_lyrics=runtime.suppress_lyrics)
            if provider not in runtime.lyrics_attempts:  # not tried yet -> fetch it
                runtime.lyrics_attempts.add(provider)
                asyncio.create_task(self._fetch_provider_bg(
                    runtime, lyrics_key, artist, title, provider))
            return self._empty_lyrics(track_id, provider=provider,
                                      providers=providers, pending=True,
                                      timing_offset=runtime.timing_offset,
                                      suppress_lyrics=runtime.suppress_lyrics)

        data = runtime.lyrics
        if data is None:
            return self._empty_lyrics(track_id, providers=providers, pending=True,
                                      timing_offset=runtime.timing_offset,
                                      suppress_lyrics=runtime.suppress_lyrics)

        position = runtime.track.get("position", 0.0)
        lines = LyricsService.lines_around(data, position)
        self._log_line(runtime, name, position, lines.get("current", ""))
        return self._lyrics_payload(track_id, data, providers, lines,
                                    runtime.timing_offset, runtime.suppress_lyrics,
                                    track_anchor_ms=_track_anchor_ms(runtime),
                                    recognition_silent=self._recognition_silent(runtime))

    async def bad_match(self, param: Optional[str]) -> dict:
        """The user flagged the served lyrics as the wrong version. Re-search with
        the next, more-aggressively-cleaned title variant (strip 'Radio Edit',
        '(Remastered)', features, ...) and swap in the result. Each press advances
        one variant; the chosen level is persisted so a recall reuses it. Returns
        ``no_alternate`` when there are no cleaner variants left to try."""
        key, _, _, name = self._resolve(param)
        runtime = self.runtimes.get(key)
        if runtime is None or not runtime.track.get("title"):
            return {"ok": False, "error": "no track"}
        artist = runtime.track.get("artist") or ""
        title = runtime.track.get("title") or ""
        lyrics_key = f"{artist}|{title}"
        variants = alternate_queries(artist, title)
        next_index = runtime.bad_match_index + 1
        if next_index >= len(variants):
            logger.info("bad-match: no other version to try for %s - %s (exhausted)",
                        artist, title)
            return {"ok": True, "no_alternate": True, "index": runtime.bad_match_index}

        sa, st = variants[next_index]
        runtime.bad_match_index = next_index
        self.lyrics_service.set_bad_match_index(artist, title, next_index)
        # Reset the served lyrics + provider state and re-search (forced) with the
        # cleaned terms; polls show 'loading' until the rescue lands.
        runtime.lyrics = None
        runtime.lyrics_db = None
        runtime.preferred_provider = None
        runtime.lyrics_attempts = set()
        runtime.lyrics_empty = set()
        if runtime.lyrics_task and not runtime.lyrics_task.done():
            runtime.lyrics_task.cancel()
        runtime.lyrics_task = asyncio.create_task(self._fetch_lyrics_bg(
            runtime, lyrics_key, artist, title,
            runtime.track.get("album"),
            (runtime.track.get("duration_ms") or 0) // 1000 or None,
            runtime.track.get("spotify_id"),
            search_artist=sa, search_title=st, force=True,
            isrc=runtime.track.get("isrc")))
        logger.info("bad-match: %s - %s -> variant %d/%d ('%s - %s')",
                    artist, title, next_index, len(variants) - 1, sa, st)
        return {"ok": True, "index": next_index, "search": f"{sa} - {st}"}

    def set_lyric_offset(self, param: Optional[str], offset: float,
                         remember: bool) -> dict:
        """Manual lyric-timing offset (seconds). The browser applies the offset to
        rendering live; this only handles the 'memory' persistence: when
        ``remember`` is true the value is stored on the song so it's reapplied next
        time; when false the stored value is forgotten and the offset reset to 0."""
        key, _, _, _ = self._resolve(param)
        runtime = self.runtimes.get(key)
        if runtime is None or not runtime.track.get("title"):
            return {"ok": False, "error": "no track"}
        artist = runtime.track.get("artist") or ""
        title = runtime.track.get("title") or ""
        if remember:
            new_offset = float(offset or 0.0)
            self.lyrics_service.set_timing_offset(artist, title, new_offset)
        else:
            new_offset = 0.0
            self.lyrics_service.clear_timing_offset(artist, title)
        # Push the new value to EVERY runtime currently on this song, not just the
        # caller's. Other clients (web tabs, ESP32 bridge) read their own runtime's
        # timing_offset on each /lyrics poll; without this they'd keep serving the
        # value loaded at track start and only pick up the change on the next play.
        runtime.timing_offset = new_offset
        lyrics_key = f"{artist}|{title}"
        for rt in self.runtimes.values():
            if rt is not runtime and rt.lyrics_key == lyrics_key:
                rt.timing_offset = new_offset
        return {"ok": True, "timing_offset": new_offset}

    def set_suppress_lyrics(self, param: Optional[str], suppress: bool) -> dict:
        """Thumbs-down: the user judged this song has no good lyrics. Persist the
        flag so lyrics are hidden when the song plays again (until the user toggles
        it off to check whether a better option has since become available)."""
        key, _, _, _ = self._resolve(param)
        runtime = self.runtimes.get(key)
        if runtime is None or not runtime.track.get("title"):
            return {"ok": False, "error": "no track"}
        artist = runtime.track.get("artist") or ""
        title = runtime.track.get("title") or ""
        runtime.suppress_lyrics = bool(suppress)
        self.lyrics_service.set_suppress_lyrics(artist, title, runtime.suppress_lyrics)
        return {"ok": True, "suppress_lyrics": runtime.suppress_lyrics}

    # -- transport --------------------------------------------------------- #

    def _live_ma_player_id(self, ma_id, stream_key):
        """Ensure transport targets a REAL Music Assistant player. A selected RTP
        stream key goes stale when the respeaker reconnects with a new SSRC;
        ``_resolve`` then falls back to passing that SSRC as the player id, which MA
        rejects ("Player <ssrc> is not available"). Validate against MA's player
        list and, if invalid, migrate to the live stream's MA id (or whatever MA
        reports as playing)."""
        if not (self.ma and self.ma.connected):
            return ma_id
        players = {p["player_id"] for p in self.ma.list_players()}
        if ma_id and ma_id in players:
            return ma_id
        # Stale/unknown id (e.g. a dead stream's SSRC): migrate to the live RTP
        # stream's MA player...
        if self.capture:
            s = self.capture.get_stream(stream_key) if stream_key else None
            if not (s and getattr(s, "active", True)):
                s = self.capture.first_active_stream()
            if s and s.ma_player_id and s.ma_player_id in players:
                return s.ma_player_id
        # ...or whatever MA currently reports as playing.
        playing = self.ma.find_playing_player_id()
        return playing if playing in players else None

    async def add_to_discovered(self, param: Optional[str],
                                 playlist_name: str = "Discovered Tracks",
                                 provider: str = "spotify") -> dict:
        """Add the player's currently-playing track to a Spotify playlist on
        Music Assistant (creates the playlist on first use). Falls back to a
        provider search by title/artist for recognition-driven tracks that
        don't carry a MA URI."""
        if not (self.ma and self.ma.connected):
            return {"ok": False, "error": "no music assistant"}
        key, _ma_id, _stream_key, _name = self._resolve(param)
        runtime = self.runtimes.get(key)
        if runtime is None or not runtime.track:
            return {"ok": False, "error": "no track identified"}
        track = runtime.track
        if not track.get("title"):
            return {"ok": False, "error": "no track identified"}
        return await self.ma.add_to_playlist(
            track.get("uri"), track.get("artist"), track.get("title"),
            playlist_name=playlist_name, provider=provider,
        )

    async def transport(self, param: Optional[str], action: str, position_ms=None) -> dict:
        key, ma_id, stream_key, _ = self._resolve(param)
        ma_id = self._live_ma_player_id(ma_id, stream_key)
        if not (self.ma and self.ma.connected and ma_id):
            return {"ok": False, "error": "no music assistant player"}
        runtime = self.runtimes.get(key)
        if action == "seek":
            if runtime and runtime.mode == "stream":
                return {"ok": False, "error": "seek unavailable in stream mode"}
            ok = await self.ma.seek(ma_id, int(position_ms or 0))
        else:
            fn = {"play": self.ma.play, "pause": self.ma.pause,
                  "play_pause": self.ma.play_pause, "next": self.ma.next,
                  "previous": self.ma.previous}.get(action)
            if fn is None:
                return {"ok": False, "error": f"unknown action {action}"}
            ok = await fn(ma_id)
        return {"ok": bool(ok)}


def create_app(controller: Controller) -> Quart:
    app = Quart(
        __name__,
        template_folder=str(RESOURCES_DIR / "templates"),
        static_folder=str(RESOURCES_DIR),
        static_url_path="/static",
    )

    @app.route("/")
    async def index():
        # Pass the version so asset URLs can be cache-busted per release.
        return await render_template("index.html", version=VERSION)

    @app.route("/health")
    async def health():
        return jsonify({"status": "ok", "version": VERSION})

    @app.route("/time")
    async def server_time():
        # Tiny endpoint used by browsers / ESPHome devices to compute the
        # offset between their local clock and the server's NTP-synced
        # clock. Returns both wall-clock ms and the configured preload
        # window so clients can render a line exactly at its
        # `display_at_epoch_ms`.
        return jsonify({
            "server_time_ms": int(time.time() * 1000),
            "preload_time": LYRICS["preload_time"],
        })

    @app.route("/players")
    async def players():
        return jsonify(controller.list_players())

    @app.before_serving
    async def _start_engine():
        controller.start_supervisor()

    @app.after_serving
    async def _stop_engine():
        await controller.stop_supervisor()

    @app.route("/current-track")
    async def current_track():
        # Renew the engine lease and return what the supervisor last computed —
        # the HTTP layer no longer drives recognition itself.
        player = request.args.get("player")
        key = controller.touch_lease(player)
        controller.set_source_player(key, request.args.get("source_player"))
        controller.set_source_metadata(key, request.args)
        return jsonify(controller.track_snapshot(player))

    @app.route("/lyrics")
    async def lyrics_route():
        player = request.args.get("player")
        key = controller.touch_lease(player)
        controller.set_source_player(key, request.args.get("source_player"))
        controller.set_source_metadata(key, request.args)
        return jsonify(await controller.lyrics(player, request.args.get("provider")))

    @app.route("/bad-match", methods=["POST"])
    async def bad_match():
        body = await request.get_json(silent=True) or {}
        return jsonify(await controller.bad_match(
            request.args.get("player") or body.get("player")))

    @app.route("/lyric-offset", methods=["POST"])
    async def lyric_offset():
        body = await request.get_json(silent=True) or {}
        return jsonify(controller.set_lyric_offset(
            request.args.get("player") or body.get("player"),
            float(body.get("offset") or 0.0),
            bool(body.get("remember", True))))

    @app.route("/suppress-lyrics", methods=["POST"])
    async def suppress_lyrics():
        body = await request.get_json(silent=True) or {}
        return jsonify(controller.set_suppress_lyrics(
            request.args.get("player") or body.get("player"),
            bool(body.get("suppress", True))))

    @app.route("/add-to-playlist", methods=["POST"])
    async def add_to_playlist():
        body = await request.get_json(silent=True) or {}
        result = await controller.add_to_discovered(
            request.args.get("player") or body.get("player"),
        )
        return jsonify(result)

    @app.route("/transport", methods=["POST"])
    async def transport():
        body = await request.get_json(silent=True) or {}
        result = await controller.transport(
            request.args.get("player") or body.get("player"),
            body.get("action", ""),
            body.get("position_ms"),
        )
        return jsonify(result)

    @app.route("/players/<key>/rename", methods=["POST"])
    async def rename(key):
        body = await request.get_json(silent=True) or {}
        name = body.get("name")
        if name:
            controller.renames[key] = name
        return jsonify({"ok": bool(name)})

    @app.after_request
    async def cache_headers(response):
        if request.path in ("/", "/current-track", "/lyrics", "/players",
                            "/bad-match", "/lyric-offset", "/suppress-lyrics",
                            "/add-to-playlist"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            # Revalidate static assets so a rebuilt add-on always serves fresh
            # JS/CSS (otherwise the browser keeps the old app.js after an update).
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
