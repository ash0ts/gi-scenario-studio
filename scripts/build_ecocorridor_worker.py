#!/usr/bin/env python3
"""Build a target-specific, self-contained EcoCorridor Python worker."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFESSIONAL_RUNTIME_COLLECTIONS = (
    ("--collect-all", "agents"),
    ("--collect-all", "wandb"),
    ("--collect-all", "weave"),
    ("--hidden-import", "pydantic"),
    ("--hidden-import", "scipy.stats.qmc"),
    ("--hidden-import", "sklearn.ensemble"),
)


def validate_professional_runtime_collection(command: list[str]) -> None:
    """Fail closed if a packaging edit drops a required professional capability."""

    pairs = {tuple(command[index : index + 2]) for index in range(len(command) - 1)}
    missing = [pair for pair in PROFESSIONAL_RUNTIME_COLLECTIONS if pair not in pairs]
    if missing:
        rendered = ", ".join(" ".join(pair) for pair in missing)
        raise ValueError(f"Packaged professional runtime collection is incomplete: {rendered}")
    if ("--collect-all", "sklearn") in pairs:
        raise ValueError("Collect only the required scikit-learn modules, not all of sklearn.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.dist.mkdir(parents=True, exist_ok=True)
    arguments.work.mkdir(parents=True, exist_ok=True)
    build_cache = arguments.work / "build-cache"
    pyinstaller_config = build_cache / "pyinstaller"
    matplotlib_config = build_cache / "matplotlib"
    xdg_cache = build_cache / "xdg"
    for directory in (pyinstaller_config, matplotlib_config, xdg_cache):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "SYSTEMROOT",
            "TMPDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
        }
    }
    # Keep every build cache inside the target-specific work directory.  This
    # prevents a packaging job from reading or writing a developer's global
    # PyInstaller/Matplotlib state and makes parallel target builds isolated.
    environment.update(
        {
            "PYINSTALLER_CONFIG_DIR": str(pyinstaller_config.resolve()),
            "MPLCONFIGDIR": str(matplotlib_config.resolve()),
            "XDG_CACHE_HOME": str(xdg_cache.resolve()),
            "PYTHONHASHSEED": "0",
            "WANDB_CONSOLE": "off",
            "WANDB_DISABLE_CODE": "true",
            "WANDB_DISABLE_GIT": "true",
            "WANDB_ERROR_REPORTING": "false",
            "WANDB_SHOW_RUN": "false",
        }
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "ecocorridor-worker",
        "--collect-submodules",
        "ecocorridor_core",
        "--collect-submodules",
        "scenario_platform",
        "--collect-all",
        "rasterio",
        "--collect-all",
        "geopandas",
        "--collect-all",
        "pyogrio",
        "--collect-all",
        "shapely",
        "--hidden-import",
        "pydantic",
        "--hidden-import",
        "scipy.stats.qmc",
        "--hidden-import",
        "sklearn.ensemble",
        "--collect-all",
        "agents",
        "--collect-all",
        "wandb",
        "--collect-all",
        "weave",
        "--distpath",
        str(arguments.dist.resolve()),
        "--workpath",
        str(arguments.work.resolve()),
        "--specpath",
        str(arguments.work.resolve()),
        str((ROOT / "scripts" / "ecocorridor_worker_entry.py").resolve()),
    ]
    validate_professional_runtime_collection(command)
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    if completed.returncode:
        return completed.returncode
    candidates = list(arguments.dist.glob("ecocorridor-worker*"))
    if len(candidates) != 1 or not candidates[0].is_file():
        print("Expected exactly one packaged EcoCorridor worker.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
