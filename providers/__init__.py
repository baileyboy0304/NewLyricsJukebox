"""
Lyrics Providers Package
This package contains different providers for fetching synchronized lyrics.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from .base import LyricsProvider
from .lrclib import LRCLIBProvider
from .netease import NetEaseProvider
from .qq import QQMusicProvider
from .musixmatch import MusixmatchProvider

# List of all available providers
available_providers = [
    LRCLIBProvider,
    NetEaseProvider,
    QQMusicProvider,
    MusixmatchProvider,
]

__all__ = [
    'LyricsProvider',
    'LRCLIBProvider',
    'NetEaseProvider',
    'QQMusicProvider',
    'MusixmatchProvider',
    'available_providers'
]
