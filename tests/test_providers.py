"""Provider/lyrics seam: two-pass selection, DB round-trip, 3-line slicing."""

import os
import sys
import tempfile
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
    # spotify(1) before lrclib(2) before the rest by priority.
    assert names[0] == "spotify"
    assert names.index("lrclib") < names.index("qq")
    assert set(names) == {"spotify", "lrclib", "musixmatch", "netease", "qq"}


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
