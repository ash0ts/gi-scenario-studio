"""Hash-verified publication of the visual EcoCorridor field-review article.

The article revises one explicitly identified W&B Report. It does not edit the
historical technical Report or the previous reader article. Figures use committed
synthetic evidence, logged as article media (not experiment results), retained
in an immutable W&B Artifact, and rendered through native W&B media panels.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .human_readable_report import HumanReadableSourceSnapshot
from .usefulness_narrative import (
    _block_representation,
    _blocks_sha256,
    _require_web_origin,
    _semantic_blocks_sha256,
)

REPORT_TITLE = "Why I Built EcoCorridor"
REPORT_SCHEMA_VERSION = "ecocorridor-visual-story-report/v2"
RECEIPT_SCHEMA_VERSION = "ecocorridor-visual-story-report-receipt/v2"
ASSET_SCHEMA_VERSION = "ecocorridor-visual-story-assets/v1"
SOURCE_PROJECTION_RELATIVE_PATH = "benchmarks/ecocorridor/ridgeway/ui_projection.json"
PREVIOUS_READER_REPORT_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
    "A-Green-Model-Result-Is-Not-a-Green-Light--VmlldzoxNzcwMDM4Nw=="
)
PREVIOUS_READER_REPORT_TITLE = "A Green Model Result Is Not a Green Light"
TARGET_REPORT_ID = "VmlldzoxNzc4MjAxMw=="
TARGET_REPORT_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
    "The-Model-Passed.-We-Sent-a-Human-to-the-Road.--" + TARGET_REPORT_ID
)
EXPECTED_FIGURE_IDS = (
    "road-mask-current-maps",
    "evidence-rail-receipt",
    "evidence-handoff",
    "agent-promotion-gate",
)
# The three generated figures are 960×1600 portraits. W&B's 24-column
# grid needs tall panels to show these at readable width; the 720×662
# application screenshot keeps its compact panel. Heights are grid units.
FIGURE_PANEL_HEIGHTS = {
    "road-mask-current-maps": 36,
    "evidence-rail-receipt": 12,
    "evidence-handoff": 36,
    "agent-promotion-gate": 36,
}
ARTICLE_RELATIVE_PATH = "docs/ecocorridor/articles/the-model-passed-we-sent-a-human-to-the-road.md"
MANIFEST_RELATIVE_PATH = "docs/ecocorridor/article-assets/the-model-passed/manifest.json"
REPORT_MARKER_PREFIX = "<!-- ecocorridor-visual-story-report:"
REPORT_READBACK_TIMEOUT_SECONDS = 20.0
REPORT_READBACK_INTERVAL_SECONDS = 0.5
MAX_ARTICLE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
FIGURE_BLOCK = re.compile(
    r"<!-- ECOVIS:(?P<figure_id>[a-z0-9-]+) -->\n"
    r"!\[(?P<alt_text>[^\]]+)\]\((?P<relative_path>[^)]+)\)\n\n"
    r"_(?P<caption>[^\n]+)_\n"
    r"<!-- /ECOVIS -->",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_relative_path(value: str, *, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("article asset path must be repository relative")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("article asset escapes the repository") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("article asset is missing or symlinked")
    return resolved


class ProtectedReportSnapshot(ContractModel):
    """Read-only identity used to prove an existing Report was not edited."""

    report_url: str
    report_id: str = Field(min_length=1)
    report_title: str = Field(min_length=1)
    report_description: str = ""
    entity: Literal["wandb"] = "wandb"
    project: Literal["ecocorridor-scenario-studio"] = "ecocorridor-scenario-studio"
    blocks_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    block_count: int = Field(ge=1)

    @classmethod
    def from_report(
        cls,
        report: Any,
        *,
        web_base_url: str,
        label: str,
    ) -> ProtectedReportSnapshot:
        url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=web_base_url,
            label=label,
        )
        report_id = str(getattr(report, "id", "") or "")
        title = str(getattr(report, "title", "") or "")
        entity = str(getattr(report, "entity", "") or "")
        project = str(getattr(report, "project", "") or "")
        blocks = list(getattr(report, "blocks", []))
        return cls(
            report_url=url,
            report_id=report_id,
            report_title=title,
            report_description=str(getattr(report, "description", "") or ""),
            entity=entity,
            project=project,
            blocks_sha256=_blocks_sha256(blocks),
            block_count=len(blocks),
        )


class ArticleRevisionSnapshot(ProtectedReportSnapshot):
    """Lossless prior body and readable prose frozen before a same-ID revision."""

    report_id: Literal["VmlldzoxNzc4MjAxMw=="] = TARGET_REPORT_ID
    blocks: tuple[dict[str, Any], ...] = Field(min_length=1)
    previous_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_previous_body(self) -> Self:
        if canonical_sha256(list(self.blocks)) != self.blocks_sha256:
            raise ValueError("previous Report body digest differs")
        if len(self.blocks) != self.block_count:
            raise ValueError("previous Report block count differs")
        if not self.report_url.endswith("--" + TARGET_REPORT_ID):
            raise ValueError("article revision target URL names another Report")
        return self

    @classmethod
    def from_report(cls, report: Any, *, web_base_url: str, label: str) -> ArticleRevisionSnapshot:
        protected = ProtectedReportSnapshot.from_report(
            report, web_base_url=web_base_url, label=label
        )
        blocks = list(getattr(report, "blocks", []))
        prose = [f"# {protected.report_title}", protected.report_description]
        for block in blocks:
            value = getattr(block, "text", None)
            if isinstance(value, str) and value:
                prefix = {"H1": "# ", "H2": "## ", "H3": "### "}.get(type(block).__name__, "")
                prose.append(prefix + value)
        return cls(
            **protected.model_dump(mode="json"),
            blocks=tuple(_block_representation(block) for block in blocks),
            previous_text="\n\n".join(part for part in prose if part) + "\n",
        )


class VisualStoryArtifactFile(ContractModel):
    logical_name: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    repository_path: str = Field(pattern=r"^[A-Za-z0-9._/-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    media_type: Literal["text/markdown", "application/json", "image/png", "image/svg+xml"]


class VisualStoryFigure(ContractModel):
    figure_id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=3)
    question: str = Field(min_length=3)
    takeaway: str = Field(min_length=3)
    alt_text: str = Field(min_length=20)
    caption: str = Field(min_length=20)
    forbidden_implication: str = Field(min_length=3)
    png_path: str = Field(pattern=r"^[A-Za-z0-9._/-]+\.png$")
    png_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    svg_path: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/-]+\.svg$")
    svg_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    values: dict[str, Any]
    source_locations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_svg_pair(self) -> Self:
        if (self.svg_path is None) != (self.svg_sha256 is None):
            raise ValueError("figure SVG path and digest must appear together")
        return self


class VisualStoryAssets(ContractModel):
    schema_version: Literal["ecocorridor-visual-story-assets/v1"] = ASSET_SCHEMA_VERSION
    title: Literal["Why I Built EcoCorridor"] = REPORT_TITLE
    article_relative_path: Literal[
        "docs/ecocorridor/articles/the-model-passed-we-sent-a-human-to-the-road.md"
    ] = ARTICLE_RELATIVE_PATH
    article_markdown: str = Field(min_length=1, max_length=MAX_ARTICLE_BYTES)
    article_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_manifest_relative_path: Literal[
        "docs/ecocorridor/article-assets/the-model-passed/manifest.json"
    ] = MANIFEST_RELATIVE_PATH
    visual_manifest_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_manifest_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_projection_relative_path: Literal[
        "benchmarks/ecocorridor/ridgeway/ui_projection.json"
    ] = SOURCE_PROJECTION_RELATIVE_PATH
    source_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    figures: tuple[VisualStoryFigure, ...] = Field(min_length=4, max_length=4)
    artifact_files: tuple[VisualStoryArtifactFile, ...] = Field(min_length=6)
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assets_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "article_relative_path": self.article_relative_path,
            "article_sha256": self.article_sha256,
            "visual_manifest_relative_path": self.visual_manifest_relative_path,
            "visual_manifest_file_sha256": self.visual_manifest_file_sha256,
            "visual_manifest_claim_sha256": self.visual_manifest_claim_sha256,
            "source_projection_relative_path": self.source_projection_relative_path,
            "source_projection_sha256": self.source_projection_sha256,
            "figure_ids": [item.figure_id for item in self.figures],
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
        }

    @model_validator(mode="after")
    def validate_assets(self) -> Self:
        if len({item.figure_id for item in self.figures}) != len(self.figures):
            raise ValueError("visual-story figure IDs must be unique")
        if tuple(item.figure_id for item in self.figures) != EXPECTED_FIGURE_IDS:
            raise ValueError("visual-story figure set or order differs")
        if len({item.logical_name for item in self.artifact_files}) != len(self.artifact_files):
            raise ValueError("visual-story artifact logical names must be unique")
        expected_manifest = canonical_sha256(
            [item.model_dump(mode="json") for item in self.artifact_files]
        )
        if self.artifact_manifest_sha256 != expected_manifest:
            raise ValueError("visual-story artifact manifest digest differs")
        expected = canonical_sha256(self.identity_payload())
        if self.assets_sha256 != expected:
            raise ValueError("visual-story assets digest differs")
        return self


class VisualStoryReportPlan(ContractModel):
    schema_version: Literal["ecocorridor-visual-story-report/v2"] = REPORT_SCHEMA_VERSION
    entity: Literal["wandb"] = "wandb"
    project: Literal["ecocorridor-scenario-studio"] = "ecocorridor-scenario-studio"
    source: HumanReadableSourceSnapshot
    protected_reports: tuple[ProtectedReportSnapshot, ...] = Field(min_length=1)
    target_report: ArticleRevisionSnapshot
    assets: VisualStoryAssets
    publication_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^eco-article-[0-9a-f]{12}$")
    artifact_name: str = Field(pattern=r"^ecocorridor-article-[0-9a-f]{12}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.source.source_commit != self.source.source_commit.lower():
            raise ValueError("source commit must be lowercase")
        if self.target_report.report_id in {item.report_id for item in self.protected_reports}:
            raise ValueError("article revision target cannot be a protected Report")
        if len({item.report_url for item in self.protected_reports}) != len(self.protected_reports):
            raise ValueError("protected Report URLs must be unique")
        previous = [
            item for item in self.protected_reports if item.report_url == PREVIOUS_READER_REPORT_URL
        ]
        if len(previous) != 1 or previous[0].report_title != PREVIOUS_READER_REPORT_TITLE:
            raise ValueError("the previous reader Report is not protected exactly")
        key = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "source_snapshot_sha256": self.source.snapshot_sha256,
                "protected_reports": [
                    item.model_dump(mode="json") for item in self.protected_reports
                ],
                "assets_sha256": self.assets.assets_sha256,
                "target_report": self.target_report.model_dump(mode="json"),
            }
        )
        if self.publication_key != key:
            raise ValueError("visual-story publication key differs")
        if self.run_id != f"eco-article-{key[:12]}":
            raise ValueError("visual-story run ID differs")
        if self.artifact_name != f"ecocorridor-article-{key[:12]}":
            raise ValueError("visual-story artifact name differs")
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"plan_sha256"}))
        if self.plan_sha256 != expected:
            raise ValueError("visual-story plan digest differs")
        return self

    @classmethod
    def create(
        cls,
        *,
        source: HumanReadableSourceSnapshot,
        protected_reports: Sequence[ProtectedReportSnapshot],
        target_report: ArticleRevisionSnapshot,
        assets: VisualStoryAssets,
    ) -> VisualStoryReportPlan:
        key = canonical_sha256(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "source_snapshot_sha256": source.snapshot_sha256,
                "protected_reports": [item.model_dump(mode="json") for item in protected_reports],
                "assets_sha256": assets.assets_sha256,
                "target_report": target_report.model_dump(mode="json"),
            }
        )
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "entity": "wandb",
            "project": "ecocorridor-scenario-studio",
            "source": source.model_dump(mode="json"),
            "protected_reports": [item.model_dump(mode="json") for item in protected_reports],
            "target_report": target_report.model_dump(mode="json"),
            "assets": assets.model_dump(mode="json"),
            "publication_key": key,
            "run_id": f"eco-article-{key[:12]}",
            "artifact_name": f"ecocorridor-article-{key[:12]}",
        }
        return cls(**payload, plan_sha256=canonical_sha256(payload))


class VisualStoryMediaReceipt(ContractModel):
    status: Literal["CREATED", "REUSED"]
    run_id: str
    run_url: str
    artifact_ref: str
    artifact_url: str
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisualStoryReportReceipt(ContractModel):
    schema_version: Literal["ecocorridor-visual-story-report-receipt/v2"] = RECEIPT_SCHEMA_VERSION
    status: Literal["UPDATED", "REUSED"]
    media_status: Literal["CREATED", "REUSED"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    report_url: str
    report_id: str
    report_title: Literal["Why I Built EcoCorridor"] = REPORT_TITLE
    run_url: str
    artifact_ref: str
    artifact_url: str
    protected_reports_preserved: Literal[True] = True
    report_readback_verified: Literal[True] = True
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if self.receipt_sha256 != expected:
            raise ValueError("visual-story Report receipt digest differs")
        return self

    @classmethod
    def create(cls, **values: Any) -> VisualStoryReportReceipt:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "report_title": REPORT_TITLE,
            "protected_reports_preserved": True,
            "report_readback_verified": True,
            **values,
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


def _artifact_file(
    root: Path,
    *,
    repository_path: str,
    logical_name: str,
    media_type: Literal["text/markdown", "application/json", "image/png", "image/svg+xml"],
) -> VisualStoryArtifactFile:
    source = _safe_relative_path(repository_path, root=root)
    return VisualStoryArtifactFile(
        logical_name=logical_name,
        repository_path=repository_path,
        sha256=_sha256(source),
        bytes=source.stat().st_size,
        media_type=media_type,
    )


def load_visual_story_assets(root: Path) -> VisualStoryAssets:
    """Read and cross-check article prose, figure manifest, and every asset byte."""

    root = root.resolve()
    article_path = _safe_relative_path(ARTICLE_RELATIVE_PATH, root=root)
    manifest_path = _safe_relative_path(MANIFEST_RELATIVE_PATH, root=root)
    if article_path.stat().st_size > MAX_ARTICLE_BYTES:
        raise ValueError("visual-story article is oversized")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("visual-story manifest is oversized")
    article_markdown = article_path.read_text(encoding="utf-8")
    if not article_markdown.startswith(f"# {REPORT_TITLE}\n"):
        raise ValueError("visual-story article title differs")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    claimed_digest = str(manifest.get("manifest_sha256", ""))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if canonical_sha256(unsigned) != claimed_digest:
        raise ValueError("visual-story manifest self-digest differs")
    if manifest.get("article_title") != REPORT_TITLE or manifest.get("license") != "CC0-1.0":
        raise ValueError("visual-story manifest title or license differs")
    if manifest.get("source_projection_path") != SOURCE_PROJECTION_RELATIVE_PATH:
        raise ValueError("visual-story source projection path differs")
    projection = _safe_relative_path(SOURCE_PROJECTION_RELATIVE_PATH, root=root)
    if _sha256(projection) != manifest.get("source_projection_sha256"):
        raise ValueError("visual-story source projection digest differs")

    raw_figures = list(manifest.get("figures", []))
    figures = tuple(VisualStoryFigure.model_validate(item) for item in raw_figures)
    figure_by_id = {item.figure_id: item for item in figures}
    matches = list(FIGURE_BLOCK.finditer(article_markdown))
    marker_ids = [match.group("figure_id") for match in matches]
    if marker_ids != [item.figure_id for item in figures]:
        raise ValueError("article figure order differs from the visual manifest")
    if article_markdown.count("<!-- ECOVIS:") != len(matches):
        raise ValueError("article contains a malformed visual marker")

    for match in matches:
        figure = figure_by_id[match.group("figure_id")]
        expected_article_path = (
            root / Path(ARTICLE_RELATIVE_PATH).parent / match.group("relative_path")
        ).resolve()
        try:
            normalized = expected_article_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"article image path escapes for {figure.figure_id}") from exc
        if normalized != figure.png_path:
            raise ValueError(f"article image path differs for {figure.figure_id}")
        if match.group("alt_text") != figure.alt_text:
            raise ValueError(f"article alt text differs for {figure.figure_id}")
        if match.group("caption") != figure.caption:
            raise ValueError(f"article caption differs for {figure.figure_id}")
        png = _safe_relative_path(figure.png_path, root=root)
        if _sha256(png) != figure.png_sha256:
            raise ValueError(f"PNG digest differs for {figure.figure_id}")
        if figure.svg_path is not None:
            svg = _safe_relative_path(figure.svg_path, root=root)
            if _sha256(svg) != figure.svg_sha256:
                raise ValueError(f"SVG digest differs for {figure.figure_id}")

    artifact_files: list[VisualStoryArtifactFile] = [
        _artifact_file(
            root,
            repository_path=ARTICLE_RELATIVE_PATH,
            logical_name="article/article.md",
            media_type="text/markdown",
        ),
        _artifact_file(
            root,
            repository_path=MANIFEST_RELATIVE_PATH,
            logical_name="article/visual-manifest.json",
            media_type="application/json",
        ),
    ]
    for figure in figures:
        artifact_files.append(
            _artifact_file(
                root,
                repository_path=figure.png_path,
                logical_name=f"figures/{Path(figure.png_path).name}",
                media_type="image/png",
            )
        )
        if figure.svg_path is not None:
            artifact_files.append(
                _artifact_file(
                    root,
                    repository_path=figure.svg_path,
                    logical_name=f"figures/{Path(figure.svg_path).name}",
                    media_type="image/svg+xml",
                )
            )
    ordered_files = tuple(sorted(artifact_files, key=lambda item: item.logical_name))
    payload = {
        "schema_version": ASSET_SCHEMA_VERSION,
        "title": REPORT_TITLE,
        "article_relative_path": ARTICLE_RELATIVE_PATH,
        "article_markdown": article_markdown,
        "article_sha256": _sha256(article_path),
        "visual_manifest_relative_path": MANIFEST_RELATIVE_PATH,
        "visual_manifest_file_sha256": _sha256(manifest_path),
        "visual_manifest_claim_sha256": claimed_digest,
        "source_projection_relative_path": SOURCE_PROJECTION_RELATIVE_PATH,
        "source_projection_sha256": str(manifest["source_projection_sha256"]),
        "figures": figures,
        "artifact_files": ordered_files,
        "artifact_manifest_sha256": canonical_sha256(
            [item.model_dump(mode="json") for item in ordered_files]
        ),
    }
    unvalidated = VisualStoryAssets.model_construct(
        **payload,
        assets_sha256="0" * 64,
    )
    assets_digest = canonical_sha256(unvalidated.identity_payload())
    return VisualStoryAssets(**payload, assets_sha256=assets_digest)


def validate_plan_bytes(payload: bytes) -> VisualStoryReportPlan:
    plan = VisualStoryReportPlan.model_validate_json(payload)
    if payload != canonical_plan_bytes(plan):
        raise ValueError("visual-story Report plan bytes are noncanonical")
    return plan


def canonical_plan_bytes(plan: VisualStoryReportPlan) -> bytes:
    return (
        json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )


def revision_artifact_payloads(plan: VisualStoryReportPlan) -> dict[str, bytes]:
    """Retain the exact pre-edit snapshot, including all original prose blocks."""

    return {
        "previous/previous-report.json": (
            json.dumps(plan.target_report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "previous/previous-text.md": plan.target_report.previous_text.encode("utf-8"),
    }


def publication_artifact_manifest_sha256(plan: VisualStoryReportPlan) -> str:
    files = [item.model_dump(mode="json") for item in plan.assets.artifact_files]
    files.extend(
        {
            "logical_name": name,
            "generated_from": "frozen_target_report",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
        for name, payload in revision_artifact_payloads(plan).items()
    )
    return canonical_sha256(sorted(files, key=lambda item: item["logical_name"]))


def report_marker(plan: VisualStoryReportPlan, media: VisualStoryMediaReceipt) -> dict[str, str]:
    return {
        "schema_version": plan.schema_version,
        "publication_key": plan.publication_key,
        "plan_sha256": plan.plan_sha256,
        "article_sha256": plan.assets.article_sha256,
        "visual_manifest_sha256": plan.assets.visual_manifest_file_sha256,
        "run_id": media.run_id,
        "artifact_ref": media.artifact_ref,
    }


def report_description(plan: VisualStoryReportPlan, media: VisualStoryMediaReceipt) -> str:
    encoded = json.dumps(report_marker(plan, media), sort_keys=True, separators=(",", ":"))
    return (
        "A source-pinned, visual interpretation of synthetic EcoCorridor evidence and one "
        "historical agent campaign. This editorial revision creates no new experiment result; "
        "the previous article is retained in its immutable publication artifact.\n\n"
        f"{REPORT_MARKER_PREFIX}{encoded} -->"
    )


def parse_report_marker(value: object) -> dict[str, str] | None:
    description = str(value or "")
    if REPORT_MARKER_PREFIX not in description:
        return None
    encoded = description.rsplit(REPORT_MARKER_PREFIX, 1)[1].split(" -->", 1)[0]
    try:
        marker = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    fields = {
        "schema_version",
        "publication_key",
        "plan_sha256",
        "article_sha256",
        "visual_manifest_sha256",
        "run_id",
        "artifact_ref",
    }
    if not isinstance(marker, dict) or set(marker) != fields:
        return None
    if not all(isinstance(item, str) for item in marker.values()):
        return None
    return marker


def _prose_blocks(reports: Any, markdown: str) -> list[Any]:
    """Keep GFM tables as their own native Markdown blocks, never image panels."""

    table = re.compile(r"(?m)^\|[^\n]+\|\n\|[ :|\-]+\|\n(?:\|[^\n]+\|(?:\n|$))+")
    blocks: list[Any] = []
    cursor = 0
    for match in table.finditer(markdown):
        prefix = markdown[cursor : match.start()].strip()
        if prefix:
            blocks.append(reports.MarkdownBlock(text=prefix))
        blocks.append(reports.MarkdownBlock(text=match.group().strip()))
        cursor = match.end()
    tail = markdown[cursor:].strip()
    if tail:
        blocks.append(reports.MarkdownBlock(text=tail))
    return blocks


def _media_panel_dimensions(blocks: Sequence[Any]) -> list[tuple[int, int]]:
    dimensions = []
    for block in blocks:
        if type(block).__name__ != "PanelGrid":
            continue
        for panel in _block_representation(block)["value"]["panels"]:
            dimensions.append((panel["layout"]["w"], panel["layout"]["h"]))
    return dimensions


def render_report_blocks(
    reports: Any,
    plan: VisualStoryReportPlan,
    media: VisualStoryMediaReceipt,
) -> list[Any]:
    """Interleave prose with byte-verified images from the article's W&B run."""

    markdown = plan.assets.article_markdown
    title_line = f"# {REPORT_TITLE}\n"
    if not markdown.startswith(title_line):
        raise ValueError("visual-story article title differs")
    markdown = markdown[len(title_line) :]
    figures = {item.figure_id: item for item in plan.assets.figures}
    blocks: list[Any] = [reports.H1(REPORT_TITLE)]
    cursor = 0
    for match in FIGURE_BLOCK.finditer(markdown):
        prose = markdown[cursor : match.start()].strip()
        if prose:
            blocks.extend(_prose_blocks(reports, prose))
        figure = figures[match.group("figure_id")]
        runset = reports.Runset(
            entity=plan.entity,
            project=plan.project,
            name=f"Verified article media · {figure.figure_id}",
            filters=(
                f"ID = '{media.run_id}' and "
                f"Config('plan_sha256') = '{plan.plan_sha256}' and "
                f"Config('article_sha256') = '{plan.assets.article_sha256}'"
            ),
        )
        blocks.append(
            reports.PanelGrid(
                runsets=[runset],
                hide_run_sets=True,
                panels=[
                    reports.MediaBrowser(
                        title=figure.title,
                        num_columns=1,
                        media_keys=[f"article/figures/{figure.figure_id}"],
                        mode="gallery",
                        gallery_axis="index",
                        layout=reports.Layout(w=24, h=FIGURE_PANEL_HEIGHTS[figure.figure_id]),
                    )
                ],
            )
        )
        blocks.append(
            reports.MarkdownBlock(
                text=(f"_{figure.caption}_\n\n**Accessible description:** {figure.alt_text}")
            )
        )
        cursor = match.end()
    tail = markdown[cursor:].strip()
    if tail:
        blocks.extend(_prose_blocks(reports, tail))
    blocks.extend(
        [
            reports.H2("Inspect the publication record"),
            reports.MarkdownBlock(
                text=(
                    f"[Article media run]({media.run_url}) · "
                    f"[immutable article artifact]({media.artifact_url})\n\n"
                    f"Source commit `{plan.source.source_commit}` · article "
                    f"`{plan.assets.article_sha256}` · visual manifest "
                    f"`{plan.assets.visual_manifest_file_sha256}` · artifact file manifest "
                    f"`{publication_artifact_manifest_sha256(plan)}`\n\n"
                    "This publication run labels `new_experiment_result=false`. It packages the "
                    "article and derived figures; it does not add a scientific result."
                )
            ),
        ]
    )
    return blocks


class VerifiedVisualStoryReportPublisher:
    """Revise only the frozen Report ID after exact source/media readback.

    W&B Reports save has no compare-and-swap argument. Two bounded rereads catch
    observed edits, but cannot eliminate an edit concurrent with the final save.
    A failed/uncertain save is safe to retry: the exact revised body is reused.
    """

    def __init__(
        self,
        *,
        api: Any,
        reports_module: Any,
        web_base_url: str,
        authorize_report_revision: bool,
        source_reader: Callable[[], VisualStoryReportPlan],
        media_publisher: Callable[[VisualStoryReportPlan], VisualStoryMediaReceipt],
        figure_verifier: Callable[[VisualStoryReportPlan], None],
    ) -> None:
        if not authorize_report_revision:
            raise PermissionError("visual-story Report revision requires explicit authorization")
        self.api = api
        self.reports = reports_module
        self.web_base_url = web_base_url.rstrip("/")
        self.source_reader = source_reader
        self.media_publisher = media_publisher
        self.figure_verifier = figure_verifier

    def _verify_protected_reports(self, plan: VisualStoryReportPlan) -> None:
        for expected in plan.protected_reports:
            observed = ProtectedReportSnapshot.from_report(
                self.reports.Report.from_url(expected.report_url),
                web_base_url=self.web_base_url,
                label="protected W&B Report URL",
            )
            if observed != expected:
                raise RuntimeError("a protected W&B Report changed during article publication")

    def _read_target(self, plan: VisualStoryReportPlan) -> Any:
        report = self.reports.Report.from_url(plan.target_report.report_url)
        if (
            str(getattr(report, "id", "")) != TARGET_REPORT_ID
            or str(getattr(report, "entity", "")) != plan.entity
            or str(getattr(report, "project", "")) != plan.project
        ):
            raise PermissionError("article revision target identity differs")
        return report

    def _check_target(
        self, plan: VisualStoryReportPlan, report: Any
    ) -> Literal["ORIGINAL", "REVISED"]:
        marker = parse_report_marker(getattr(report, "description", ""))
        if marker is not None and marker.get("plan_sha256") == plan.plan_sha256:
            match = re.fullmatch(
                rf"{plan.entity}/{plan.project}/{re.escape(plan.artifact_name)}:(v[0-9]+)",
                marker.get("artifact_ref", ""),
            )
            if match is None:
                raise RuntimeError("revised Report artifact identity differs")
            media = VisualStoryMediaReceipt(
                status="REUSED",
                run_id=plan.run_id,
                run_url=f"{self.web_base_url}/{plan.entity}/{plan.project}/runs/{plan.run_id}",
                artifact_ref=marker["artifact_ref"],
                artifact_url=(
                    f"{self.web_base_url}/{plan.entity}/{plan.project}/artifacts/"
                    f"article-publication/{plan.artifact_name}/{match.group(1)}"
                ),
                artifact_manifest_sha256=publication_artifact_manifest_sha256(plan),
            )
            self._verify_destination(report, plan=plan, media=media)
            return "REVISED"
        observed = ArticleRevisionSnapshot.from_report(
            report, web_base_url=self.web_base_url, label="article revision target"
        )
        if observed != plan.target_report:
            raise RuntimeError("target W&B Report changed since the frozen revision snapshot")
        return "ORIGINAL"

    def _verify_destination(
        self,
        report: Any,
        *,
        plan: VisualStoryReportPlan,
        media: VisualStoryMediaReceipt,
    ) -> tuple[str, str]:
        if (
            str(getattr(report, "entity", "")) != plan.entity
            or str(getattr(report, "project", "")) != plan.project
            or str(getattr(report, "title", "")) != REPORT_TITLE
            or str(getattr(report, "description", "")) != report_description(plan, media)
        ):
            raise RuntimeError("visual-story Report identity differs")
        url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="visual-story W&B Report URL",
        )
        report_id = str(getattr(report, "id", "") or "")
        if report_id != TARGET_REPORT_ID or not url.endswith("--" + TARGET_REPORT_ID):
            raise RuntimeError("visual-story W&B Report changed its stable ID")
        if url in {item.report_url for item in plan.protected_reports}:
            raise RuntimeError("visual-story destination reused a protected Report")
        if report_id in {item.report_id for item in plan.protected_reports}:
            raise RuntimeError("visual-story destination reused a protected Report ID")
        expected_blocks = render_report_blocks(self.reports, plan, media)
        if _semantic_blocks_sha256(list(report.blocks)) != _semantic_blocks_sha256(expected_blocks):
            raise RuntimeError("visual-story W&B Report body differs")
        # General Report canonicalization ignores backend layout placement. For
        # these article figures, width/height are also part of readable delivery.
        if _media_panel_dimensions(report.blocks) != _media_panel_dimensions(expected_blocks):
            raise RuntimeError("visual-story W&B media panel dimensions differ")
        return url, report_id

    def publish(self, frozen_plan: VisualStoryReportPlan) -> VisualStoryReportReceipt:
        first = self.source_reader()
        if first != frozen_plan:
            raise RuntimeError("verified source differs from the frozen visual-story plan")
        self._verify_protected_reports(frozen_plan)
        self._check_target(frozen_plan, self._read_target(frozen_plan))
        self.figure_verifier(frozen_plan)
        media = self.media_publisher(frozen_plan)
        report = self._read_target(frozen_plan)
        target_status = self._check_target(frozen_plan, report)
        status: Literal["UPDATED", "REUSED"]
        if target_status == "ORIGINAL":
            second = self.source_reader()
            if second != frozen_plan:
                raise RuntimeError("verified source changed before visual-story Report revision")
            self._verify_protected_reports(frozen_plan)
            blocks = render_report_blocks(self.reports, frozen_plan, media)
            # Last network operation before save: refuse an observed manual edit.
            report = self._read_target(frozen_plan)
            if self._check_target(frozen_plan, report) != "ORIGINAL":
                raise RuntimeError("target W&B Report changed immediately before revision")
            report.title = REPORT_TITLE
            report.description = report_description(frozen_plan, media)
            report.blocks = blocks
            report.width = "readable"
            report.save()
            status = "UPDATED"
        else:
            self._verify_destination(report, plan=frozen_plan, media=media)
            status = "REUSED"

        url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="revised visual-story W&B Report URL",
        )
        deadline = time.monotonic() + REPORT_READBACK_TIMEOUT_SECONDS
        while True:
            try:
                readback = self.reports.Report.from_url(url)
                verified_url, report_id = self._verify_destination(
                    readback,
                    plan=frozen_plan,
                    media=media,
                )
                self._verify_protected_reports(frozen_plan)
                return VisualStoryReportReceipt.create(
                    status=status,
                    media_status=media.status,
                    plan_sha256=frozen_plan.plan_sha256,
                    publication_key=frozen_plan.publication_key,
                    source_commit=frozen_plan.source.source_commit,
                    report_url=verified_url,
                    report_id=report_id,
                    run_url=media.run_url,
                    artifact_ref=media.artifact_ref,
                    artifact_url=media.artifact_url,
                )
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry avoids remote-content leakage
                if time.monotonic() >= deadline:
                    raise RuntimeError("visual-story W&B Report failed exact readback") from None
                time.sleep(REPORT_READBACK_INTERVAL_SECONDS)


def expected_run_config(plan: VisualStoryReportPlan) -> dict[str, Any]:
    return {
        "run_kind": "article_publication",
        "schema_version": plan.schema_version,
        "plan_sha256": plan.plan_sha256,
        "publication_key": plan.publication_key,
        "article_sha256": plan.assets.article_sha256,
        "visual_manifest_sha256": plan.assets.visual_manifest_file_sha256,
        "artifact_manifest_sha256": publication_artifact_manifest_sha256(plan),
        "source_repository": "ash0ts/gi-scenario-studio",
        "source_commit": plan.source.source_commit,
        "figure_count": len(plan.assets.figures),
        "revises_report_id": plan.target_report.report_id,
        "previous_blocks_sha256": plan.target_report.blocks_sha256,
        "new_experiment_result": False,
        "cc0_synthetic_only": True,
    }


def expected_artifact_metadata(plan: VisualStoryReportPlan) -> dict[str, Any]:
    return {
        "schema_version": plan.schema_version,
        "plan_sha256": plan.plan_sha256,
        "publication_key": plan.publication_key,
        "article_sha256": plan.assets.article_sha256,
        "visual_manifest_sha256": plan.assets.visual_manifest_file_sha256,
        "artifact_manifest_sha256": publication_artifact_manifest_sha256(plan),
        "source_commit": plan.source.source_commit,
        "revises_report_id": plan.target_report.report_id,
        "previous_blocks_sha256": plan.target_report.blocks_sha256,
        "new_experiment_result": False,
        "cc0_synthetic_only": True,
    }


def validate_artifact_download(
    plan: VisualStoryReportPlan,
    *,
    root: Path,
    downloaded_root: Path,
) -> None:
    previous = revision_artifact_payloads(plan)
    expected_paths = {item.logical_name for item in plan.assets.artifact_files} | set(previous)
    observed_paths: set[str] = set()
    for candidate in downloaded_root.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError("article artifact download contains a symlink")
        if candidate.is_file():
            observed_paths.add(candidate.relative_to(downloaded_root).as_posix())
    if observed_paths != expected_paths:
        raise RuntimeError("article artifact download file set differs")
    for name, payload in previous.items():
        if (downloaded_root / name).read_bytes() != payload:
            raise RuntimeError("previous Report artifact snapshot digest differs")
    for item in plan.assets.artifact_files:
        downloaded = downloaded_root / item.logical_name
        if downloaded.is_symlink() or not downloaded.is_file():
            raise RuntimeError("article artifact download is incomplete")
        if downloaded.stat().st_size != item.bytes or _sha256(downloaded) != item.sha256:
            raise RuntimeError("article artifact download digest differs")
        source = _safe_relative_path(item.repository_path, root=root)
        if source.read_bytes() != downloaded.read_bytes():
            raise RuntimeError("article artifact download differs from source bytes")


def safe_https(value: object, *, label: str) -> str:
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


__all__ = [
    "ARTICLE_RELATIVE_PATH",
    "MANIFEST_RELATIVE_PATH",
    "PREVIOUS_READER_REPORT_TITLE",
    "PREVIOUS_READER_REPORT_URL",
    "REPORT_TITLE",
    "SOURCE_PROJECTION_RELATIVE_PATH",
    "TARGET_REPORT_ID",
    "TARGET_REPORT_URL",
    "ArticleRevisionSnapshot",
    "ProtectedReportSnapshot",
    "VerifiedVisualStoryReportPublisher",
    "VisualStoryAssets",
    "VisualStoryMediaReceipt",
    "VisualStoryReportPlan",
    "VisualStoryReportReceipt",
    "canonical_plan_bytes",
    "expected_artifact_metadata",
    "expected_run_config",
    "load_visual_story_assets",
    "parse_report_marker",
    "publication_artifact_manifest_sha256",
    "render_report_blocks",
    "report_description",
    "revision_artifact_payloads",
    "safe_https",
    "validate_artifact_download",
    "validate_plan_bytes",
]
