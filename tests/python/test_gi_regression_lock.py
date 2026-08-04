from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture()
def checker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_gi_regression_lock.py"
    spec = importlib.util.spec_from_file_location("gi_regression_lock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(*, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_lock_rejects_untracked_content_in_gi_owned_paths(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments[0] == "ls-files":
            return _result(stdout="schemas/v2.0/unreviewed.schema.json\n")
        return _result()

    monkeypatch.setattr(checker, "git", fake_git)

    assert checker.main() == 1
    assert "schemas/v2.0/unreviewed.schema.json" in capsys.readouterr().out


def test_lock_requires_the_immutable_base_to_be_an_ancestor(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return _result(returncode=1)
        return _result()

    monkeypatch.setattr(checker, "git", fake_git)

    assert checker.main() == 2
    assert "requires HEAD to descend" in capsys.readouterr().out


def test_lock_covers_gi_ci_generators_and_installer_identity(checker: ModuleType) -> None:
    assert ".github/workflows/ci.yml" in checker.LOCKED_PATHS
    assert "apps/desktop" in checker.LOCKED_PATHS
    assert "scripts/generate_schemas.py" in checker.LOCKED_PATHS
    assert "scripts/generate_guided_ui_fixture.py" in checker.LOCKED_PATHS


def test_gi_and_ecocorridor_release_identities_are_isolated(checker: ModuleType) -> None:
    assert checker.app_identity_issues() == []


def test_existing_gi_dependency_records_survive_eco_additions(checker: ModuleType) -> None:
    if checker.git("cat-file", "-e", f"{checker.BASE_SHA}^{{commit}}").returncode:
        pytest.skip(
            "immutable base is unavailable in this shallow checkout; "
            "the fail-closed Eco isolation job checks it with full history"
        )
    assert checker.dependency_isolation_issues() == []


def test_identity_gate_rejects_a_bundle_identifier_collision(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = checker.json.loads
    call_count = 0

    def colliding_loads(value: str) -> object:
        nonlocal call_count
        result = original_loads(value)
        call_count += 1
        if call_count == 2:
            assert isinstance(result, dict)
            result["identifier"] = checker.EXPECTED_GI_IDENTIFIER
        return result

    monkeypatch.setattr(checker.json, "loads", colliding_loads)

    issues = checker.app_identity_issues()

    assert "GI and EcoCorridor bundle identifiers collide" in issues
