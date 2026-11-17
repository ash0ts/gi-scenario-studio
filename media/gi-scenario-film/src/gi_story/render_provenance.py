"""Deterministic source fingerprints for rendered film clips."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    """Return a lowercase SHA-256 digest for one file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render_input_hashes(film_root: Path, story: dict, segment: dict) -> dict[str, str]:
    """Hash every source that can change a clip's pixels or factual content."""

    repository_root = film_root.parents[1]
    paths = {
        film_root / "story" / "story-manifest.json",
        film_root / "manim.cfg",
        film_root / "pyproject.toml",
        film_root / "uv.lock",
        film_root / "assets" / "fonts" / "SHA256SUMS",
        film_root / "scripts" / "render.py",
        film_root / segment["source_file"],
        *(film_root / "src" / "gi_story").glob("*.py"),
        *(film_root / "assets" / "fonts").glob("*.ttf"),
        *(film_root / "assets" / "fonts").glob("*OFL.txt"),
    }
    for record in story["claim_sources"].values():
        paths.add(repository_root / record["path"])
    return {path.relative_to(repository_root).as_posix(): sha256(path) for path in sorted(paths)}


def render_input_fingerprint(hashes: dict[str, str]) -> str:
    """Collapse the sorted source map into one stable fingerprint."""

    payload = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
