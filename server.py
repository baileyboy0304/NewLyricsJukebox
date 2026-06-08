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
    lyrics_task: object = None        # background fetch task for the current track
    log_key: Optional[str] = None     # last (mode,title) we logged


class Controller:
    def __init__(self, ma=None, capture=None):
        self.ma = ma
        self.capture = capture
        self.lyrics_service = LyricsService()
        self.recognizers: Dict[str, "object"] = {}
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
        keyed by SSRC), so never start a recognizer on it. Prefer an exact match,
        then a stream whose MA identity matches, then the first active stream.
        """
        if not self.capture:
            return None
        if stream_key and self.capture.get_stream(stream_key):
            return stream_key
        if ma_id:
            s = self.capture.find_stream(ma_player_id=ma_id)
            if s:
                return s.key
        s = self.capture.first_active_stream()
        return s.key if s else None

    def _ensure_recognizer(self, stream_key: str):
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

    # -- current track ----------------------------------------------------- #

    def _log_mode(self, runtime, name, mode, title, extra=""):
        key = f"{mode}|{title}"
        if runtime.log_key != key:
            runtime.log_key = key
            logger.info("current-track player=%s mode=%s title=%s%s", name, mode, title, extra)

    async def current_track(self, param: Optional[str]) -> dict:
        key, ma_id, stream_key, name = self._resolve(param)
        runtime = self.runtimes.setdefault(key, PlayerRuntime(key=key))

        state: Optional[PlayerState] = None
        if self.ma and self.ma.connected and ma_id:
            state = await self.ma.get_player_state(ma_id)

        if state is not None:
            mode = classify_source_mode(state)
        elif ma_id:
            # Transient MA read failure on an MA-backed player — do NOT flip to
            # recognition (that would start the respeaker recognizer in queue
            # mode). Keep the last known track until MA responds again.
            self._log_mode(runtime, name, runtime.mode, runtime.track.get("title"), " (ma-none)")
            return runtime.track or {
                "source": runtime.mode, "player": name, "title": None,
                "artist": None, "is_playing": False, "seekable": runtime.mode == "queue",
                "track_id": None}
        else:
            mode = "stream"
        runtime.mode = mode

        if mode == "queue" and state is not None:
            track = {
                "source": "queue",
                "player": name,
                "title": state.title,
                "artist": state.artist,
                "album": state.album,
                "album_art_url": state.image_url,
                "position": state.position,
                "duration_ms": state.duration_ms,
                "is_playing": state.is_playing,
                "seekable": True,
            }
        else:
            stream_key = self._resolve_stream_key(stream_key, ma_id)
            rec = self._ensure_recognizer(stream_key) if stream_key else None
            result = rec.current if rec else None
            if result is None:
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
        self._log_mode(runtime, name, mode, track.get("title"))
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
        try:
            await self.lyrics_service.fetch(artist, title, album, duration, on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Lyrics fetch failed for %s - %s", artist, title)

    async def lyrics(self, param: Optional[str]) -> dict:
        key, _, _, _ = self._resolve(param)
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

        position = runtime.track.get("position", 0.0)
        lines = LyricsService.lines_around(data, position)
        return {
            "track_id": track_id,
            "has_lyrics": data.has_lyrics,
            "is_instrumental": data.is_instrumental,
            "provider": data.line_provider,
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
        return await render_template("index.html")

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
        return jsonify(await controller.lyrics(request.args.get("player")))

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
    async def no_store(response):
        if request.path in ("/", "/current-track", "/lyrics", "/players"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
