"""NewLyricsJukebox entry point.

Starts UDP audio capture, connects to Music Assistant, and serves the web UI on
port 9014 (Quart + Hypercorn). Designed to run as a Home Assistant add-on via
run.sh, and directly for development.
"""

import asyncio
import logging

from config import AUDIO_RECOGNITION, LOG_LEVEL, SERVER, UDP_AUDIO
from logging_config import setup_logging
from server import Controller, create_app

logger = logging.getLogger("newlyricsjukebox")


async def _run():
    setup_logging(LOG_LEVEL)

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
        except OSError as exc:
            logger.error("Could not start UDP capture: %s", exc)
            capture = None

    from music_assistant import MusicAssistant
    ma = MusicAssistant()
    await ma.connect()

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
