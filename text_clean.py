"""Shared title/artist cleaning (stdlib only).

Used in two places that must agree on what counts as "version noise":
- ``lyrics.alternate_queries`` — the 'bad match' re-search variants.
- ``recognition.engine._same_recording`` — deciding whether a Shazam and an
  ACRCloud match are the SAME recording despite differently-labelled variants
  (e.g. "... (One More Time)" vs "... (2003 Remaster)").
"""

import re

# Parenthetical/bracketed or trailing-dash "version" markers a fuzzy match trips
# over (radio plays an edit/remaster while the lyrics DB has the album cut).
_VERSION_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*\b("
    r"radio edit|edit|remaster(?:ed)?|re-?recorded|version|mix|live|acoustic|"
    r"mono|stereo|deluxe|bonus|single|demo|instrumental|from the film|"
    r"from the motion picture|original|explicit|clean"
    r")\b[^\)\]]*[\)\]]"
    r"|\s*-\s*\b("
    r"radio edit|.*remaster(?:ed)?.*|.*\bversion\b.*|.*\bmix\b.*|live|acoustic|"
    r"single version|mono|stereo"
    r")\s*$",
    re.IGNORECASE,
)
# Featured-artist noise.
_FEATURES = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*$", re.IGNORECASE)


def strip_version_noise(text: str) -> str:
    out = text or ""
    prev = None
    while prev != out:           # repeat: "Song (Live) (Remastered)" -> "Song"
        prev = out
        out = _VERSION_NOISE.sub("", out).strip()
    return re.sub(r"\s{2,}", " ", out).strip() or (text or "").strip()


def strip_features(text: str) -> str:
    out = _FEATURES.sub("", text or "").strip()
    # An artist field can also list collaborators with separators.
    out = re.split(r"\s*(?:,|&|;|/| x | vs\.? )\s*", out, maxsplit=1)[0].strip()
    return out or (text or "").strip()
