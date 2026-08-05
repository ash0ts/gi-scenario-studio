"""Explicit-consent, context-only public-data acquisition.

Provider assets are never converted into resistance, focal areas, or action
effects by this module. Those ecological meanings require separate reviewed
contracts and semantic revisions.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Self
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from scenario_platform.process_lock import FileLock, FileLockTimeout
from scenario_platform.storage import contained_path

_MAX_STALE_STAGE_FILES = 256
_STAGE_FILE_PATTERN = re.compile(r"^\.[a-z][a-z0-9_-]{2,127}\.[A-Za-z0-9_-]+\.download$")


@dataclass(frozen=True, slots=True)
class ProviderURLRule:
    """One exact, documented distribution route for an approved dataset."""

    hostname: str
    path_pattern: str
    allowed_query_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    provider: str
    dataset: str
    vintage: str
    typical_resolution: str
    license: str
    attribution: str
    url_rules: tuple[ProviderURLRule, ...]
    intended_use: str


PROVIDERS: dict[str, ProviderSpec] = {
    "usgs_3dep": ProviderSpec(
        provider_id="usgs_3dep",
        provider="U.S. Geological Survey",
        dataset="3D Elevation Program (3DEP)",
        vintage="provider item metadata",
        typical_resolution="varies by product and geography; confirm before download",
        license="U.S. federal public-domain data; preserve source metadata",
        attribution="U.S. Geological Survey 3D Elevation Program",
        url_rules=(
            ProviderURLRule(
                hostname="prd-tnm.s3.amazonaws.com",
                path_pattern=(
                    r"^/StagedProducts/Elevation/(?:1m|13|19|30m|OPR|LPC|Source)/"
                    r"[^\\\x00-\x1f]+\.(?:tif|tiff|zip|xml|json|txt|jpg|jpeg)$"
                ),
            ),
            ProviderURLRule(
                hostname="prd-tnm.s3.us-west-2.amazonaws.com",
                path_pattern=(
                    r"^/StagedProducts/Elevation/(?:1m|13|19|30m|OPR|LPC|Source)/"
                    r"[^\\\x00-\x1f]+\.(?:tif|tiff|zip|xml|json|txt|jpg|jpeg)$"
                ),
            ),
        ),
        intended_use="terrain and boundary context only",
    ),
    "annual_nlcd_1_2": ProviderSpec(
        provider_id="annual_nlcd_1_2",
        provider="U.S. Geological Survey EROS",
        dataset="Annual NLCD Collection 1.2",
        vintage="selected annual product",
        typical_resolution="30 m",
        license="U.S. federal public-domain data; preserve source metadata",
        attribution="U.S. Geological Survey Annual NLCD",
        url_rules=(
            ProviderURLRule(
                hostname="usgs-landcover.s3.amazonaws.com",
                path_pattern=(
                    r"^/annual-nlcd/c1/v0/(?:cu|ak|hi)/"
                    r"(?:tile/h\d{2}v\d{2}|mosaic)/"
                    r"[^\\\x00-\x1f]+\.(?:tif|tiff|zip|xml|json)$"
                ),
            ),
            ProviderURLRule(
                hostname="usgs-landcover.s3.us-west-2.amazonaws.com",
                path_pattern=(
                    r"^/annual-nlcd/c1/v0/(?:cu|ak|hi)/"
                    r"(?:tile/h\d{2}v\d{2}|mosaic)/"
                    r"[^\\\x00-\x1f]+\.(?:tif|tiff|zip|xml|json)$"
                ),
            ),
        ),
        intended_use="land-cover context only",
    ),
    "pad_us_4_1": ProviderSpec(
        provider_id="pad_us_4_1",
        provider="U.S. Geological Survey Gap Analysis Project",
        dataset="Protected Areas Database of the United States 4.1",
        vintage="4.1",
        typical_resolution="vector inventory; source precision varies",
        license="U.S. federal public-domain data; respect source restrictions",
        attribution="U.S. Geological Survey Gap Analysis Project, PAD-US 4.1",
        url_rules=(
            # These are the four PAD-US 4.1 collections linked from the USGS
            # download page: national inventory, state downloads, vector
            # analysis, and raster analysis. Other ScienceBase items are not
            # interchangeable merely because they share a provider hostname.
            ProviderURLRule(
                hostname="www.sciencebase.gov",
                path_pattern=(
                    r"^/catalog/file/get/"
                    r"(?:652d4fc5d34e44db0e2ee45e|6759abcfd34edfeb8710a004|"
                    r"6759b69fd34edfeb8710a3ea|6759b67ed34edfeb8710a3db)$"
                ),
                allowed_query_keys=("name",),
            ),
            ProviderURLRule(
                hostname="prod-is-usgs-sb-prod-content.s3.amazonaws.com",
                path_pattern=(
                    r"^/(?:652d4fc5d34e44db0e2ee45e|6759abcfd34edfeb8710a004|"
                    r"6759b69fd34edfeb8710a3ea|6759b67ed34edfeb8710a3db)/"
                    r"PADUS4_1[^/\\\x00-\x1f]*\.(?:zip|xml|json|txt|pdf)$"
                ),
                allowed_query_keys=(
                    "awsaccesskeyid",
                    "expires",
                    "signature",
                    "response-content-disposition",
                    "response-content-type",
                    "x-amz-algorithm",
                    "x-amz-credential",
                    "x-amz-date",
                    "x-amz-expires",
                    "x-amz-security-token",
                    "x-amz-signature",
                    "x-amz-signedheaders",
                ),
            ),
        ),
        intended_use="ownership and protected-area context only",
    ),
    "tiger_line_2025": ProviderSpec(
        provider_id="tiger_line_2025",
        provider="U.S. Census Bureau",
        dataset="2025 TIGER/Line roads",
        vintage="2025",
        typical_resolution="vector cartographic geometry; not survey-grade",
        license="U.S. federal data; preserve Census source and vintage",
        attribution="U.S. Census Bureau 2025 TIGER/Line",
        url_rules=(
            ProviderURLRule(
                hostname="www2.census.gov",
                path_pattern=r"^/geo/tiger/TIGER2025/ROADS/tl_2025_\d{5}_roads\.zip$",
            ),
        ),
        intended_use="road and study-context review only",
    ),
}


@dataclass(frozen=True, slots=True)
class DownloadPreview:
    provider_id: str
    provider: str
    dataset: str
    vintage: str
    resolution: str
    license: str
    attribution: str
    extent_description: str
    expected_bytes: int
    intended_use: str
    source_url: str
    consent_statement: str


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    asset_id: str
    relative_path: str
    sha256: str
    bytes: int
    source_url: str
    requested_url: str
    final_url: str
    redirect_urls: tuple[str, ...]
    provider_id: str
    attribution: str
    created: bool


@dataclass(frozen=True, slots=True)
class StagedDownload:
    """Verified provider bytes that have not entered the retained CAS yet.

    The staging path is process-local operational state.  It is deliberately
    absent from public contracts and provider receipts; callers must either
    retain it through :func:`retain_staged_asset` or unlink it in a ``finally``
    block while keeping its :class:`ProviderStagingLease` active.
    """

    asset_id: str
    temporary_path: Path
    sha256: str
    bytes: int
    source_url: str
    requested_url: str
    final_url: str
    redirect_urls: tuple[str, ...]
    provider_id: str
    attribution: str


@dataclass(slots=True)
class ProviderStagingLease:
    """Exclusive ownership of one project's provider staging directory."""

    cache_root: Path
    staging_path: Path
    _lock: object
    active: bool = True

    def assert_active_for(self, cache_root: Path) -> None:
        if not self.active or cache_root.resolve() != self.cache_root:
            raise ValueError("DATA_PROVIDER_STAGING_LEASE_REQUIRED")

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        release = getattr(self._lock, "release", None)
        if callable(release):
            release()


def _recover_stale_provider_staging(staging: Path) -> None:
    """Delete only bounded, recognizable provider staging files.

    The caller holds the cross-process lease, so every valid file in this
    private directory is from an interrupted prior owner. Validation completes
    before any unlink; unexpected content fails closed and is never traversed.
    """

    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("DATA_PROVIDER_STAGING_PATH_INVALID")
    entries: list[Path] = []
    with os.scandir(staging) as scanner:
        for entry in scanner:
            if len(entries) >= _MAX_STALE_STAGE_FILES:
                raise ValueError("DATA_PROVIDER_STAGING_RECOVERY_LIMIT")
            entries.append(Path(entry.path))
    for entry in entries:
        metadata = entry.lstat()
        if (
            not _STAGE_FILE_PATTERN.fullmatch(entry.name)
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError("DATA_PROVIDER_STAGING_UNSAFE_ENTRY")
    for entry in entries:
        entry.unlink()


def _require_private_owned_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("DATA_PROVIDER_STAGING_PATH_INVALID")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("DATA_PROVIDER_STAGING_OWNER_INVALID")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("DATA_PROVIDER_STAGING_PERMISSIONS_INVALID")


def _prepare_private_lock_file(lock_path: Path) -> tuple[int, int]:
    """Create/open the lock identity without following its final component."""

    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID") from exc
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID")
        if metadata.st_nlink != 1:
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_OWNER_INVALID")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_PERMISSIONS_INVALID")
        current = lock_path.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID")
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def acquire_provider_staging(
    *,
    cache_root: Path,
    timeout_seconds: float = 120.0,
) -> ProviderStagingLease:
    """Serialize provider staging across worker processes and recover crashes."""

    if timeout_seconds < 0:
        raise ValueError("DATA_PROVIDER_STAGING_TIMEOUT_INVALID")
    cache_root.mkdir(parents=True, exist_ok=True)
    resolved_root = cache_root.resolve(strict=True)
    if cache_root.is_symlink() or not resolved_root.is_dir():
        raise ValueError("DATA_PROVIDER_CACHE_PATH_INVALID")
    staging = contained_path(resolved_root, ".downloads")
    try:
        staging.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ValueError("DATA_PROVIDER_STAGING_PATH_INVALID") from exc
    _require_private_owned_directory(staging)
    lock_path = contained_path(resolved_root, ".provider-download.lock")
    prepared_lock_identity = _prepare_private_lock_file(lock_path)
    lock = FileLock(str(lock_path), thread_local=False)
    try:
        lock.acquire(timeout=timeout_seconds)
    except FileLockTimeout as exc:
        raise ValueError("DATA_PROVIDER_STAGING_BUSY") from exc
    try:
        if _prepare_private_lock_file(lock_path) != prepared_lock_identity:
            raise ValueError("DATA_PROVIDER_STAGING_LOCK_INVALID")
    except BaseException:
        lock.release()
        raise
    lease = ProviderStagingLease(
        cache_root=resolved_root,
        staging_path=staging,
        _lock=lock,
    )
    try:
        _recover_stale_provider_staging(staging)
    except BaseException:
        lease.release()
        raise
    return lease


class _Response(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def getcode(self) -> int | None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


class _Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> _Response: ...


class _NoAutomaticRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses so the caller can validate Location before following."""

    def _return_response(
        self,
        request: urllib.request.Request,
        response: _Response,
        code: int,
        message: str,
        headers: object,
    ) -> _Response:
        del request, code, message, headers
        return response

    http_error_301 = _return_response
    http_error_302 = _return_response
    http_error_303 = _return_response
    http_error_307 = _return_response
    http_error_308 = _return_response


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    """Durably publish a provider-CAS directory entry where supported."""

    if os.name == "nt":  # Windows does not expose portable directory fsync.
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def preview_download(
    *,
    provider_id: str,
    source_url: str,
    extent_description: str,
    expected_bytes: int,
    selected_vintage: str | None = None,
    selected_resolution: str | None = None,
) -> DownloadPreview:
    try:
        provider = PROVIDERS[provider_id]
    except KeyError as exc:
        raise ValueError("DATA_PROVIDER_NOT_ALLOWLISTED") from exc
    _validate_provider_url(provider, source_url)
    if not extent_description.strip():
        raise ValueError("download extent is required")
    if expected_bytes <= 0:
        raise ValueError("expected download bytes must be positive")
    return DownloadPreview(
        provider_id=provider.provider_id,
        provider=provider.provider,
        dataset=provider.dataset,
        vintage=selected_vintage or provider.vintage,
        resolution=selected_resolution or provider.typical_resolution,
        license=provider.license,
        attribution=provider.attribution,
        extent_description=extent_description.strip(),
        expected_bytes=expected_bytes,
        intended_use=provider.intended_use,
        source_url=source_url,
        consent_statement=(
            "Download this contextual source asset and record its bytes, license, attribution, "
            "vintage, extent, and checksum. It will not become ecological resistance, a focal "
            "area, or an intervention effect without separate human review."
        ),
    )


def stage_approved_asset(
    *,
    preview: DownloadPreview,
    asset_id: str,
    cache_root: Path,
    explicit_consent: bool,
    maximum_bytes: int,
    lease: ProviderStagingLease,
    opener: _Opener | None = None,
) -> StagedDownload:
    if not explicit_consent:
        raise ValueError("DATA_DOWNLOAD_EXPLICIT_CONSENT_REQUIRED")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", asset_id):
        raise ValueError("asset_id is invalid")
    if maximum_bytes <= 0 or preview.expected_bytes > maximum_bytes:
        raise ValueError("DATA_DOWNLOAD_RESOURCE_POLICY_BLOCKED")
    provider = PROVIDERS.get(preview.provider_id)
    if provider is None:
        raise ValueError("DATA_PROVIDER_NOT_ALLOWLISTED")
    _validate_provider_url(provider, preview.source_url)
    lease.assert_active_for(cache_root)
    staging = lease.staging_path
    client = opener or urllib.request.build_opener(_NoAutomaticRedirectHandler())
    temporary_path: Path | None = None
    digest = hashlib.sha256()
    total = 0
    download_complete = False
    try:
        current_url = preview.source_url
        redirect_urls: list[str] = []
        response: _Response | None = None
        for _redirect_count in range(6):
            request = urllib.request.Request(
                current_url,
                headers={"User-Agent": "EcoCorridor-Scenario-Studio/0.1 data-acquisition"},
                method="GET",
            )
            candidate = client.open(request, timeout=60.0)
            reported_url = candidate.geturl()
            if reported_url != current_url:
                candidate.__exit__(None, None, None)
                _validate_provider_url(provider, reported_url)
                raise ValueError("DATA_PROVIDER_REDIRECT_POLICY_UNVERIFIABLE")
            status_reader = getattr(candidate, "getcode", None)
            status = status_reader() if callable(status_reader) else 200
            if status not in {301, 302, 303, 307, 308}:
                response = candidate
                break
            location_reader = getattr(candidate.headers, "get", None)
            location = location_reader("Location") if callable(location_reader) else None
            candidate.__exit__(None, None, None)
            if not isinstance(location, str) or not location.strip():
                raise ValueError("DATA_PROVIDER_REDIRECT_LOCATION_MISSING")
            redirected_url = urljoin(current_url, location.strip())
            # This validation occurs before constructing or opening the next
            # request. An arbitrary S3 redirect therefore never receives a
            # request, even if the initial provider endpoint was approved.
            _validate_provider_url(provider, redirected_url)
            if redirected_url == current_url or redirected_url in redirect_urls:
                raise ValueError("DATA_PROVIDER_REDIRECT_LOOP")
            redirect_urls.append(redirected_url)
            current_url = redirected_url
        if response is None:
            raise ValueError("DATA_PROVIDER_TOO_MANY_REDIRECTS")
        with response:
            final_url = current_url
            content_length_reader = getattr(response.headers, "get", None)
            content_length = (
                content_length_reader("Content-Length") if callable(content_length_reader) else None
            )
            if content_length is not None:
                try:
                    if int(content_length) > maximum_bytes:
                        raise ValueError("DATA_DOWNLOAD_RESOURCE_POLICY_BLOCKED")
                except (TypeError, ValueError) as exc:
                    if isinstance(exc, ValueError) and str(exc).startswith("DATA_"):
                        raise
                    raise ValueError("DATA_PROVIDER_INVALID_CONTENT_LENGTH") from exc
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{asset_id}.", suffix=".download", dir=staging
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as target:
                source: BinaryIO = response  # type: ignore[assignment]
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise ValueError("DATA_DOWNLOAD_RESOURCE_POLICY_BLOCKED")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            download_complete = True
    finally:
        if temporary_path is not None and not download_complete:
            temporary_path.unlink(missing_ok=True)
    if total == 0:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ValueError("DATA_PROVIDER_EMPTY_RESPONSE")
    digest_value = digest.hexdigest()
    assert temporary_path is not None
    return StagedDownload(
        asset_id=asset_id,
        temporary_path=temporary_path,
        sha256=digest_value,
        bytes=total,
        source_url=preview.source_url,
        requested_url=preview.source_url,
        final_url=final_url,
        redirect_urls=tuple(redirect_urls),
        provider_id=provider.provider_id,
        attribution=provider.attribution,
    )


def retain_staged_asset(
    *,
    staged: StagedDownload,
    cache_root: Path,
    lease: ProviderStagingLease,
) -> DownloadReceipt:
    """Publish staged bytes to the immutable provider CAS without overwrite.

    This operation intentionally does not delete the staging file.  The caller
    owns cleanup so it can place CAS retention and authoritative repository
    publication inside one serialized policy transaction.
    """

    provider = PROVIDERS.get(staged.provider_id)
    if provider is None:
        raise ValueError("DATA_PROVIDER_NOT_ALLOWLISTED")
    lease.assert_active_for(cache_root)
    staging = lease.staging_path
    temporary_path = staged.temporary_path.resolve(strict=True)
    if staging.resolve(strict=True) not in temporary_path.parents:
        raise ValueError("DATA_PROVIDER_STAGING_PATH_INVALID")
    if (
        not temporary_path.is_file()
        or temporary_path.is_symlink()
        or temporary_path.stat().st_size != staged.bytes
        or _sha256_file(temporary_path) != staged.sha256
    ):
        raise ValueError("DATA_PROVIDER_STAGED_INTEGRITY_MISMATCH")
    destination = contained_path(
        cache_root,
        Path("provider") / provider.provider_id / "sha256" / staged.sha256,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        # A hard link publishes the fully fsynced staging inode without an
        # overwrite window.  Existing content-addressed bytes are reused only
        # after exact type, size, and digest verification.
        os.link(temporary_path, destination)
        created = True
    except FileExistsError:
        if (
            not destination.is_file()
            or destination.is_symlink()
            or destination.stat().st_size != staged.bytes
            or _sha256_file(destination) != staged.sha256
        ):
            raise ValueError("DATA_PROVIDER_CACHE_CONFLICT") from None
    # The staged inode was fsynced before linking. Persist the CAS directory
    # entry before the caller is allowed to commit its authoritative SQLite
    # asset and receipt; this also makes a retry after an earlier fsync failure
    # durable before it publishes the database rows.
    _fsync_directory(destination.parent)
    return DownloadReceipt(
        asset_id=staged.asset_id,
        relative_path=destination.relative_to(cache_root).as_posix(),
        sha256=staged.sha256,
        bytes=staged.bytes,
        source_url=staged.source_url,
        requested_url=staged.requested_url,
        final_url=staged.final_url,
        redirect_urls=staged.redirect_urls,
        provider_id=provider.provider_id,
        attribution=provider.attribution,
        created=created,
    )


def download_approved_asset(
    *,
    preview: DownloadPreview,
    asset_id: str,
    cache_root: Path,
    explicit_consent: bool,
    maximum_bytes: int,
    opener: _Opener | None = None,
) -> DownloadReceipt:
    """Compatibility wrapper for callers without repository quota authority."""

    lease = acquire_provider_staging(cache_root=cache_root)
    staged: StagedDownload | None = None
    try:
        staged = stage_approved_asset(
            preview=preview,
            asset_id=asset_id,
            cache_root=cache_root,
            explicit_consent=explicit_consent,
            maximum_bytes=maximum_bytes,
            lease=lease,
            opener=opener,
        )
        return retain_staged_asset(staged=staged, cache_root=cache_root, lease=lease)
    finally:
        try:
            if staged is not None:
                staged.temporary_path.unlink(missing_ok=True)
        finally:
            lease.release()


def _validate_provider_url(provider: ProviderSpec, source_url: str) -> None:
    parsed = urlparse(source_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DATA_PROVIDER_URL_BLOCKED") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("DATA_PROVIDER_URL_BLOCKED")
    hostname = parsed.hostname.casefold().rstrip(".")
    encoded_path = parsed.path.casefold()
    if any(token in encoded_path for token in ("%2f", "%5c", "%00")):
        raise ValueError("DATA_PROVIDER_URL_BLOCKED")
    path = unquote(parsed.path)
    if (
        "\\" in path
        or "\x00" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or unquote(path) != path
    ):
        raise ValueError("DATA_PROVIDER_URL_BLOCKED")
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("DATA_PROVIDER_URL_BLOCKED") from exc
    query_keys = [key for key, _value in query]
    for rule in provider.url_rules:
        if hostname != rule.hostname or re.fullmatch(rule.path_pattern, path) is None:
            continue
        if len(query_keys) != len(set(query_keys)):
            raise ValueError("DATA_PROVIDER_URL_BLOCKED")
        if not {key.casefold() for key in query_keys}.issubset(rule.allowed_query_keys):
            raise ValueError("DATA_PROVIDER_URL_BLOCKED")
        return
    raise ValueError("DATA_PROVIDER_URL_BLOCKED")
