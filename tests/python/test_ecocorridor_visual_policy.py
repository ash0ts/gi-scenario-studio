from __future__ import annotations

import re
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

    # Decision contracts legitimately use a numeric ``threshold`` field. Keep
    # the visual-policy guard scoped to Playwright screenshot assertions so a
    # scientific rule cannot be mistaken for a pixel-tolerance escape hatch.
    screenshot_assertions = re.findall(r"toHaveScreenshot\((.*?)\);", visual_spec, flags=re.DOTALL)
    assert screenshot_assertions
    assert all("maxDiffPixelRatio" not in assertion for assertion in screenshot_assertions)
    assert all("maxDiffPixels" not in assertion for assertion in screenshot_assertions)
    assert all("threshold:" not in assertion for assertion in screenshot_assertions)


def test_every_linux_baseline_has_a_local_renderer_counterpart() -> None:
    screenshots = ROOT / "apps/ecocorridor-desktop/tests/__screenshots__"
    linux_baselines = sorted(screenshots.glob("*-linux.png"))

    assert linux_baselines
    assert all(
        baseline.with_name(baseline.name.removesuffix("-linux.png") + ".png").is_file()
        for baseline in linux_baselines
    )
