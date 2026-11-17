"""Validate and inventory the no-login synthetic GitHub Pages artifact."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import stat
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from ecocorridor_core.rendered_claim_safety import (
    assert_safe_normalized_rendered_ecological_copy,
    normalize_rendered_ecological_claim_text,
)

ALLOWED_SUFFIXES = {".css", ".html", ".js", ".woff", ".woff2"}
FONT_SUFFIXES = {".woff", ".woff2"}
APPROVED_FONT_PACKAGES = (
    "@fontsource/atkinson-hyperlegible",
    "@fontsource/barlow-condensed",
    "@fontsource/ibm-plex-mono",
)
GENERATED_MANIFEST = "public-replay-manifest.json"
PRODUCER_IDENTITY = "ecocorridor-pages-producer-identity.json"
PROTECTED_RIDGEWAY_FILES = frozenset(
    {
        "ridgeway-protected-producer-attestation.json",
        "ridgeway-protected-rerun-differential.json",
        "ridgeway-producer-identity.json",
        "ridgeway-producer-provenance.json",
    }
)
MAX_FILES = 256
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_SOURCE_COPY_BYTES = 2 * 1024 * 1024
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RIDGEWAY_HYPOTHESIS_IDS = frozenset(
    {
        "h1_low_barrier",
        "h2_moderate_barrier",
        "h3_high_barrier",
        "h4_future_pressure",
    }
)
RIDGEWAY_HYPOTHESIS_RANKING_METRIC = (
    "Minimum required target-pair resistance reduction (%) within each resistance "
    "hypothesis; rank 1 is the highest score among the 128 directly evaluated portfolios."
)
RIDGEWAY_PUBLIC_NON_GOALS = (
    "Observed animal movement or crossing use",
    "Collision reduction",
    "Population viability or biodiversity benefit",
    "Crossing, fencing, or construction design",
    "Regulatory or permitting determination",
)
PUBLIC_REPLAY_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "ecocorridor-replay"
    / "src"
    / "PublicReplayApp.tsx"
)
PUBLIC_REPLAY_APP = PUBLIC_REPLAY_SOURCE.parents[1]
REDACTED_STORY_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages" / "ecocorridor-evidence" / "src" / "index.ts"
)
RIDGEWAY_DIRECT_EVIDENCE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "ecocorridor"
    / "ridgeway"
    / "direct-evidence-manifest.json"
)
RIDGEWAY_PUBLIC_PROJECTION = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "ecocorridor-desktop"
    / "src"
    / "data"
    / "ridgewayFixture.json"
)
PROCESS_INTERVAL_OVERLAP_KEYS = frozenset(
    {
        "schema_version",
        "observation_method",
        "maximum_overlapping_retained_process_intervals",
        "retained_interval_count",
        "coincident_endpoint_policy",
        "scientific_identity_role",
        "limitation",
    }
)
PROCESS_INTERVAL_OVERLAP_LIMITATION = (
    "Derived only from retained post-spawn launched_at-to-confirmed_at measurement "
    "intervals. It may understate true process concurrency because launched_at is "
    "recorded after process creation, and coincident endpoints may overstate interval "
    "overlap at retained timestamp resolution. It is not the configured worker count, "
    "CPU utilization, or proof of simultaneous physical execution."
)
REQUIRED_CSP = {
    "default-src 'self'",
    "connect-src 'none'",
    "font-src 'self'",
    "img-src 'self' data:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "media-src 'none'",
    "worker-src 'none'",
    "manifest-src 'none'",
    "navigate-to 'none'",
    "script-src 'self'",
    "style-src 'self'",
}
FORBIDDEN_TEXT = (
    re.compile(r"WANDB_API_KEY", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"/Users/[^/]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\]+\\"),
    re.compile(r"(?:__TAURI__|ipc\.localhost)"),
    re.compile(r"__ECOCORRIDOR_HOST__"),
    re.compile(
        r"connectivity\.(?:bundle|planning|asset|grid|hypothesis|focal|scenario|"
        r"intervention|safeguard|model|analysis|results|evidence|export|guided|digitization)\."
    ),
    re.compile(r"observability\.(?:connect|sync|retry|open_|validate_|preview_)"),
    re.compile(r"assistant\.(?:explain|follow_up|feedback|get_trace|intake)"),
    re.compile(r"(?:createProjectWorkspace|chooseConnectivityBundle|chooseConnectivityAsset)"),
)
NON_GOAL_RENDER_SINK = re.compile(
    r"(?:textContent|innerHTML|outerHTML|insertAdjacentHTML|setAttribute)"
    r".{0,256}(?:nonGoals|non_goals)|"
    r"(?:nonGoals|non_goals).{0,256}"
    r"(?:textContent|innerHTML|outerHTML|insertAdjacentHTML|setAttribute)",
    re.DOTALL,
)
JS_UNICODE_ESCAPE = re.compile(
    r"\\u\{(?P<braced>[0-9a-fA-F]{1,6})\}|\\u(?P<short>[0-9a-fA-F]{4})|\\x(?P<byte>[0-9a-fA-F]{2})"
)
CSS_UNICODE_ESCAPE = re.compile(r"\\(?P<codepoint>[0-9a-fA-F]{1,6})(?:\r\n|[\t\n\r\f ])?")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def approved_font_sources() -> dict[str, dict[str, str]]:
    """Bind emitted fonts to exact files from the replay's locked dependencies."""

    package_manifest = _json_object(
        PUBLIC_REPLAY_APP / "package.json",
        label="public replay package manifest",
    )
    dependencies = package_manifest.get("dependencies")
    if not isinstance(dependencies, dict):
        raise TypeError("public replay package manifest omits dependencies")
    approved: dict[str, dict[str, str]] = {}
    for package_name in APPROVED_FONT_PACKAGES:
        expected_version = dependencies.get(package_name)
        if not isinstance(expected_version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", expected_version
        ):
            raise ValueError(f"public replay font dependency is not exactly pinned: {package_name}")
        package_root = PUBLIC_REPLAY_APP / "node_modules" / package_name
        metadata = _json_object(package_root / "package.json", label=f"installed {package_name}")
        if metadata.get("name") != package_name or metadata.get("version") != expected_version:
            raise ValueError(f"installed public replay font does not match its pin: {package_name}")
        files_root = (package_root / "files").resolve(strict=True)
        resolved_package_root = package_root.resolve(strict=True)
        if not files_root.is_relative_to(resolved_package_root):
            raise ValueError(f"public replay font files escape their package: {package_name}")
        for candidate in sorted(files_root.iterdir()):
            if candidate.suffix.casefold() not in FONT_SUFFIXES:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"public replay font source is unsafe: {package_name}")
            approved.setdefault(
                digest(candidate),
                {
                    "package": package_name,
                    "version": expected_version,
                    "file": candidate.name,
                },
            )
    if not approved:
        raise ValueError("public replay has no approved locked font sources")
    return approved


def _regular(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def validate_public_replay_source_copy(source_path: Path | None = None) -> None:
    """Apply the production claim policy to all hard-coded public replay UI copy."""

    sources = (
        (source_path,)
        if source_path is not None
        else (
            PUBLIC_REPLAY_SOURCE,
            REDACTED_STORY_SOURCE,
        )
    )
    audited: dict[str, str] = {}
    try:
        for candidate in sources:
            source = _regular(candidate, label="public replay UI source")
            if source.stat().st_size > MAX_SOURCE_COPY_BYTES:
                raise ValueError("public replay UI source exceeds its claim-audit size limit")
            audited[source.as_posix()] = source.read_text(encoding="utf-8")
        assert_safe_normalized_rendered_ecological_copy(audited)
        if source_path is None and re.search(
            r"\b(?:nonGoals|non_goals)\b", audited[PUBLIC_REPLAY_SOURCE.as_posix()]
        ):
            raise ValueError(
                "the public replay renderer may not read structured non-goal evidence directly"
            )
    except UnicodeDecodeError as exc:
        raise ValueError("public replay UI source is not UTF-8") from exc
    except ValueError as exc:
        raise ValueError("public replay source copy violates ecological claim policy") from exc


def validate_redacted_story_source_binding(
    source_path: Path = REDACTED_STORY_SOURCE,
    manifest_path: Path = RIDGEWAY_DIRECT_EVIDENCE_MANIFEST,
    projection_path: Path = RIDGEWAY_PUBLIC_PROJECTION,
) -> None:
    """Require export code to pin the exact reviewed evidence and projection bytes."""

    source = _regular(source_path, label="redacted decision-story source")
    text = source.read_text(encoding="utf-8")
    expected = {
        "PINNED_MANIFEST_SHA256": digest(
            _regular(manifest_path, label="Ridgeway direct-evidence manifest")
        ),
        "PINNED_PROJECTION_SHA256": digest(
            _regular(projection_path, label="Ridgeway public projection")
        ),
    }
    for constant, expected_digest in expected.items():
        match = re.search(rf'const {constant} = "([0-9a-f]{{64}})";', text)
        if match is None or match.group(1) != expected_digest:
            raise ValueError(
                f"redacted decision-story {constant} is not bound to the reviewed bytes"
            )
    canonical_match = re.search(r'const PINNED_PROJECTION_VALUE_SHA256 = "([0-9a-f]{64})";', text)
    if canonical_match is None:
        raise ValueError("redacted decision-story omits its canonical projection-value digest")
    required_runtime_bindings = (
        "globalThis.crypto?.subtle",
        "await sha256(stableJson(fixture)) !== PINNED_PROJECTION_VALUE_SHA256",
    )
    if any(binding not in text for binding in required_runtime_bindings):
        raise ValueError(
            "redacted decision-story runtime does not compare the active fixture with the "
            "reviewed projection values"
        )


def _json_object(path: Path, *, label: str) -> dict[str, object]:
    value = json.loads(_regular(path, label=label).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object")
    return value


def _mask_validated_structural_non_goals(
    text: str,
    *,
    allowed_non_goals: tuple[str, ...],
) -> str:
    """Retain the claim policy's narrow structural non-goal exemption.

    Production bundling serializes the reviewed projection into JavaScript.
    Remove only the exact reviewed Ridgeway ``decisionBrief.nonGoals`` array.
    A property name alone is not authority: arbitrary ``nonGoals`` arrays stay
    in the text and are checked like all other emitted copy.
    """

    if allowed_non_goals != RIDGEWAY_PUBLIC_NON_GOALS:
        return text
    encoded = json.dumps(list(RIDGEWAY_PUBLIC_NON_GOALS), separators=(",", ":"))
    masked = text
    for key in ("nonGoals:", '"nonGoals":', "non_goals:", '"non_goals":'):
        masked = masked.replace(f"{key}{encoded}", f"{key}[]")
    return masked


def _canonical_non_goal_literal_count(text: str) -> int:
    encoded = json.dumps(list(RIDGEWAY_PUBLIC_NON_GOALS), separators=(",", ":"))
    return sum(
        text.count(f"{key}{encoded}")
        for key in ("nonGoals:", '"nonGoals":', "non_goals:", '"non_goals":')
    )


def _assert_no_mutated_non_goal_array(text: str) -> None:
    """Reject array literals using the protected key unless exactly reviewed."""

    encoded = json.dumps(list(RIDGEWAY_PUBLIC_NON_GOALS), separators=(",", ":"))
    for key in ("nonGoals:", '"nonGoals":', "non_goals:", '"non_goals":'):
        prefix = f"{key}["
        allowed = f"{key}{encoded}"
        if text.count(prefix) != text.count(allowed):
            raise ValueError("built artifact contains a mutated structured non-goal array")


class _RenderedHTMLText(HTMLParser):
    """Collect browser-visible HTML text and human-facing attributes linearly."""

    _VISIBLE_ATTRIBUTES = frozenset({"alt", "aria-label", "placeholder", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        self.parts.extend(
            value
            for name, value in attrs
            if name.casefold() in self._VISIBLE_ATTRIBUTES and value is not None
        )


def _html_rendered_text(text: str) -> str:
    parser = _RenderedHTMLText()
    parser.feed(text)
    parser.close()
    return " ".join(parser.parts)


def _escape_is_active(text: str, start: int) -> bool:
    """Return whether the backslash at ``start`` is not itself escaped."""

    preceding = 0
    cursor = start - 1
    while cursor >= 0 and text[cursor] == "\\":
        preceding += 1
        cursor -= 1
    return preceding % 2 == 0


def _decode_js_unicode_escapes(text: str) -> str:
    """Decode one JavaScript lexical escape layer without inventing execution.

    UTF-16 surrogate escapes are common serializer output. Combine a valid
    adjacent pair into its scalar value and reject unpaired or invalid values;
    silently deleting a surrogate would create a claim-policy blind spot.
    Escaped backslashes remain literal so text such as ``\\\\u0069`` is not
    mistaken for a character the browser would render.
    """

    matches = list(JS_UNICODE_ESCAPE.finditer(text))
    if not matches:
        return text
    result: list[str] = []
    cursor = 0
    index = 0
    while index < len(matches):
        match = matches[index]
        result.append(text[cursor : match.start()])
        if not _escape_is_active(text, match.start()):
            result.append(match.group(0))
            cursor = match.end()
            index += 1
            continue

        token = match.group("braced") or match.group("short") or match.group("byte")
        assert token is not None
        value = int(token, 16)
        if match.group("short") is not None and 0xD800 <= value <= 0xDBFF:
            if index + 1 >= len(matches):
                raise ValueError("built artifact contains an unpaired JavaScript surrogate escape")
            following = matches[index + 1]
            following_token = following.group("short")
            if (
                following.start() != match.end()
                or following_token is None
                or not _escape_is_active(text, following.start())
                or not 0xDC00 <= int(following_token, 16) <= 0xDFFF
            ):
                raise ValueError("built artifact contains an unpaired JavaScript surrogate escape")
            scalar = 0x10000 + ((value - 0xD800) << 10) + (int(following_token, 16) - 0xDC00)
            result.append(chr(scalar))
            cursor = following.end()
            index += 2
            continue
        if 0xD800 <= value <= 0xDFFF or value > 0x10FFFF:
            raise ValueError("built artifact contains an invalid JavaScript Unicode escape")
        result.append(chr(value))
        cursor = match.end()
        index += 1
    result.append(text[cursor:])
    return "".join(result)


def _decode_css_unicode_escapes(text: str) -> str:
    def decode(match: re.Match[str]) -> str:
        if not _escape_is_active(text, match.start()):
            return match.group(0)
        value = int(match.group("codepoint"), 16)
        if value == 0 or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
            return " "
        return chr(value)

    return CSS_UNICODE_ESCAPE.sub(decode, text)


def _decode_emitted_copy(text: str, *, suffix: str) -> tuple[str, ...]:
    """Normalize common browser-decoded forms before applying claim policy.

    The release artifact is executable HTML/CSS/JavaScript, so scanning only
    its source spelling misses Unicode/hex escapes, HTML entities, markup, and
    concatenated string fragments. This deliberately produces a conservative
    plain-text view; a false positive blocks release for human review.
    """

    normalized = text
    if suffix == ".js":
        normalized = _decode_js_unicode_escapes(normalized)
    elif suffix == ".css":
        normalized = _decode_css_unicode_escapes(normalized)
    # A few bounded entity passes cover HTML and JS-to-innerHTML serialization
    # without treating entity-like text in standalone CSS/JSON as executable.
    if suffix in {".html", ".js", ".svg"}:
        for _ in range(3):
            previous = normalized
            normalized = html.unescape(normalized)
            if normalized == previous:
                break
    views = [normalized]
    if suffix == ".html":
        views.append(_html_rendered_text(normalized))
    elif suffix == ".js" and "<" in normalized and ">" in normalized:
        # JS may assign an HTML literal to innerHTML. Parse a second view, but
        # retain the unstripped JS view so angle brackets can never hide text.
        views.append(_html_rendered_text(normalized))
    result: list[str] = []
    for view in views:
        view = normalize_rendered_ecological_claim_text(view)
        # Browser-visible fragments commonly sit across quotes, arrays, tags,
        # or concatenation operators. Preserve actual clause and statement
        # boundaries so an unrelated limitation cannot negate later copy.
        view = re.sub(r"\r\n?|\n", ";", view)
        view = re.sub(r"[^\w'’.!?;–-]+", " ", view, flags=re.UNICODE)
        view = re.sub(r"\s+", " ", view).strip()
        if view and view not in result:
            result.append(view)
    return tuple(result)


def _assert_safe_emitted_ecological_copy(
    text: str,
    *,
    suffix: str,
    allowed_non_goals: tuple[str, ...],
) -> None:
    masked = _mask_validated_structural_non_goals(
        text,
        allowed_non_goals=allowed_non_goals,
    )
    assert_safe_normalized_rendered_ecological_copy(masked)
    for decoded in _decode_emitted_copy(masked, suffix=suffix):
        if decoded != masked:
            assert_safe_normalized_rendered_ecological_copy(decoded)


def _valid_retained_process_interval_overlap(value: object) -> bool:
    maximum = (
        value.get("maximum_overlapping_retained_process_intervals")
        if isinstance(value, dict)
        else None
    )
    interval_count = value.get("retained_interval_count") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and set(value) == PROCESS_INTERVAL_OVERLAP_KEYS
        and value.get("schema_version") == "ridgeway-retained-process-interval-overlap/v1.0"
        and value.get("observation_method") == "MAXIMUM_OVERLAPPING_RETAINED_PROCESS_INTERVALS"
        and not isinstance(maximum, bool)
        and isinstance(maximum, int)
        and not isinstance(interval_count, bool)
        and interval_count == 1_596
        and 1 <= maximum <= interval_count
        and value.get("coincident_endpoint_policy")
        == "COUNTED_AS_OVERLAPPING_AT_RETAINED_TIMESTAMP_RESOLUTION"
        and value.get("scientific_identity_role") == "AUDIT_ONLY_EXCLUDED"
        and value.get("limitation") == PROCESS_INTERVAL_OVERLAP_LIMITATION
    )


def _validate_protected_ridgeway_receipts(
    root: Path,
    *,
    source_sha: str,
    evidence_manifest_sha256: str,
) -> None:
    missing = sorted(name for name in PROTECTED_RIDGEWAY_FILES if not (root / name).is_file())
    if missing:
        raise ValueError(
            "public replay omits protected Ridgeway producer evidence: " + ", ".join(missing)
        )
    attestation = _json_object(
        root / "ridgeway-protected-producer-attestation.json",
        label="protected Ridgeway producer attestation",
    )
    differential = _json_object(
        root / "ridgeway-protected-rerun-differential.json",
        label="protected Ridgeway rerun differential",
    )
    identity = _json_object(
        root / "ridgeway-producer-identity.json",
        label="protected Ridgeway producer identity",
    )
    provenance = _json_object(
        root / "ridgeway-producer-provenance.json",
        label="protected Ridgeway producer provenance",
    )
    artifact = provenance.get("artifact")
    if not isinstance(artifact, dict):
        raise TypeError("protected Ridgeway producer provenance omits its artifact")
    if (
        attestation.get("schema_version") != "ridgeway-protected-producer-attestation/v1.0"
        or attestation.get("source_commit_sha") != source_sha
        or attestation.get("execution_contract") != "FULL_DIRECT_CIRCUITSCAPE_RASTER_PAIRWISE_RERUN"
        or attestation.get("fixture_id") != "ridgeway_crossing"
        or attestation.get("evidence_disposition") != "VERIFIED_DIRECT_EVIDENCE"
        or attestation.get("direct_evidence_manifest_sha256") != evidence_manifest_sha256
        or attestation.get("matrix_job_count") != 1_536
        or attestation.get("map_job_count") != 60
        or differential.get("schema_version") != "ridgeway-protected-rerun-differential/v1.1"
        or differential.get("source_commit_sha") != source_sha
        or differential.get("status") != "PASSED"
        or differential.get("reference_manifest_sha256") != evidence_manifest_sha256
        or differential.get("matrix_job_count") != 1_536
        or differential.get("map_job_count") != 60
        or differential.get("absolute_tolerance") != 1e-8
        or differential.get("relative_tolerance") != 1e-7
        or differential.get("teaching_selection_matched") is not True
        or not _valid_retained_process_interval_overlap(
            differential.get("reference_retained_process_interval_overlap")
        )
        or not _valid_retained_process_interval_overlap(
            differential.get("producer_retained_process_interval_overlap")
        )
        or differential.get("retained_process_interval_overlap_comparison_policy")
        != "AUDIT_ONLY_EXCLUDED_FROM_SCIENTIFIC_EQUIVALENCE"
        or identity.get("schema_version") != "ecocorridor-github-actions-producer-identity/v1.0"
        or identity.get("workflow_path") != ".github/workflows/ecocorridor-ridgeway-evidence.yml"
        or identity.get("head_sha") != source_sha
        or provenance.get("schema_version") != "ecocorridor-github-actions-provenance/v1.0"
        or provenance.get("workflow_path") != ".github/workflows/ecocorridor-ridgeway-evidence.yml"
        or provenance.get("head_sha") != source_sha
        or provenance.get("head_branch") != "main"
        or provenance.get("event") != "workflow_dispatch"
        or provenance.get("conclusion") != "success"
        or provenance.get("repository") != identity.get("repository")
        or provenance.get("run_id") != identity.get("run_id")
        or provenance.get("run_attempt") != identity.get("run_attempt")
        or artifact.get("name") != "ecocorridor-ridgeway-protected-evidence"
        or not isinstance(artifact.get("id"), int)
        or isinstance(artifact.get("id"), bool)
        or artifact["id"] <= 0
        or not isinstance(artifact.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])
    ):
        raise ValueError("public replay protected Ridgeway producer evidence is inconsistent")
    for key in (
        "archive_sha256",
        "producer_archive_sha256",
        "producer_direct_evidence_manifest_sha256",
        "producer_engine_manifest_sha256",
        "producer_workflow_manifest_sha256",
        "protected_rerun_differential_sha256",
    ):
        if not isinstance(attestation.get(key), str) or not SHA256.fullmatch(attestation[key]):
            raise ValueError(f"public replay Ridgeway attestation has an invalid {key}")
    if attestation["protected_rerun_differential_sha256"] != digest(
        root / "ridgeway-protected-rerun-differential.json"
    ):
        raise ValueError("public replay Ridgeway differential is not bound to its attestation")


def validate_and_inventory(
    root: Path,
    *,
    source_sha: str,
    fixture: Path,
    projection: Path,
    trust_record: Path,
    data_manifest: Path,
    require_verified_evidence: bool = False,
) -> dict[str, object]:
    validate_public_replay_source_copy()
    validate_redacted_story_source_binding()
    supplied_root = root
    if supplied_root.is_symlink():
        raise ValueError("public replay root may not be a symlink")
    root = supplied_root.resolve(strict=True)
    if not root.is_dir() or not SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("public replay requires a regular root and exact source commit")
    fixture = _regular(fixture, label="desktop Ridgeway fixture")
    projection = _regular(projection, label="Ridgeway public projection")
    trust_record = _regular(trust_record, label="Ridgeway release-trust record")
    data_manifest = _regular(data_manifest, label="ecological data manifest")
    if fixture.read_bytes() != projection.read_bytes():
        raise ValueError("desktop and benchmark Ridgeway projections are not byte-identical")
    try:
        replay = json.loads(projection.read_text(encoding="utf-8"))
        trust = json.loads(trust_record.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Ridgeway replay policy files must contain valid JSON") from exc
    if not isinstance(replay, dict) or not isinstance(trust, dict):
        raise TypeError("Ridgeway replay policy files must contain JSON objects")
    if trust.get("schemaVersion") != "ecocorridor-release-trust/v1.0" or trust.get(
        "fixtureId"
    ) != replay.get("fixtureId"):
        raise ValueError("Ridgeway replay and trust contracts are incompatible")

    try:
        assert_safe_normalized_rendered_ecological_copy(replay)
    except ValueError as exc:
        raise ValueError("Ridgeway public replay violates ecological claim policy") from exc
    replay_eligible = replay.get("releaseReplayEligible") is True
    decision_brief = replay.get("decisionBrief")
    non_goals = decision_brief.get("nonGoals") if isinstance(decision_brief, dict) else None
    if not isinstance(non_goals, list) or tuple(non_goals) != RIDGEWAY_PUBLIC_NON_GOALS:
        raise ValueError(
            "Ridgeway public replay non-goals differ from the reviewed release boundary"
        )
    allowed_non_goals = RIDGEWAY_PUBLIC_NON_GOALS
    if replay_eligible:
        manifest_sha = replay.get("manifestSha256")
        portfolios = replay.get("portfolios")
        scenarios = replay.get("scenarios")
        study = replay.get("study")
        current_rasters = replay.get("currentRasters")
        review_mask = replay.get("reviewMask")
        if (
            replay.get("schemaVersion") != "ecocorridor-replay/v1.2"
            or replay.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
            or not isinstance(manifest_sha, str)
            or not SHA256.fullmatch(manifest_sha)
            or not isinstance(replay.get("baselinePortfolioId"), str)
            or not replay.get("baselinePortfolioId")
            or trust.get("trusted") is not True
            or trust.get("evidenceManifestSha256") != manifest_sha
            or not isinstance(study, dict)
            or study.get("portfolioCount") != 128
            or study.get("scenarioCount") != 12
            or study.get("matrixJobCount") != 1536
            or study.get("mapJobCount") != 60
            or study.get("hypothesisRankingMetric") != RIDGEWAY_HYPOTHESIS_RANKING_METRIC
            or not isinstance(portfolios, list)
            or len(portfolios) != 4
            or not isinstance(scenarios, list)
            or len(scenarios) != 12
            or not isinstance(current_rasters, list)
            or len(current_rasters) != 60
            or not isinstance(review_mask, dict)
        ):
            raise ValueError("Ridgeway verified replay is missing trusted direct evidence")
        scenario_pair_counts = {
            item.get("id"): item.get("includedPairs")
            for item in scenarios
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("includedPairs"), int)
            and not isinstance(item.get("includedPairs"), bool)
            and item.get("includedPairs") > 0
        }
        if len(scenario_pair_counts) != 12:
            raise ValueError("Ridgeway verified replay has an invalid focal-pair scenario basis")
        baseline_id = replay["baselinePortfolioId"]
        expected_map_ids = {
            f"map_{portfolio_id}_{scenario_id}"
            for portfolio_id in [baseline_id, *(item.get("id") for item in portfolios)]
            for scenario_id in scenario_pair_counts
            if isinstance(portfolio_id, str)
        }
        observed_map_ids = {
            item.get("runId")
            for item in current_rasters
            if isinstance(item, dict)
            and isinstance(item.get("runId"), str)
            and isinstance(item.get("sha256"), str)
            and SHA256.fullmatch(item["sha256"])
            and isinstance(item.get("cells"), list)
            and isinstance(item.get("width"), int)
            and isinstance(item.get("height"), int)
            and len(item["cells"]) == item["width"] * item["height"]
        }
        if observed_map_ids != expected_map_ids:
            raise ValueError(
                "Ridgeway verified replay has incomplete retained current-raster evidence"
            )
        for portfolio in portfolios:
            if (
                not isinstance(portfolio, dict)
                or not isinstance(portfolio.get("id"), str)
                or not isinstance(portfolio.get("hypothesisRanks"), dict)
                or set(portfolio["hypothesisRanks"]) != RIDGEWAY_HYPOTHESIS_IDS
                or not isinstance(portfolio.get("scenarioResults"), list)
                or len(portfolio["scenarioResults"]) != 12
            ):
                raise ValueError("Ridgeway verified replay has incomplete teaching evidence")
            for hypothesis_id, observation in portfolio["hypothesisRanks"].items():
                rank = observation.get("rank") if isinstance(observation, dict) else None
                score = observation.get("score") if isinstance(observation, dict) else None
                if (
                    hypothesis_id not in RIDGEWAY_HYPOTHESIS_IDS
                    or not isinstance(rank, int)
                    or isinstance(rank, bool)
                    or not 1 <= rank <= study["portfolioCount"]
                    or not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not math.isfinite(score)
                ):
                    raise ValueError(
                        "Ridgeway verified replay has invalid hypothesis-rank evidence"
                    )
            portfolio_id = portfolio["id"]
            for result in portfolio["scenarioResults"]:
                if not isinstance(result, dict):
                    raise TypeError("Ridgeway verified replay has incomplete teaching evidence")
                scenario_id = result.get("scenarioId")
                pairs = result.get("pairResults")
                expected_count = scenario_pair_counts.get(scenario_id)
                if (
                    expected_count is None
                    or not isinstance(pairs, list)
                    or len(pairs) != expected_count
                    or result.get("runId") != f"matrix_{portfolio_id}_{scenario_id}"
                    or result.get("baselineMapRunId") != f"map_{baseline_id}_{scenario_id}"
                    or result.get("portfolioMapRunId") != f"map_{portfolio_id}_{scenario_id}"
                ):
                    raise ValueError("Ridgeway verified replay has incomplete focal-pair evidence")
                for pair in pairs:
                    pair_id = pair.get("pairId") if isinstance(pair, dict) else None
                    if (
                        not isinstance(pair_id, str)
                        or pair.get("claimId") != f"ECO-PAIR-{portfolio_id}-{scenario_id}-{pair_id}"
                        or pair.get("baselineRunId") != f"matrix_{baseline_id}_{scenario_id}"
                        or pair.get("portfolioRunId") != f"matrix_{portfolio_id}_{scenario_id}"
                        or not isinstance(pair.get("baselineMatrixSha256"), str)
                        or not SHA256.fullmatch(pair["baselineMatrixSha256"])
                        or not isinstance(pair.get("portfolioMatrixSha256"), str)
                        or not SHA256.fullmatch(pair["portfolioMatrixSha256"])
                    ):
                        raise ValueError("Ridgeway verified replay has invalid focal-pair lineage")
    else:
        portfolios = replay.get("portfolios")
        if (
            replay.get("schemaVersion") != "ecocorridor-replay/v1.1"
            or replay.get("evidenceState") != "ENGINE_RUN_REQUIRED"
            or replay.get("manifestSha256") is not None
            or replay.get("baselinePortfolioId") is not None
            or trust.get("trusted") is not False
            or trust.get("evidenceManifestSha256") is not None
            or not isinstance(replay.get("releaseBlocker"), str)
            or not replay.get("releaseBlocker")
            or not isinstance(portfolios, list)
            or len(portfolios) != 4
        ):
            raise ValueError("Ridgeway unavailable replay is not fail closed")
        for portfolio in portfolios:
            if (
                not isinstance(portfolio, dict)
                or portfolio.get("id") is not None
                or portfolio.get("costUsd") is not None
                or portfolio.get("hypothesisRanks") is not None
                or portfolio.get("scenarioResults") != []
            ):
                raise ValueError("Ridgeway unavailable replay contains reportable evidence")
    if require_verified_evidence and not replay_eligible:
        raise ValueError("public replay deployment requires a trusted v1.2 direct-evidence package")
    if require_verified_evidence:
        _validate_protected_ridgeway_receipts(
            root,
            source_sha=source_sha,
            evidence_manifest_sha256=replay["manifestSha256"],
        )

    files: list[dict[str, object]] = []
    total_bytes = 0
    canonical_non_goal_literals = 0
    font_sources: dict[str, dict[str, str]] | None = None
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in names:
            candidate = Path(directory) / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError(f"public replay contains an unsafe directory: {name}")
        for name in filenames:
            candidate = Path(directory) / name
            relative = candidate.relative_to(root).as_posix()
            if relative == GENERATED_MANIFEST:
                continue
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"public replay contains an unsafe file: {relative}")
            suffix = candidate.suffix.casefold()
            if (
                suffix not in ALLOWED_SUFFIXES
                and relative != PRODUCER_IDENTITY
                and relative not in PROTECTED_RIDGEWAY_FILES
            ):
                raise ValueError(f"public replay contains an unapproved file type: {relative}")
            size = candidate.stat().st_size
            if size <= 0 or size > MAX_FILE_BYTES:
                raise ValueError(f"public replay file has an unsafe size: {relative}")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("public replay exceeds its total byte policy")
            if suffix in {".css", ".html", ".js", ".json", ".svg"}:
                try:
                    text = candidate.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"public replay textual artifact is not UTF-8: {relative}"
                    ) from exc
                if any(pattern.search(text) for pattern in FORBIDDEN_TEXT):
                    raise ValueError(f"public replay contains private/runtime material: {relative}")
                try:
                    _assert_no_mutated_non_goal_array(text)
                    if NON_GOAL_RENDER_SINK.search(text):
                        raise ValueError(
                            "structured non-goals may not flow to a browser rendering sink"
                        )
                    canonical_non_goal_literals += _canonical_non_goal_literal_count(text)
                    _assert_safe_emitted_ecological_copy(
                        text,
                        suffix=suffix,
                        allowed_non_goals=allowed_non_goals,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"public replay built artifact violates ecological claim policy: {relative}"
                    ) from exc
            sha256 = digest(candidate)
            record: dict[str, object] = {
                "path": relative,
                "bytes": size,
                "sha256": sha256,
            }
            if suffix in FONT_SUFFIXES:
                if font_sources is None:
                    font_sources = approved_font_sources()
                source = font_sources.get(sha256)
                if source is None:
                    raise ValueError(
                        f"public replay font is not from an exact locked source package: {relative}"
                    )
                record["source_binding"] = source
            files.append(record)
    if not files or len(files) > MAX_FILES:
        raise ValueError("public replay file count is outside its release policy")
    if canonical_non_goal_literals > 1 or (
        require_verified_evidence and canonical_non_goal_literals != 1
    ):
        raise ValueError(
            "public replay must contain exactly one reviewed structured non-goal literal"
        )
    index = root / "index.html"
    if not index.is_file() or index.is_symlink():
        raise ValueError("public replay index.html is missing or unsafe")
    index_text = index.read_text(encoding="utf-8")
    csp_match = re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"',
        index_text,
        re.IGNORECASE,
    )
    if csp_match is None:
        raise ValueError("public replay has no inline Content Security Policy")
    directive_items = [item.strip() for item in csp_match.group(1).split(";") if item.strip()]
    directive_names = [item.split(maxsplit=1)[0].casefold() for item in directive_items]
    if len(directive_names) != len(set(directive_names)) or set(directive_items) != REQUIRED_CSP:
        raise ValueError("public replay Content Security Policy is not fail-closed")
    return {
        "schema_version": "1.0",
        "artifact_kind": "CC0_SYNTHETIC_PUBLIC_REPLAY",
        "source_commit_sha": source_sha,
        "fixture_sha256": digest(fixture),
        "release_trust_sha256": digest(trust_record),
        "verified_direct_evidence": replay_eligible,
        "ecological_data_manifest_sha256": digest(data_manifest),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": sorted(files, key=lambda item: str(item["path"])),
    }


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--trust-record", type=Path, required=True)
    parser.add_argument("--data-manifest", type=Path, required=True)
    parser.add_argument("--require-verified-evidence", action="store_true")
    arguments = parser.parse_args()
    try:
        payload = validate_and_inventory(
            arguments.root,
            source_sha=arguments.source_sha,
            fixture=arguments.fixture,
            projection=arguments.projection,
            trust_record=arguments.trust_record,
            data_manifest=arguments.data_manifest,
            require_verified_evidence=arguments.require_verified_evidence,
        )
        output = arguments.root.resolve(strict=True) / GENERATED_MANIFEST
        atomic_write(
            output,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    except (OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Public replay boundary passed for {payload['file_count']} static file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
