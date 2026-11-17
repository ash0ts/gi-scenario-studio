# Film fonts

The film uses the same type families as the desktop application:

- IBM Plex Sans Condensed for headings.
- Atkinson Hyperlegible for explanatory copy.
- IBM Plex Mono for measurements, identifiers, and equations.

The binary font files are distributed under the SIL Open Font License. Their
licenses and exact SHA-256 values live beside the fonts. Run
`uv run python scripts/fetch_fonts.py` only when intentionally refreshing the
vendored assets, then review and update `SHA256SUMS`.

Refresh sources are pinned to immutable upstream commits:

- Atkinson Hyperlegible: `1cb311624b2ddf88e9e37873999d165a8cd28b46`
- IBM Plex: `bf260093582f04622aacc1e9f9ca604d7ccd0c42`
