"""Provider/lyrics seam: two-pass selection, DB round-trip, 3-line slicing."""

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point storage at a temp dir before importing config/lyrics.
_TMP = tempfile.mkdtemp(prefix="nlj_test_")
os.environ["NLJ_DATA_DIR"] = _TMP

from lyrics import LyricsData, LyricsService  # noqa: E402


def service():
    return LyricsService()


def test_providers_loaded_and_ordered():
    svc = service()
    names = [p.name for p in svc.providers]
    # lrclib(2) is now highest priority (Spotify removed); qq is last.
    assert names[0] == "lrclib"
    assert names.index("lrclib") < names.index("qq")
    assert set(names) == {"lrclib", "musixmatch", "netease", "qq"}


def test_line_sync_selected_by_priority():
    svc = service()
    db = {
        "saved_lyrics": {
            "qq": [[0.0, "qq line"]],
            "lrclib": [[0.0, "lrclib line"]],
        },
        "word_synced_lyrics": {},
        "metadata": {},
    }
    data = svc._select("A", "B", db)
    assert data.line_provider == "lrclib"  # priority 2 beats qq's 5


def test_word_sync_independent_with_boost():
    svc = service()
    db = {
        "saved_lyrics": {"lrclib": [[0.0, "x"]]},
        "word_synced_lyrics": {
            "qq": [{"start": 0, "end": 1, "text": "x", "words": []}],
            "musixmatch": [{"start": 0, "end": 1, "text": "x", "words": []}],
        },
        "metadata": {},
    }
    data = svc._select("A", "B", db)
    # line-sync from lrclib, word-sync prefers musixmatch (3-10 < 5-10).
    assert data.line_provider == "lrclib"
    assert data.word_provider == "musixmatch"
    assert data.has_word_sync


def test_user_preference_overrides():
    svc = service()
    db = {
        "saved_lyrics": {"lrclib": [[0.0, "x"]], "qq": [[0.0, "y"]]},
        "word_synced_lyrics": {},
        "metadata": {},
        "preferred_provider": "qq",
    }
    data = svc._select("A", "B", db)
    assert data.line_provider == "qq"


def test_db_round_trip():
    svc = service()
    svc._write_provider("Artist", "Title", "lrclib",
                        [(1.0, "hello"), (2.0, "world")], {"is_instrumental": False}, [])
    db = svc._read_db("Artist", "Title")
    assert db is not None
    assert db["saved_lyrics"]["lrclib"][0] == [1.0, "hello"]


class _FakeProvider:
    def __init__(self, name, priority, lyrics):
        self.name = name
        self.priority = priority
        self.enabled = True
        self._lyrics = lyrics

    def get_lyrics(self, artist, title, album=None, duration=None):
        return {"lyrics": self._lyrics}


def test_parallel_fetch_selects_best(tmp_path=None):
    """Regression: parallel fetch must not KeyError on as_completed wrappers."""
    import asyncio

    svc = service()
    svc.providers = [
        _FakeProvider("musixmatch", 1, [(0.0, "mxm line")]),
        _FakeProvider("qq", 5, [(0.0, "qq line")]),
    ]
    svc._by_name = {p.name: p for p in svc.providers}

    data = asyncio.run(svc.fetch("FetchArtist", f"FetchTitle-{uuid.uuid4()}"))
    assert data.line_provider == "musixmatch"
    assert data.line_synced[0][1] == "mxm line"


def test_spotify_id_passed_only_to_providers_that_accept_it():
    """A Spotify track id (from ACRCloud) must reach providers whose get_lyrics
    accepts ``spotify_id`` (Musixmatch) and be silently ignored by those that
    don't — without raising a TypeError."""
    import asyncio

    seen = {}

    class _SpotifyAware:
        name, priority, enabled = "musixmatch", 3, True
        async def get_lyrics(self, a, t, al=None, d=None, spotify_id=None):
            seen["mxm"] = spotify_id
            return {"lyrics": [(0.0, "mxm")]}

    class _Plain:
        name, priority, enabled = "lrclib", 2, True
        async def get_lyrics(self, a, t, al=None, d=None):
            seen["lrc"] = True            # would TypeError if spotify_id forced in
            return {"lyrics": [(0.0, "lrc")]}

    svc = service()
    svc.providers = [_Plain(), _SpotifyAware()]
    svc._by_name = {p.name: p for p in svc.providers}

    asyncio.run(svc.fetch("A", f"SpotifyTitle-{uuid.uuid4()}",
                          spotify_id="abc123"))
    assert seen["mxm"] == "abc123"        # aware provider got the id
    assert seen.get("lrc") is True        # plain provider ran, no crash


def test_lines_around():
    data = LyricsData(artist="A", title="B", line_synced=[
        (0.0, "one"), (5.0, "two"), (10.0, "three"),
    ])
    around = LyricsService.lines_around(data, 6.0)
    assert around == {"previous": "one", "current": "two", "next": "three"}


def test_lines_around_before_start():
    data = LyricsData(artist="A", title="B", line_synced=[(5.0, "first")])
    around = LyricsService.lines_around(data, 1.0)
    assert around["current"] == ""


def test_provider_names_in_ordered_by_priority():
    """The +/- cycle lists every provider that returned lyrics, by priority."""
    svc = service()
    db = {"saved_lyrics": {"qq": [[0.0, "q"]], "lrclib": [[0.0, "l"]],
                           "musixmatch": [[0.0, "m"]]}}
    # lrclib(2) < musixmatch(3) < qq(5)
    assert svc.provider_names_in(db) == ["lrclib", "musixmatch", "qq"]
    assert svc.provider_names_in({}) == []


def test_lyrics_for_provider_picks_that_provider():
    """Cycling to a provider serves exactly that provider's lyrics/metadata."""
    svc = service()
    db = {
        "saved_lyrics": {"lrclib": [[0.0, "lrclib line"]],
                         "netease": [[1.0, "netease line"]]},
        "metadata": {"netease": {"is_instrumental": False}},
        "word_synced_lyrics": {},
    }
    data = svc.lyrics_for_provider("A", "B", db, "netease")
    assert data.line_provider == "netease"
    assert data.line_synced == [(1.0, "netease line")]
    assert svc.lyrics_for_provider("A", "B", db, "qq") is None


def test_enabled_provider_names_is_full_cycle_list():
    """The +/- cycle lists ALL enabled providers, not only those with lyrics."""
    svc = service()
    names = svc.enabled_provider_names()
    assert names[0] == "lrclib"             # priority order (Spotify removed)
    assert "musixmatch" in names and "netease" in names


def test_set_preferred_is_used_on_recall():
    """A picked provider persists and is served when the song is recalled."""
    import asyncio
    svc = service()

    class _P:
        def __init__(s, name, pri, lines):
            s.name, s.priority, s.enabled, s._lines = name, pri, True, lines
        async def get_lyrics(s, a, t, al=None, d=None):
            return {"lyrics": s._lines}

    svc.providers = [_P("musixmatch", 1, [(0.0, "mxm")]), _P("lrclib", 2, [(0.0, "lrc")])]
    svc._by_name = {p.name: p for p in svc.providers}

    title = f"PrefTitle-{uuid.uuid4()}"
    # First play caches both; auto picks musixmatch (priority 1).
    data = asyncio.run(svc.fetch("PrefArtist", title))
    assert data.line_provider == "musixmatch"

    # User picks lrclib -> persisted; recall now serves lrclib.
    svc.set_preferred("PrefArtist", title, "lrclib")
    recalled = asyncio.run(svc.fetch("PrefArtist", title))
    assert recalled.line_provider == "lrclib"
    assert recalled.line_synced[0][1] == "lrc"


def test_fetch_provider_caches_one_provider_on_demand():
    """Cycling to an un-cached provider fetches just that provider and caches it."""
    import asyncio
    svc = service()

    class _P:
        name, priority, enabled = "netease", 4, True
        async def get_lyrics(self, a, t, al=None, d=None):
            return {"lyrics": [(0.0, "netease line")]}

    p = _P()
    svc.providers = [p]
    svc._by_name = {p.name: p}
    song = f"Song-{uuid.uuid4()}"
    data = asyncio.run(svc.fetch_provider("OnDemand", song, "netease"))
    assert data is not None and data.line_provider == "netease"
    # Now cached for cycling.
    db = svc.read_db("OnDemand", song)
    assert "netease" in db.get("saved_lyrics", {})
