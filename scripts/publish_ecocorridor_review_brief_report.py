#!/usr/bin/env python3
"""Publish a verified Review Brief campaign into the existing EcoCorridor Report.

The command never creates a report.  It first logs and reads back a versioned
provider-backed campaign artifact, then appends one marker-bounded
``review-brief-agent-evaluation/v1`` section to the explicitly supplied Report.
Existing blocks and earlier owned campaign sections are immutable. Dry-run
planning imports neither W&B nor the reports SDK.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.guided_evidence import GuidedEvidenceResource
from ecocorridor_core.review_brief_animation import (
    MEDIA_MANIFEST_FILENAME,
    ReviewBriefAnimationMediaManifest,
    load_validated_review_brief_animation_media,
)
from ecocorridor_core.review_brief_evaluation import (
    METHOD_IDS,
    REPORT_SECTION_ID,
    ReviewBriefArtifactReceipt,
    ReviewBriefCampaignArtifact,
    build_review_brief_publication_plan,
    build_ridgeway_review_brief_dataset,
    publish_and_update_review_brief_report,
    validate_review_brief_publication_plan,
    verify_review_brief_campaign,
)

SECTION_START_PREFIX = f"<!-- ecocorridor-owned-section:{REPORT_SECTION_ID}:start"
SECTION_END = f"<!-- ecocorridor-owned-section:{REPORT_SECTION_ID}:end -->"
MAX_CAMPAIGN_BYTES = 32 * 1024 * 1024


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
        raise ValueError(f"{label} must be credential-free HTTPS")
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


@contextmanager
def _wandb_base_environment(api_base_url: str) -> Iterator[None]:
    previous = os.environ.get("WANDB_BASE_URL")
    os.environ["WANDB_BASE_URL"] = api_base_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WANDB_BASE_URL", None)
        else:
            os.environ["WANDB_BASE_URL"] = previous


def _git_source(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("repository HEAD is not a full Git commit")
    return commit, not bool(status.strip())


def _load_current_dataset(root: Path) -> Any:
    ridgeway = root / "benchmarks" / "ecocorridor" / "ridgeway"
    manifest_path = ridgeway / "direct-evidence-manifest.json"
    projection_path = ridgeway / "ui_projection.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = ridgeway / str(manifest["evidence_bundle"]["filename"])
    resource = GuidedEvidenceResource.verify(
        projection_path=projection_path,
        projection_sha256=hashlib.sha256(projection_path.read_bytes()).hexdigest(),
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        archive_path=archive,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )
    return build_ridgeway_review_brief_dataset(resource)


def load_verified_campaign(
    path: str | Path, *, repository_root: Path
) -> ReviewBriefCampaignArtifact:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_CAMPAIGN_BYTES:
        raise ValueError("review-brief campaign file is missing, unsafe, or oversized")
    campaign = verify_review_brief_campaign(
        ReviewBriefCampaignArtifact.model_validate_json(source.read_bytes())
    )
    expected_dataset = _load_current_dataset(repository_root)
    if campaign.dataset != expected_dataset:
        raise ValueError(
            "review-brief campaign is not bound to the current verified Ridgeway evidence"
        )
    return campaign


def attach_validated_media_to_publication_plan(
    plan: Mapping[str, Any],
    *,
    campaign: ReviewBriefCampaignArtifact,
    media_manifest_path: str | Path,
) -> dict[str, Any]:
    """Attach only a byte-verified media bundle for this exact campaign and story."""

    validate_review_brief_publication_plan(plan)
    manifest, directory = load_validated_review_brief_animation_media(
        media_manifest_path,
        campaign=campaign,
    )
    if (
        manifest.campaign_sha256 != plan["campaign_sha256"]
        or manifest.dataset_sha256 != plan["dataset_sha256"]
        or manifest.story_sha256 != plan["story_sha256"]
        or manifest.source_commit != plan["source_commit"]
        or manifest.campaign_wandb_run_id != plan["campaign_wandb_run_id"]
        or manifest.campaign_wandb_run_url != plan["campaign_wandb_run_url"]
    ):
        raise ValueError("review-brief media does not match the publication plan")
    attached = copy.deepcopy(dict(plan))
    attached["video_rendered"] = True
    attached["video_logged"] = False
    attached["validated_media"] = manifest.model_dump(mode="json")
    attached["wandb_projection"]["artifact_type"] = "evaluation-bundle"
    local_paths: dict[str, str] = {}
    media_files = [*manifest.files]
    manifest_source = Path(media_manifest_path).resolve()
    manifest_bytes = manifest_source.read_bytes()
    artifact_files = list(attached["artifact_files"])
    artifact_files.append(
        {
            "logical_name": f"animation/{MEDIA_MANIFEST_FILENAME}",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "bytes": len(manifest_bytes),
            "media_type": "application/json",
        }
    )
    local_paths[f"animation/{MEDIA_MANIFEST_FILENAME}"] = str(manifest_source)
    for item in media_files:
        logical_name = f"animation/{item.logical_name}"
        artifact_files.append(
            {
                "logical_name": logical_name,
                "sha256": item.sha256,
                "bytes": item.bytes,
                "media_type": item.media_type,
            }
        )
        local_paths[logical_name] = str(directory / item.logical_name)
    attached["artifact_files"] = sorted(
        artifact_files,
        key=lambda item: str(item["logical_name"]),
    )
    attached["artifact_manifest_sha256"] = canonical_sha256(attached["artifact_files"])
    attached["_artifact_local_paths"] = local_paths
    attached.pop("plan_sha256", None)
    attached["plan_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in attached.items()
            if key not in {"_artifact_payloads", "_artifact_local_paths"}
        }
    )
    validate_review_brief_publication_plan(attached)
    return attached


def _media_path_for_role(plan: Mapping[str, Any], role: str) -> str:
    manifest = ReviewBriefAnimationMediaManifest.model_validate(plan["validated_media"])
    matches = [item for item in manifest.files if item.role == role]
    if len(matches) != 1:
        raise ValueError(f"review-brief media requires exactly one {role} file")
    return str(plan["_artifact_local_paths"][f"animation/{matches[0].logical_name}"])


def _block_text(block: Any) -> str | None:
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _owned_section_markers(blocks: Sequence[Any]) -> list[str]:
    """Validate marker balance and return every complete owned-section marker."""

    markers: list[str] = []
    inside = False
    current = ""
    for block in blocks:
        text = _block_text(block)
        if text is not None and text.startswith(SECTION_START_PREFIX):
            if inside:
                raise ValueError("W&B Report has nested Review Brief section markers")
            inside = True
            current = text
            continue
        if text == SECTION_END:
            if not inside:
                raise ValueError("W&B Report has an unmatched Review Brief end marker")
            inside = False
            markers.append(current)
            current = ""
    if inside:
        raise ValueError("W&B Report has an unterminated Review Brief section")
    return markers


def append_owned_report_section(existing: Sequence[Any], owned: Sequence[Any]) -> list[Any]:
    """Append one owned campaign section while preserving every existing block."""

    existing_markers = _owned_section_markers(existing)
    owned_markers = _owned_section_markers(owned)
    if len(owned_markers) != 1:
        raise ValueError("publisher must supply exactly one complete Review Brief section")
    if owned_markers[0] in existing_markers:
        # An exact retry after a successful Report save is idempotent and does
        # not rewrite or duplicate historical content.
        return list(existing)
    return [*existing, *owned]


def replace_owned_report_section(existing: Sequence[Any], owned: Sequence[Any]) -> list[Any]:
    """Compatibility alias retained with append-only semantics."""

    return append_owned_report_section(existing, owned)


def build_report_section_blocks(
    reports: Any,
    plan: Mapping[str, Any],
    receipt: ReviewBriefArtifactReceipt,
    *,
    entity: str,
    project: str,
) -> list[Any]:
    marker = (
        f"{SECTION_START_PREFIX} campaign={plan['campaign_sha256']} plan={plan['plan_sha256']} -->"
    )
    runset = reports.Runset(
        entity=entity,
        project=project,
        name="Verified Review Brief campaign",
        filters=(
            f"ID = '{plan['wandb_projection']['run_id']}' and "
            f"Config('campaign_sha256') = '{plan['campaign_sha256']}' and "
            f"Config('dataset_sha256') = '{plan['dataset_sha256']}' and "
            f"Config('plan_sha256') = '{plan['plan_sha256']}'"
        ),
    )
    method_panels = [
        reports.ScalarChart(
            title=f"{method_id}: cases passed",
            metric=f"review_brief/{method_id}/passed_case_count",
        )
        for method_id in METHOD_IDS
    ]
    if plan["video_rendered"]:
        animation_blocks = [
            reports.MarkdownBlock(
                text=(
                    "The silent animation, poster, seven scene checkpoints, contact sheet, and "
                    "accessible transcript passed deterministic frame, responsive layout, "
                    "reduced-motion, H.264 High/yuv420p/TV-range, exact frame-count, and "
                    "no-audio validation. Every media byte is bound to this campaign, story, "
                    f"and claim ledger by manifest `{plan['validated_media']['media_manifest_sha256']}`."
                )
            ),
            reports.PanelGrid(
                runsets=[runset],
                panels=[
                    reports.MediaBrowser(
                        title="Validated Review Brief animation evidence",
                        num_columns=2,
                        media_keys=[
                            "review_brief/animation",
                            "review_brief/poster",
                            "review_brief/contact_sheet",
                            "review_brief/checkpoints",
                        ],
                        mode="gallery",
                        gallery_axis="index",
                        layout=reports.Layout(w=24, h=14),
                    )
                ],
            ),
        ]
    else:
        animation_blocks = [
            reports.MarkdownBlock(
                text=(
                    "The artifact contains a claim-ledger-bound silent-first animation story "
                    "contract and transcript. **No video was rendered or logged.** Rendering "
                    "remains a separate gate so motion cannot imply results absent from this "
                    "campaign."
                )
            )
        ]
    return [
        reports.MarkdownBlock(text=marker),
        reports.H1("Review Brief agent evaluation"),
        reports.CalloutBlock(text=plan["report_copy"]["boundary"]),
        reports.H2("What was tested"),
        reports.MarkdownBlock(
            text=(
                f"A campaign evaluated **{plan['method_count']} methods**—one local control and "
                f"two provider-backed methods—over **{plan['case_count']} fixed cases** "
                f"({plan['record_count']} method-by-case records). The two provider methods "
                f"produced **{plan['provider_conversation_count']} "
                "recorded case conversations with verified provider/Weave receipts**; capability "
                "preflights are additional and are not counted as case records. "
                f"**{plan['pre_provider_block_count']} provider rows** stopped before invocation "
                "because their fixed guard boundary prohibited external context. Four cases are "
                "derived from the verified Ridgeway teaching roles; the remaining cases exercise "
                "fail-closed behavior. All methods receive the same dataset and deterministic "
                "scorers. Every fail-closed row wrote and read back a checksum-verified rejection "
                "audit in an isolated application EcoProjectStore, confirmed the "
                "rejection-excludes-draft trigger, queried the real eco_review_brief_drafts table "
                "for its call identity, and observed zero saved application drafts."
            )
        ),
        reports.H2("Default-method decision"),
        reports.MarkdownBlock(
            text=(
                f"The fixed policy returned **{plan['default_method_decision']['decision']}**. "
                "Promotion requires every native gate, non-inferior role and safety results, and "
                "at least **25% fewer** provider-reported input tokens than the "
                "single-shot comparator, plus a recomputable cost estimate on one signed basis "
                "and an independent "
                "semantic basis for scoring action/discipline appropriateness. The 25% threshold "
                "is a project-defined operational choice, not a user-specified or "
                "ecological-science threshold. Unless every gate passes, the deterministic "
                "exhaustive method remains the production default. "
                "The native snapshot's closed review-topic facet independently gates action and "
                "primary-discipline appropriateness. The recorded native candidate status is "
                f"**{plan['default_method_decision']['candidate_status']}**. "
                "This report records the decision; it does not change the desktop automatically."
            )
        ),
        reports.H2("Observed deterministic checks"),
        reports.PanelGrid(
            runsets=[runset],
            panels=[
                *method_panels,
                reports.LinePlot(
                    title="Cases passed by fixed method order",
                    x="review_brief/method_index",
                    y="review_brief/cases_passed",
                    title_x="0 deterministic · 1 single-shot · 2 native agent",
                    title_y="Deterministic cases passed",
                ),
            ],
        ),
        reports.H2("Auditable tables"),
        reports.PanelGrid(
            runsets=[runset],
            panels=[
                reports.WeavePanelSummaryTable(
                    table_name="review_brief_cases", layout=reports.Layout(w=24, h=10)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="review_brief_method_results",
                    layout=reports.Layout(w=24, h=16),
                ),
                reports.WeavePanelSummaryTable(
                    table_name="review_brief_trace_tools",
                    layout=reports.Layout(w=24, h=14),
                ),
                reports.WeavePanelSummaryTable(
                    table_name="review_brief_scorecards", layout=reports.Layout(w=24, h=8)
                ),
                reports.WeavePanelSummaryTable(
                    table_name="review_brief_claim_ledger", layout=reports.Layout(w=24, h=12)
                ),
            ],
        ),
        reports.H2("Why this is useful"),
        reports.MarkdownBlock(text=plan["report_copy"]["why_useful"]),
        reports.H2("Animation and graphic evidence"),
        *animation_blocks,
        reports.H2("Run, artifact, and limitations"),
        reports.MarkdownBlock(
            text=(
                f"[Open the exact W&B run]({receipt.run_url}) · immutable campaign artifact "
                f"`{receipt.artifact_ref}` · artifact manifest "
                f"`{receipt.artifact_manifest_sha256}`\n\n{plan['report_copy']['limitations']}"
            )
        ),
        reports.MarkdownBlock(text=SECTION_END),
    ]


class WandbSdkReviewBriefAdapter:
    """Small SDK adapter whose network boundary is replaceable in tests."""

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        api_base_url: str,
        web_base_url: str,
        authorize_synthetic_public_upload: bool,
        wandb_module: Any | None = None,
        reports_module: Any | None = None,
    ) -> None:
        if not entity.strip() or not project.strip():
            raise ValueError("W&B entity and project are required")
        if not authorize_synthetic_public_upload:
            raise PermissionError("publishing requires explicit synthetic-public upload consent")
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

    def publish_campaign(self, plan: Mapping[str, Any]) -> ReviewBriefArtifactReceipt:
        validate_review_brief_publication_plan(plan)
        projection = plan["wandb_projection"]
        _require_web_origin(
            projection["run_url"],
            web_base_url=self.web_base_url,
            label="campaign W&B run URL",
        )
        with tempfile.TemporaryDirectory(prefix="ecocorridor-review-brief-wandb-") as temporary:
            settings = self.wandb.Settings(
                base_url=self.api_base_url,
                console="off",
                disable_code=True,
                disable_git=True,
                x_disable_machine_info=True,
                x_disable_meta=True,
                x_save_requirements=False,
                silent=True,
            )
            run = self.wandb.init(
                entity=self.entity,
                project=self.project,
                id=projection["run_id"],
                name=projection["run_name"],
                job_type=projection["job_type"],
                tags=[
                    "assistant_evaluation",
                    "review_brief",
                    "provider_backed",
                    "synthetic_ridgeway",
                ],
                config={
                    "run_kind": "assistant_evaluation",
                    "campaign_sha256": plan["campaign_sha256"],
                    "dataset_sha256": plan["dataset_sha256"],
                    "plan_sha256": plan["plan_sha256"],
                    "source_repository": plan["source_repository"],
                    "source_commit": plan["source_commit"],
                    "campaign_wandb_run_id": plan["campaign_wandb_run_id"],
                    "provider_backed": True,
                    "provider_cost_basis_ref": plan["provider_cost_basis"]["reference"],
                    "provider_cost_basis_sha256": plan["provider_cost_basis"]["basis_sha256"],
                    "provider_input_usd_per_million": plan["provider_cost_basis"][
                        "input_usd_per_million"
                    ],
                    "provider_output_usd_per_million": plan["provider_cost_basis"][
                        "output_usd_per_million"
                    ],
                    "provider_conversation_count": plan["provider_conversation_count"],
                    "pre_provider_block_count": plan["pre_provider_block_count"],
                    "default_method_decision": plan["default_method_decision"]["decision"],
                    "context_measurement_method": plan["default_method_decision"][
                        "context_measurement_method"
                    ],
                    "native_candidate_status": plan["default_method_decision"]["candidate_status"],
                    "cost_comparison_complete": plan["default_method_decision"][
                        "cost_comparison_complete"
                    ],
                    "action_discipline_appropriateness_complete": plan["default_method_decision"][
                        "action_discipline_appropriateness_complete"
                    ],
                    "video_rendered": bool(plan["video_rendered"]),
                },
                resume="must",
                save_code=False,
                dir=temporary,
                settings=settings,
            )
            try:
                if (
                    _safe_https_url(getattr(run, "url", ""), label="W&B run URL")
                    != projection["run_url"]
                ):
                    raise RuntimeError("publisher did not resume the exact campaign W&B run")
                for table_name, table in plan["tables"].items():
                    run.log(
                        {table_name: self.wandb.Table(columns=table["columns"], data=table["rows"])}
                    )
                if plan["video_rendered"]:
                    manifest = ReviewBriefAnimationMediaManifest.model_validate(
                        plan["validated_media"]
                    )
                    checkpoint_paths = [
                        str(plan["_artifact_local_paths"][f"animation/{item.logical_name}"])
                        for item in manifest.files
                        if item.role == "CHECKPOINT"
                    ]
                    run.log(
                        {
                            "review_brief/animation": self.wandb.Video(
                                _media_path_for_role(plan, "VIDEO"),
                                fps=manifest.film_spec.fps,
                                format="mp4",
                            ),
                            "review_brief/poster": self.wandb.Image(
                                _media_path_for_role(plan, "POSTER"),
                                caption=f"Campaign {plan['campaign_sha256'][:16]}",
                            ),
                            "review_brief/contact_sheet": self.wandb.Image(
                                _media_path_for_role(plan, "CONTACT_SHEET"),
                                caption="Seven claim-ledger-bound scene checkpoints",
                            ),
                            "review_brief/checkpoints": [
                                self.wandb.Image(path) for path in checkpoint_paths
                            ],
                        }
                    )
                scorecard = plan["tables"]["review_brief_scorecards"]
                indexes = {name: index for index, name in enumerate(scorecard["columns"])}
                for method_index, row in enumerate(scorecard["rows"]):
                    method_id = str(row[indexes["method_id"]])
                    passed = int(row[indexes["passed_case_count"]])
                    run.log(
                        {
                            "review_brief/method_index": method_index,
                            "review_brief/cases_passed": passed,
                        }
                    )
                    run.summary[f"review_brief/{method_id}/passed_case_count"] = passed
                    run.summary[f"review_brief/{method_id}/case_count"] = int(
                        row[indexes["case_count"]]
                    )
                    run.summary[f"review_brief/{method_id}/deterministic_gate_passed"] = int(
                        bool(row[indexes["deterministic_gate_passed"]])
                    )
                artifact = self.wandb.Artifact(
                    projection["artifact_name"],
                    type=projection["artifact_type"],
                    metadata={
                        "schema_version": plan["schema_version"],
                        "campaign_sha256": plan["campaign_sha256"],
                        "dataset_sha256": plan["dataset_sha256"],
                        "artifact_manifest_sha256": plan["artifact_manifest_sha256"],
                        "source_commit": plan["source_commit"],
                        "campaign_wandb_run_id": plan["campaign_wandb_run_id"],
                        "provider_backed": True,
                        "provider_cost_basis_sha256": plan["provider_cost_basis"]["basis_sha256"],
                        "video_rendered": bool(plan["video_rendered"]),
                        "video_logged": bool(plan["video_rendered"]),
                    },
                )
                for item in plan["artifact_files"]:
                    name = str(item["logical_name"])
                    if name in plan["_artifact_payloads"]:
                        content = str(plan["_artifact_payloads"][name])
                        with artifact.new_file(name, mode="w", encoding="utf-8") as stream:
                            stream.write(content)
                    else:
                        artifact.add_file(plan["_artifact_local_paths"][name], name=name)
                logged = run.log_artifact(artifact).wait()
                artifact_ref = str(getattr(logged, "qualified_name", "") or "")
                if not re.fullmatch(
                    rf"{re.escape(self.entity)}/{re.escape(self.project)}/[^:\s]+:v[0-9]+",
                    artifact_ref,
                ):
                    raise RuntimeError("W&B did not return an immutable campaign artifact version")
                run_url = projection["run_url"]
                artifact_url = _require_web_origin(
                    getattr(logged, "url", ""),
                    web_base_url=self.web_base_url,
                    label="W&B artifact URL",
                )
                run.summary["review_brief/status"] = "COMPLETE"
                run.summary["review_brief/provider_conversation_count"] = plan[
                    "provider_conversation_count"
                ]
                run.summary["review_brief/pre_provider_block_count"] = plan[
                    "pre_provider_block_count"
                ]
                run.summary["review_brief/default_method_decision"] = plan[
                    "default_method_decision"
                ]["decision"]
                run.summary["review_brief/native_candidate_status"] = plan[
                    "default_method_decision"
                ]["candidate_status"]
                run.summary["review_brief/cost_comparison_complete"] = int(
                    bool(plan["default_method_decision"]["cost_comparison_complete"])
                )
                run.summary["review_brief/action_discipline_appropriateness_complete"] = int(
                    bool(
                        plan["default_method_decision"][
                            "action_discipline_appropriateness_complete"
                        ]
                    )
                )
                run.summary["review_brief/artifact_ref"] = artifact_ref
                run.summary["review_brief/artifact_manifest_sha256"] = plan[
                    "artifact_manifest_sha256"
                ]
                run.summary["review_brief/video_rendered"] = int(bool(plan["video_rendered"]))
                run.summary["review_brief/video_logged"] = int(bool(plan["video_rendered"]))
                if plan["video_rendered"]:
                    run.summary["review_brief/media_manifest_sha256"] = plan["validated_media"][
                        "media_manifest_sha256"
                    ]
                run.finish(exit_code=0)
            except Exception:
                run.finish(exit_code=1)
                raise
        return ReviewBriefArtifactReceipt(
            campaign_sha256=plan["campaign_sha256"],
            dataset_sha256=plan["dataset_sha256"],
            run_id=projection["run_id"],
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=plan["artifact_manifest_sha256"],
            provider_readback_verified=False,
        )

    def verify_campaign_artifact(
        self,
        plan: Mapping[str, Any],
        receipt: ReviewBriefArtifactReceipt,
    ) -> bool:
        artifact = self.wandb.Api(overrides={"base_url": self.api_base_url}).artifact(
            receipt.artifact_ref
        )
        metadata = dict(getattr(artifact, "metadata", {}) or {})
        expected_metadata = {
            "campaign_sha256": plan["campaign_sha256"],
            "dataset_sha256": plan["dataset_sha256"],
            "artifact_manifest_sha256": plan["artifact_manifest_sha256"],
            "source_commit": plan["source_commit"],
            "campaign_wandb_run_id": plan["campaign_wandb_run_id"],
            "provider_backed": True,
            "provider_cost_basis_sha256": plan["provider_cost_basis"]["basis_sha256"],
            "video_rendered": bool(plan["video_rendered"]),
            "video_logged": bool(plan["video_rendered"]),
        }
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            return False
        with tempfile.TemporaryDirectory(prefix="ecocorridor-review-brief-readback-") as temporary:
            downloaded = Path(artifact.download(root=temporary)).resolve()
            expected_files = {str(item["logical_name"]): item for item in plan["artifact_files"]}
            observed: set[str] = set()
            for path in downloaded.rglob("*"):
                if path.is_dir():
                    continue
                relative = path.relative_to(downloaded).as_posix()
                item = expected_files.get(relative)
                if path.is_symlink() or item is None:
                    return False
                content = path.read_bytes()
                if (
                    len(content) != item["bytes"]
                    or hashlib.sha256(content).hexdigest() != item["sha256"]
                ):
                    return False
                observed.add(relative)
            return observed == set(expected_files)

    def upsert_owned_report_section(
        self,
        *,
        report_url: str,
        plan: Mapping[str, Any],
        receipt: ReviewBriefArtifactReceipt,
    ) -> str:
        target = _require_web_origin(
            report_url,
            web_base_url=self.web_base_url,
            label="existing W&B Report URL",
        )
        with _wandb_base_environment(self.api_base_url):
            report = self.reports.Report.from_url(target)
            if (
                str(getattr(report, "entity", "")) != self.entity
                or str(getattr(report, "project", "")) != self.project
            ):
                raise PermissionError("existing W&B Report belongs to another entity or project")
            owned = build_report_section_blocks(
                self.reports,
                plan,
                receipt,
                entity=self.entity,
                project=self.project,
            )
            report.blocks = append_owned_report_section(list(report.blocks), owned)
            report.save()
        return _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="updated W&B Report URL",
        )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--entity")
    parser.add_argument("--project")
    parser.add_argument("--wandb-api-base-url")
    parser.add_argument("--wandb-web-base-url")
    parser.add_argument("--report-url")
    parser.add_argument(
        "--media-manifest",
        type=Path,
        help="Validated post-campaign animation manifest; omitted means no media is logged.",
    )
    parser.add_argument("--authorize-synthetic-public-upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    commit, clean = _git_source(root)
    campaign = load_verified_campaign(arguments.campaign, repository_root=root)
    if campaign.source_commit != commit:
        raise SystemExit("campaign source commit must equal the checked-out repository HEAD")
    plan = build_review_brief_publication_plan(campaign, source_tree_clean=clean)
    if arguments.media_manifest is not None:
        plan = attach_validated_media_to_publication_plan(
            plan,
            campaign=campaign,
            media_manifest_path=arguments.media_manifest,
        )
    validate_review_brief_publication_plan(plan)
    if arguments.dry_run:
        public_plan = {
            key: value
            for key, value in plan.items()
            if key not in {"_artifact_payloads", "_artifact_local_paths"}
        }
        _write_json(arguments.output, public_plan)
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "plan_sha256": plan["plan_sha256"],
                    "default_method_decision": plan["default_method_decision"]["decision"],
                    "current_default_method": plan["default_method_decision"][
                        "current_default_method"
                    ],
                    "native_candidate_status": plan["default_method_decision"]["candidate_status"],
                    "video_rendered": bool(plan["video_rendered"]),
                },
                sort_keys=True,
            )
        )
        return 0
    if not all(
        (
            arguments.entity,
            arguments.project,
            arguments.wandb_api_base_url,
            arguments.wandb_web_base_url,
            arguments.report_url,
        )
    ):
        raise SystemExit(
            "online publication requires --entity, --project, --wandb-api-base-url, "
            "--wandb-web-base-url, and --report-url"
        )
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("online publication requires WANDB_API_KEY in process memory")
    adapter = WandbSdkReviewBriefAdapter(
        entity=arguments.entity,
        project=arguments.project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        authorize_synthetic_public_upload=arguments.authorize_synthetic_public_upload,
    )
    receipt = publish_and_update_review_brief_report(
        plan=plan,
        report_url=arguments.report_url,
        adapter=adapter,
    )
    _write_json(arguments.output, receipt)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "run_url": receipt["run_url"],
                "report_url": receipt["report_url"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
