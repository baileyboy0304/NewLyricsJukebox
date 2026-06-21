"""Trimmed Music Assistant WebSocket client.

Only what the rebuild needs: connect/auth, list players, read current player
state (title/artist/album/thumbnail/position/duration/source), and transport
control (play/pause/next/previous/seek). Uses the official
``music-assistant-client`` package; the import is lazy so the rest of the app
(and the tests) load without it.
"""

import asyncio
import logging
from typing import List, Optional

from config import MUSIC_ASSISTANT
from ma_models import PlayerState, now

logger = logging.getLogger(__name__)


def _state_str(value) -> str:
    """MA delivers state as an enum (.value) or a plain string. Normalize."""
    if value is None:
        return "idle"
    val = getattr(value, "value", value)
    return str(val).lower()


def _coalesce_media_type(*types) -> Optional[str]:
    """Combine media-type signals, letting ``radio`` win.

    On radio, MA enriches the now-playing into track metadata (album/duration),
    so the queue ``media_item`` can look like a plain track while the player's
    ``current_media`` is still a radio stream. If ANY signal says radio, the
    player is on radio — keep it classified that way so we recognize the audio
    instead of trusting the (often mismatched/lagging) station metadata.
    """
    present = [t for t in types if t and t != "idle"]
    if any(t == "radio" for t in present):
        return "radio"
    return present[0] if present else None


class MusicAssistant:
    def __init__(self):
        self._url = MUSIC_ASSISTANT["server_url"]
        self._token = MUSIC_ASSISTANT["token"]
        self.preferred_player_id = MUSIC_ASSISTANT["player_id"] or None
        self._client = None
        self._listen_task = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> bool:
        if not self._url:
            logger.warning("Music Assistant server URL not configured")
            return False
        try:
            from music_assistant_client import MusicAssistantClient
        except ImportError:
            logger.error("music-assistant-client not installed")
            return False
        try:
            self._client = MusicAssistantClient(
                server_url=self._url,
                aiohttp_session=None,
                token=self._token or None,
            )
            await asyncio.wait_for(self._client.connect(), timeout=5.0)
            self._listen_task = asyncio.create_task(self._client.start_listening())
            logger.info("Connected to Music Assistant at %s", self._url)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Music Assistant connection failed: %s", exc)
            self._client = None
            return False

    async def disconnect(self):
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # -- players ----------------------------------------------------------- #

    def list_players(self) -> List[dict]:
        if not self._client:
            return []
        out = []
        for player in getattr(self._client.players, "players", []):
            out.append({
                "player_id": player.player_id,
                "name": getattr(player, "display_name", None) or player.name,
            })
        return out

    def find_playing_player_id(self) -> Optional[str]:
        """Return the id of a player currently playing with known media. Handles
        grouped / external (Spotify Connect) setups where the player our RTP
        stream resolves to isn't the one MA shows the track on."""
        if not self._client:
            return None
        for player in getattr(self._client.players, "players", []):
            if _state_str(getattr(player, "playback_state", None)) != "playing":
                continue
            media = getattr(player, "current_media", None)
            if media is not None and getattr(media, "title", None):
                return player.player_id
        return None

    def players_related(self, a_id: Optional[str], b_id: Optional[str]) -> bool:
        """True if two MA players are the same, or grouped / synced together.

        Used to decide whether one player's now-playing metadata legitimately
        describes another player's audio (a speaker that's part of the group whose
        coordinator holds the track). A standalone player — e.g. a mic that has
        nothing to do with whatever else is playing — is NOT related, so we must
        not borrow an unrelated player's track for it."""
        if not a_id or not b_id:
            return False
        if a_id == b_id:
            return True
        if not self._client:
            return False

        def obj(pid):
            try:
                return self._client.players.get(pid)
            except Exception:  # noqa: BLE001
                return None

        pa, pb = obj(a_id), obj(b_id)

        def childs(p):
            return set(getattr(p, "group_childs", None) or [])

        # b is a member of a's group (or vice-versa), or one is synced to / shares
        # an active group with the other.
        if pa is not None:
            if b_id in childs(pa) or getattr(pa, "synced_to", None) == b_id \
                    or getattr(pa, "active_group", None) == b_id:
                return True
        if pb is not None:
            if a_id in childs(pb) or getattr(pb, "synced_to", None) == a_id \
                    or getattr(pb, "active_group", None) == a_id:
                return True
        if pa is not None and pb is not None:
            ga = getattr(pa, "active_group", None)
            if ga and ga == getattr(pb, "active_group", None):
                return True
        return False

    async def get_player_state(self, player_id: str) -> Optional[PlayerState]:
        if not self._client:
            return None
        # get_active_queue() may return either a queue id (str) or a PlayerQueue
        # object depending on the client version. Normalize to (queue, queue_id).
        queue = None
        queue_id = player_id
        try:
            active = await self._client.player_queues.get_active_queue(player_id)
        except Exception:  # noqa: BLE001
            active = None
        if active is not None:
            if hasattr(active, "queue_id"):       # it's a PlayerQueue object
                queue = active
                queue_id = active.queue_id
            else:                                  # it's an id string
                queue_id = active
                try:
                    queue = self._client.player_queues.get(queue_id)
                except Exception:  # noqa: BLE001
                    queue = None

        player = self._client.players.get(queue_id) or self._client.players.get(player_id)
        if player is None:
            return None

        player_state = _state_str(getattr(player, "playback_state", None))
        queue_state = _state_str(getattr(queue, "state", None)) if queue else "idle"
        is_playing = player_state == "playing" or queue_state == "playing"
        state = "playing" if is_playing else ("paused" if "pause" in (player_state, queue_state) else "idle")

        # Resolve the active source name from the player's source list.
        active_source_id = getattr(player, "active_source", None)
        active_source_name = None
        for src in getattr(player, "source_list", []) or []:
            if getattr(src, "id", None) == active_source_id:
                active_source_name = getattr(src, "name", None)
                break

        # Identity + timeline: prefer the live queue item, fall back to the
        # player's current_media (external streams).
        title = artist = album = image_url = media_type = track_uri = None
        duration_ms = None
        position = 0.0
        position_last_updated = now()

        item_media_type = current_media_type = None
        current_item = getattr(queue, "current_item", None) if queue else None
        media_item = getattr(current_item, "media_item", None) if current_item else None
        if media_item is not None:
            title = getattr(media_item, "name", None)
            artists = getattr(media_item, "artists", None)
            artist = artists[0].name if artists else getattr(media_item, "artist", None)
            album_obj = getattr(media_item, "album", None)
            album = getattr(album_obj, "name", None) if album_obj else None
            item_media_type = _state_str(getattr(media_item, "media_type", None))
            track_uri = getattr(media_item, "uri", None) or track_uri
        current_media = getattr(player, "current_media", None)
        if current_media is not None:
            title = title or getattr(current_media, "title", None)
            artist = artist or getattr(current_media, "artist", None)
            album = album or getattr(current_media, "album", None)
            image_url = getattr(current_media, "image_url", None)
            current_media_type = _state_str(getattr(current_media, "media_type", None))
            track_uri = track_uri or getattr(current_media, "uri", None)
        # Radio wins over an enriched track type (see _coalesce_media_type).
        media_type = _coalesce_media_type(item_media_type, current_media_type)

        if current_item is not None and getattr(current_item, "duration", None):
            duration_ms = int(current_item.duration * 1000)
        elif current_media is not None and getattr(current_media, "duration", None):
            duration_ms = int(current_media.duration * 1000)

        if queue is not None:
            raw = getattr(queue, "corrected_elapsed_time", None) if is_playing \
                else getattr(queue, "elapsed_time", None)
            position = float(raw or 0.0)
        else:
            raw = getattr(player, "corrected_elapsed_time", None) if is_playing \
                else getattr(player, "elapsed_time", None)
            position = float(raw or 0.0)

        try:
            image_url = image_url or self._client.get_media_item_image_url(current_item, size=320)
        except Exception:  # noqa: BLE001
            pass

        return PlayerState(
            player_id=player_id,
            name=getattr(player, "display_name", None) or player.name,
            state=state,
            title=title,
            artist=artist,
            album=album,
            image_url=image_url,
            position=position,
            duration_ms=duration_ms,
            media_type=media_type,
            queue_id=queue_id,
            active_source_id=active_source_id,
            active_source_name=active_source_name,
            position_last_updated=position_last_updated,
            track_uri=track_uri,
        )

    # -- transport --------------------------------------------------------- #

    async def _player_cmd(self, command: str, player_id: str):
        if not self._client:
            return False
        try:
            await self._client.send_command(f"players/cmd/{command}", player_id=player_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Transport command %s failed: %s", command, exc)
            return False

    async def play(self, player_id):     return await self._player_cmd("play", player_id)
    async def pause(self, player_id):    return await self._player_cmd("pause", player_id)
    async def play_pause(self, player_id): return await self._player_cmd("play_pause", player_id)
    async def next(self, player_id):     return await self._player_cmd("next", player_id)
    async def previous(self, player_id): return await self._player_cmd("previous", player_id)

    # -- playlists --------------------------------------------------------- #

    async def add_to_playlist(
        self,
        track_uri: Optional[str],
        artist: Optional[str],
        title: Optional[str],
        playlist_name: str = "Discovered Tracks",
        provider: str = "spotify",
    ) -> dict:
        """Add a track to the named playlist on the given provider, creating
        the playlist if it doesn't exist. If ``track_uri`` is missing we search
        the provider for ``artist + title`` and use the top hit. Returns a dict
        with ``ok`` plus diagnostic fields."""
        if not self._client:
            return {"ok": False, "error": "not connected to music assistant"}
        try:
            music = self._client.music
            # Resolve the track URI on the target provider when we don't already
            # have one (recognition-driven tracks have only title/artist).
            uri = track_uri
            if not uri:
                if not (title and artist):
                    return {"ok": False, "error": "no track identified"}
                try:
                    from music_assistant_models.enums import MediaType
                    media_types = [MediaType.TRACK]
                except Exception:  # noqa: BLE001
                    media_types = None
                query = f"{artist} {title}"
                kwargs = {"search_query": query, "limit": 5, "provider": provider}
                if media_types is not None:
                    kwargs["media_types"] = media_types
                results = await music.search(**kwargs)
                tracks = getattr(results, "tracks", None) or []
                if not tracks:
                    return {"ok": False, "error": f"no {provider} match for track"}
                uri = tracks[0].uri
            # Find or create the playlist on the requested provider.
            playlist = None
            try:
                playlists = await music.get_library_playlists(
                    search=playlist_name, provider=provider)
            except TypeError:
                playlists = await music.get_library_playlists()
            for p in playlists or []:
                if (getattr(p, "name", None) or "").lower() != playlist_name.lower():
                    continue
                for pm in getattr(p, "provider_mappings", []) or []:
                    if (getattr(pm, "provider_domain", None) == provider
                            or getattr(pm, "provider_instance", None) == provider):
                        playlist = p
                        break
                if playlist:
                    break
            created = False
            if playlist is None:
                playlist = await music.create_playlist(
                    playlist_name, provider_instance_or_domain=provider)
                created = True
            if playlist is None:
                return {"ok": False, "error": "could not create playlist"}
            await music.add_playlist_tracks(playlist.item_id, [uri])
            logger.info("Added %r to playlist %r (created=%s, uri=%s)",
                        title, playlist_name, created, uri)
            return {"ok": True, "uri": uri, "playlist": playlist_name,
                    "created": created}
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_to_playlist failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def seek(self, player_id, position_ms: int):
        """Seek — queue mode only. MA's queue seek takes seconds."""
        if not self._client:
            return False
        try:
            active = await self._client.player_queues.get_active_queue(player_id)
            queue_id = active.queue_id if hasattr(active, "queue_id") else active
            await self._client.player_queues.seek(queue_id, int(position_ms) // 1000)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Seek failed: %s", exc)
            return False
