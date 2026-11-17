from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_github_release_assets.py"
    spec = importlib.util.spec_from_file_location("verify_github_release_assets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _manifest(verifier: ModuleType) -> dict[str, object]:
    return {
        "schema_version": verifier.SCHEMA_VERSION,
        "assets": [
            {"name": "NOTICE", "size_in_bytes": 6, "digest": _digest(b"notice")},
            {"name": "app.dmg", "size_in_bytes": 3, "digest": _digest(b"dmg")},
        ],
    }


def _release(manifest: dict[str, object]) -> dict[str, object]:
    assets = manifest["assets"]
    assert isinstance(assets, list)
    return {
        "tag_name": "v0.1.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": item["name"],
                "size": item["size_in_bytes"],
                "digest": item["digest"],
                "state": "uploaded",
            }
            for item in assets
        ],
    }


def test_create_manifest_records_sorted_immediate_file_identities(tmp_path: Path) -> None:
    verifier = _load()
    root = tmp_path / "release-assets"
    root.mkdir()
    root.joinpath("app.dmg").write_bytes(b"dmg")
    root.joinpath("NOTICE").write_bytes(b"notice")

    manifest = verifier.create_manifest(root)

    assert manifest == _manifest(verifier)
    assert verifier.validate_manifest(manifest) == {
        "NOTICE": {"name": "NOTICE", "size_in_bytes": 6, "digest": _digest(b"notice")},
        "app.dmg": {"name": "app.dmg", "size_in_bytes": 3, "digest": _digest(b"dmg")},
    }


@pytest.mark.parametrize(
    "bad_kind",
    ["empty", "directory", "symlink", "unsafe-name", "reserved-name"],
)
def test_create_manifest_rejects_non_release_entries(tmp_path: Path, bad_kind: str) -> None:
    verifier = _load()
    root = tmp_path / "release-assets"
    root.mkdir()
    root.joinpath("valid.bin").write_bytes(b"valid")
    if bad_kind == "empty":
        root.joinpath("empty.bin").write_bytes(b"")
    elif bad_kind == "directory":
        root.joinpath("nested").mkdir()
    elif bad_kind == "symlink":
        root.joinpath("linked.bin").symlink_to(root / "valid.bin")
    elif bad_kind == "unsafe-name":
        root.joinpath("unsafe name.bin").write_bytes(b"unsafe")
    else:
        root.joinpath("CON.txt").write_bytes(b"unsafe")

    with pytest.raises(ValueError):
        verifier.create_manifest(root)


def test_verify_release_accepts_exact_uploaded_server_digests() -> None:
    verifier = _load()
    manifest = _manifest(verifier)

    assert (
        verifier.verify_release(
            manifest,
            _release(manifest),
            expected_tag="v0.1.0",
            expected_draft=False,
        )
        == 2
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda release: release.update(tag_name="v0.1.1"), "tag"),
        (lambda release: release.update(draft=True), "draft"),
        (lambda release: release.update(prerelease=True), "prerelease"),
        (lambda release: release["assets"].pop(), "asset set"),
        (
            lambda release: release["assets"].append(
                {
                    "name": "extra.txt",
                    "size": 1,
                    "digest": _digest(b"x"),
                    "state": "uploaded",
                }
            ),
            "asset set",
        ),
        (lambda release: release["assets"][0].update(size=99), "size or server digest"),
        (
            lambda release: release["assets"][0].update(digest="sha256:" + "f" * 64),
            "size or server digest",
        ),
        (lambda release: release["assets"][0].update(state="new"), "not uploaded"),
    ],
)
def test_verify_release_rejects_mismatched_release_evidence(mutation: object, message: str) -> None:
    verifier = _load()
    manifest = _manifest(verifier)
    release = _release(manifest)
    assert callable(mutation)
    mutation(release)

    with pytest.raises(ValueError, match=message):
        verifier.verify_release(
            manifest,
            release,
            expected_tag="v0.1.0",
            expected_draft=False,
        )


def test_verify_release_rejects_duplicate_or_missing_digest_records() -> None:
    verifier = _load()
    manifest = _manifest(verifier)
    release = _release(manifest)
    release["assets"].append(dict(release["assets"][0]))
    with pytest.raises(ValueError, match="duplicated"):
        verifier.verify_release(
            manifest,
            release,
            expected_tag="v0.1.0",
            expected_draft=False,
        )

    release = _release(manifest)
    release["assets"][0]["digest"] = None
    with pytest.raises(ValueError, match="server SHA-256"):
        verifier.verify_release(
            manifest,
            release,
            expected_tag="v0.1.0",
            expected_draft=False,
        )


def test_validate_manifest_rejects_duplicate_and_unsorted_assets() -> None:
    verifier = _load()
    manifest = _manifest(verifier)
    assets = manifest["assets"]
    assert isinstance(assets, list)
    assets.append(dict(assets[0]))
    with pytest.raises(ValueError, match="duplicated"):
        verifier.validate_manifest(manifest)

    manifest = _manifest(verifier)
    assets = manifest["assets"]
    assert isinstance(assets, list)
    assets.reverse()
    with pytest.raises(ValueError, match="strict filename order"):
        verifier.validate_manifest(manifest)


def test_cli_create_and_verify_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load()
    root = tmp_path / "release-assets"
    root.mkdir()
    root.joinpath("app.dmg").write_bytes(b"dmg")
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_github_release_assets.py",
            "create",
            "--assets-directory",
            str(root),
            "--output",
            str(manifest_path),
        ],
    )
    assert verifier.main() == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(_release(manifest)), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_github_release_assets.py",
            "verify",
            "--manifest",
            str(manifest_path),
            "--release-json",
            str(release_path),
            "--expected-tag",
            "v0.1.0",
            "--expected-draft",
            "false",
        ],
    )
    assert verifier.main() == 0
