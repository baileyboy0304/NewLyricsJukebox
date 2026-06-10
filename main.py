"""NewLyricsJukebox entry point.

Starts UDP audio capture, connects to Music Assistant, and serves the web UI on
port 9014 (Quart + Hypercorn). Designed to run as a Home Assistant add-on via
run.sh, and directly for development.
"""

import asyncio
import logging

from config import AUDIO_RECOGNITION, LOG_LEVEL, SERVER, UDP_AUDIO, VERSION
from logging_config import setup_logging
from server import Controller, create_app

logger = logging.getLogger("newlyricsjukebox")


async def _run():
    setup_logging(LOG_LEVEL)
    logger.info("=== NewLyricsJukebox version %s ===", VERSION)

    capture = None
    if UDP_AUDIO["enabled"] and AUDIO_RECOGNITION["enabled"]:
        from recognition.udp_capture import UdpAudioCapture
        capture = UdpAudioCapture(
            port=UDP_AUDIO["port"],
            sample_rate=UDP_AUDIO["sample_rate"],
            channels=UDP_AUDIO["channels"],
        )
        try:
            await capture.start()
            # Warm up Shazam's core now so the first track is recognized fast.
            from recognition.shazam import ShazamRecognizer
            asyncio.create_task(ShazamRecognizer().warmup())
        except OSError as exc:
            logger.error("Could not start UDP capture: %s", exc)
            capture = None

    from music_assistant import MusicAssistant
    ma = MusicAssistant()
    await ma.connect()

    # Warm the Spotify client at boot (it is otherwise created lazily on the first
    # cache-miss lyrics fetch) so its credential / auth status is visible in the
    # startup log instead of staying silent.
    from config import is_provider_enabled
    if is_provider_enabled("spotify"):
        try:
            from providers.spotify_api import get_shared_spotify_client
            get_shared_spotify_client()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spotify client warm-up failed: %s", exc)

    controller = Controller(ma=ma, capture=capture)
    app = create_app(controller)

    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"{SERVER['host']}:{SERVER['port']}"]
    config.use_reloader = False
    config.graceful_timeout = 2
    logger.info("Serving NewLyricsJukebox on %s:%d", SERVER["host"], SERVER["port"])

    try:
        await serve(app, config)
    finally:
        if capture:
            capture.stop()
        await ma.disconnect()


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
