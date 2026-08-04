from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_visual_regression_uses_reviewed_linux_baselines_without_relaxing_pixels() -> None:
    config = (ROOT / "apps/ecocorridor-desktop/playwright.config.ts").read_text(encoding="utf-8")
    visual_spec = (ROOT / "apps/ecocorridor-desktop/tests/visual.spec.ts").read_text(
        encoding="utf-8"
    )

    assert 'process.platform === "linux" ? "-linux" : ""' in config
    assert "snapshotPlatformSuffix" in config
    assert "maxDiffPixelRatio" not in config
    assert "maxDiffPixels" not in config
    assert "threshold:" not in config
    assert "maxDiffPixelRatio" not in visual_spec
    assert "maxDiffPixels" not in visual_spec
    assert "threshold:" not in visual_spec


def test_every_linux_baseline_has_a_local_renderer_counterpart() -> None:
    screenshots = ROOT / "apps/ecocorridor-desktop/tests/__screenshots__"
    linux_baselines = sorted(screenshots.glob("*-linux.png"))

    assert linux_baselines
    assert all(
        baseline.with_name(baseline.name.removesuffix("-linux.png") + ".png").is_file()
        for baseline in linux_baselines
    )
