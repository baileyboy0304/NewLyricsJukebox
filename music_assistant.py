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

    async def get_player_state(self, player_id: str) -> Optional[PlayerState]:
        if not self._client:
            return None
        try:
            queue_id = await self._client.player_queues.get_active_queue(player_id)
        except Exception:  # noqa: BLE001
            queue_id = player_id
        queue = self._client.player_queues.get(queue_id) if queue_id else None
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
        title = artist = album = image_url = media_type = None
        duration_ms = None
        position = 0.0
        position_last_updated = now()

        current_item = getattr(queue, "current_item", None) if queue else None
        media_item = getattr(current_item, "media_item", None) if current_item else None
        if media_item is not None:
            title = getattr(media_item, "name", None)
            artists = getattr(media_item, "artists", None)
            artist = artists[0].name if artists else getattr(media_item, "artist", None)
            album_obj = getattr(media_item, "album", None)
            album = getattr(album_obj, "name", None) if album_obj else None
            media_type = _state_str(getattr(media_item, "media_type", None))
        current_media = getattr(player, "current_media", None)
        if current_media is not None:
            title = title or getattr(current_media, "title", None)
            artist = artist or getattr(current_media, "artist", None)
            album = album or getattr(current_media, "album", None)
            image_url = getattr(current_media, "image_url", None)
            media_type = media_type or _state_str(getattr(current_media, "media_type", None))

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

    async def seek(self, player_id, position_ms: int):
        """Seek — queue mode only. MA's queue seek takes seconds."""
        if not self._client:
            return False
        try:
            queue_id = await self._client.player_queues.get_active_queue(player_id)
            await self._client.player_queues.seek(queue_id, int(position_ms) // 1000)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Seek failed: %s", exc)
            return False
