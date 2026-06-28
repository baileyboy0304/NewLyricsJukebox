"""MusicAssistant lifecycle: timeouts + auto-reconnect (regression).

These guard the bug where an HA restart killed the MA websocket but NLJ kept
``self._client`` populated, so MA awaits silently froze the supervisor and
recognition never started. The fix is a background runner that watches the
listen task and a hard timeout on every MA call.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NLJ_DATA_DIR", tempfile.mkdtemp(prefix="nlj_ma_"))
os.environ.setdefault("NLJ_OPTIONS_FILE", "/tmp/nlj_no_options.json")

import music_assistant as ma_mod  # noqa: E402


def _new_ma():
    """Build a MusicAssistant pointed at a dummy URL so connect() exercises the
    lifecycle path (an empty URL short-circuits with a warning)."""
    ma = ma_mod.MusicAssistant()
    ma._url = "http://test-ma:8095"
    return ma


class _FakeClient:
    """Stand-in for music_assistant_client.MusicAssistantClient. ``listen_event``
    decides when start_listening() returns — letting tests script a websocket
    death."""

    def __init__(self, *a, **kw):
        self.listen_event = asyncio.Event()
        self.disconnected = False

    async def connect(self):
        return None

    async def start_listening(self):
        # Returns the moment the test sets listen_event — simulates the
        # music-assistant-client's listener returning when the socket dies.
        await self.listen_event.wait()

    async def disconnect(self):
        self.disconnected = True


class _HangingClient(_FakeClient):
    """connect() never returns — forces the timeout path in _connect_once."""

    async def connect(self):
        await asyncio.Event().wait()


def _install_fake_client_factory(monkeypatch_attr, factory):
    """Patch the lazy import inside _connect_once so we control client creation."""
    import sys as _sys
    import types

    fake_module = types.ModuleType("music_assistant_client")
    fake_module.MusicAssistantClient = factory
    _sys.modules["music_assistant_client"] = fake_module
    return fake_module


def test_connected_clears_when_listen_task_dies_and_runner_reconnects():
    """Regression: HA restart kills the websocket -> start_listening() returns ->
    `connected` must flip to False and the runner must reconnect on its own."""
    async def scenario():
        clients = []

        def factory(*a, **kw):
            c = _FakeClient()
            clients.append(c)
            return c

        _install_fake_client_factory("MusicAssistantClient", factory)
        ma = _new_ma()
        # Tighten reconnect backoff so the test stays fast.
        ma.RECONNECT_INITIAL = 0.05
        ma.RECONNECT_MAX = 0.05

        ok = await ma.connect()
        assert ok is True
        assert ma.connected is True
        assert len(clients) == 1

        # Kill the websocket (start_listening returns).
        clients[0].listen_event.set()
        # Wait for the runner to notice, clear connected, then reconnect.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if ma.connected and len(clients) >= 2:
                break
        assert ma.connected is True, "runner failed to reconnect after socket death"
        assert len(clients) == 2, "expected a fresh client after reconnect"

        await ma.disconnect()
        assert ma.connected is False

    asyncio.run(scenario())


def test_connect_returns_false_when_underlying_connect_hangs():
    """Regression: a stuck MA must not block startup. CALL_TIMEOUT bounds the
    initial connect attempt; connect() returns False rather than hanging."""
    async def scenario():
        _install_fake_client_factory("MusicAssistantClient", _HangingClient)
        ma = _new_ma()
        ma.CALL_TIMEOUT = 0.1
        ma.RECONNECT_INITIAL = 0.05
        ma.RECONNECT_MAX = 0.05

        ok = await ma.connect()
        assert ok is False
        assert ma.connected is False

        await ma.disconnect()

    asyncio.run(scenario())
