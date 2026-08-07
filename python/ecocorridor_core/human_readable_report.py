"""Hash-verified, reader-first W&B Report publication for EcoCorridor.

The historical Report is evidence, not an editable document surface.  This
module first verifies that Report, its retained campaign artifact, and the
committed Ridgeway fixture.  It can then create a separate, plain-language
Report in the same W&B project.  It never edits the source Report and never
creates a W&B Run or Artifact.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .usefulness_narrative import (
    HISTORICAL_ARTIFACT_REF,
    HISTORICAL_ARTIFACT_URL,
    HISTORICAL_BASELINE_INPUT_TOKENS,
    HISTORICAL_CAMPAIGN_SHA256,
    HISTORICAL_CONTEXT_REDUCTION_PERCENT,
    HISTORICAL_NATIVE_INPUT_TOKENS,
    HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
    HISTORICAL_PROVIDER_CONVERSATION_COUNT,
    HISTORICAL_RECORD_COUNT,
    HISTORICAL_RUN_URL,
    IMPLEMENTATION_PR_URL,
    SOURCE_REPOSITORY,
    RidgewayNarrativeEvidence,
    UsefulnessNarrativeReceipt,
    _blocks_sha256,
    _require_web_origin,
)

REPORT_SCHEMA_VERSION = "ecocorridor-human-readable-report/v1"
RECEIPT_SCHEMA_VERSION = "ecocorridor-human-readable-report-receipt/v1"
REPORT_TITLE = "Passing the model is not the same as being ready to build"
REPORT_MARKER_PREFIX = "<!-- ecocorridor-human-readable-report:"
REPORT_READBACK_TIMEOUT_SECONDS = 20.0
REPORT_READBACK_INTERVAL_SECONDS = 0.5


class HumanReadableSection(ContractModel):
    """One exact article section before conversion to W&B blocks."""

    section_id: str = Field(pattern=r"^[a-z0-9-]+$")
    heading: str = Field(min_length=3, max_length=160)
    markdown: str = Field(min_length=1, max_length=16_000)


class RidgewayArticleEvidence(ContractModel):
    """Plain-language Ridgeway facts bound to the verified projection digest."""

    evidence: RidgewayNarrativeEvidence
    complete_scenario_count: Literal[12] = 12
    connectivity_checks_passed: Literal[True] = True
    review_trigger_percent: Literal[15.0] = 15.0
    safeguard_mask_min_percent: float
    safeguard_mask_max_percent: float

    @model_validator(mode="after")
    def validate_article_evidence(self) -> Self:
        if abs(self.safeguard_mask_min_percent - 47.34703286028441) > 1e-12:
            raise ValueError("Ridgeway minimum safeguard mask value differs")
        if abs(self.safeguard_mask_max_percent - 65.64856911527522) > 1e-12:
            raise ValueError("Ridgeway maximum safeguard mask value differs")
        if self.evidence.required_count != self.complete_scenario_count:
            raise ValueError("Ridgeway complete-scenario count differs")
        if self.evidence.pass_count != self.complete_scenario_count:
            raise ValueError("Ridgeway connectivity checks are not complete and passing")
        return self

    @classmethod
    def from_verified_projection(
        cls,
        evidence: RidgewayNarrativeEvidence,
    ) -> RidgewayArticleEvidence:
        # These values are part of the same pinned ui_projection.json whose
        # complete bytes are verified before RidgewayNarrativeEvidence exists.
        return cls(
            evidence=evidence,
            complete_scenario_count=12,
            connectivity_checks_passed=True,
            review_trigger_percent=15.0,
            safeguard_mask_min_percent=47.34703286028441,
            safeguard_mask_max_percent=65.64856911527522,
        )


class HumanReadableArticle(ContractModel):
    """Provider-neutral, hashable article content."""

    schema_version: Literal["ecocorridor-human-readable-article/v1"] = (
        "ecocorridor-human-readable-article/v1"
    )
    title: Literal["Passing the model is not the same as being ready to build"] = REPORT_TITLE
    summary: str = Field(min_length=20, max_length=1_500)
    sections: tuple[HumanReadableSection, ...]
    article_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_article(self) -> Self:
        if len(self.sections) < 6:
            raise ValueError("human-readable article requires at least six sections")
        if len({section.section_id for section in self.sections}) != len(self.sections):
            raise ValueError("human-readable article section IDs must be unique")
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"article_sha256"}))
        if self.article_sha256 != expected:
            raise ValueError("human-readable article digest differs")
        return self

    @classmethod
    def create(
        cls,
        *,
        summary: str,
        sections: Sequence[HumanReadableSection],
    ) -> HumanReadableArticle:
        payload = {
            "schema_version": "ecocorridor-human-readable-article/v1",
            "title": REPORT_TITLE,
            "summary": summary,
            "sections": [section.model_dump(mode="json") for section in sections],
        }
        return cls(**payload, article_sha256=canonical_sha256(payload))


class HumanReadableSourceSnapshot(ContractModel):
    """Exact source identities verified before any new Report is created."""

    schema_version: Literal["ecocorridor-human-readable-source/v1"] = (
        "ecocorridor-human-readable-source/v1"
    )
    source_repository: Literal["ash0ts/gi-scenario-studio"] = SOURCE_REPOSITORY
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    historical_report_url: str
    historical_report_id: str = Field(min_length=1)
    historical_report_title: str = Field(min_length=1)
    historical_report_blocks_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_report_block_count: int = Field(ge=1)
    historical_run_url: Literal[
        "https://wandb.ai/wandb/ecocorridor-scenario-studio/runs/"
        "review-brief-realistic-120b-5dad47c-v5"
    ] = HISTORICAL_RUN_URL
    historical_artifact_ref: Literal[
        "wandb/ecocorridor-scenario-studio/ecocorridor-review-brief-33f5f7be9aee:v0"
    ] = HISTORICAL_ARTIFACT_REF
    historical_artifact_url: Literal[
        "https://wandb.ai/wandb/ecocorridor-scenario-studio/artifacts/evaluation-bundle/"
        "ecocorridor-review-brief-33f5f7be9aee/v0"
    ] = HISTORICAL_ARTIFACT_URL
    historical_campaign_sha256: Literal[
        "33f5f7be9aee7bbf09586cfe704358f5cebe9286e0adecf4ec79101609f918c3"
    ] = HISTORICAL_CAMPAIGN_SHA256
    historical_artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_trace_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ridgeway: RidgewayArticleEvidence
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"snapshot_sha256"}))
        if self.snapshot_sha256 != expected:
            raise ValueError("human-readable source snapshot digest differs")
        return self

    @classmethod
    def from_verified_receipt(
        cls,
        receipt: UsefulnessNarrativeReceipt,
        *,
        ridgeway: RidgewayNarrativeEvidence,
    ) -> HumanReadableSourceSnapshot:
        payload = {
            "schema_version": "ecocorridor-human-readable-source/v1",
            "source_repository": SOURCE_REPOSITORY,
            "source_commit": receipt.source_commit,
            "historical_report_url": receipt.report_url,
            "historical_report_id": receipt.report_id,
            "historical_report_title": receipt.report_title,
            "historical_report_blocks_sha256": receipt.historical_prefix_sha256,
            "historical_report_block_count": receipt.final_block_count,
            "historical_run_url": receipt.historical_run_url,
            "historical_artifact_ref": HISTORICAL_ARTIFACT_REF,
            "historical_artifact_url": HISTORICAL_ARTIFACT_URL,
            "historical_campaign_sha256": receipt.historical_campaign_sha256,
            "historical_artifact_manifest_sha256": (
                receipt.historical_artifact_manifest_sha256
            ),
            "historical_trace_receipts_sha256": (
                receipt.historical_provider_trace_receipts_sha256
            ),
            "ridgeway": RidgewayArticleEvidence.from_verified_projection(ridgeway).model_dump(
                mode="json"
            ),
        }
        return cls(**payload, snapshot_sha256=canonical_sha256(payload))


class HumanReadableReportPlan(ContractModel):
    """Frozen read-only source snapshot plus exact article to create."""

    schema_version: Literal["ecocorridor-human-readable-report/v1"] = REPORT_SCHEMA_VERSION
    entity: Literal["wandb"] = "wandb"
    project: Literal["ecocorridor-scenario-studio"] = "ecocorridor-scenario-studio"
    source: HumanReadableSourceSnapshot
    article: HumanReadableArticle
    publication_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        key = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "source_snapshot_sha256": self.source.snapshot_sha256,
                "article_sha256": self.article.article_sha256,
            }
        )
        if self.publication_key != key:
            raise ValueError("human-readable publication key differs")
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"plan_sha256"}))
        if self.plan_sha256 != expected:
            raise ValueError("human-readable report plan digest differs")
        return self

    @classmethod
    def create(cls, source: HumanReadableSourceSnapshot) -> HumanReadableReportPlan:
        article = build_human_readable_article(source)
        key = canonical_sha256(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "source_snapshot_sha256": source.snapshot_sha256,
                "article_sha256": article.article_sha256,
            }
        )
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "entity": "wandb",
            "project": "ecocorridor-scenario-studio",
            "source": source.model_dump(mode="json"),
            "article": article.model_dump(mode="json"),
            "publication_key": key,
        }
        return cls(**payload, plan_sha256=canonical_sha256(payload))


class HumanReadableReportReceipt(ContractModel):
    """Receipt written only after destination and untouched source readback."""

    schema_version: Literal["ecocorridor-human-readable-report-receipt/v1"] = (
        RECEIPT_SCHEMA_VERSION
    )
    status: Literal["CREATED", "REUSED"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_url: str
    source_report_blocks_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_preserved: Literal[True] = True
    report_url: str
    report_id: str = Field(min_length=1)
    report_title: Literal[
        "Passing the model is not the same as being ready to build"
    ] = REPORT_TITLE
    article_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_readback_verified: Literal[True] = True
    new_wandb_run_created: Literal[False] = False
    new_artifact_created: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.report_url == self.source_report_url:
            raise ValueError("human-readable Report must differ from the historical Report")
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if self.receipt_sha256 != expected:
            raise ValueError("human-readable Report receipt digest differs")
        return self

    @classmethod
    def create(cls, **values: Any) -> HumanReadableReportReceipt:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "source_report_preserved": True,
            "report_title": REPORT_TITLE,
            "report_readback_verified": True,
            "new_wandb_run_created": False,
            "new_artifact_created": False,
            **values,
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


def build_human_readable_article(
    source: HumanReadableSourceSnapshot,
) -> HumanReadableArticle:
    """Explain the verified record before exposing audit identifiers."""

    ridgeway = source.ridgeway
    fixture = ridgeway.evidence
    token_difference = HISTORICAL_BASELINE_INPUT_TOKENS - HISTORICAL_NATIVE_INPUT_TOKENS
    sections = (
        HumanReadableSection(
            section_id="decision-problem",
            heading="The decision problem",
            markdown=(
                "A wildlife-connectivity model can produce a precise-looking map without "
                "answering the question a review team actually faces: **which option deserves "
                "field investigation, and what could still make it fail?**\n\n"
                "EcoCorridor organizes direct Circuitscape results, costs, safeguards, "
                "assumptions, and source files around that decision. It does not turn a model "
                "result into an ecological approval."
            ),
        ),
        HumanReadableSection(
            section_id="ridgeway-example",
            heading="A concrete example: Ridgeway Crossing",
            markdown=(
                "Ridgeway is a synthetic training landscape, not a real road or wildlife "
                "population. One candidate looks promising at first glance, but the complete "
                "record changes the next action. Circuitscape uses an electrical-circuit analogy "
                "to show where modeled connectivity may concentrate. That signal does not show "
                "where animals actually move.\n\n"
                "| Check | Direct evidence | What a reviewer should conclude |\n"
                "|---|---:|---|\n"
                f"| Scenario evidence | All {ridgeway.complete_scenario_count} synthetic "
                "scenarios completed and no required connectivity check failed | The selected "
                "synthetic checks passed, but that is not an approval |\n"
                f"| Screening cost | USD {fixture.cost_usd:,.0f} of a USD "
                f"{fixture.budget_usd:,.0f} cap | Within the assumed screening budget |\n"
                "| Connectivity safeguard | Modeled difficulty connecting habitat areas "
                f"(effective resistance) improved "
                f"{abs(fixture.resistance_change_percent):.2f}% in the least favorable case | "
                "The check designed to avoid worsening the safeguard guild passed under the "
                "stated assumptions |\n"
                f"| Road-area warning | {ridgeway.safeguard_mask_min_percent:.2f}% to "
                f"{ridgeway.safeguard_mask_max_percent:.2f}% change in the model signal near the "
                "road | Above "
                f"the authored {ridgeway.review_trigger_percent:.0f}% trigger, so field review "
                "is required; this does not predict crossing use |\n\n"
                "**Outcome: carry forward for field and feasibility review—not approval.** The "
                "road-mask finding is exactly the kind of caveat that a headline score can hide."
            ),
        ),
        HumanReadableSection(
            section_id="current-value",
            heading="What the software is useful for today",
            markdown=(
                "The implemented workflow is useful as an **audit and screening discipline**:\n\n"
                "- it withholds a portfolio from comparison until every required target and "
                "safeguard scenario finishes;\n"
                "- it keeps scenario-specific resistance results separate instead of blending "
                "incompatible ecological hypotheses;\n"
                "- it resolves a displayed claim back to the direct run, scenario, raster change, "
                "source files, model settings, quality checks, and assumptions; and\n"
                "- it can say `SURVEY_FIRST` when evidence, authorization, or feasibility is "
                "incomplete.\n\n"
                "Those are product capabilities. Whether they make real practitioners faster or "
                "less likely to miss a safeguard still requires a controlled study."
            ),
        ),
        HumanReadableSection(
            section_id="agent-result",
            heading="What the first agent experiment taught us",
            markdown=(
                f"The historical test contained **{HISTORICAL_RECORD_COUNT} scored records**. "
                f"W&B Weave recorded **{HISTORICAL_PROVIDER_CONVERSATION_COUNT} real model "
                f"conversations**, while **{HISTORICAL_PRE_PROVIDER_BLOCK_COUNT} unsafe or "
                "invalid requests were stopped before any model call**.\n\n"
                "| Method | Provider input tokens |\n"
                "|---|---:|\n"
                f"| Full-context comparator | {HISTORICAL_BASELINE_INPUT_TOKENS:,} |\n"
                f"| Read-only agent workflow | {HISTORICAL_NATIVE_INPUT_TOKENS:,} |\n"
                f"| Difference | {token_difference:,} fewer tokens "
                f"({HISTORICAL_CONTEXT_REDUCTION_PERCENT:.2f}%) |\n\n"
                "The promotion target was 25%, so the recorded decision was to keep the simpler, "
                "deterministic checklist as the default. "
                "That is a useful negative result. It prevented an agent from being promoted on "
                "novelty alone.\n\n"
                "The experiment did **not** prove adaptive agent usefulness. The agent followed a "
                "fixed sequence, and the comparison method was given an answer guide. "
                "A corrected experiment must give each method the same evidence and allow the "
                "agent to choose when to retrieve and stop."
            ),
        ),
        HumanReadableSection(
            section_id="claim-boundary",
            heading="What we can—and cannot—claim",
            markdown=(
                "| Claim | Current status |\n"
                "|---|---|\n"
                "| The historical calls were traced and checked before release | **Supported for "
                "the recorded campaign** |\n"
                "| The first agent reduced context enough to justify promotion | **No** |\n"
                "| EcoCorridor improves practitioner decisions or review speed | **Not tested "
                "yet** |\n"
                "| The software predicts animal movement, crossing use, collision reduction, or "
                "population benefit | **No—outside the model and product claim boundary** |\n"
                "| Ridgeway identifies an approved or design-ready crossing | **No—synthetic "
                "screening example only** |"
            ),
        ),
        HumanReadableSection(
            section_id="next-evidence",
            heading="What evidence comes next",
            markdown=(
                "The next program is designed to answer the unanswered questions rather than "
                "decorate the current result:\n\n"
                "1. Generate direct evidence on five different synthetic landscape structures.\n"
                "2. Compare deterministic review, fair full-context review, selective agent "
                "retrieval, forced exhaustive retrieval, and an oracle diagnostic control.\n"
                "3. Stress the safety boundary with prompt injection, stale evidence, sensitive "
                "locations, malformed output, tool tampering, and trace outages.\n"
                "4. Run a randomized practitioner crossover only after the synthetic truth and "
                "rubric are independently adjudicated.\n"
                "5. Report partner shadow studies as descriptive workflow evidence—not as proof "
                "of ecological outcomes."
            ),
        ),
        HumanReadableSection(
            section_id="audit-trail",
            heading="Inspect the evidence",
            markdown=(
                f"- [Historical campaign run]({source.historical_run_url})\n"
                f"- [Immutable campaign artifact]({source.historical_artifact_url})\n"
                f"- [Original historical Report]({source.historical_report_url})\n"
                f"- [Implementation and review history]({IMPLEMENTATION_PR_URL})\n\n"
                "Machine-verifiable identities:\n\n"
                f"- campaign: `{source.historical_campaign_sha256}`\n"
                f"- campaign artifact manifest: "
                f"`{source.historical_artifact_manifest_sha256}`\n"
                f"- retained Weave trace receipts: "
                f"`{source.historical_trace_receipts_sha256}`\n"
                f"- source Report blocks: `{source.historical_report_blocks_sha256}`\n"
                f"- Ridgeway projection: `{source.ridgeway.evidence.projection_sha256}`\n\n"
                "The identifiers are placed here, after the explanation, so they support the "
                "story instead of replacing it."
            ),
        ),
    )
    return HumanReadableArticle.create(
        summary=(
            "A $400,000 Ridgeway option fit within its $500,000 screening budget, and all 12 "
            "synthetic scenarios completed without a failed connectivity check. But the model "
            "also flagged a large 47–66% shift around the road in its electrical-circuit "
            "analogy—well above the 15% investigation trigger. This is not observed animal "
            "movement. The right next step is field review, not approval. The agent then failed "
            "its own usefulness test: it cut model input by only 2.37%, far short of the 25% "
            "target, so the simpler checklist remains the default."
        ),
        sections=sections,
    )


def _report_marker(plan: HumanReadableReportPlan) -> dict[str, str]:
    return {
        "schema_version": plan.schema_version,
        "publication_key": plan.publication_key,
        "plan_sha256": plan.plan_sha256,
        "source_report_blocks_sha256": plan.source.historical_report_blocks_sha256,
        "article_sha256": plan.article.article_sha256,
    }


def report_description(plan: HumanReadableReportPlan) -> str:
    marker = json.dumps(_report_marker(plan), sort_keys=True, separators=(",", ":"))
    return (
        "A plain-language, bounded interpretation of the verified EcoCorridor evidence. "
        "The original historical Report remains unchanged. No new experiment result is "
        "created by this article.\n\n"
        f"{REPORT_MARKER_PREFIX}{marker} -->"
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
    expected_fields = {
        "schema_version",
        "publication_key",
        "plan_sha256",
        "source_report_blocks_sha256",
        "article_sha256",
    }
    if not isinstance(marker, dict) or set(marker) != expected_fields:
        return None
    if not all(isinstance(item, str) for item in marker.values()):
        return None
    return marker


def render_report_blocks(reports: Any, article: HumanReadableArticle) -> list[Any]:
    blocks: list[Any] = [
        reports.H1(article.title),
        reports.CalloutBlock(text=article.summary),
    ]
    for section in article.sections:
        blocks.extend(
            [
                reports.H2(section.heading),
                reports.MarkdownBlock(text=section.markdown),
            ]
        )
    return blocks


class VerifiedHumanReadableReportPublisher:
    """Create or read back one hash-verified derivative Report snapshot.

    ``source_reader`` must perform the full remote historical Run, Artifact,
    trace-receipt, source-Report, and local Ridgeway verification and return a
    receipt.  The reader runs before discovery and again immediately before a
    first save.  The source Report is read once more after creation.
    """

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        web_base_url: str,
        authorize_new_report: bool,
        api: Any,
        reports_module: Any,
        source_reader: Callable[[], tuple[UsefulnessNarrativeReceipt, RidgewayNarrativeEvidence]],
    ) -> None:
        if not authorize_new_report:
            raise PermissionError("creating a new W&B Report requires explicit authorization")
        if entity != "wandb" or project != "ecocorridor-scenario-studio":
            raise PermissionError("human-readable Report is bound to the EcoCorridor project")
        self.entity = entity
        self.project = project
        self.web_base_url = web_base_url.rstrip("/")
        self.api = api
        self.reports = reports_module
        self.source_reader = source_reader

    def read_plan(self) -> HumanReadableReportPlan:
        receipt, ridgeway = self.source_reader()
        source = HumanReadableSourceSnapshot.from_verified_receipt(
            receipt,
            ridgeway=ridgeway,
        )
        return HumanReadableReportPlan.create(source)

    def _discover(self, plan: HumanReadableReportPlan) -> Any | None:
        expected_marker = _report_marker(plan)
        candidates = list(
            self.api.reports(path=f"{self.entity}/{self.project}", per_page=100)
        )
        exact: list[Any] = []
        for candidate in candidates:
            title = str(
                getattr(candidate, "display_name", getattr(candidate, "title", ""))
            )
            marker = parse_report_marker(getattr(candidate, "description", ""))
            if marker == expected_marker:
                exact.append(candidate)
            elif title == REPORT_TITLE:
                raise PermissionError(
                    "human-readable Report title is occupied by different source evidence"
                )
        if len(exact) > 1:
            raise RuntimeError("multiple Reports carry the same verified publication key")
        if not exact:
            return None
        url = _require_web_origin(
            getattr(exact[0], "url", ""),
            web_base_url=self.web_base_url,
            label="existing human-readable W&B Report URL",
        )
        return self.reports.Report.from_url(url)

    def _verify_destination(
        self,
        report: Any,
        *,
        plan: HumanReadableReportPlan,
    ) -> tuple[str, str]:
        if (
            str(getattr(report, "entity", "")) != self.entity
            or str(getattr(report, "project", "")) != self.project
            or str(getattr(report, "title", "")) != REPORT_TITLE
            or parse_report_marker(getattr(report, "description", ""))
            != _report_marker(plan)
        ):
            raise RuntimeError("human-readable Report identity or marker differs")
        report_url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="human-readable W&B Report URL",
        )
        report_id = str(getattr(report, "id", "") or "")
        if not report_id:
            raise RuntimeError("human-readable W&B Report lacks a stable ID")
        if (
            report_url == plan.source.historical_report_url
            or report_id == plan.source.historical_report_id
        ):
            raise RuntimeError("publisher attempted to reuse the historical Report as destination")
        expected = render_report_blocks(self.reports, plan.article)
        if _blocks_sha256(list(report.blocks)) != _blocks_sha256(expected):
            raise RuntimeError("human-readable W&B Report body differs")
        return report_url, report_id

    def _verify_source_unchanged(self, plan: HumanReadableReportPlan) -> None:
        source_report = self.reports.Report.from_url(plan.source.historical_report_url)
        if (
            str(getattr(source_report, "id", "")) != plan.source.historical_report_id
            or str(getattr(source_report, "title", ""))
            != plan.source.historical_report_title
            or _blocks_sha256(list(source_report.blocks))
            != plan.source.historical_report_blocks_sha256
        ):
            raise RuntimeError("historical source Report changed during publication")

    def publish(self, frozen_plan: HumanReadableReportPlan) -> HumanReadableReportReceipt:
        first = self.read_plan()
        if first != frozen_plan:
            raise RuntimeError("verified source differs from the frozen article plan")
        existing = self._discover(frozen_plan)
        status: Literal["CREATED", "REUSED"]
        if existing is None:
            second = self.read_plan()
            if second != frozen_plan:
                raise RuntimeError("verified source changed before new Report creation")
            report = self.reports.Report(
                entity=self.entity,
                project=self.project,
                title=REPORT_TITLE,
                description=report_description(frozen_plan),
                blocks=render_report_blocks(self.reports, frozen_plan.article),
                width="fluid",
            )
            report.save()
            status = "CREATED"
        else:
            report = existing
            status = "REUSED"

        report_url = _require_web_origin(
            getattr(report, "url", ""),
            web_base_url=self.web_base_url,
            label="created human-readable W&B Report URL",
        )
        deadline = time.monotonic() + REPORT_READBACK_TIMEOUT_SECONDS
        while True:
            try:
                readback = self.reports.Report.from_url(report_url)
                verified_url, report_id = self._verify_destination(
                    readback,
                    plan=frozen_plan,
                )
                self._verify_source_unchanged(frozen_plan)
                return HumanReadableReportReceipt.create(
                    status=status,
                    plan_sha256=frozen_plan.plan_sha256,
                    publication_key=frozen_plan.publication_key,
                    source_report_url=frozen_plan.source.historical_report_url,
                    source_report_blocks_sha256=(
                        frozen_plan.source.historical_report_blocks_sha256
                    ),
                    report_url=verified_url,
                    report_id=report_id,
                    article_sha256=frozen_plan.article.article_sha256,
                )
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry avoids remote-content leakage
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "new human-readable W&B Report failed exact readback"
                    ) from None
                time.sleep(REPORT_READBACK_INTERVAL_SECONDS)


def validate_report_plan_bytes(payload: bytes) -> HumanReadableReportPlan:
    """Require the canonical bytes emitted by the verification phase."""

    plan = HumanReadableReportPlan.model_validate_json(payload)
    canonical = (
        json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    if payload != canonical:
        raise ValueError("human-readable Report plan bytes are noncanonical")
    return plan


def canonical_report_plan_bytes(plan: HumanReadableReportPlan) -> bytes:
    return (
        json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def safe_report_filename(plan: HumanReadableReportPlan) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", plan.publication_key):
        raise ValueError("invalid publication key")
    return f"ecocorridor-human-readable-report-{plan.publication_key[:12]}.json"
