"""Controller: lyrics must be non-blocking (defect: 10s+ delay from slow QQ)."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NLJ_DATA_DIR", tempfile.mkdtemp(prefix="nlj_ctl_"))
os.environ.setdefault("NLJ_OPTIONS_FILE", "/tmp/nlj_no_options.json")

from server import Controller, PlayerRuntime  # noqa: E402


class _Fast:
    name = "lrclib"
    priority = 2
    enabled = True

    def get_lyrics(self, artist, title, album=None, duration=None):
        return {"lyrics": [(0.0, "hello"), (5.0, "world")]}


class _SlowFail:
    name = "qq"
    priority = 5
    enabled = True

    def get_lyrics(self, artist, title, album=None, duration=None):
        time.sleep(1.0)   # simulates QQ's slow retries
        return None


def test_lyrics_response_is_non_blocking():
    async def scenario():
        c = Controller(ma=None, capture=None)
        c.lyrics_service.providers = [_Fast(), _SlowFail()]
        c.lyrics_service._by_name = {p.name: p for p in c.lyrics_service.providers}
        c.runtimes["auto"] = PlayerRuntime(
            key="auto",
            track={"title": "Mamma Mia", "artist": "ABBA",
                   "track_id": "ABBA|Mamma Mia", "position": 0.0},
        )

        # First call must return immediately (pending), NOT wait on the slow provider.
        t0 = time.monotonic()
        r1 = await c.lyrics(None)
        assert (time.monotonic() - t0) < 0.3
        assert r1["pending"] is True
        assert r1["has_lyrics"] is False

        # The fast provider should populate lyrics well before the slow one finishes.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if c.runtimes["auto"].lyrics is not None:
                break
        r2 = await c.lyrics(None)
        assert r2["has_lyrics"] is True
        assert r2["provider"] == "lrclib"

        # Clean up the background task.
        task = c.runtimes["auto"].lyrics_task
        if task:
            task.cancel()

    asyncio.run(scenario())
