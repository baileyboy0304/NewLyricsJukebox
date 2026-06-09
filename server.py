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
from lyrics import LyricsData, LyricsService
from ma_models import PlayerState

logger = logging.getLogger(__name__)


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
    log_key: Optional[str] = None     # last (mode,title) we logged
    log_line: Optional[str] = None    # last current-lyric line we logged
    log_class: Optional[str] = None   # last MA classification we logged


class Controller:
    def __init__(self, ma=None, capture=None):
        self.ma = ma
        self.capture = capture
        self.lyrics_service = LyricsService()
        self.recognizers: Dict[str, "object"] = {}
        # Serializes recognizer start/stop so overlapping current-track polls can't
        # interleave mid-swap and leave two recognizers running for one player
        # (the browser polls ~10x/s, so _set_active_recognizer races itself).
        self._rec_lock = asyncio.Lock()
        self.runtimes: Dict[str, PlayerRuntime] = {}
        # display-name overrides set via the rename UI
        self.renames: Dict[str, str] = {}

    # -- player discovery / resolution ------------------------------------ #

    def list_players(self) -> dict:
        configured = PLAYERS["configured"]
        streams = self.capture.list_streams() if self.capture else []
        ma_players = self.ma.list_players() if (self.ma and self.ma.connected) else []

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

    async def _set_active_recognizer(self, stream_key):
        """Run AT MOST ONE recognizer — for the currently selected stream. Any
        recognizer for a different (often stale, new-SSRC) stream is stopped, so
        we don't pile up recognizers all hammering Shazam in parallel.

        Held under ``_rec_lock``: the stop step awaits, and without the lock a
        concurrent poll could create a recognizer in that window, leaving two
        running for one player (the duplicate-everything bug)."""
        async with self._rec_lock:
            for key in list(self.recognizers):
                if key != stream_key:
                    rec = self.recognizers.pop(key)
                    await rec.stop()
                    logger.info("Stopped recognizer for stream %s", key)
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

    async def _stop_all_recognizers(self):
        async with self._rec_lock:
            for key in list(self.recognizers):
                rec = self.recognizers.pop(key)
                await rec.stop()
                logger.info("Stopped recognizer for stream %s", key)

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
        }
        track["track_id"] = f"{track.get('artist')}|{track.get('title')}"
        return track

    async def current_track(self, param: Optional[str]) -> dict:
        key, ma_id, stream_key, name = self._resolve(param)
        name = self._friendly_name(key, name)
        runtime = self.runtimes.setdefault(key, PlayerRuntime(key=key))

        state: Optional[PlayerState] = None
        if self.ma and self.ma.connected and ma_id:
            state = await self.ma.get_player_state(ma_id)

        # The selected key can be a STALE SSRC: the respeaker reconnects with a
        # new SSRC on every source change, so the originally-selected stream key
        # no longer resolves to a live player. Re-point at the stream actually
        # delivering our audio now, so we classify the CURRENT source (radio vs
        # Spotify Connect) instead of a ghost id — otherwise a switch to radio
        # gets stuck behind a lingering "still playing" Connect player (1b below).
        if state is None and self.capture:
            live = self.capture.first_active_stream()
            if live is not None and live.ma_player_id and live.ma_player_id != ma_id:
                ma_id, stream_key = live.ma_player_id, live.key
                name = self._friendly_name(live.key, live.name)
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
            await self._stop_all_recognizers()      # MA describes it; no recognition
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
            if playing_id and playing_id != ma_id:
                state2 = await self.ma.get_player_state(playing_id)
                if state2 is not None and state2.title and not self._is_radio(state2):
                    track = self._ma_track(state2, state2.name or name)
                    runtime.mode = track["source"]
                    runtime.track = track
                    await self._stop_all_recognizers()
                    self._log_mode(runtime, track["player"], track["source"], track["title"])
                    return track

        # 2) Transient MA read failure (None) on a player that was just showing MA
        #    metadata — keep the last track rather than flipping to recognition.
        if state is None and runtime.track.get("from_ma") and runtime.track.get("title"):
            self._log_mode(runtime, name, runtime.mode, runtime.track.get("title"), " (ma-none)")
            return runtime.track

        # 3) FALLBACK: recognition, for streams MA can't describe (e.g. radio with
        #    no now-playing metadata). Exactly one recognizer runs at a time.
        runtime.mode = "stream"
        stream_key = self._resolve_stream_key(stream_key, ma_id)
        rec = await self._set_active_recognizer(stream_key)
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
        }
        track["track_id"] = f"{track.get('artist')}|{track.get('title')}"
        runtime.track = track
        self._log_mode(runtime, name, "stream", track.get("title"))
        return track

    # -- lyrics ------------------------------------------------------------ #

    def _empty_lyrics(self, track_id=None, pending=False):
        return {
            "track_id": track_id,
            "has_lyrics": False,
            "is_instrumental": False,
            "provider": None,
            "word_sync_provider": None,
            "has_word_sync": False,
            "pending": pending,
            "lines": {"previous": "", "current": "", "next": ""},
            "line_synced": [],
            "word_synced": [],
        }

    async def _fetch_lyrics_bg(self, runtime, lyrics_key, artist, title, album, duration):
        def on_update(data):
            if runtime.lyrics_key == lyrics_key:   # ignore if track changed
                runtime.lyrics = data
                # Refresh the per-song DB so /lyrics can list every provider that
                # returned (for the +/- cycle). Fires only when a provider lands,
                # not per poll, so the disk read is cheap.
                runtime.lyrics_db = self.lyrics_service.read_db(artist, title)
        try:
            await self.lyrics_service.fetch(artist, title, album, duration, on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Lyrics fetch failed for %s - %s", artist, title)

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
            if runtime.lyrics_task and not runtime.lyrics_task.done():
                runtime.lyrics_task.cancel()
            runtime.lyrics_task = asyncio.create_task(self._fetch_lyrics_bg(
                runtime, lyrics_key, artist, title,
                runtime.track.get("album"),
                (runtime.track.get("duration_ms") or 0) // 1000 or None,
            ))

        data = runtime.lyrics
        if data is None:
            return self._empty_lyrics(track_id, pending=True)

        # Providers that returned lyrics for this song — the list the +/- buttons
        # cycle through. If the user picked a specific one, serve its lyrics.
        providers = self.lyrics_service.provider_names_in(runtime.lyrics_db or {})
        if provider and provider in providers:
            chosen = self.lyrics_service.lyrics_for_provider(
                artist, title, runtime.lyrics_db, provider)
            if chosen is not None:
                data = chosen

        position = runtime.track.get("position", 0.0)
        lines = LyricsService.lines_around(data, position)
        self._log_line(runtime, name, position, lines.get("current", ""))
        return {
            "track_id": track_id,
            "has_lyrics": data.has_lyrics,
            "is_instrumental": data.is_instrumental,
            "provider": data.line_provider,
            "providers": providers,
            "word_sync_provider": data.word_provider,
            "has_word_sync": data.has_word_sync,
            "pending": False,
            "lines": lines,
            "line_synced": [{"start": s, "text": t} for s, t in data.line_synced],
            "word_synced": data.word_synced,
        }

    # -- transport --------------------------------------------------------- #

    async def transport(self, param: Optional[str], action: str, position_ms=None) -> dict:
        key, ma_id, _, _ = self._resolve(param)
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

    @app.route("/players")
    async def players():
        return jsonify(controller.list_players())

    @app.route("/current-track")
    async def current_track():
        return jsonify(await controller.current_track(request.args.get("player")))

    @app.route("/lyrics")
    async def lyrics_route():
        return jsonify(await controller.lyrics(
            request.args.get("player"), request.args.get("provider")))

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
        if request.path in ("/", "/current-track", "/lyrics", "/players"):
            response.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            # Revalidate static assets so a rebuilt add-on always serves fresh
            # JS/CSS (otherwise the browser keeps the old app.js after an update).
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
