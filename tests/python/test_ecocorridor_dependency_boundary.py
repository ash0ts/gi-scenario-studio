from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
ECO_WORKFLOWS = (
    "ecocorridor-assistant-evaluation.yml",
    "ecocorridor-ci.yml",
    "ecocorridor-observability-integration.yml",
    "ecocorridor-pages.yml",
    "ecocorridor-release.yml",
    "ecocorridor-ridgeway-evidence.yml",
)


def load_install_helper() -> ModuleType:
    path = ROOT / "scripts" / "install_ecocorridor_geospatial_runtime.py"
    spec = importlib.util.spec_from_file_location("ecocorridor_geo_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ecocorridor_preserves_the_provenance_pinned_dependency_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]

    assert "geopandas>=1.0" not in extras["ecocorridor"]
    assert all(not dependency.startswith("swmmanywhere") for dependency in extras["ecocorridor"])

    # Preserve the existing GI Model Starter contract while keeping it out of
    # EcoCorridor's Python 3.12 installation boundary.
    assert "geopandas>=1.0" in extras["geo"]
    assert "swmmanywhere>=0.2.2" in extras["geo"]


def test_report_publisher_dependency_is_isolated_from_shared_gi_observability() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]

    assert extras["ecocorridor-report"] == ["wandb-workspaces==0.4.5"]
    assert "wandb-workspaces==0.4.5" not in extras["observability"]


def test_only_report_publishing_workflows_install_the_report_publisher_extra() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    consumers = {
        path.name
        for path in workflow_root.glob("*.yml")
        if "--extra ecocorridor-report" in path.read_text(encoding="utf-8")
    }

    assert consumers == {
        "ecocorridor-ci.yml",
        "ecocorridor-release.yml",
        "ecocorridor-review-brief-evaluation.yml",
    }


def test_ecocorridor_docs_install_the_isolated_report_publisher_extra() -> None:
    for relative in (
        "docs/ecocorridor/README.md",
        "docs/ecocorridor/wandb-search-report.md",
    ):
        document = (ROOT / relative).read_text(encoding="utf-8")
        assert "--extra ecocorridor-report" in document, relative

    shared_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--extra ecocorridor-report" not in shared_readme


def test_ecocorridor_workflows_never_select_the_gi_geo_extra() -> None:
    for filename in ECO_WORKFLOWS:
        workflow = (ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
        assert "--extra geo" not in workflow, filename


def test_geospatial_installer_enforces_exact_reviewed_runtime() -> None:
    helper = load_install_helper()
    required = "\n".join(
        f"{package}==1.0 --hash=sha256:abc" for package in helper.EXPECTED_PACKAGES
    )

    assert set(helper.validate_exported_requirements(required)) == set(helper.EXPECTED_PACKAGES)

    for forbidden in helper.FORBIDDEN_PACKAGES:
        with pytest.raises(ValueError, match=forbidden):
            helper.validate_exported_requirements(f"{required}\n{forbidden}==1.0")

    with pytest.raises(ValueError, match="unreviewed-package"):
        helper.validate_exported_requirements(f"{required}\nunreviewed-package==1.0")


def test_geospatial_installer_uses_hashes_wheels_and_explicit_pruning() -> None:
    helper = load_install_helper()
    export = helper.export_command(Path("requirements.txt"))
    install = helper.install_command(Path("requirements.txt"))

    assert export.count("--prune") == 2
    assert "swmmanywhere" in export
    assert "pydantic" in export
    assert "--require-hashes" in install
    assert install[install.index("--only-binary") + 1] == ":all:"


def test_geospatial_workflows_use_the_validated_lock_projection() -> None:
    required_workflows = (
        "ecocorridor-ci.yml",
        "ecocorridor-release.yml",
    )
    for filename in required_workflows:
        workflow = (ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
        assert "scripts/install_ecocorridor_geospatial_runtime.py" in workflow, filename
