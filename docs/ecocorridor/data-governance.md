# Ecological data governance

W&B project visibility and permission to replicate an ecological asset are two
independent decisions. A verified `Restricted` project is necessary for the
professional cloud workflow, but it never authorizes sensitive locations,
Tribal knowledge, private-land records, licensed telemetry, or other restricted
sources by itself.

Every asset records:

- owner and license;
- sensitivity classification;
- raw and generalized cloud permissions;
- export permission and retention policy;
- spatial-redaction policy;
- approved deployment type;
- reviewer and review timestamp; and
- checksum, original/derived relationship, and transformation history.

Allowed cloud dispositions are:

- `RAW_ALLOWED`
- `GENERALIZED_DERIVATIVE_ONLY`
- `DEDICATED_OR_SELF_MANAGED_ONLY`
- `PROHIBITED`

Full synchronization is blocked until every asset has an authorized
representation for the chosen destination. A prohibited asset cannot be
silently omitted while the UI claims the project is fully synchronized.

## Project-scope verification

`ProjectScopeResolver` returns `RESTRICTED` only from a documented exact-scope
field or a documented Dedicated/Self-Managed administrative endpoint. Project
access, membership, a successful upload, W&B `PRIVATE`, or a user checkbox are
not substitutes. Multi-tenant W&B Cloud professional upload remains disabled
while the supported API cannot prove the exact scope.

The proof is bound to the normalized API base URL and selected deployment, not
only to a project name. Critical-infrastructure-restricted assets require an
authorized Dedicated or Self-Managed destination even if a raw-upload flag was
entered. Generalized derivatives are uploaded once while retaining every
reviewed source-to-derivative provenance edge.

## Repository boundary

Only checksum-verified, self-authored CC0 public synthetic ecological assets
may be committed. `scripts/check_ecological_data_manifest.py` rejects an
unclassified raster, vector, shapefile component, GeoPackage, fixture,
benchmark artifact, retained evidence archive, or screenshot baseline. The
gate is extension-independent inside the Ridgeway, search-benchmark, and
screenshot boundaries, so a newly introduced binary format cannot silently
bypass review.

Each classification record includes exact bytes and SHA-256, media type,
asset role, repository use, ownership, license, provenance, sensitivity,
public-replay permission, and explicit declarations that no real location or
species-occurrence data is present. The checker also verifies that:

- the public desktop fixture is byte-for-byte equal to the approved Ridgeway
  UI projection and pins the direct-evidence manifest;
- matrix and map job counts agree across the direct manifest and projection;
- the retained archive contains only bounded, unique, safe regular-file paths
  and matches its declared byte, file, archive, and internal-index digests; and
- historical search datasets match their immutable manifest and superseded
  Ridgeway archive/index chain, resolve the resource sidecar's declared source
  hashes to passive snapshots, and carry an independent
  `HISTORICAL_BENCHMARK_ONLY` / production `PROHIBITED` disposition. They are
  never rebound to the active Ridgeway manifest or current measurement code;
  an immutable W&B artifact cannot promote this historical campaign.

The public replay is built only from the approved synthetic fixture. A record's
`public_replay_allowed` field is authorization, not proof that the file is
actually bundled; benchmark-only and screenshot assets remain outside the web
application build.

Signed desktop resources use a separate `signed_desktop_bundle_allowed`
decision. The manifest checker permits exactly the current Ridgeway UI
projection, release-trust record, direct-evidence manifest, and archive selected
by that manifest. The projection and trust record remain public-replay approved;
the direct manifest and archive remain explicitly prohibited from the public
web replay. Release staging copies all four, and the packaged-boundary smoke
rechecks their byte counts, hashes, sensitivity declarations, and distinct
authorizations from the signed resource tree. Superseded evidence archives
remain explicitly unauthorized for desktop packaging.
