"""Fetch the exact OFL font binaries used by the film.

This is intentionally separate from the renderer so network access is never required
once the checked-in assets are present.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "assets" / "fonts"
ATKINSON_COMMIT = "1cb311624b2ddf88e9e37873999d165a8cd28b46"
IBM_PLEX_COMMIT = "bf260093582f04622aacc1e9f9ca604d7ccd0c42"

FILES = {
    "AtkinsonHyperlegible-Regular.ttf": f"https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible/{ATKINSON_COMMIT}/fonts/ttf/AtkinsonHyperlegible-Regular.ttf",
    "AtkinsonHyperlegible-Bold.ttf": f"https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible/{ATKINSON_COMMIT}/fonts/ttf/AtkinsonHyperlegible-Bold.ttf",
    "ATKINSON-OFL.txt": f"https://raw.githubusercontent.com/googlefonts/atkinson-hyperlegible/{ATKINSON_COMMIT}/OFL.txt",
    "IBMPlexSansCondensed-SemiBold.ttf": f"https://raw.githubusercontent.com/IBM/plex/{IBM_PLEX_COMMIT}/packages/plex-sans-condensed/fonts/complete/ttf/IBMPlexSansCondensed-SemiBold.ttf",
    "IBMPlexMono-Regular.ttf": f"https://raw.githubusercontent.com/IBM/plex/{IBM_PLEX_COMMIT}/packages/plex-mono/fonts/complete/ttf/IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf": f"https://raw.githubusercontent.com/IBM/plex/{IBM_PLEX_COMMIT}/packages/plex-mono/fonts/complete/ttf/IBMPlexMono-Medium.ttf",
    "IBM-PLEX-OFL.txt": f"https://raw.githubusercontent.com/IBM/plex/{IBM_PLEX_COMMIT}/LICENSE.txt",
}


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        destination = DESTINATION / name
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
        destination.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        print(f"{digest}  {name}")


if __name__ == "__main__":
    main()
