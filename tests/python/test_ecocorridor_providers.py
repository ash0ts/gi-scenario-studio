from __future__ import annotations

import io
from pathlib import Path

import pytest
from ecocorridor_core.providers import download_approved_asset, preview_download


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
