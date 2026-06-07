"""The spine of the app: one decision drives everything.

``classify_source_mode(player)`` decides whether a player is:

* ``"queue"`` — queue-based (Spotify-via-MA, local media). MA owns a seekable
  queue with a reliable position, so we use MA metadata + position directly and
  run NO audio recognition.
* ``"stream"`` — stream-based (radio, Spotify Connect, AirPlay). MA has no
  authoritative track/position, so we run UDP audio recognition.

The discriminator (strongest signal first), derived from the legacy MA client:

1. ``media_type == "radio"`` -> stream.
2. ``active_source_name`` resolvable -> external if it does NOT end in "Queue".
3. ``active_source_id`` set -> external if it differs from the queue/player id.
4. Fallback staleness heuristic: not playing AND position hasn't updated for >=5s.
"""

from ma_models import PlayerState, now

STALE_SECONDS = 5.0


def classify_source_mode(player: PlayerState) -> str:
    """Return "queue" or "stream" for a player. See module docstring."""
    # 1. Radio is always a stream.
    if (player.media_type or "").lower() == "radio":
        return "stream"

    # 2. Resolved source name is the most reliable signal. MA names the native
    #    queue source "<Player> Queue"; external sources do not.
    if player.active_source_name:
        return "queue" if player.active_source_name.lower().endswith("queue") else "stream"

    # 3. If we only have a source id, compare it to the queue/player id.
    if player.active_source_id:
        owned = {player.queue_id, player.player_id}
        owned.discard(None)
        return "queue" if player.active_source_id in owned else "stream"

    # 4. Last resort: a player that isn't playing and whose position has gone
    #    stale is treated as an external stream we can't track via MA.
    if not player.is_playing and player.position_last_updated:
        if (now() - player.position_last_updated) >= STALE_SECONDS:
            return "stream"

    # Default: assume a healthy MA queue.
    return "queue"
