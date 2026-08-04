from __future__ import annotations

import importlib.util
import json
import subprocess
from collections import Counter
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


def test_shared_security_migration_is_exact_and_does_not_broaden_lock_drift(
    checker: ModuleType,
) -> None:
    assert checker.SECURITY_MIGRATION_REMOVED_REQUIREMENTS == {"presidio-anonymizer>=2.2.359,<3"}
    assert checker.SECURITY_MIGRATION_REMOVED_LOCK_PACKAGES == {
        "presidio-anonymizer",
    }
    assert checker.EXPECTED_RELEASE_SECURITY_DEPENDENCIES == [
        "PyNaCl==1.6.2",
        "pyinstaller==6.21.0",
    ]
    assert checker.EXPECTED_WEAVE_TRANSITIVE_CRYPTOGRAPHY == "48.0.1"
    assert checker.EXPECTED_WEAVE_CRYPTOGRAPHY_CHAIN == (
        ("weave", "polyfile-weave"),
        ("polyfile-weave", "pdfminer-six"),
        ("pdfminer-six", "cryptography"),
    )
    removed = json.dumps(
        {"name": "cryptography", "version": "48.0.1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    unrelated = json.dumps(
        {"name": "unrelated-gi-package", "version": "1.0.0"},
        sort_keys=True,
        separators=(",", ":"),
    )
    base = Counter({removed: 1, unrelated: 1})

    assert checker._missing_locked_base_names(base, Counter()) == [
        "cryptography",
        "unrelated-gi-package",
    ]


def test_gi_and_ecocorridor_release_identities_are_isolated(checker: ModuleType) -> None:
    assert checker.app_identity_issues() == []


def test_existing_gi_dependency_records_survive_eco_additions(checker: ModuleType) -> None:
    if checker.git("cat-file", "-e", f"{checker.BASE_SHA}^{{commit}}").returncode:
        pytest.skip(
            "immutable base is unavailable in this shallow checkout; "
            "the fail-closed Eco isolation job checks it with full history"
        )
    assert checker.dependency_isolation_issues() == []


def _pnpm_fixture(*, root_version: str, root_integrity: str, child_edge: str) -> str:
    return f"""lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

importers:

  apps/desktop:
    dependencies:
      protected-root:
        specifier: 1.0.0
        version: {root_version}

packages:

  child@2.0.0:
    resolution: {{integrity: sha512-child}}

  protected-root@1.0.0:
    resolution: {{integrity: {root_integrity}}}
    peerDependencies:
      peer: '*'
    peerDependenciesMeta:
      peer:
        optional: true

snapshots:

  child@2.0.0: {{}}

  protected-root@{root_version}:
    dependencies:
      child: {child_edge}
"""


def test_gi_pnpm_closure_rejects_reachable_integrity_drift(checker: ModuleType) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-tampered",
        child_edge="2.0.0",
    )

    issues = checker._pnpm_gi_closure_issues(base, current)

    assert "GI pnpm package metadata or integrity drifted: protected-root@1.0.0" in issues


def test_gi_pnpm_closure_rejects_reachable_dependency_edge_drift(checker: ModuleType) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.1",
    ).replace("child@2.0.0", "child@2.0.1")

    issues = checker._pnpm_gi_closure_issues(base, current)

    assert "GI pnpm dependency edge drifted: protected-root@1.0.0 -> child@2.0.0" in issues


def test_gi_pnpm_closure_rejects_an_added_protected_dependency_edge(checker: ModuleType) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = base.replace(
        "      child: 2.0.0\n",
        "      child: 2.0.0\n      malware: 9.9.9\n",
    ).replace(
        "  protected-root@1.0.0:\n    resolution:",
        "  malware@9.9.9:\n    resolution: {integrity: sha512-malware}\n\n"
        "  protected-root@1.0.0:\n    resolution:",
        1,
    )
    current = current.replace(
        "  protected-root@1.0.0:\n    dependencies:",
        "  malware@9.9.9: {}\n\n  protected-root@1.0.0:\n    dependencies:",
        1,
    )

    issues = checker._pnpm_gi_closure_issues(base, current)

    assert (
        "GI pnpm protected snapshot gained a dependency edge: protected-root@1.0.0 -> malware@9.9.9"
    ) in issues


def test_gi_pnpm_closure_rejects_shared_install_setting_drift(checker: ModuleType) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = base.replace("autoInstallPeers: true", "autoInstallPeers: false")

    assert "shared pnpm lock settings drifted" in checker._pnpm_gi_closure_issues(base, current)


def test_gi_pnpm_closure_never_normalizes_patch_hashes_as_peer_context(
    checker: ModuleType,
) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = base.replace(
        "version: 1.0.0\n",
        "version: 1.0.0(patch_hash=unreviewed)\n",
        1,
    ).replace(
        "  protected-root@1.0.0:\n    dependencies:",
        "  protected-root@1.0.0(patch_hash=unreviewed):\n    dependencies:",
        1,
    )

    issues = checker._pnpm_gi_closure_issues(base, current)

    assert "GI pnpm closure contains an unreviewed patched dependency" in issues


def test_gi_pnpm_closure_allows_only_reviewed_additive_optional_peers(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _pnpm_fixture(
        root_version="1.0.0",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    )
    current = _pnpm_fixture(
        root_version="1.0.0(peer@3.0.0)",
        root_integrity="sha512-reviewed",
        child_edge="2.0.0",
    ).replace(
        "      child: 2.0.0\n",
        "      child: 2.0.0\n    optionalDependencies:\n      peer: 3.0.0\n",
    )
    current = current.replace(
        "  child@2.0.0: {}\n",
        "  child@2.0.0: {}\n\n  peer@3.0.0: {}\n",
    ).replace(
        "  child@2.0.0:\n    resolution: {integrity: sha512-child}\n",
        "  child@2.0.0:\n    resolution: {integrity: sha512-child}\n\n"
        "  peer@3.0.0:\n    resolution: {integrity: sha512-peer}\n",
    )

    packages = checker._section_records(current, "packages")
    snapshots = checker._section_records(current, "snapshots")
    monkeypatch.setattr(
        checker,
        "REVIEWED_GI_PNPM_OPTIONAL_PEER_ADDITIONS",
        {"protected-root@1.0.0": {"peer": "3.0.0"}},
    )
    monkeypatch.setattr(
        checker,
        "REVIEWED_GI_PNPM_PEER_CONTEXT_KEYS",
        {"protected-root@1.0.0": "protected-root@1.0.0(peer@3.0.0)"},
    )
    monkeypatch.setattr(checker, "REVIEWED_GI_PNPM_ADDITIVE_ROOTS", ("peer@3.0.0",))
    monkeypatch.setattr(
        checker,
        "REVIEWED_GI_PNPM_ADDITIVE_CLOSURE_SHA256",
        checker._pnpm_closure_sha256(("peer@3.0.0",), packages, snapshots),
    )

    assert checker._pnpm_gi_closure_issues(base, current) == []


def test_gi_pnpm_closure_locks_reviewed_optional_peer_subtree_bytes(
    checker: ModuleType,
) -> None:
    if checker.git("cat-file", "-e", f"{checker.BASE_SHA}^{{commit}}").returncode:
        pytest.skip("immutable base is unavailable in this shallow checkout")
    base = checker._base_file("pnpm-lock.yaml")
    current = (checker.ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    packages = checker._section_records(current, "packages")
    reviewed = packages["@types/node@22.20.1"]
    tampered = reviewed.replace("sha512-", "sha512-X", 1)
    assert tampered != reviewed

    issues = checker._pnpm_gi_closure_issues(base, current.replace(reviewed, tampered, 1))

    assert any("optional-peer closure bytes drifted" in issue for issue in issues)


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
