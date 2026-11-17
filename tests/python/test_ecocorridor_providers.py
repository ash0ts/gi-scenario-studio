from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from ecocorridor_core.providers import (
    acquire_provider_staging,
    download_approved_asset,
    preview_download,
)


class _Response(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(payload)
        self._url = url
        self._status = status
        self.headers = headers or {}

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self._status


class _Opener:
    def __init__(self, payload: bytes, final_url: str) -> None:
        self.payload = payload
        self.final_url = final_url

    def open(self, request, timeout: float):
        assert timeout == 60.0
        assert request.full_url.startswith("https://")
        return _Response(self.payload, self.final_url)


class _RedirectOpener:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def open(self, request, timeout: float):
        assert timeout == 60.0
        self.requested.append(request.full_url)
        return self.responses[request.full_url]


TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER2025/ROADS/tl_2025_01001_roads.zip"
THREE_DEP_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/example/context.tif"
)
PAD_URL = (
    "https://www.sciencebase.gov/catalog/file/get/652d4fc5d34e44db0e2ee45e?name=PADUS4_1_GDB.zip"
)
PAD_FINAL_URL = (
    "https://prod-is-usgs-sb-prod-content.s3.amazonaws.com/"
    "652d4fc5d34e44db0e2ee45e/PADUS4_1_GDB.zip"
    "?AWSAccessKeyId=test-public-download&Expires=1777587284&Signature=example"
)


def test_provider_staging_lease_serializes_concurrent_owners(tmp_path: Path) -> None:
    first = acquire_provider_staging(cache_root=tmp_path)
    second_started = threading.Event()
    second_acquired = threading.Event()

    def acquire_second() -> None:
        second_started.set()
        second = acquire_provider_staging(cache_root=tmp_path, timeout_seconds=5)
        second_acquired.set()
        second.release()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert second_started.wait(2)
    assert not second_acquired.wait(0.1)
    first.release()
    thread.join(5)

    assert not thread.is_alive()
    assert second_acquired.is_set()


def test_provider_staging_recovers_a_crashed_process_without_touching_cas(
    tmp_path: Path,
) -> None:
    retained = tmp_path / "provider" / "tiger_line_2025" / "sha256" / ("a" * 64)
    retained.parent.mkdir(parents=True)
    child = (
        "from pathlib import Path\n"
        "import os, sys\n"
        "from ecocorridor_core.providers import acquire_provider_staging\n"
        "lease = acquire_provider_staging(cache_root=Path(sys.argv[1]))\n"
        "staged = lease.staging_path / '.crash-stage.deadbeef.download'\n"
        "staged.write_bytes(b'retained-cas')\n"
        "os.link(staged, Path(sys.argv[2]))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", child, str(tmp_path), str(retained)], check=True)
    stale = tmp_path / ".downloads" / ".crash-stage.deadbeef.download"
    assert stale.read_bytes() == b"retained-cas"

    recovered = acquire_provider_staging(cache_root=tmp_path, timeout_seconds=5)
    try:
        assert list(recovered.staging_path.iterdir()) == []
        assert retained.read_bytes() == b"retained-cas"
    finally:
        recovered.release()


@pytest.mark.parametrize(
    "unsafe_target",
    [
        "staging_symlink",
        "staging_file",
        "lock_symlink",
        "lock_directory",
        "special_entry",
    ],
)
def test_provider_staging_recovery_rejects_unsafe_entries_without_deleting_cas(
    tmp_path: Path,
    unsafe_target: str,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    retained = cache / "provider" / "tiger_line_2025" / "sha256" / ("b" * 64)
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained-cas")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "do-not-delete"
    outside_file.write_bytes(b"outside")
    if unsafe_target == "staging_symlink":
        (cache / ".downloads").symlink_to(outside, target_is_directory=True)
    elif unsafe_target == "staging_file":
        (cache / ".downloads").write_bytes(b"not-a-directory")
    elif unsafe_target == "lock_symlink":
        (cache / ".provider-download.lock").symlink_to(outside_file)
    elif unsafe_target == "lock_directory":
        (cache / ".provider-download.lock").mkdir()
    else:
        staging = cache / ".downloads"
        staging.mkdir(mode=0o700)
        (staging / "unexpected-directory").mkdir()

    with pytest.raises(ValueError, match="STAGING|symbolic link"):
        acquire_provider_staging(cache_root=cache, timeout_seconds=0)

    assert retained.read_bytes() == b"retained-cas"
    assert outside_file.read_bytes() == b"outside"


def test_provider_staging_rejects_hardlinked_lock_without_mutating_target(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "private-target"
    outside.write_bytes(b"must-not-be-truncated")
    outside.chmod(0o600)
    (cache / ".provider-download.lock").hardlink_to(outside)

    with pytest.raises(ValueError, match="STAGING_LOCK_INVALID"):
        acquire_provider_staging(cache_root=cache, timeout_seconds=0)

    assert outside.read_bytes() == b"must-not-be-truncated"


def test_provider_staging_rejects_lock_path_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()

    class _ReplacingLock:
        def __init__(self, path: str, *, thread_local: bool) -> None:
            del thread_local
            self.path = Path(path)

        def acquire(self, *, timeout: float) -> None:
            del timeout
            self.path.unlink()
            self.path.write_bytes(b"")
            self.path.chmod(0o600)

        def release(self) -> None:
            return None

    monkeypatch.setattr("ecocorridor_core.providers.FileLock", _ReplacingLock)

    with pytest.raises(ValueError, match="STAGING_LOCK_INVALID"):
        acquire_provider_staging(cache_root=cache, timeout_seconds=0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
def test_provider_staging_requires_private_directory_permissions(tmp_path: Path) -> None:
    staging = tmp_path / ".downloads"
    staging.mkdir(mode=0o755)
    staging.chmod(0o755)

    with pytest.raises(ValueError, match="STAGING_PERMISSIONS_INVALID"):
        acquire_provider_staging(cache_root=tmp_path, timeout_seconds=0)


def test_provider_staging_recovery_is_bounded_and_fails_before_unlink(tmp_path: Path) -> None:
    staging = tmp_path / ".downloads"
    staging.mkdir(mode=0o700)
    for index in range(257):
        (staging / f".stale-asset.{index:08d}.download").write_bytes(b"x")

    with pytest.raises(ValueError, match="STAGING_RECOVERY_LIMIT"):
        acquire_provider_staging(cache_root=tmp_path, timeout_seconds=0)

    assert len(list(staging.iterdir())) == 257


def test_preview_explains_context_only_use() -> None:
    preview = preview_download(
        provider_id="tiger_line_2025",
        source_url=TIGER_URL,
        extent_description="Synthetic county test extent",
        expected_bytes=512,
    )
    assert preview.vintage == "2025"
    assert "not become ecological resistance" in preview.consent_statement


def test_download_requires_consent_and_enforces_allowlist(tmp_path: Path) -> None:
    preview = preview_download(
        provider_id="usgs_3dep",
        source_url=THREE_DEP_URL,
        extent_description="Approved study extent",
        expected_bytes=4,
    )
    with pytest.raises(ValueError, match="EXPLICIT_CONSENT"):
        download_approved_asset(
            preview=preview,
            asset_id="terrain-context",
            cache_root=tmp_path,
            explicit_consent=False,
            maximum_bytes=10,
        )
    with pytest.raises(ValueError, match="URL_BLOCKED"):
        download_approved_asset(
            preview=preview,
            asset_id="terrain-context",
            cache_root=tmp_path,
            explicit_consent=True,
            maximum_bytes=10,
            opener=_Opener(b"data", "https://attacker.example/context.tif"),
        )


def test_approved_download_is_atomic_and_hashed(tmp_path: Path) -> None:
    preview = preview_download(
        provider_id="pad_us_4_1",
        source_url=PAD_URL,
        extent_description="Approved planning extent",
        expected_bytes=4,
    )
    receipt = download_approved_asset(
        preview=preview,
        asset_id="ownership-context",
        cache_root=tmp_path,
        explicit_consent=True,
        maximum_bytes=10,
        opener=_Opener(b"data", preview.source_url),
    )
    assert receipt.sha256 == "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"
    assert (tmp_path / receipt.relative_path).read_bytes() == b"data"
    assert receipt.requested_url == PAD_URL
    assert receipt.final_url == PAD_URL
    assert receipt.redirect_urls == ()


@pytest.mark.parametrize(
    ("provider_id", "url"),
    [
        (
            "usgs_3dep",
            "https://unrelated-public-bucket.s3.amazonaws.com/StagedProducts/Elevation/1m/a.tif",
        ),
        ("usgs_3dep", "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/a.zip"),
        (
            "annual_nlcd_1_2",
            "https://usgs-landcover.s3.amazonaws.com/legacy-nlcd/2021_landcover.tif",
        ),
        (
            "pad_us_4_1",
            "https://www.sciencebase.gov/catalog/file/get/000000000000000000000000",
        ),
        (
            "tiger_line_2025",
            "https://www2.census.gov/geo/tiger/TIGER2024/ROADS/tl_2024_01001_roads.zip",
        ),
    ],
)
def test_provider_routes_reject_arbitrary_s3_and_wrong_dataset_paths(
    provider_id: str, url: str
) -> None:
    with pytest.raises(ValueError, match="URL_BLOCKED"):
        preview_download(
            provider_id=provider_id,
            source_url=url,
            extent_description="Policy test",
            expected_bytes=4,
        )


def test_approved_canonical_routes_are_dataset_specific() -> None:
    canonical = {
        "usgs_3dep": THREE_DEP_URL,
        "annual_nlcd_1_2": (
            "https://usgs-landcover.s3.us-west-2.amazonaws.com/annual-nlcd/c1/v0/"
            "cu/tile/h04v01/Annual_NLCD_LndCov_2025_CU_C1V0.tif"
        ),
        "pad_us_4_1": PAD_URL,
        "tiger_line_2025": TIGER_URL,
    }
    for provider_id, url in canonical.items():
        assert (
            preview_download(
                provider_id=provider_id,
                source_url=url,
                extent_description="Canonical provider route",
                expected_bytes=4,
            ).source_url
            == url
        )


def test_redirect_target_is_validated_before_the_next_request(tmp_path: Path) -> None:
    attacker = "https://arbitrary-bucket.s3.amazonaws.com/PADUS4_1_GDB.zip"
    opener = _RedirectOpener(
        {
            PAD_URL: _Response(b"", PAD_URL, status=302, headers={"Location": attacker}),
        }
    )
    preview = preview_download(
        provider_id="pad_us_4_1",
        source_url=PAD_URL,
        extent_description="Approved planning extent",
        expected_bytes=4,
    )
    with pytest.raises(ValueError, match="URL_BLOCKED"):
        download_approved_asset(
            preview=preview,
            asset_id="ownership-context",
            cache_root=tmp_path,
            explicit_consent=True,
            maximum_bytes=10,
            opener=opener,
        )
    assert opener.requested == [PAD_URL]


def test_approved_redirect_chain_preserves_exact_requested_and_final_urls(
    tmp_path: Path,
) -> None:
    opener = _RedirectOpener(
        {
            PAD_URL: _Response(b"", PAD_URL, status=302, headers={"Location": PAD_FINAL_URL}),
            PAD_FINAL_URL: _Response(b"data", PAD_FINAL_URL),
        }
    )
    preview = preview_download(
        provider_id="pad_us_4_1",
        source_url=PAD_URL,
        extent_description="Approved planning extent",
        expected_bytes=4,
    )
    receipt = download_approved_asset(
        preview=preview,
        asset_id="ownership-context",
        cache_root=tmp_path,
        explicit_consent=True,
        maximum_bytes=10,
        opener=opener,
    )
    assert opener.requested == [PAD_URL, PAD_FINAL_URL]
    assert receipt.requested_url == PAD_URL
    assert receipt.final_url == PAD_FINAL_URL
    assert receipt.redirect_urls == (PAD_FINAL_URL,)


@pytest.mark.parametrize("escape", ["%2f", "%2F", "%5c", "%5C", "%00"])
def test_provider_routes_reject_encoded_path_separators_and_nul(escape: str) -> None:
    with pytest.raises(ValueError, match="URL_BLOCKED"):
        preview_download(
            provider_id="usgs_3dep",
            source_url=(
                "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/"
                f"tile{escape}escape.tif"
            ),
            extent_description="Policy test",
            expected_bytes=4,
        )
