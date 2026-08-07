"""Verification-only W&B Report narrative for the EcoCorridor evidence program.

This module deliberately publishes no experiment run, artifact, metric, or media.
It verifies the retained historical campaign and the exact implementation
checkpoint already present in the live Report.  Because W&B Reports expose no
compare-and-swap save, an absent or different checkpoint fails without saving.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .review_brief_evaluation import (
    ReviewBriefCampaignArtifact,
    verify_review_brief_campaign,
)

SECTION_ID = "usefulness-implementation-checkpoint/v1"
SECTION_START = f"<!-- ecocorridor-owned-section:{SECTION_ID}:start"
SECTION_END = f"<!-- ecocorridor-owned-section:{SECTION_ID}:end -->"
HISTORICAL_SECTION_START = "<!-- ecocorridor-owned-section:review-brief-agent-evaluation/v1:start"
HISTORICAL_SECTION_END = "<!-- ecocorridor-owned-section:review-brief-agent-evaluation/v1:end -->"
HISTORICAL_PLAN_SHA256 = "00aa1d0ae633b9498e32c329a04d7a1cf7cb6e0743e4e44e757f4a4c6ee72a03"
HISTORICAL_MEDIA_SHA256 = "bd984a8a9fb418e8812a1a9e07fe7c3bcc3a45a8ad254ef181034b91db1d4e6c"
HISTORICAL_MARKER_BODY_SHA256 = "5278d092dbd80bba60146b095f7ecd6f35c43fc98e7959f69fc2656a61424187"
HISTORICAL_SECTION_SEMANTIC_SHA256 = (
    "8e93525eeb35466711db8d910dfbe4df237169a270ec00933fb24ecacbf53be8"
)

SOURCE_REPOSITORY = "ash0ts/gi-scenario-studio"
IMPLEMENTATION_CHECKPOINT_COMMIT = "bbae947a32207008ebb07b597b5373b586818336"
IMPLEMENTATION_PR_URL = "https://github.com/ash0ts/gi-scenario-studio/pull/13"
IMPLEMENTATION_CHECKPOINT_URL = (
    f"https://github.com/ash0ts/gi-scenario-studio/commit/{IMPLEMENTATION_CHECKPOINT_COMMIT}"
)
IMPLEMENTATION_PR_CHECKPOINT_URL = (
    "https://github.com/ash0ts/gi-scenario-studio/pull/13#issuecomment-5214956815"
)
PUBLISHED_CHECKPOINT_SOURCE_COMMIT = "2464708d1a4e6ef2fae7eef8cf39b6cd6a605abb"

HISTORICAL_RUN_ID = "review-brief-realistic-120b-5dad47c-v5"
HISTORICAL_RUN_PATH = f"wandb/ecocorridor-scenario-studio/{HISTORICAL_RUN_ID}"
HISTORICAL_RUN_URL = f"https://wandb.ai/wandb/ecocorridor-scenario-studio/runs/{HISTORICAL_RUN_ID}"
HISTORICAL_ARTIFACT_REF = (
    "wandb/ecocorridor-scenario-studio/ecocorridor-review-brief-33f5f7be9aee:v0"
)
HISTORICAL_ARTIFACT_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/artifacts/evaluation-bundle/"
    "ecocorridor-review-brief-33f5f7be9aee/v0"
)
HISTORICAL_CAMPAIGN_SHA256 = "33f5f7be9aee7bbf09586cfe704358f5cebe9286e0adecf4ec79101609f918c3"
HISTORICAL_SECTION_MARKER = (
    f"{HISTORICAL_SECTION_START} campaign={HISTORICAL_CAMPAIGN_SHA256} "
    f"plan={HISTORICAL_PLAN_SHA256} media={HISTORICAL_MEDIA_SHA256} "
    f"body={HISTORICAL_MARKER_BODY_SHA256} -->"
)
HISTORICAL_DATASET_SHA256 = "437647a2533a23eb038b3512939f3574a6edd0629f97c71d63795a429fb54f10"
HISTORICAL_ARTIFACT_MANIFEST_SHA256 = (
    "8f6b6fb1054d24743ee5057f0eaaa3199d9beb31f00c1cc5317b27944b9526cf"
)
HISTORICAL_CAMPAIGN_FILE_SHA256 = "95b5e9b2b13a74bd0804d6205b56892875d52d773fd480a8b8b23fd731bf7fbf"
HISTORICAL_DATASET_FILE_SHA256 = "cc480357373dd8a09720d5f5aeeecac53447bce835a7374a156b82d2da5edbff"
HISTORICAL_PROVIDER_COST_BASIS_SHA256 = (
    "820df98770299de18b1893c1cbc48cd576c5d641b57218fef1a79afed3240a55"
)
HISTORICAL_SOURCE_COMMIT = "5dad47c8de2a88b3348446416a805b121aaae7e6"
HISTORICAL_MODEL = "openai/gpt-oss-120b"
HISTORICAL_RECORD_COUNT = 42
HISTORICAL_PROVIDER_CONVERSATION_COUNT = 22
HISTORICAL_PRE_PROVIDER_BLOCK_COUNT = 6
HISTORICAL_BASELINE_INPUT_TOKENS = 10_798
HISTORICAL_NATIVE_INPUT_TOKENS = 10_542
HISTORICAL_DECISION = "KEEP_DETERMINISTIC"
HISTORICAL_DEFAULT_METHOD = "deterministic_exhaustive"
HISTORICAL_CONTEXT_REDUCTION_PERCENT = (
    100.0
    * (HISTORICAL_BASELINE_INPUT_TOKENS - HISTORICAL_NATIVE_INPUT_TOKENS)
    / HISTORICAL_BASELINE_INPUT_TOKENS
)
HISTORICAL_ARTIFACT_FILE_IDENTITIES: tuple[tuple[str, str, int], ...] = (
    (
        "animation/01-question.png",
        "bf63c02ca30c65f4383ed66b6f846379b8e7992ed2bb3475ce1cc76ae79544df",
        74_609,
    ),
    (
        "animation/02-dataset.png",
        "223fae723c07dd2963cb77c23b5d83c4801a9c4e93df978f1e0c99ca2e4b16f0",
        77_663,
    ),
    (
        "animation/03-methods.png",
        "f49fd94ab3c51c238688053707991065338a6e7b58ee899c5d110cf6ecda8947",
        88_226,
    ),
    (
        "animation/04-trust.png",
        "1a798a3e64a75c456017643a3bad22e6ec3991e2335d03efbffba904ff15bce4",
        85_571,
    ),
    (
        "animation/05-observations.png",
        "c6faa4a4b7da897131ddb887c5823f5b405464363e5c28e206bce4de6fab37fc",
        75_745,
    ),
    (
        "animation/06-default-decision.png",
        "99d13639d8221d553d9025bd195a3644f18515bfca423245631f54f75848b53f",
        92_381,
    ),
    (
        "animation/07-limits.png",
        "6e219659c46bcbdd8af332872f8f5477345b4c14f4109b0fc2f43bded9c86bba",
        91_469,
    ),
    (
        "animation/contact-sheet.png",
        "a9aa6610f872efedf571d2901b7715b80c427db34d27d048117dac8a5f08ce22",
        185_380,
    ),
    (
        "animation/ecocorridor-review-brief-campaign-poster.png",
        "c6faa4a4b7da897131ddb887c5823f5b405464363e5c28e206bce4de6fab37fc",
        75_745,
    ),
    (
        "animation/ecocorridor-review-brief-campaign.html",
        "2b97ab1f847f21083b00cc304985316053aba6f7356c97cda060a32e42207ff2",
        24_776,
    ),
    (
        "animation/ecocorridor-review-brief-campaign.mp4",
        "1aa9493e39def320ccbcf9db6f69fd67c6d2cfa6c4c9d11fbf5b834d8ab0417e",
        1_744_843,
    ),
    (
        "animation/review-brief-animation-media-manifest.json",
        "fc5c2f0f884668f7d3cfda75850ac62053560013d7aa60ae2350dc57a1a989e8",
        6_033,
    ),
    (
        "animation/review-brief-animation-render-contract.json",
        "223f369c4bd82b72c13e4abe9d6d9e94e8a7c60b01e97d24d43dd45bd477a7d3",
        16_871,
    ),
    (
        "animation/review-brief-animation-transcript.txt",
        "174181f43cf242ea4631c55128cb88d66880d72bdd957181fae6987fd558f914",
        3_269,
    ),
    (
        "animation/review-brief-animation-validation.json",
        "0d7e5ce0615a41bc39c01a6328c6421cbb2e558df974e4130cf950a40842559d",
        235_402,
    ),
    (
        "review-brief-animation-story.json",
        "e051bdf8d07313888006e1e519b6ed006e0a99813dfb5d5a75fdf5cbde2cf8e1",
        11_785,
    ),
    (
        "review-brief-animation-transcript.txt",
        "1596e04c57dff004ab35adf31f96902afe484dfb677959628a7d27dcd1dfcf64",
        2_183,
    ),
    (
        "review-brief-campaign.json",
        "95b5e9b2b13a74bd0804d6205b56892875d52d773fd480a8b8b23fd731bf7fbf",
        248_834,
    ),
    (
        "review-brief-dataset.json",
        "cc480357373dd8a09720d5f5aeeecac53447bce835a7374a156b82d2da5edbff",
        45_213,
    ),
)
HISTORICAL_ARTIFACT_FILES = frozenset(
    logical_name for logical_name, _sha256, _bytes in HISTORICAL_ARTIFACT_FILE_IDENTITIES
)
HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256 = (
    "e2cac1bc6bd14fa1f2bf3ed7cd02563d4564aa67ada8424f06b45f3d8ef3be98"
)
HISTORICAL_DOWNLOADED_TOTAL_BYTES = 3_185_998
HISTORICAL_ARTIFACT_MEDIA_TYPES = {
    ".html": "text/html",
    ".json": "application/json",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".txt": "text/plain",
}

RIDGEWAY_PROJECTION_SHA256 = "57091a4776e255d597cf9561ad245c121786a5d2562e3227021c5ba38091f08e"
RIDGEWAY_DIRECT_MANIFEST_SHA256 = "abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e"
RIDGEWAY_CARRY_PORTFOLIO_ID = "portfolio_d3aa8b1e170ffd76b322"
RIDGEWAY_CONTROLLING_SCENARIO_ID = "scenario_safeguard_guild_h1_low_barrier"

EXPECTED_REPORT_TITLE = "EcoCorridor historical search benchmark · d43c377f · 2be48975"
EXPECTED_REPORT_ID = "VmlldzoxNzY1OTU0MQ=="
REPORT_READBACK_TIMEOUT_SECONDS = 20.0
REPORT_READBACK_INTERVAL_SECONDS = 0.5


class HistoricalCampaignReadback(ContractModel):
    """Immutable historical artifact identities validated from downloaded bytes."""

    campaign_sha256: Literal["33f5f7be9aee7bbf09586cfe704358f5cebe9286e0adecf4ec79101609f918c3"] = (
        HISTORICAL_CAMPAIGN_SHA256
    )
    campaign_file_sha256: Literal[
        "95b5e9b2b13a74bd0804d6205b56892875d52d773fd480a8b8b23fd731bf7fbf"
    ] = HISTORICAL_CAMPAIGN_FILE_SHA256
    dataset_sha256: Literal["437647a2533a23eb038b3512939f3574a6edd0629f97c71d63795a429fb54f10"] = (
        HISTORICAL_DATASET_SHA256
    )
    dataset_file_sha256: Literal[
        "cc480357373dd8a09720d5f5aeeecac53447bce835a7374a156b82d2da5edbff"
    ] = HISTORICAL_DATASET_FILE_SHA256
    provider_trace_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_trace_receipt_count: Literal[22] = HISTORICAL_PROVIDER_CONVERSATION_COUNT
    exact_artifact_file_count: Literal[19] = len(HISTORICAL_ARTIFACT_FILES)
    artifact_manifest_sha256: Literal[
        "8f6b6fb1054d24743ee5057f0eaaa3199d9beb31f00c1cc5317b27944b9526cf"
    ]
    downloaded_file_manifest_sha256: Literal[
        "e2cac1bc6bd14fa1f2bf3ed7cd02563d4564aa67ada8424f06b45f3d8ef3be98"
    ]
    downloaded_total_bytes: Literal[3185998]


class RidgewayNarrativeEvidence(ContractModel):
    """Exact committed fixture values allowed in the narrative."""

    projection_sha256: Literal[
        "57091a4776e255d597cf9561ad245c121786a5d2562e3227021c5ba38091f08e"
    ] = RIDGEWAY_PROJECTION_SHA256
    direct_manifest_sha256: Literal[
        "abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e"
    ] = RIDGEWAY_DIRECT_MANIFEST_SHA256
    portfolio_id: Literal["portfolio_d3aa8b1e170ffd76b322"] = RIDGEWAY_CARRY_PORTFOLIO_ID
    scenario_id: Literal["scenario_safeguard_guild_h1_low_barrier"] = (
        RIDGEWAY_CONTROLLING_SCENARIO_ID
    )
    pass_count: Literal[12] = 12
    required_count: Literal[12] = 12
    cost_usd: Literal[400000.0] = 400000.0
    budget_usd: Literal[500000.0] = 500000.0
    rule_status: Literal["review"] = "review"
    resistance_change_percent: float
    review_mask_change_percent: float

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if abs(self.resistance_change_percent - (-1.6031418683724497)) > 1e-12:
            raise ValueError("Ridgeway resistance value differs from the committed projection")
        if abs(self.review_mask_change_percent - 47.34703286028441) > 1e-12:
            raise ValueError("Ridgeway review-mask value differs from the committed projection")
        return self


class UsefulnessNarrativeReceipt(ContractModel):
    """Local receipt created only after historical-run and Report readback."""

    schema_version: Literal["ecocorridor-usefulness-narrative-receipt/v1"] = (
        "ecocorridor-usefulness-narrative-receipt/v1"
    )
    status: Literal["COMPLETE"] = "COMPLETE"
    source_repository: Literal["ash0ts/gi-scenario-studio"] = SOURCE_REPOSITORY
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    implementation_checkpoint_commit: Literal["bbae947a32207008ebb07b597b5373b586818336"] = (
        IMPLEMENTATION_CHECKPOINT_COMMIT
    )
    historical_campaign_sha256: Literal[
        "33f5f7be9aee7bbf09586cfe704358f5cebe9286e0adecf4ec79101609f918c3"
    ] = HISTORICAL_CAMPAIGN_SHA256
    historical_artifact_manifest_sha256: Literal[
        "8f6b6fb1054d24743ee5057f0eaaa3199d9beb31f00c1cc5317b27944b9526cf"
    ] = HISTORICAL_ARTIFACT_MANIFEST_SHA256
    historical_downloaded_file_manifest_sha256: Literal[
        "e2cac1bc6bd14fa1f2bf3ed7cd02563d4564aa67ada8424f06b45f3d8ef3be98"
    ]
    historical_provider_trace_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_run_url: str
    report_url: str
    report_id: Literal["VmlldzoxNzY1OTU0MQ=="] = EXPECTED_REPORT_ID
    report_title: Literal["EcoCorridor historical search benchmark · d43c377f · 2be48975"] = (
        EXPECTED_REPORT_TITLE
    )
    section_marker: str
    section_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_section_semantic_sha256: Literal[
        "8e93525eeb35466711db8d910dfbe4df237169a270ec00933fb24ecacbf53be8"
    ] = HISTORICAL_SECTION_SEMANTIC_SHA256
    ridgeway_projection_sha256: Literal[
        "57091a4776e255d597cf9561ad245c121786a5d2562e3227021c5ba38091f08e"
    ] = RIDGEWAY_PROJECTION_SHA256
    original_block_count: int = Field(ge=1)
    final_block_count: int = Field(ge=1)
    historical_run_readback_verified: Literal[True] = True
    report_readback_verified: Literal[True] = True
    historical_prefix_preserved: Literal[True] = True
    observed_effectiveness_result_count: Literal[0] = 0
    corrected_campaign_result_count: Literal[0] = 0
    new_wandb_run_created: Literal[False] = False
    new_artifact_created: Literal[False] = False
    new_result_media_logged: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if self.receipt_sha256 != expected:
            raise ValueError("usefulness narrative receipt digest differs")
        if self.final_block_count < self.original_block_count:
            raise ValueError("W&B Report block count cannot decrease")
        return self

    @classmethod
    def create(cls, **values: Any) -> UsefulnessNarrativeReceipt:
        payload = {
            "schema_version": "ecocorridor-usefulness-narrative-receipt/v1",
            "status": "COMPLETE",
            "source_repository": SOURCE_REPOSITORY,
            "implementation_checkpoint_commit": IMPLEMENTATION_CHECKPOINT_COMMIT,
            "historical_campaign_sha256": HISTORICAL_CAMPAIGN_SHA256,
            "historical_artifact_manifest_sha256": HISTORICAL_ARTIFACT_MANIFEST_SHA256,
            "report_id": EXPECTED_REPORT_ID,
            "report_title": EXPECTED_REPORT_TITLE,
            "historical_section_semantic_sha256": HISTORICAL_SECTION_SEMANTIC_SHA256,
            "ridgeway_projection_sha256": RIDGEWAY_PROJECTION_SHA256,
            "historical_run_readback_verified": True,
            "report_readback_verified": True,
            "historical_prefix_preserved": True,
            "observed_effectiveness_result_count": 0,
            "corrected_campaign_result_count": 0,
            "new_wandb_run_created": False,
            "new_artifact_created": False,
            "new_result_media_logged": False,
            **values,
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


def _safe_https_url(value: object, *, label: str) -> str:
    rendered = str(value or "")
    parsed = urlsplit(rendered)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    return rendered


def _https_origin(value: object, *, label: str) -> tuple[str, str, int]:
    rendered = _safe_https_url(value, label=label)
    parsed = urlsplit(rendered)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    return parsed.scheme, str(parsed.hostname).casefold(), port


def _require_web_origin(value: object, *, web_base_url: str, label: str) -> str:
    rendered = _safe_https_url(value, label=label)
    if _https_origin(rendered, label=label) != _https_origin(
        web_base_url, label="W&B web base URL"
    ):
        raise PermissionError(f"{label} belongs to another W&B deployment")
    return rendered


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _historical_artifact_media_type(logical_name: str) -> str:
    media_type = HISTORICAL_ARTIFACT_MEDIA_TYPES.get(Path(logical_name).suffix.casefold())
    if media_type is None:
        raise ValueError("historical W&B artifact contains an unsupported media type")
    return media_type


def _verify_downloaded_file_manifest(
    directory: Path,
    *,
    expected_file_identities: Sequence[tuple[str, str, int]] = (
        HISTORICAL_ARTIFACT_FILE_IDENTITIES
    ),
    expected_manifest_sha256: str = HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256,
    expected_total_bytes: int = HISTORICAL_DOWNLOADED_TOTAL_BYTES,
    expected_artifact_manifest_sha256: str = HISTORICAL_ARTIFACT_MANIFEST_SHA256,
) -> tuple[str, int, str]:
    """Verify every downloaded byte against one complete pinned file manifest."""

    root = directory.resolve()
    if directory.is_symlink() or not root.is_dir():
        raise ValueError("historical W&B artifact download is missing or unsafe")
    expected_manifest = {
        logical_name: {"sha256": sha256, "bytes": byte_count}
        for logical_name, sha256, byte_count in expected_file_identities
    }
    if len(expected_manifest) != len(expected_file_identities):
        raise ValueError("historical W&B artifact pinned file manifest has duplicate paths")
    expected_files = frozenset(expected_manifest)
    observed: set[str] = set()
    observed_manifest: list[dict[str, str | int]] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("historical W&B artifact contains an unsafe file")
        relative = path.relative_to(root).as_posix()
        if relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError("historical W&B artifact contains an unsafe path")
        observed.add(relative)
    if observed != expected_files:
        raise ValueError("historical W&B artifact file set differs")
    for relative in sorted(observed):
        path = root / relative
        expected = expected_manifest[relative]
        byte_count = path.stat().st_size
        sha256 = _sha256_file(path)
        if byte_count != expected["bytes"] or sha256 != expected["sha256"]:
            raise ValueError(f"historical W&B artifact retained file bytes differ: {relative}")
        observed_manifest.append({"logical_name": relative, "sha256": sha256, "bytes": byte_count})
    downloaded_file_manifest_sha256 = canonical_sha256(observed_manifest)
    downloaded_total_bytes = sum(int(item["bytes"]) for item in observed_manifest)
    if (
        downloaded_file_manifest_sha256 != expected_manifest_sha256
        or downloaded_total_bytes != expected_total_bytes
    ):
        raise ValueError("historical W&B artifact downloaded file manifest differs")
    artifact_manifest_sha256 = canonical_sha256(
        [
            {
                **item,
                "media_type": _historical_artifact_media_type(str(item["logical_name"])),
            }
            for item in observed_manifest
        ]
    )
    if artifact_manifest_sha256 != expected_artifact_manifest_sha256:
        raise ValueError("historical W&B artifact publication manifest differs")
    return (
        downloaded_file_manifest_sha256,
        downloaded_total_bytes,
        artifact_manifest_sha256,
    )


def verify_downloaded_historical_artifact(directory: Path) -> HistoricalCampaignReadback:
    """Verify the immutable campaign bytes and every retained trace receipt."""

    root = directory.resolve()
    (
        downloaded_file_manifest_sha256,
        downloaded_total_bytes,
        artifact_manifest_sha256,
    ) = _verify_downloaded_file_manifest(root)

    campaign_path = root / "review-brief-campaign.json"
    dataset_path = root / "review-brief-dataset.json"
    campaign_bytes = campaign_path.read_bytes()
    dataset_bytes = dataset_path.read_bytes()
    if hashlib.sha256(campaign_bytes).hexdigest() != HISTORICAL_CAMPAIGN_FILE_SHA256:
        raise ValueError("historical campaign file digest differs")
    if hashlib.sha256(dataset_bytes).hexdigest() != HISTORICAL_DATASET_FILE_SHA256:
        raise ValueError("historical dataset file digest differs")

    campaign = verify_review_brief_campaign(
        ReviewBriefCampaignArtifact.model_validate_json(campaign_bytes)
    )
    canonical_campaign = (
        json.dumps(campaign.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    canonical_dataset = (
        json.dumps(campaign.dataset.model_dump(mode="json"), indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    if campaign_bytes != canonical_campaign or dataset_bytes != canonical_dataset:
        raise ValueError("historical campaign or dataset bytes are noncanonical")
    decision = campaign.default_method_decision
    if (
        campaign.campaign_sha256 != HISTORICAL_CAMPAIGN_SHA256
        or campaign.dataset.dataset_sha256 != HISTORICAL_DATASET_SHA256
        or campaign.source_repository != SOURCE_REPOSITORY
        or campaign.source_commit != HISTORICAL_SOURCE_COMMIT
        or campaign.campaign_wandb_run_id != HISTORICAL_RUN_ID
        or campaign.campaign_wandb_run_url != HISTORICAL_RUN_URL
        or len(campaign.records) != HISTORICAL_RECORD_COUNT
        or campaign.provider_conversation_count != HISTORICAL_PROVIDER_CONVERSATION_COUNT
        or campaign.pre_provider_block_count != HISTORICAL_PRE_PROVIDER_BLOCK_COUNT
        or decision.baseline_role_input_tokens != HISTORICAL_BASELINE_INPUT_TOKENS
        or decision.native_role_input_tokens != HISTORICAL_NATIVE_INPUT_TOKENS
        or decision.decision != HISTORICAL_DECISION
        or decision.current_default_method != HISTORICAL_DEFAULT_METHOD
        or decision.promotion_gate_passed is not False
    ):
        raise ValueError("historical campaign identity or decision differs")

    receipts: list[dict[str, Any]] = []
    for record in campaign.records:
        provider = record.execution.provider_execution
        if provider is None:
            continue
        receipt = {
            "case_id": record.case_id,
            "method_id": record.method_id,
            "provider_call_id": provider.provider_call_id,
            "provider_response_sha256": provider.provider_response_sha256,
            "weave_call_id": provider.weave_call_id,
            "weave_trace_id": provider.weave_trace_id,
            "weave_trace_url": provider.weave_trace_url,
            "trace_status": provider.trace_status,
            "trace_readback_verified": provider.trace_readback_verified,
        }
        if (
            receipt["trace_status"] != "VERIFIED"
            or receipt["trace_readback_verified"] is not True
            or not receipt["provider_call_id"]
            or not receipt["weave_call_id"]
            or not receipt["weave_trace_id"]
        ):
            raise ValueError("historical provider trace receipt is incomplete")
        _safe_https_url(receipt["weave_trace_url"], label="historical Weave trace URL")
        receipts.append(receipt)
    receipts.sort(key=lambda item: (str(item["method_id"]), str(item["case_id"])))
    if len(receipts) != HISTORICAL_PROVIDER_CONVERSATION_COUNT:
        raise ValueError("historical provider trace receipt count differs")
    if len({item["provider_call_id"] for item in receipts}) != len(receipts):
        raise ValueError("historical provider call identities are not unique")
    if len({item["weave_call_id"] for item in receipts}) != len(receipts):
        raise ValueError("historical Weave call identities are not unique")

    return HistoricalCampaignReadback(
        provider_trace_receipts_sha256=canonical_sha256(receipts),
        artifact_manifest_sha256=artifact_manifest_sha256,
        downloaded_file_manifest_sha256=downloaded_file_manifest_sha256,
        downloaded_total_bytes=downloaded_total_bytes,
    )


def load_ridgeway_narrative_evidence(repository_root: Path) -> RidgewayNarrativeEvidence:
    """Resolve article values only from checksum-classified committed fixture bytes."""

    root = repository_root.resolve()
    data_manifest_path = root / "benchmarks/ecocorridor/data-manifest.json"
    projection_path = root / "benchmarks/ecocorridor/ridgeway/ui_projection.json"
    direct_manifest_path = root / "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json"
    for path in (data_manifest_path, projection_path, direct_manifest_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Ridgeway narrative evidence is missing or unsafe")
    if _sha256_file(projection_path) != RIDGEWAY_PROJECTION_SHA256:
        raise ValueError("Ridgeway projection digest differs")
    if _sha256_file(direct_manifest_path) != RIDGEWAY_DIRECT_MANIFEST_SHA256:
        raise ValueError("Ridgeway direct-evidence manifest digest differs")

    data_manifest = json.loads(data_manifest_path.read_bytes())
    entries = [
        item
        for item in data_manifest.get("files", [])
        if item.get("path") == "benchmarks/ecocorridor/ridgeway/ui_projection.json"
    ]
    if len(entries) != 1:
        raise ValueError("Ridgeway projection data classification is missing or ambiguous")
    entry = entries[0]
    if (
        entry.get("sha256") != RIDGEWAY_PROJECTION_SHA256
        or entry.get("byte_size") != projection_path.stat().st_size
        or entry.get("license") != "CC0-1.0"
        or entry.get("synthetic") is not True
        or entry.get("sensitivity") != "PUBLIC"
        or entry.get("contains_real_locations") is not False
        or entry.get("contains_species_occurrences") is not False
        or entry.get("public_replay_allowed") is not True
    ):
        raise ValueError("Ridgeway projection data classification differs")

    projection = json.loads(projection_path.read_bytes())
    direct_manifest = json.loads(direct_manifest_path.read_bytes())
    if (
        projection.get("schemaVersion") != "ecocorridor-replay/v1.2"
        or projection.get("manifestSha256") != RIDGEWAY_DIRECT_MANIFEST_SHA256
        or projection.get("cc0") is not True
        or projection.get("fixtureId") != "ridgeway_crossing"
        or projection.get("evidenceState") != "VERIFIED_DIRECT_EVIDENCE"
        or direct_manifest.get("schema_version") != "ridgeway-direct-evidence/v1.2"
        or direct_manifest.get("matrix_job_count") != 1_536
        or direct_manifest.get("map_job_count") != 60
    ):
        raise ValueError("Ridgeway projection and direct manifest are inconsistent")
    portfolios = [
        item
        for item in projection.get("portfolios", [])
        if item.get("id") == RIDGEWAY_CARRY_PORTFOLIO_ID
        and item.get("projectionKey") == "carry-forward"
    ]
    if len(portfolios) != 1:
        raise ValueError("Ridgeway carry-forward portfolio is missing or ambiguous")
    portfolio = portfolios[0]
    results = [
        item
        for item in portfolio.get("scenarioResults", [])
        if item.get("scenarioId") == RIDGEWAY_CONTROLLING_SCENARIO_ID
    ]
    if len(results) != 1:
        raise ValueError("Ridgeway controlling scenario is missing or ambiguous")
    result = results[0]
    return RidgewayNarrativeEvidence(
        pass_count=portfolio.get("passCount"),
        required_count=portfolio.get("requiredCount"),
        cost_usd=portfolio.get("costUsd"),
        budget_usd=projection.get("decisionBrief", {}).get("budgetUsd"),
        rule_status=portfolio.get("ruleStatus"),
        resistance_change_percent=result.get("resistanceChangePct"),
        review_mask_change_percent=result.get("reviewMaskChangePct"),
    )


def _report_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _report_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_report_value(nested) for nested in value]
    spec = getattr(value, "_spec", None)
    if isinstance(spec, Mapping):
        return _report_value(spec)
    public = getattr(value, "__dict__", None)
    if isinstance(public, Mapping):
        return {
            str(key): _report_value(nested)
            for key, nested in public.items()
            if not str(key).startswith("_")
        }
    raise TypeError("W&B Report block contains unsupported state")


def _block_representation(block: Any) -> dict[str, Any]:
    spec = getattr(block, "_spec", None)
    value = _report_value(spec) if isinstance(spec, Mapping) else _report_value(block)
    return {"block_type": type(block).__name__, "value": value}


def _normalize_report_filter(value: str) -> list[dict[str, str]]:
    clauses: list[dict[str, str]] = []
    for raw_clause in re.split(r"\s+and\s+", value.strip()):
        metric = re.fullmatch(
            r"(?:ID|Metric\((?:\"ID\"|'ID')\))\s*(?:=|==)\s*'([^']+)'",
            raw_clause,
        )
        if metric is not None:
            clauses.append({"field": "ID", "value": metric.group(1)})
            continue
        config = re.fullmatch(
            r"Config\((?:\"([^\"]+)\"|'([^']+)')\)\s*(?:=|==)\s*'([^']+)'",
            raw_clause,
        )
        if config is None:
            raise ValueError("W&B Report Runset filter has unsupported semantics")
        clauses.append(
            {
                "field": "Config",
                "key": str(config.group(1) or config.group(2)),
                "value": config.group(3),
            }
        )
    if not clauses:
        raise ValueError("W&B Report Runset filter is empty")
    return sorted(clauses, key=lambda item: (item["field"], item.get("key", ""), item["value"]))


def _selector_name(value: Any) -> Any:
    if isinstance(value, Mapping) and set(value) == {"name"} and isinstance(value.get("name"), str):
        return str(value["name"])
    return value


def _normalize_panel_grid_roundtrip(value: Any) -> Any:
    if not isinstance(value, Mapping):
        raise TypeError("W&B PanelGrid state is not an object")
    normalized = copy.deepcopy(dict(value))
    panels = normalized.get("panels")
    runsets = normalized.get("runsets")
    if not isinstance(panels, list) or not isinstance(runsets, list):
        raise TypeError("W&B PanelGrid panels or runsets are not lists")
    for panel in panels:
        if not isinstance(panel, dict):
            raise TypeError("W&B PanelGrid panel is not an object")
        panel.pop("layout", None)
        for selector in ("metric", "x"):
            if selector in panel:
                panel[selector] = _selector_name(panel[selector])
        if "y" in panel:
            if not isinstance(panel["y"], list):
                raise TypeError("W&B PanelGrid y selector is not a list")
            panel["y"] = [_selector_name(item) for item in panel["y"]]
    for runset in runsets:
        if not isinstance(runset, dict):
            raise TypeError("W&B PanelGrid runset is not an object")
        if "filters" in runset:
            if not isinstance(runset["filters"], str):
                raise TypeError("W&B PanelGrid Runset filter is not text")
            runset["filters"] = _normalize_report_filter(runset["filters"])
        order = runset.get("order")
        if not isinstance(order, list):
            raise TypeError("W&B PanelGrid Runset order is not a list")
        for item in order:
            if not isinstance(item, dict):
                raise TypeError("W&B PanelGrid Runset order item is not an object")
            if "name" in item:
                item["name"] = _selector_name(item["name"])
    return normalized


def _semantic_block_representation(block: Any) -> dict[str, Any]:
    representation = _block_representation(block)
    if representation["block_type"] == "PanelGrid":
        representation["value"] = _normalize_panel_grid_roundtrip(representation["value"])
    return representation


def _semantic_blocks_sha256(blocks: Sequence[Any]) -> str:
    return canonical_sha256(
        {
            "canonicalizer": "wandb-reports-panelgrid-roundtrip/v1",
            "blocks": [_semantic_block_representation(block) for block in blocks],
        }
    )


def _blocks_sha256(blocks: Sequence[Any]) -> str:
    return canonical_sha256([_block_representation(block) for block in blocks])


def _block_text(block: Any) -> str | None:
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _checkpoint_sections(blocks: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    sections: list[tuple[str, list[Any]]] = []
    marker: str | None = None
    body: list[Any] = []
    for block in blocks:
        text = _block_text(block)
        if text is not None and text.startswith(SECTION_START):
            if marker is not None:
                raise ValueError("W&B Report contains nested implementation checkpoints")
            marker = text
            body = []
            continue
        if text == SECTION_END:
            if marker is None:
                raise ValueError("W&B Report contains an unmatched checkpoint end marker")
            sections.append((marker, body))
            marker = None
            body = []
            continue
        if marker is not None:
            body.append(block)
    if marker is not None:
        raise ValueError("W&B Report contains an unterminated implementation checkpoint")
    return sections


def append_checkpoint_section(existing: Sequence[Any], owned: Sequence[Any]) -> list[Any]:
    sections = _checkpoint_sections(owned)
    if len(sections) != 1:
        raise ValueError("publisher must supply exactly one implementation checkpoint")
    expected_marker = sections[0][0]
    current = _checkpoint_sections(existing)
    if any(marker == expected_marker for marker, _ in current):
        return list(existing)
    if current:
        raise RuntimeError("Report already contains a different v1 implementation checkpoint")
    return [*existing, *owned]


def _require_historical_report_section(blocks: Sequence[Any]) -> None:
    sections: list[tuple[str, list[Any]]] = []
    marker: str | None = None
    body: list[Any] = []
    for block in blocks:
        text = _block_text(block)
        if text is not None and text.startswith(HISTORICAL_SECTION_START):
            if marker is not None:
                raise PermissionError("target Report has nested historical campaign sections")
            marker = text
            body = []
            continue
        if text == HISTORICAL_SECTION_END:
            if marker is None:
                raise PermissionError("target Report has an unmatched historical end marker")
            sections.append((marker, body))
            marker = None
            body = []
            continue
        if marker is not None:
            body.append(block)
    if marker is not None:
        raise PermissionError("target Report has an unterminated historical campaign section")
    if (
        len(sections) != 1
        or sections[0][0] != HISTORICAL_SECTION_MARKER
        or _semantic_blocks_sha256(sections[0][1]) != HISTORICAL_SECTION_SEMANTIC_SHA256
    ):
        raise PermissionError("target Report lacks the exact retained historical campaign section")


def _require_published_checkpoint_section(
    blocks: Sequence[Any],
    *,
    reports: Any,
    ridgeway: RidgewayNarrativeEvidence,
) -> tuple[str, list[Any]]:
    """Verify the exact live v1 checkpoint without permitting a rewrite.

    The already-published section is the immutable authority.  Rebuilding the
    expected blocks from its pinned publisher commit catches a forged marker,
    altered copy, or a body digest that no longer matches the visible Report.
    """

    sections = _checkpoint_sections(blocks)
    if len(sections) != 1:
        raise RuntimeError(
            "the existing W&B Report must contain exactly one published v1 checkpoint; "
            "in-place publication is disabled"
        )
    marker, body = sections[0]
    match = re.fullmatch(
        re.escape(SECTION_START)
        + r" source=([0-9a-f]{40})"
        + r" implementation=([0-9a-f]{40})"
        + r" historical=([0-9a-f]{64})"
        + r" body=([0-9a-f]{64}) -->",
        marker,
    )
    if match is None:
        raise RuntimeError("the published v1 checkpoint marker is malformed")
    published_source, implementation, historical, declared_body = match.groups()
    observed_body = _blocks_sha256(body)
    expected = build_checkpoint_section(
        reports,
        source_commit=PUBLISHED_CHECKPOINT_SOURCE_COMMIT,
        ridgeway=ridgeway,
    )
    expected_marker, expected_body = _checkpoint_sections(expected)[0]
    if (
        published_source != PUBLISHED_CHECKPOINT_SOURCE_COMMIT
        or implementation != IMPLEMENTATION_CHECKPOINT_COMMIT
        or historical != HISTORICAL_CAMPAIGN_SHA256
        or declared_body != observed_body
        or marker != expected_marker
        or _blocks_sha256(expected_body) != observed_body
    ):
        raise RuntimeError("the published v1 checkpoint differs from its immutable source")
    return marker, body


def build_checkpoint_body(
    reports: Any,
    *,
    source_commit: str,
    ridgeway: RidgewayNarrativeEvidence,
) -> list[Any]:
    """Build narrative blocks containing no new observed-result claim."""

    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("publisher source commit must be a full Git commit")
    reduction = HISTORICAL_CONTEXT_REDUCTION_PERCENT
    return [
        reports.H1("Usefulness evidence program — implementation checkpoint"),
        reports.CalloutBlock(
            text=(
                "IMPLEMENTATION CHECKPOINT — NOT A USEFULNESS RESULT. The evidence program "
                "is now implemented and fail-closed, but the corrected experiments have not "
                "yet been executed. The deterministic Review Brief workflow remains the "
                "production default."
            )
        ),
        reports.H2("What the historical campaign established"),
        reports.MarkdownBlock(
            text=(
                f"The retained campaign contains **{HISTORICAL_RECORD_COUNT} scored records**, "
                f"**{HISTORICAL_PROVIDER_CONVERSATION_COUNT} provider-backed Weave "
                f"conversations**, and **{HISTORICAL_PRE_PROVIDER_BLOCK_COUNT} pre-provider "
                "blocks**. Exact trace readback and fail-closed execution were demonstrated "
                "under those recorded conditions.\n\n"
                f"The native workflow used {HISTORICAL_NATIVE_INPUT_TOKENS:,} provider input "
                f"tokens versus {HISTORICAL_BASELINE_INPUT_TOKENS:,} for the comparator—a "
                f"**{reduction:.2f}% reduction**, below the declared **25%** gate. The recorded "
                f"decision remains **`{HISTORICAL_DECISION}`**. This is a useful negative "
                "result, not an agent victory.\n\n"
                "The native workflow followed a fixed tool sequence, so it did not demonstrate "
                "selective traversal or learned stopping. The single-shot comparator also "
                "received the expected answer contract, so it was not a fair quality or "
                "efficiency comparator. The historical result remains unchanged; this section "
                "reports corrected implementation status only."
            )
        ),
        reports.H2("Why the project could matter"),
        reports.MarkdownBlock(
            text=(
                "Connectivity screening can look persuasive while obscuring the assumption, "
                "safeguard, direct model run, or artifact hash that controls a disposition. "
                "EcoCorridor is designed to make that chain inspectable and to keep a favorable "
                "headline metric from becoming an approval.\n\n"
                "The synthetic Ridgeway audit drill makes the point: its carry-forward candidate "
                f"passes **{ridgeway.pass_count}/{ridgeway.required_count} modeled scenario "
                f"rules** and costs **USD {ridgeway.cost_usd:,.0f}** against a "
                f"**USD {ridgeway.budget_usd:,.0f}** cap, yet remains `{ridgeway.rule_status}` "
                "because the controlling safeguard case has a "
                f"**{ridgeway.resistance_change_percent:.4f}% baseline-relative "
                "effective-resistance change** alongside "
                f"**{ridgeway.review_mask_change_percent:.4f}% diagnostic road-mask current "
                "redistribution**. The proper next action "
                "is investigation—not approval. The current-map statistic is diagnostic, not "
                "observed animal movement or crossing use."
            )
        ),
        reports.H2("What is now implemented"),
        reports.MarkdownBlock(
            text=(
                "The source now contains five structurally different CC0 landscape families; a "
                "corrected **300-task** evidence-complexity design; fair full-context, adaptive, "
                "forced-exhaustive, deterministic, and oracle controls; **1,000 frozen safety "
                "variants**; exact finding-to-evidence scoring; and practitioner crossover, "
                "blinded handoff, and retrospective shadow-project protocols. W&B/Weave "
                "readback, preregistration ordering, privacy projection, and claim-authority "
                "gates fail closed.\n\n"
                "The direct-study lane contains a **160-job plan**, but this checkpoint publishes "
                "no direct-study execution result. Local development executions remain "
                "**`TEST_ONLY_UNPUBLISHABLE`** unless a protected, signed and attested release "
                "engine produces the retained package. Implemented controls and protocols are "
                "not observed effectiveness results."
            )
        ),
        reports.H2("Current claim ladder"),
        reports.MarkdownBlock(
            text=(
                "| Claim | Status | Evidence boundary |\n"
                "|---|---|---|\n"
                "| Historical trace integrity | **ESTABLISHED UNDER RECORDED CONDITIONS** | "
                "42 records and 22 provider conversations; no untraced release |\n"
                "| Synthetic task correctness | **BLOCKED** | Protected engine provenance, "
                "publishable package, and independent adjudication are incomplete |\n"
                "| Adaptive-agent selectivity and efficiency | **BLOCKED** | Corrected "
                "three-model provider campaign has not run |\n"
                "| Statistical agent safety | **BLOCKED** | Provider-backed attack campaign "
                "has not run across every model and agentic method |\n"
                "| Practitioner workflow improvement | **BLOCKED** | Pilot and powered "
                "randomized crossover have not run |\n"
                "| Reviewer handoff utility | **BLOCKED** | Six blinded dyads have not run |\n"
                "| External workflow utility | **BLOCKED** | Three authorized retrospective "
                "partner projects have not run |\n\n"
                "No higher claim may be inferred from historical trace integrity or local test "
                "execution."
            )
        ),
        reports.H2("What must happen next"),
        reports.MarkdownBlock(
            text=(
                "1. Produce a protected, signed and attested Circuitscape engine release and a "
                "publishable direct-evidence package.\n"
                "2. Freeze independent ecological and transportation adjudication of the "
                "synthetic cases.\n"
                "3. Supply two distinct owner-approved gateway models plus provider credentials, "
                "then run the corrected agent and safety campaigns with exact Weave readback.\n"
                "4. Complete organizational review, consent, retention, accessibility, pilot, "
                "powered practitioner crossover, blinded handoff, and authorized shadow work.\n"
                "5. Append observed results only after their exact artifacts, traces, and claim "
                "ledger pass readback."
            )
        ),
        reports.H2("Audit trail and bounded conclusion"),
        reports.MarkdownBlock(
            text=(
                f"Historical evidence: [exact W&B run]({HISTORICAL_RUN_URL}) · "
                f"implementation: [checkpoint commit]({IMPLEMENTATION_CHECKPOINT_URL}) · "
                f"[draft PR]({IMPLEMENTATION_PR_URL}) · "
                f"[verification checkpoint]({IMPLEMENTATION_PR_CHECKPOINT_URL}) · publisher "
                f"source `{source_commit}`.\n\n"
                "The project now has a stronger mechanism for testing whether EcoCorridor and "
                "its read-only agent reduce review burden without losing safeguards. The "
                "corrected experiment has not yet produced that evidence. No result here "
                "establishes observed animal movement, crossing use, collision reduction, "
                "population response, biodiversity benefit, an optimal or approved corridor, "
                "regulatory acceptance, or a design-ready crossing. **No new animation was "
                "generated or logged** because no completed, hash-bound corrected campaign "
                "exists."
            )
        ),
    ]


def build_checkpoint_section(
    reports: Any,
    *,
    source_commit: str,
    ridgeway: RidgewayNarrativeEvidence,
) -> list[Any]:
    body = build_checkpoint_body(reports, source_commit=source_commit, ridgeway=ridgeway)
    body_sha256 = _blocks_sha256(body)
    marker = (
        f"{SECTION_START} source={source_commit} "
        f"implementation={IMPLEMENTATION_CHECKPOINT_COMMIT} "
        f"historical={HISTORICAL_CAMPAIGN_SHA256} body={body_sha256} -->"
    )
    return [
        reports.MarkdownBlock(text=marker),
        *body,
        reports.MarkdownBlock(text=SECTION_END),
    ]


class WandbUsefulnessNarrativeVerifier:
    """Verify the retained campaign and already-published immutable section.

    W&B's Reports SDK performs last-writer-wins upserts and exposes no compare-
    and-swap precondition.  This verifier therefore has no Report save path:
    an exact live v1 section is verified, while an absent or different section
    fails closed for a separately reviewed immutable-version workflow.
    """

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        api_base_url: str,
        web_base_url: str,
        authorize_report_verification: bool,
        wandb_module: Any | None = None,
        reports_module: Any | None = None,
        artifact_download_verifier: Callable[
            [Path], HistoricalCampaignReadback
        ] = verify_downloaded_historical_artifact,
        historical_section_verifier: Callable[
            [Sequence[Any]], None
        ] = _require_historical_report_section,
    ) -> None:
        if not authorize_report_verification:
            raise PermissionError("remote W&B Report verification requires explicit authorization")
        if entity != "wandb" or project != "ecocorridor-scenario-studio":
            raise PermissionError("narrative verifier is bound to the existing article project")
        if wandb_module is None:
            import wandb as wandb_module  # type: ignore[import-not-found,no-redef]
        if reports_module is None:
            import wandb_workspaces.reports.v2 as reports_module  # type: ignore[import-not-found,no-redef]
        self.entity = entity
        self.project = project
        self.api_base_url = _safe_https_url(api_base_url, label="W&B API base URL").rstrip("/")
        self.web_base_url = _safe_https_url(web_base_url, label="W&B web base URL").rstrip("/")
        self.wandb = wandb_module
        self.reports = reports_module
        self.artifact_download_verifier = artifact_download_verifier
        self.historical_section_verifier = historical_section_verifier

    def _api(self) -> Any:
        return self.wandb.Api(overrides={"base_url": self.api_base_url})

    def verify_historical_run(self) -> tuple[str, HistoricalCampaignReadback]:
        api = self._api()
        run = api.run(HISTORICAL_RUN_PATH)
        config = dict(getattr(run, "config", {}) or {})
        summary = dict(getattr(run, "summary", {}) or {})
        run_url = _require_web_origin(
            getattr(run, "url", ""),
            web_base_url=self.web_base_url,
            label="historical W&B run URL",
        )
        expected = {
            "id": str(getattr(run, "id", "")) == HISTORICAL_RUN_ID,
            "state": str(getattr(run, "state", "")).casefold() == "finished",
            "url": run_url == HISTORICAL_RUN_URL,
            "campaign": config.get("campaign_sha256") == HISTORICAL_CAMPAIGN_SHA256,
            "dataset": config.get("dataset_sha256") == HISTORICAL_DATASET_SHA256,
            "source": config.get("source_commit") == HISTORICAL_SOURCE_COMMIT,
            "model": config.get("provider_model") == HISTORICAL_MODEL,
            "config_decision": config.get("default_method_decision") == HISTORICAL_DECISION,
            "records": summary.get("review_brief/record_count") == HISTORICAL_RECORD_COUNT,
            "conversations": summary.get("review_brief/provider_conversation_count")
            == HISTORICAL_PROVIDER_CONVERSATION_COUNT,
            "blocks": summary.get("review_brief/pre_provider_block_count")
            == HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
            "baseline_tokens": summary.get("review_brief/baseline_role_input_tokens")
            == HISTORICAL_BASELINE_INPUT_TOKENS,
            "native_tokens": summary.get("review_brief/native_role_input_tokens")
            == HISTORICAL_NATIVE_INPUT_TOKENS,
            "decision": summary.get("review_brief/default_method_decision") == HISTORICAL_DECISION,
            "default": summary.get("review_brief/current_default_method")
            == HISTORICAL_DEFAULT_METHOD,
            "not_promoted": summary.get("review_brief/promotion_gate_passed") == 0,
            "complete": summary.get("review_brief/status") == "COMPLETE",
            "artifact": summary.get("review_brief/artifact_ref") == HISTORICAL_ARTIFACT_REF,
        }
        failed = sorted(name for name, valid in expected.items() if not valid)
        if failed:
            raise RuntimeError(
                "historical W&B campaign failed exact narrative readback: " + ", ".join(failed)
            )
        artifact = api.artifact(HISTORICAL_ARTIFACT_REF)
        metadata = dict(getattr(artifact, "metadata", {}) or {})
        artifact_url = _require_web_origin(
            getattr(artifact, "url", ""),
            web_base_url=self.web_base_url,
            label="historical W&B artifact URL",
        )
        expected_metadata = {
            "schema_version": "ecocorridor-review-brief-publication/v1",
            "campaign_sha256": HISTORICAL_CAMPAIGN_SHA256,
            "dataset_sha256": HISTORICAL_DATASET_SHA256,
            "source_commit": HISTORICAL_SOURCE_COMMIT,
            "campaign_wandb_run_id": HISTORICAL_RUN_ID,
            "artifact_manifest_sha256": HISTORICAL_ARTIFACT_MANIFEST_SHA256,
            "provider_cost_basis_sha256": HISTORICAL_PROVIDER_COST_BASIS_SHA256,
            "provider_backed": True,
            "video_rendered": True,
            "video_logged": True,
        }
        if (
            str(getattr(artifact, "qualified_name", "")) != HISTORICAL_ARTIFACT_REF
            or str(getattr(artifact, "type", "")) != "evaluation-bundle"
            or artifact_url != HISTORICAL_ARTIFACT_URL
            or any(metadata.get(key) != value for key, value in expected_metadata.items())
        ):
            raise RuntimeError("historical W&B artifact identity or metadata differs")
        with tempfile.TemporaryDirectory(prefix="ecocorridor-narrative-artifact-") as temporary:
            downloaded = Path(artifact.download(root=temporary)).resolve()
            artifact_readback = self.artifact_download_verifier(downloaded)
        return run_url, artifact_readback

    def verify(
        self,
        *,
        report_url: str,
        source_commit: str,
        ridgeway: RidgewayNarrativeEvidence,
    ) -> UsefulnessNarrativeReceipt:
        historical_run_url, historical_artifact = self.verify_historical_run()
        target = _require_web_origin(
            report_url,
            web_base_url=self.web_base_url,
            label="existing W&B Report URL",
        )
        report = self.reports.Report.from_url(target)
        if (
            str(getattr(report, "entity", "")) != self.entity
            or str(getattr(report, "project", "")) != self.project
            or str(getattr(report, "id", "")) != EXPECTED_REPORT_ID
            or str(getattr(report, "title", "")) != EXPECTED_REPORT_TITLE
        ):
            raise PermissionError(
                "existing W&B Report identity differs from the historical article"
            )
        existing = list(report.blocks)
        self.historical_section_verifier(existing)
        prefix_sha256 = _blocks_sha256(existing)
        if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("verifier source commit must be a full Git commit")
        marker, body = _require_published_checkpoint_section(
            existing,
            reports=self.reports,
            ridgeway=ridgeway,
        )
        body_sha256 = _blocks_sha256(body)
        updated_url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="verified W&B Report URL",
        )
        deadline = time.monotonic() + REPORT_READBACK_TIMEOUT_SECONDS
        while True:
            verified = False
            final_count = 0
            try:
                readback = self.reports.Report.from_url(updated_url)
                if (
                    str(getattr(readback, "entity", "")) != self.entity
                    or str(getattr(readback, "project", "")) != self.project
                    or str(getattr(readback, "id", "")) != EXPECTED_REPORT_ID
                    or str(getattr(readback, "title", "")) != EXPECTED_REPORT_TITLE
                ):
                    raise PermissionError("read-back W&B Report identity differs")
                blocks = list(readback.blocks)
                final_count = len(blocks)
                self.historical_section_verifier(blocks)
                readback_marker, readback_body = _require_published_checkpoint_section(
                    blocks,
                    reports=self.reports,
                    ridgeway=ridgeway,
                )
                prefix_preserved = (
                    len(blocks) >= len(existing)
                    and _blocks_sha256(blocks[: len(existing)]) == prefix_sha256
                )
                verified = bool(
                    prefix_preserved
                    and readback_marker == marker
                    and _blocks_sha256(readback_body) == body_sha256
                )
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry avoids remote-content leakage
                verified = False
            if verified:
                return UsefulnessNarrativeReceipt.create(
                    source_commit=source_commit,
                    historical_artifact_manifest_sha256=(
                        historical_artifact.artifact_manifest_sha256
                    ),
                    historical_downloaded_file_manifest_sha256=(
                        historical_artifact.downloaded_file_manifest_sha256
                    ),
                    historical_provider_trace_receipts_sha256=(
                        historical_artifact.provider_trace_receipts_sha256
                    ),
                    historical_run_url=historical_run_url,
                    report_url=updated_url,
                    section_marker=marker,
                    section_body_sha256=body_sha256,
                    historical_prefix_sha256=prefix_sha256,
                    original_block_count=len(existing),
                    final_block_count=final_count,
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("W&B Report failed exact verification-only narrative readback")
            time.sleep(REPORT_READBACK_INTERVAL_SECONDS)


def atomic_write_receipt(path: Path, receipt: UsefulnessNarrativeReceipt) -> None:
    payload = receipt.model_dump_json(indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    observed = UsefulnessNarrativeReceipt.model_validate_json(path.read_bytes())
    if observed != receipt:
        raise RuntimeError("usefulness narrative receipt failed exact local readback")
