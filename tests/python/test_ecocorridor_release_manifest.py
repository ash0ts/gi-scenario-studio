from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from ecocorridor_core.contracts import EngineManifest

pytest.importorskip("nacl")
from nacl.signing import SigningKey


def _load_script(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tauri_updater_signature(
    private_key: SigningKey, *, artifact: bytes, filename: str
) -> tuple[str, str]:
    public_bytes = private_key.verify_key.encode()
    key_id = hashlib.sha256(public_bytes).digest()[:8]
    public_inner = (
        f"untrusted comment: minisign public key {key_id[::-1].hex().upper()}\n"
        + base64.b64encode(b"Ed" + key_id + public_bytes).decode("ascii")
        + "\n"
    )
    primary = private_key.sign(hashlib.blake2b(artifact, digest_size=64).digest()).signature
    trusted = f"timestamp:1785790492\tfile:{filename}"
    global_signature = private_key.sign(primary + trusted.encode("utf-8")).signature
    signature_inner = (
        "untrusted comment: signature from tauri secret key\n"
        + base64.b64encode(b"ED" + key_id + primary).decode("ascii")
        + "\ntrusted comment: "
        + trusted
        + "\n"
        + base64.b64encode(global_signature).decode("ascii")
        + "\n"
    )
    return (
        base64.b64encode(public_inner.encode("utf-8")).decode("ascii"),
        base64.b64encode(signature_inner.encode("utf-8")).decode("ascii"),
    )


def test_worker_packaging_uses_only_worktree_local_caches_and_defensive_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _load_script("build_ecocorridor_worker.py")
    distribution = tmp_path / "dist"
    work = tmp_path / "work"
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **values: object) -> object:
        captured["command"] = command
        captured.update(values)
        distribution.mkdir(parents=True, exist_ok=True)
        (distribution / "ecocorridor-worker").write_bytes(b"packaged worker")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setenv("WANDB_API_KEY", "must-not-reach-packaging")
    monkeypatch.setenv("PYINSTALLER_CONFIG_DIR", "/global/pyinstaller")
    monkeypatch.setenv("MPLCONFIGDIR", "/global/matplotlib")
    monkeypatch.setenv("XDG_CACHE_HOME", "/global/cache")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder.sys,
        "argv",
        [
            "build_ecocorridor_worker.py",
            "--dist",
            str(distribution),
            "--work",
            str(work),
        ],
    )

    assert builder.main() == 0
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "WANDB_API_KEY" not in environment
    assert Path(environment["PYINSTALLER_CONFIG_DIR"]).is_relative_to(work)
    assert Path(environment["MPLCONFIGDIR"]).is_relative_to(work)
    assert Path(environment["XDG_CACHE_HOME"]).is_relative_to(work)
    assert environment["WANDB_DISABLE_CODE"] == "true"
    assert environment["WANDB_DISABLE_GIT"] == "true"
    assert environment["WANDB_ERROR_REPORTING"] == "false"
    command = captured["command"]
    assert isinstance(command, list)
    assert not any(
        command[index : index + 2] == ["--collect-all", "sklearn"]
        for index in range(len(command) - 1)
    )
    for package in ("geopandas", "pyogrio", "rasterio", "shapely"):
        assert any(
            command[index : index + 2] == ["--collect-all", package]
            for index in range(len(command) - 1)
        )


def test_packaged_engine_smoke_requires_hand_calculated_series_result() -> None:
    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    result = {
        "status": "SUCCEEDED",
        "engine_name": "Circuitscape",
        "circuitscape_version": "5.17.1",
        "pair_metrics": [{"effective_resistance": 4.0}],
    }

    smoke.validate_hand_calculated_engine_result(result)
    result["pair_metrics"][0]["effective_resistance"] = 4.01
    with pytest.raises(SystemExit, match="four-ohm series landscape"):
        smoke.validate_hand_calculated_engine_result(result)


def test_packaged_guided_evidence_uses_distinct_public_and_desktop_authorizations(
    tmp_path: Path,
) -> None:
    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    root = Path(__file__).resolve().parents[2]
    data_manifest = root / "benchmarks/ecocorridor/data-manifest.json"
    ridgeway = root / "benchmarks/ecocorridor/ridgeway"
    direct_manifest = ridgeway / "direct-evidence-manifest.json"
    archive_name = json.loads(direct_manifest.read_text(encoding="utf-8"))["evidence_bundle"][
        "filename"
    ]
    projection = ridgeway / "ui_projection.json"
    trust = root / "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json"
    archive = ridgeway / archive_name

    smoke.validate_staged_data_classification(
        data_manifest,
        projection,
        trust,
        direct_manifest,
        archive,
    )
    records = json.loads(data_manifest.read_text(encoding="utf-8"))["files"]
    by_path = {record["path"]: record for record in records}
    assert (
        by_path["benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json"][
            "public_replay_allowed"
        ]
        is False
    )
    assert (
        by_path[f"benchmarks/ecocorridor/ridgeway/{archive_name}"]["public_replay_allowed"] is False
    )

    blocked = tmp_path / "blocked-data-manifest.json"
    for record in records:
        if record["path"] == "apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json":
            record["signed_desktop_bundle_allowed"] = False
    blocked.write_text(json.dumps({"manifest_version": "1.1", "files": records}), encoding="utf-8")
    with pytest.raises(SystemExit, match="exactly the current trusted desktop evidence set"):
        smoke.validate_staged_data_classification(
            blocked,
            projection,
            trust,
            direct_manifest,
            archive,
        )


def test_packaged_boundary_success_receipt_is_unsigned_path_free_ci_evidence(
    tmp_path: Path,
) -> None:
    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    release = tmp_path / "release"
    files = {
        "release-manifest.json": b"signed manifest",
        "release-manifest.sig": b"signature",
        "engine-manifest.json": b"engine manifest",
        "dependency-inventory.json": b"inventory",
        "guided/data-manifest.json": b"data manifest",
        "guided/ridgeway-release-trust.json": b"trust",
        "guided/direct-evidence-manifest.json": b"direct manifest",
        "bin/ecocorridor-worker": b"worker",
        "engine/bin/ecocorridor-engine": b"engine",
    }
    for relative, payload in files.items():
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    public_key = tmp_path / "resource-public-key.hex"
    public_key.write_text("a" * 64 + "\n", encoding="ascii")
    output = tmp_path / "ecocorridor-packaged-boundary-smoke.json"

    smoke.write_success_disposition(
        output,
        release_root=release,
        public_key=public_key,
        expected_version="0.1.0",
        expected_source_commit="b" * 40,
        expected_target="linux-x64",
        signed_manifest={"resources": [{"path": path} for path in sorted(files)]},
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "ecocorridor-packaged-boundary-smoke/v1.0"
    assert receipt["evidence_kind"] == "UNSIGNED_CI_SUCCESS_RECEIPT"
    assert receipt["status"] == "PASSED"
    assert receipt["source_commit_sha"] == "b" * 40
    assert receipt["signed_resource_count"] == len(files)
    assert receipt["ridgeway_release_trust_sha256"] == hashlib.sha256(b"trust").hexdigest()
    assert all(str(tmp_path) not in str(value) for value in receipt.values())
    assert "HAND_CALCULATED_CIRCUITSCAPE_RESULT_REPRODUCED" in receipt["checks"]

    with pytest.raises(SystemExit, match="outside the signed resource tree"):
        smoke.write_success_disposition(
            release / "receipt.json",
            release_root=release,
            public_key=public_key,
            expected_version="0.1.0",
            expected_source_commit="b" * 40,
            expected_target="linux-x64",
            signed_manifest={"resources": []},
        )


def test_packaged_engine_manifest_hashes_final_staged_tree(tmp_path: Path) -> None:
    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    engine = tmp_path / "engine"
    (engine / "bin").mkdir(parents=True)
    executable = engine / "bin" / "ecocorridor-engine"
    executable.write_bytes(b"final staged engine bytes")
    digest = smoke.digest_tree(engine)
    manifest = tmp_path / "engine-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "engine_name": "Circuitscape",
                "julia_version": "1.12.6",
                "circuitscape_version": "5.17.1",
                "julia_runtime_sha256": digest,
                "native_artifacts_sha256": digest,
            }
        ),
        encoding="utf-8",
    )

    smoke.validate_engine_manifest(engine, manifest)
    executable.write_bytes(b"changed after manifest creation")
    with pytest.raises(SystemExit, match="differs from its typed EngineManifest"):
        smoke.validate_engine_manifest(engine, manifest)


def test_packaged_worker_inventory_must_match_declared_native_runtime() -> None:
    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    declared = {
        "schema_version": "1.0",
        "python_version": "3.12.11",
        "python_packages": {
            "geopandas": "1.1.4",
            "numpy": "2.4.1",
            "pyogrio": "0.13.0",
            "pyinstaller": "6.21.0",
            "rasterio": "1.5.0",
            "shapely": "2.1.2",
        },
        "gdal_version": "3.12.1",
        "pyogrio_gdal_version": "3.11.4",
        "proj_version": "9.7.1",
        "geos_version": "3.13.1",
    }
    packaged = {key: value for key, value in declared.items() if key != "python_packages"}
    packaged["python_packages"] = {
        key: value for key, value in declared["python_packages"].items() if key != "pyinstaller"
    }
    smoke.validate_packaged_runtime_inventory(packaged, declared)
    packaged["gdal_version"] = "0.0.0"
    with pytest.raises(SystemExit, match="native runtime differs"):
        smoke.validate_packaged_runtime_inventory(packaged, declared)


def test_engine_stage_dereferences_only_contained_regular_file_symlinks(
    tmp_path: Path,
) -> None:
    stage = _load_script("stage_ecocorridor_release.py")
    engine = tmp_path / "engine"
    library = engine / "lib" / "libsolver.1.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"direct packaged solver library")
    alias = library.with_name("libsolver.dylib")
    alias.symlink_to(library.name)
    versioned_icu = engine / "lib" / "icu" / "76.1"
    versioned_icu.mkdir(parents=True)
    (versioned_icu / "icudata.dat").write_bytes(b"packaged ICU data")
    current_icu = versioned_icu.parent / "current"
    current_icu.symlink_to(versioned_icu.name, target_is_directory=True)
    destination = tmp_path / "staged-engine"

    stage.copy_safe_tree(engine, destination)

    staged_alias = destination / "lib" / alias.name
    assert staged_alias.read_bytes() == library.read_bytes()
    assert staged_alias.is_file()
    assert not staged_alias.is_symlink()
    assert os.path.samefile(staged_alias, destination / "lib" / library.name)
    assert (destination / "lib" / "icu" / "current" / "icudata.dat").read_bytes() == (
        b"packaged ICU data"
    )
    staged_current_icu = destination / "lib" / "icu" / "current"
    assert not staged_current_icu.is_symlink()
    assert not os.path.samefile(staged_current_icu, destination / "lib" / "icu" / "76.1")
    stage.require_symlink_free_tree(destination)


@pytest.mark.parametrize("link_kind", ["external", "ancestor", "cycle", "dangling"])
def test_engine_stage_rejects_unsafe_symlinks(tmp_path: Path, link_kind: str) -> None:
    stage = _load_script("stage_ecocorridor_release.py")
    engine = tmp_path / "engine"
    engine.mkdir()
    link = engine / "unsafe-link"
    if link_kind == "external":
        target = tmp_path / "outside.dylib"
        target.write_bytes(b"outside")
        link.symlink_to(target)
    elif link_kind == "ancestor":
        target = engine / "real-directory"
        target.mkdir()
        link = target / "back-to-engine"
        link.symlink_to(engine, target_is_directory=True)
    elif link_kind == "cycle":
        link.symlink_to("other-link", target_is_directory=True)
        (engine / "other-link").symlink_to(link.name, target_is_directory=True)
    else:
        link.symlink_to("missing.dylib")

    with pytest.raises(SystemExit, match="symlink"):
        stage.require_safe_tree(engine)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file test")
def test_engine_stage_rejects_special_files(tmp_path: Path) -> None:
    stage = _load_script("stage_ecocorridor_release.py")
    engine = tmp_path / "engine"
    engine.mkdir()
    os.mkfifo(engine / "solver-pipe")
    with pytest.raises(SystemExit, match="special file"):
        stage.require_safe_tree(engine)


def test_release_manifest_is_complete_and_ed25519_signed(tmp_path: Path) -> None:
    resources = tmp_path / "release"
    worker = resources / "bin" / "ecocorridor-worker"
    engine = resources / "engine" / "bin" / "ecocorridor-engine"
    worker.parent.mkdir(parents=True)
    engine.parent.mkdir(parents=True)
    worker.write_bytes(b"worker")
    engine.write_bytes(b"engine")

    signing_key = SigningKey.generate()
    key_path = tmp_path / "release-key.seed.hex"
    key_path.write_text(signing_key.encode().hex() + "\n", encoding="ascii")
    key_path.chmod(0o600)
    manifest_path = resources / "release-manifest.json"
    signature_path = resources / "release-manifest.sig"
    public_key_path = tmp_path / "release-public-key.hex"
    command = [
        sys.executable,
        "scripts/create_ecocorridor_release_manifest.py",
        "--root",
        str(resources),
        "--output",
        str(manifest_path),
        "--signature-output",
        str(signature_path),
        "--public-key-output",
        str(public_key_path),
        "--signing-key",
        str(key_path),
        "--application-version",
        "0.1.0",
        "--source-commit",
        "a" * 40,
        "--target",
        "linux-x64",
        "--executable",
        "bin/ecocorridor-worker",
        "--executable",
        "engine/bin/ecocorridor-engine",
    ]
    subprocess.run(command, check=True)

    manifest_bytes = manifest_path.read_bytes()
    public_key = signing_key.verify_key
    public_key.verify(manifest_bytes, signature_path.read_bytes())
    assert public_key_path.read_text(encoding="ascii").strip() == public_key.encode().hex()

    manifest = json.loads(manifest_bytes)
    paths = {item["path"] for item in manifest["resources"]}
    assert paths == {"bin/ecocorridor-worker", "engine/bin/ecocorridor-engine"}
    assert manifest["application_id"] == "org.ecocorridor.scenariostudio"
    assert manifest["schema_version"] == "1.1"
    assert manifest["application_version"] == "0.1.0"
    assert manifest["source_commit_sha"] == "a" * 40
    assert manifest["target"] == "linux-x64"
    assert len(signature_path.read_bytes()) == 64

    smoke = _load_script("smoke_ecocorridor_release_bundle.py")
    verified = smoke.verify_signed_release_inventory(
        resources,
        public_key_path,
        expected_version="0.1.0",
        expected_source_commit="a" * 40,
        expected_target="linux-x64",
    )
    assert verified["source_commit_sha"] == "a" * 40

    worker.write_bytes(b"tampered worker")
    with pytest.raises(SystemExit, match="differs from its inventory"):
        smoke.verify_signed_release_inventory(
            resources,
            public_key_path,
            expected_version="0.1.0",
            expected_source_commit="a" * 40,
            expected_target="linux-x64",
        )


@pytest.mark.parametrize(
    "seed_file",
    [
        b"a" * 64,
        b"A" * 64 + b"\n",
        b"a" * 62 + b"\n",
        b"a" * 64 + b"\r\n",
        b"-----BEGIN PRIVATE KEY-----\nnot-a-seed\n-----END PRIVATE KEY-----\n",
    ],
)
def test_release_manifest_rejects_noncanonical_signing_seed(
    tmp_path: Path, seed_file: bytes
) -> None:
    resources = tmp_path / "release"
    resources.mkdir()
    executable = resources / "worker"
    executable.write_bytes(b"worker")
    key_path = tmp_path / "invalid.seed.hex"
    key_path.write_bytes(seed_file)
    key_path.chmod(0o600)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_release_manifest.py",
            "--root",
            str(resources),
            "--output",
            str(resources / "release-manifest.json"),
            "--signature-output",
            str(resources / "release-manifest.sig"),
            "--public-key-output",
            str(tmp_path / "public.hex"),
            "--signing-key",
            str(key_path),
            "--application-version",
            "0.1.0",
            "--source-commit",
            "a" * 40,
            "--target",
            "linux-x64",
            "--executable",
            "worker",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "canonical 32-byte Ed25519 seed" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-key permission policy")
def test_release_manifest_rejects_group_or_world_accessible_signing_seed(
    tmp_path: Path,
) -> None:
    release_manifest = _load_script("create_ecocorridor_release_manifest.py")
    signing_key = SigningKey.generate()
    key_path = tmp_path / "release-key.seed.hex"
    key_path.write_text(signing_key.encode().hex() + "\n", encoding="ascii")
    key_path.chmod(0o640)

    with pytest.raises(SystemExit, match="owner-only POSIX permissions"):
        release_manifest.load_signing_key(key_path)

    key_path.chmod(0o600)
    assert release_manifest.load_signing_key(key_path).encode() == signing_key.encode()


def test_engine_manifest_helper_emits_worker_contract(tmp_path: Path) -> None:
    runtime = tmp_path / "engine-runtime"
    project = tmp_path / "julia-project"
    artifacts = tmp_path / "native-artifacts"
    runtime.mkdir()
    project.mkdir()
    artifacts.mkdir()
    (runtime / "runtime.bin").write_bytes(b"runtime")
    (artifacts / "solver.bin").write_bytes(b"native")
    (project / "Manifest.toml").write_text(
        '[[deps.Circuitscape]]\nversion = "5.17.1"\n', encoding="utf-8"
    )
    wrapper = tmp_path / "wrapper.jl"
    parser_path = tmp_path / "parser.py"
    metrics = tmp_path / "metrics.py"
    executable = tmp_path / "ecocorridor-engine"
    wrapper.write_text("function julia_main()::Cint\n0\nend\n", encoding="utf-8")
    parser_path.write_text("# parser\n", encoding="utf-8")
    metrics.write_text("# metrics\n", encoding="utf-8")
    executable.write_bytes(b"packaged executable")
    output = tmp_path / "engine-manifest.json"

    subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_engine_manifest.py",
            "--output",
            str(output),
            "--manifest-id",
            "release-test",
            "--executable",
            str(executable),
            "--julia-runtime",
            str(runtime),
            "--julia-project",
            str(project),
            "--native-artifacts",
            str(artifacts),
            "--wrapper",
            str(wrapper),
            "--parser",
            str(parser_path),
            "--metric-definitions",
            str(metrics),
            "--neighbor-rule",
            "EIGHT",
            "--resistance-average",
            "ARITHMETIC",
            "--platform",
            "test-target",
        ],
        check=True,
    )

    manifest = EngineManifest.model_validate_json(output.read_text(encoding="utf-8"))
    assert manifest.manifest_id == "release-test"
    assert manifest.platform == "test-target"
    assert manifest.circuitscape_version == "5.17.1"
    assert manifest.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()


def test_tauri_release_overlay_is_fail_closed_and_target_specific() -> None:
    output = Path("apps/ecocorridor-desktop/src-tauri") / "test-release.generated.conf.json"
    public_key, _ = _tauri_updater_signature(
        SigningKey.generate(), artifact=b"probe", filename="probe.bin"
    )
    try:
        subprocess.run(
            [
                sys.executable,
                "scripts/configure_ecocorridor_tauri_release.py",
                "--output",
                str(output),
                "--updater-public-key",
                public_key,
                "--updater-endpoint",
                "https://github.com/ash0ts/gi-scenario-studio/releases/latest/download/latest.json",
                "--windows-thumbprint",
                "A" * 40,
                "--windows-timestamp-url",
                "https://timestamp.example.test",
            ],
            check=True,
        )
        config = json.loads(output.read_text(encoding="utf-8"))
        assert config["bundle"]["createUpdaterArtifacts"] is True
        assert config["bundle"]["resources"] == {"release-generated": "release"}
        assert config["bundle"]["windows"]["certificateThumbprint"] == "A" * 40
        assert config["plugins"]["updater"]["endpoints"][0].startswith("https://")
        assert config["plugins"]["updater"]["pubkey"] == public_key
    finally:
        output.unlink(missing_ok=True)


def test_tauri_release_overlay_rejects_fake_updater_key() -> None:
    output = Path("apps/ecocorridor-desktop/src-tauri") / "test-release.generated.conf.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/configure_ecocorridor_tauri_release.py",
            "--output",
            str(output),
            "--updater-public-key",
            "untrusted comment: minisign public key\nRWQTESTPUBLICKEY",
            "--updater-endpoint",
            "https://github.com/ash0ts/gi-scenario-studio/releases/latest/download/latest.json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "minisign" in completed.stderr
    output.unlink(missing_ok=True)


def test_update_manifest_requires_all_signed_targets(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    targets = (
        "EcoCorridor_v0.1.0_macos-arm64.app.tar.gz",
        "EcoCorridor_v0.1.0_macos-x64.app.tar.gz",
        "EcoCorridor_v0.1.0_windows-x64-setup.exe",
    )
    private_key = SigningKey.generate()
    public_key = ""
    for name in targets:
        artifact = b"installer"
        (assets / name).write_bytes(artifact)
        public_key, signature = _tauri_updater_signature(
            private_key, artifact=artifact, filename=name
        )
        (assets / f"{name}.sig").write_text(signature + "\n", encoding="utf-8")
    output = assets / "latest.json"
    subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_update_manifest.py",
            "--assets",
            str(assets),
            "--output",
            str(output),
            "--repository",
            "ash0ts/gi-scenario-studio",
            "--tag",
            "v0.1.0",
            "--version",
            "v0.1.0",
            "--updater-public-key",
            public_key,
        ],
        check=True,
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.1.0"
    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "darwin-x86_64",
        "windows-x86_64",
    }


def test_update_manifest_rejects_tampered_artifact_and_mismatched_public_key(
    tmp_path: Path,
) -> None:
    updater = _load_script("create_ecocorridor_update_manifest.py")
    artifact = tmp_path / "update.bin"
    artifact.write_bytes(b"signed installer")
    private_key = SigningKey.generate()
    public_key, signature = _tauri_updater_signature(
        private_key, artifact=artifact.read_bytes(), filename=artifact.name
    )

    updater.verify_tauri_minisign_signature(
        artifact,
        signature_container=signature,
        public_key_container=public_key,
    )
    artifact.write_bytes(b"tampered installer")
    with pytest.raises(ValueError, match="does not verify"):
        updater.verify_tauri_minisign_signature(
            artifact,
            signature_container=signature,
            public_key_container=public_key,
        )

    other_public_key, _ = _tauri_updater_signature(
        SigningKey.generate(), artifact=b"tampered installer", filename=artifact.name
    )
    with pytest.raises(ValueError, match="key ID"):
        updater.verify_tauri_minisign_signature(
            artifact,
            signature_container=signature,
            public_key_container=other_public_key,
        )


def test_update_manifest_rejects_version_drift_and_symlinked_signature(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    targets = (
        "EcoCorridor_v0.1.0_macos-arm64.app.tar.gz",
        "EcoCorridor_v0.1.0_macos-x64.app.tar.gz",
        "EcoCorridor_v0.1.0_windows-x64-setup.exe",
    )
    private_key = SigningKey.generate()
    public_key = ""
    for name in targets:
        artifact = b"installer"
        (assets / name).write_bytes(artifact)
        public_key, signature = _tauri_updater_signature(
            private_key, artifact=artifact, filename=name
        )
        (assets / f"{name}.sig").write_text(signature + "\n", encoding="utf-8")
    command = [
        sys.executable,
        "scripts/create_ecocorridor_update_manifest.py",
        "--assets",
        str(assets),
        "--output",
        str(assets / "latest.json"),
        "--repository",
        "ash0ts/gi-scenario-studio",
        "--tag",
        "v0.1.0",
        "--version",
        "0.2.0",
        "--updater-public-key",
        public_key,
    ]
    mismatched = subprocess.run(command, check=False, capture_output=True, text=True)
    assert mismatched.returncode != 0
    assert "must match" in mismatched.stderr

    external = tmp_path / "external.sig"
    external.write_text("signed-value\n", encoding="utf-8")
    signature = assets / f"{targets[0]}.sig"
    signature.unlink()
    signature.symlink_to(external)
    command[-3] = "v0.1.0"
    unsafe = subprocess.run(command, check=False, capture_output=True, text=True)
    assert unsafe.returncode != 0
    assert "signature is missing" in unsafe.stderr


def test_release_asset_collection_is_target_specific_and_hash_inventoried(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    release = tmp_path / "release"
    output = tmp_path / "collected"
    bundle.mkdir()
    release.mkdir()
    (bundle / "EcoCorridor.dmg").write_bytes(b"signed disk image")
    updater = bundle / "EcoCorridor.app.tar.gz"
    updater.write_bytes(b"signed updater")
    (bundle / "EcoCorridor.app.tar.gz.sig").write_text("signature\n", encoding="utf-8")
    for name in ("release-manifest.json", "release-manifest.sig", "engine-manifest.json"):
        (release / name).write_bytes(f"release {name}".encode())
    (release / "dependency-inventory.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "application_id": "org.ecocorridor.scenariostudio",
                "application_version": "0.1.0",
                "source_commit_sha": "a" * 40,
                "target": "macos-arm64",
            }
        ),
        encoding="utf-8",
    )
    public_key = tmp_path / "resource-public-key.hex"
    public_key.write_text("a" * 64 + "\n", encoding="ascii")
    smoke = tmp_path / "offline-smoke.txt"
    smoke.write_text(
        "EcoCorridor staged worker and direct Circuitscape engine smoke passed.\n",
        encoding="utf-8",
    )
    health_receipts = []
    for index in range(2):
        receipt = tmp_path / f"installed-health-{index}.json"
        receipt.write_text(json.dumps({"health_run_id": f"run-{index}"}), encoding="utf-8")
        health_receipts.append(receipt)

    subprocess.run(
        [
            sys.executable,
            "scripts/collect_ecocorridor_release_assets.py",
            "--bundle-root",
            str(bundle),
            "--release-root",
            str(release),
            "--public-resource-key",
            str(public_key),
            "--smoke-report",
            str(smoke),
            "--installed-health-receipt",
            str(health_receipts[0]),
            "--installed-health-receipt",
            str(health_receipts[1]),
            "--output",
            str(output),
            "--target",
            "macos-arm64",
            "--version",
            "v0.1.0",
        ],
        check=True,
    )
    asset_manifest = next(output.glob("*.asset-manifest.json"))
    payload = json.loads(asset_manifest.read_text(encoding="utf-8"))
    assert payload["target"] == "macos-arm64"
    assert payload["version"] == "v0.1.0"
    assert all(item["bytes"] > 0 and len(item["sha256"]) == 64 for item in payload["assets"])
    assert any(item["role"] == "target-dependency-inventory" for item in payload["assets"])
    assert sum(item["role"] == "installed-host-health" for item in payload["assets"]) == 2


def test_cyclonedx_inventory_covers_all_ecosystems(tmp_path: Path) -> None:
    output = tmp_path / "sbom.json"
    subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_sbom.py",
            "--output",
            str(output),
            "--version",
            "0.1.0",
        ],
        check=True,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    ecosystems = {
        property_["value"]
        for component in payload["components"]
        for property_ in component["properties"]
        if property_["name"] == "org.ecocorridor.package.ecosystem"
    }
    assert {"cargo", "generic", "julia", "npm", "pypi"}.issubset(ecosystems)
    assert any(
        component["name"] == "Circuitscape" and component["version"] == "5.17.1"
        for component in payload["components"]
    )


def test_full_release_sbom_requires_and_covers_each_native_target(tmp_path: Path) -> None:
    inventories = tmp_path / "native"
    inventories.mkdir()
    for target in ("macos-arm64", "macos-x64", "windows-x64"):
        (inventories / f"{target}.dependency-inventory.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "application_id": "org.ecocorridor.scenariostudio",
                    "target": target,
                    "python_version": "3.12.11",
                    "gdal_version": "3.10.3",
                    "pyogrio_gdal_version": "3.11.4",
                    "proj_version": "9.6.0",
                    "geos_version": "3.13.1",
                    "python_packages": {"pyinstaller": "6.16.0"},
                }
            ),
            encoding="utf-8",
        )
    output = tmp_path / "full-release-sbom.json"
    subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_sbom.py",
            "--output",
            str(output),
            "--version",
            "0.1.0",
            "--native-inventory-directory",
            str(inventories),
        ],
        check=True,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    native = {(item["name"], item["version"]) for item in payload["components"]}
    assert {
        ("GDAL", "3.10.3"),
        ("GDAL", "3.11.4"),
        ("PROJ", "9.6.0"),
        ("GEOS", "3.13.1"),
    } <= native

    (inventories / "windows-x64.dependency-inventory.json").unlink()
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/create_ecocorridor_sbom.py",
            "--output",
            str(output),
            "--version",
            "0.1.0",
            "--native-inventory-directory",
            str(inventories),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "all three target" in completed.stderr


def test_installed_host_health_receipts_require_two_real_restart_launches(
    tmp_path: Path,
) -> None:
    validator = _load_script("validate_ecocorridor_installed_health.py")

    def payload(run_id: str) -> dict[str, object]:
        return {
            "schema_version": "ecocorridor-installed-host-health/v1.0",
            "health_run_id": run_id,
            "application_id": "org.ecocorridor.scenariostudio",
            "application_version": "0.1.0",
            "source_commit_sha": "a" * 40,
            "release_target": "macos-arm64",
            "release_manifest_sha256": "b" * 64,
            "release_manifest_verified": True,
            "worker_started": True,
            "system_ping": {
                "api_version": "1.0",
                "schema_version": "1.0",
                "engine_configured": True,
                "method_count": 4,
            },
            "guided_open": {
                "state": "VERIFIED_FIXTURE_REPLAY",
                "evidence_manifest_sha256": "c" * 64,
            },
            "keychain": {
                "write_succeeded": True,
                "exact_read_succeeded": True,
                "delete_succeeded": True,
                "post_delete_absent": True,
            },
        }

    receipts = []
    for index in range(2):
        path = tmp_path / f"receipt-{index}.json"
        path.write_text(json.dumps(payload(f"run-{index}")), encoding="utf-8")
        receipts.append(
            validator.load_receipt(
                path,
                expected_version="0.1.0",
                expected_source_commit="a" * 40,
                expected_target="macos-arm64",
            )
        )
    validator.validate_restart(receipts)
    receipts[1]["health_run_id"] = receipts[0]["health_run_id"]
    with pytest.raises(ValueError, match="distinct native host launches"):
        validator.validate_restart(receipts)

    incomplete = payload("run-incomplete")
    incomplete["keychain"]["post_delete_absent"] = False
    incomplete_path = tmp_path / "incomplete-keychain.json"
    incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(ValueError, match="isolated keychain roundtrip"):
        validator.load_receipt(
            incomplete_path,
            expected_version="0.1.0",
            expected_source_commit="a" * 40,
            expected_target="macos-arm64",
        )

    leaking = payload("run-leaking")
    leaking["keychain"]["account"] = "must-not-appear"
    leaking_path = tmp_path / "leaking-keychain.json"
    leaking_path.write_text(json.dumps(leaking), encoding="utf-8")
    with pytest.raises(ValueError, match="prohibited sensitive field"):
        validator.load_receipt(
            leaking_path,
            expected_version="0.1.0",
            expected_source_commit="a" * 40,
            expected_target="macos-arm64",
        )


def test_installed_host_health_receipt_rejects_non_authoritative_ping(tmp_path: Path) -> None:
    validator = _load_script("validate_ecocorridor_installed_health.py")
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "ecocorridor-installed-host-health/v1.0",
                "health_run_id": "run-1",
                "application_id": "org.ecocorridor.scenariostudio",
                "application_version": "0.1.0",
                "source_commit_sha": "a" * 40,
                "release_target": "windows-x64",
                "release_manifest_sha256": "b" * 64,
                "release_manifest_verified": True,
                "worker_started": True,
                "system_ping": {
                    "api_version": "1.0",
                    "schema_version": "1.0",
                    "engine_configured": False,
                    "method_count": 4,
                },
                "guided_open": {
                    "state": "VERIFIED_FIXTURE_REPLAY",
                    "evidence_manifest_sha256": "c" * 64,
                },
                "keychain": {
                    "write_succeeded": True,
                    "exact_read_succeeded": True,
                    "delete_succeeded": True,
                    "post_delete_absent": True,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="authoritative engine-backed ping"):
        validator.load_receipt(
            path,
            expected_version="0.1.0",
            expected_source_commit="a" * 40,
            expected_target="windows-x64",
        )
