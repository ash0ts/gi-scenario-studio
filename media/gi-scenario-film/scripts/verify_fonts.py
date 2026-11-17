"""Fail closed if a vendored film font or license has drifted."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def main() -> None:
    for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, name = line.split(maxsplit=1)
        path = ROOT / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"Font checksum mismatch for {name}: {actual}")
    print("Film font checksums verified")


if __name__ == "__main__":
    main()
