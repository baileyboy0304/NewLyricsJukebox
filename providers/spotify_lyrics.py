"""
Spotify Lyrics Provider
Uses Spotify's lyrics API (powered by Musixmatch) through a proxy server
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent)) 

from typing import Optional, Dict, Any, List, Tuple
import requests
import os
import time
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import logging
from .base import LyricsProvider
from providers.spotify_api import get_shared_spotify_client
from logging_config import get_logger
from config import get_provider_config, SPOTIFY

# Configure logging
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

logger = get_logger(__name__)

# Spotify's own (Musixmatch-powered) lyrics, fetched directly from the web-player
# endpoint using an sp_dc cookie — the same method syrics / librelyrics use. This
# is the only way to read Spotify lyrics; the developer client id/secret can't.
_WEB_TOKEN_URL = ("https://open.spotify.com/get_access_token"
                  "?reason=transport&productType=web_player")
_COLOR_LYRICS_URL = ("https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}"
                     "?format=json&vocalRemoval=false&market=from_token")
_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class SpotifyLyrics(LyricsProvider):
    """Spotify lyrics provider using hosted API"""

    # FIX: Class-level flag to log Spotify unavailable only once
    _spotify_unavailable_logged = False

    # Shared web-player access token (derived from sp_dc), cached until expiry.
    _web_token = None
    _web_token_exp = 0.0
    _sp_dc_bad_logged = False
    
    def __init__(self) -> None:
        """Initialize Spotify lyrics provider with config settings"""
        super().__init__(provider_name="spotify")
        
        # Get config settings
        config = get_provider_config("spotify")
        
        # Initialize API settings from config
        self.api_url = config.get('base_url', 'https://spotify-lyrics-api-azure.vercel.app')
        self.sp_dc = SPOTIFY.get("sp_dc", "")
        # NOTE: We use get_shared_spotify_client() lazily in get_lyrics() instead of storing
        # an instance here. This ensures all API calls use the singleton instance and
        # statistics are consolidated across the entire app.

    # -- direct sp_dc lyrics (no third-party proxy) ----------------------- #

    def _get_web_token(self) -> Optional[str]:
        """Exchange the sp_dc cookie for a Spotify web-player access token, cached
        until shortly before it expires. Returns None if sp_dc is missing/invalid."""
        if not self.sp_dc:
            return None
        now = time.time()
        if SpotifyLyrics._web_token and now < SpotifyLyrics._web_token_exp - 30:
            return SpotifyLyrics._web_token
        try:
            resp = requests.get(
                _WEB_TOKEN_URL,
                headers={"Cookie": f"sp_dc={self.sp_dc}", "User-Agent": _WEB_UA,
                         "App-platform": "WebPlayer"},
                timeout=self.timeout,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spotify web token request failed: %s", exc)
            return None
        token = data.get("accessToken")
        if data.get("isAnonymous") or not token:
            if not SpotifyLyrics._sp_dc_bad_logged:
                logger.warning("Spotify sp_dc cookie invalid/expired — got an anonymous "
                               "token; lyrics unavailable until it's refreshed")
                SpotifyLyrics._sp_dc_bad_logged = True
            return None
        SpotifyLyrics._sp_dc_bad_logged = False
        SpotifyLyrics._web_token = token
        SpotifyLyrics._web_token_exp = data.get("accessTokenExpirationTimestampMs", 0) / 1000.0
        logger.info("Spotify web-player token obtained from sp_dc cookie")
        return token

    def _fetch_color_lyrics(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Fetch synced lyrics straight from Spotify's color-lyrics endpoint."""
        token = self._get_web_token()
        if not token:
            return None
        try:
            resp = requests.get(
                _COLOR_LYRICS_URL.format(track_id=track_id),
                headers={"authorization": f"Bearer {token}", "app-platform": "WebPlayer",
                         "User-Agent": _WEB_UA},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spotify color-lyrics request failed: %s", exc)
            return None
        if resp.status_code == 404:
            return None                      # genuinely no lyrics for this track
        if resp.status_code == 401:
            SpotifyLyrics._web_token = None  # token rejected -> force refresh next time
            logger.warning("Spotify color-lyrics 401 (token rejected); will refresh sp_dc token")
            return None
        if resp.status_code != 200:
            logger.warning("Spotify color-lyrics returned %s", resp.status_code)
            return None
        try:
            lyr = resp.json().get("lyrics", {})
        except Exception:  # noqa: BLE001
            return None
        if lyr.get("syncType") != "LINE_SYNCED":
            return None                      # only keep time-synced lyrics
        lines = []
        for ln in lyr.get("lines", []):
            words = (ln.get("words") or "").strip()
            if not words or words == "♪":
                continue
            lines.append((int(ln.get("startTimeMs", 0)) / 1000.0, words))
        if lines:
            return {"lyrics": lines, "is_instrumental": False}
        return None

    async def get_lyrics(self, artist: str, title: str, 
                          album: str = None, duration: int = None) -> Optional[Dict[str, Any]]:
        """
        Get lyrics for a track by searching Spotify
        
        Args:
            artist (str): Artist name
            title (str): Song title
            album (str, optional): Album name (not used - Spotify uses track ID)
            duration (int, optional): Duration in seconds (not used - Spotify uses track ID)

        Returns:
            Optional[Dict[str, Any]]: Dictionary containing synced lyrics and metadata
        """
        try:
            # Return None if both artist and title are empty
            if not artist.strip() and not title.strip():
                logger.debug("Spotify - Empty artist and title, skipping lyrics search")
                return None

            # Get the shared singleton instance (consolidates all stats)
            spotify_client = get_shared_spotify_client()
            
            if spotify_client is None or not spotify_client.initialized:
                # FIX: Log only once at DEBUG level - expected when Spotify not configured
                if not SpotifyLyrics._spotify_unavailable_logged:
                    logger.warning("Spotify client not initialized - Spotify lyrics unavailable")
                    SpotifyLyrics._spotify_unavailable_logged = True
                return None
                
            # First try to get currently playing track
            track = await spotify_client.get_current_track()
            
            # If no track is playing or it's a different track, search for the requested track
            # Use case-insensitive comparison to avoid unnecessary searches due to casing differences
            # CRITICAL: Use OR not AND - we must search if EITHER artist OR title differs
            # Using AND caused a bug where skipping to a different song by the same artist
            # would use the cached track URL (wrong song) because only title differed
            track_matches = False
            if track:
                current_artist = track.get('artist', '').lower().strip()
                current_title = track.get('title', '').lower().strip()
                target_artist = artist.lower().strip()
                target_title = title.lower().strip()
                track_matches = (current_artist == target_artist and current_title == target_title)
            
            if not track_matches:
                logger.info(f"Spotify - Searching Spotify for {artist} - {title}")
                # Run blocking search in executor to avoid freezing the UI
                loop = asyncio.get_running_loop()
                track = await loop.run_in_executor(None, spotify_client.search_track, artist, title)
                if not track:
                    logger.info(f"No track found on Spotify for: {artist} - {title}")
                    return None
            
            # Use the track URL
            track_url = track['url']
            loop = asyncio.get_running_loop()

            # PREFERRED: fetch straight from Spotify with the sp_dc cookie (no flaky
            # third-party proxy). Falls through to the proxy below if not configured
            # or it returns nothing.
            track_id = track.get('track_id') or (track_url.rstrip('/').split('/')[-1]
                                                 if track_url else None)
            if self.sp_dc and track_id:
                direct = await loop.run_in_executor(None, self._fetch_color_lyrics, track_id)
                if direct:
                    logger.info(f"Spotify - Lyrics via sp_dc for {artist} - {title} "
                                f"({len(direct['lyrics'])} lines)")
                    return direct

            # CRITICAL FIX: Implement retry logic with exponential backoff
            # Retry on 404 (might be temporary server issue), server errors (5xx), and connection errors
            last_error = None

            for attempt in range(self.retries):
                try:
                    # Run blocking request in executor to avoid freezing the UI
                    response = await loop.run_in_executor(
                        None, 
                        lambda: requests.get(
                            f"{self.api_url}/?url={track_url}&format=lrc",
                            timeout=self.timeout
                        )
                    )
                    
                    # Distinguish between different error types
                    if response.status_code == 404:
                        # 404 might be temporary server issue (Vercel cold start, deployment issue)
                        # Retry 2-3 times before giving up
                        if attempt < self.retries - 1:
                            backoff = 2 * (2 ** attempt)  # Exponential: 3s, 6s, 12s
                            logger.warning(f"Spotify Proxy returned 404 (server might be unavailable), retry {attempt + 1}/{self.retries} in {backoff}s")
                            await asyncio.sleep(backoff)  # Async sleep (non-blocking)
                            continue
                        else:
                            logger.info(f"Spotify Proxy - No lyrics found for {artist} - {title} (404 after {self.retries} attempts)")
                            return None
                    elif response.status_code == 429:
                        # Rate limited - retry with backoff
                        retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                        logger.warning(f"Spotify Proxy rate limited, retrying after {retry_after}s")
                        await asyncio.sleep(retry_after)  # Async sleep (non-blocking)
                        continue
                    elif response.status_code >= 500:
                        # Server error - retry with exponential backoff
                        if attempt < self.retries - 1:
                            backoff = 2 ** attempt  # Exponential: 1s, 2s, 4s
                            logger.warning(f"Spotify Proxy server error {response.status_code}, retry {attempt + 1}/{self.retries} in {backoff}s")
                            await asyncio.sleep(backoff)  # Async sleep (non-blocking)
                            continue
                        else:
                            logger.error(f"Spotify Proxy server error {response.status_code} after {self.retries} attempts")
                            return None
                    elif response.status_code != 200:
                        # Other error codes (4xx except 404/429) - don't retry
                        logger.error(f"Spotify Proxy returned status {response.status_code}")
                        return None
                    
                    # Success - parse response
                    try:
                        data = response.json()
                    except Exception as e:
                        logger.error(f"Spotify Proxy returned invalid JSON: {e}")
                        return None
                    
                    if data.get('error'):
                        logger.error(f"Spotify - API error: {data.get('message')}")
                        return None
                    
                    # Log the response for debugging
                    logger.debug(f"Spotify lyrics response: {data}")
                    
                    # Check if lyrics are properly synced
                    if (data.get('syncType') == 'UNSYNCED' or 
                        not data.get('lines') or 
                        all(line.get('timeTag', '00:00.00') == '00:00.00' for line in data['lines'])):
                        logger.warning(f"Unsynced lyrics found for {artist} - {title}, skipping")
                        return None

                    # Convert to standard format
                    processed_lyrics = []
                    for line in data['lines']:
                        if not line.get('words', '').strip():  # Skip empty lines
                            continue
                            
                        # Convert timeTag to seconds
                        time_parts = line['timeTag'].split(':')
                        seconds = float(time_parts[0]) * 60 + float(time_parts[1])
                        processed_lyrics.append((seconds, line['words']))
                    
                    if processed_lyrics:
                        return {
                            "lyrics": processed_lyrics,
                            "is_instrumental": False
                        }
                    return None
                    
                except requests.exceptions.Timeout:
                    # Timeout - retry with backoff
                    if attempt < self.retries - 1:
                        backoff = 2 ** attempt
                        logger.warning(f"Spotify Proxy timeout, retry {attempt + 1}/{self.retries} in {backoff}s")
                        await asyncio.sleep(backoff)  # Async sleep (non-blocking)
                        continue
                    else:
                        logger.error(f"Spotify Proxy timeout after {self.retries} attempts")
                        return None
                        
                except requests.exceptions.ConnectionError as e:
                    # Connection error - retry with backoff
                    if attempt < self.retries - 1:
                        backoff = 2 ** attempt
                        logger.warning(f"Spotify Proxy connection error, retry {attempt + 1}/{self.retries} in {backoff}s")
                        await asyncio.sleep(backoff)  # Async sleep (non-blocking)
                        continue
                    else:
                        logger.error(f"Spotify Proxy connection error after {self.retries} attempts: {e}")
                        return None

        except Exception as e:
            logger.error(f"Spotify - Error fetching lyrics: {e}")
            return None 