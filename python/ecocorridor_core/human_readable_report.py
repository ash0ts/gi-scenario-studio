"""Hash-verified, reader-first W&B Report publication for EcoCorridor.

The historical Report is evidence, not an editable document surface. This
module first verifies that Report, its retained campaign artifact, and the
committed Ridgeway fixture. It can then create a separate, human long-form
Report in the same W&B project. It never edits the source Report and never
creates a W&B Run or Artifact.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from textwrap import dedent
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

REPORT_SCHEMA_VERSION = "ecocorridor-human-readable-report/v2"
RECEIPT_SCHEMA_VERSION = "ecocorridor-human-readable-report-receipt/v2"
REPORT_TITLE = "A Green Model Result Is Not a Green Light"
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

    schema_version: Literal["ecocorridor-human-readable-article/v2"] = (
        "ecocorridor-human-readable-article/v2"
    )
    title: Literal["A Green Model Result Is Not a Green Light"] = REPORT_TITLE
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
            "schema_version": "ecocorridor-human-readable-article/v2",
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

    schema_version: Literal["ecocorridor-human-readable-report/v2"] = REPORT_SCHEMA_VERSION
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

    schema_version: Literal["ecocorridor-human-readable-report-receipt/v2"] = (
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
    report_title: Literal["A Green Model Result Is Not a Green Light"] = REPORT_TITLE
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
    """Tell the verified story before exposing audit identifiers."""

    ridgeway = source.ridgeway
    fixture = ridgeway.evidence
    token_difference = HISTORICAL_BASELINE_INPUT_TOKENS - HISTORICAL_NATIVE_INPUT_TOKENS
    sections = (
        HumanReadableSection(
            section_id="green-light",
            heading="Every model has a moment when it tries to become a decision",
            markdown=dedent(
                """
                _The story of a fictional wildlife crossing, a very real review problem, and an
                AI agent that got a clipboard instead of the keys._

                It happens somewhere between the dashboard and the meeting. A number that meant
                “under these assumptions, this particular check did not fail” changes clothes,
                walks into a slide deck, and introduces itself as “we should build this.”

                Very smooth. Extremely employable. Occasionally dangerous.

                We built **EcoCorridor Scenario Studio** because wildlife-connectivity work
                deserves a better handoff between model result and professional judgment. The
                software helps a review team compare bounded crossing, fencing, and
                habitat-restoration concepts across ecological scenarios, costs, safeguards, and
                unresolved field questions. Every reported connectivity value must come from a
                direct [Circuitscape](https://docs.circuitscape.org/Circuitscape.jl/latest/usage/)
                run. Every important claim can be traced back to the scenario, source maps,
                modeled change, assumptions, quality checks, and exact files that produced it.

                That sounds like a lot of machinery. The reason for it is pleasantly simple:

                > A promising model result should make the next investigation clearer. It should
                > not impersonate an approval.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="ridgeway-example",
            heading="Welcome to Ridgeway, a landscape we made up",
            markdown=dedent(
                f"""
                Ridgeway is our fictional teaching landscape. It has four habitat areas separated
                by one highway, two anonymous target groups, one safeguard group, four different
                ways of describing landscape resistance, and eight possible intervention sites.

                No real municipality is represented, and no threatened-species location is
                hiding behind a cute pseudonym.

                The exercise asks a practical screening question:

                > Which intervention package, if any, deserves field and feasibility review?

                One candidate looks quite good at first glance. Its assumed screening cost is
                **${fixture.cost_usd:,.0f}**, below a **${fixture.budget_usd:,.0f}** cap. All
                **{ridgeway.complete_scenario_count} synthetic scenarios** completed, and no
                required target or safeguard connectivity check failed. Even in the least
                favorable safeguard case, modeled difficulty connecting habitat areas improved
                by **{abs(fixture.resistance_change_percent):.4f}%**.

                This is the part of the meeting where somebody reaches for the green highlighter.

                Then the road-area diagnostic speaks up. Compared with the no-action baseline,
                the total Circuitscape modeled current—the sum of current values inside the
                area—changed sharply in a place we had already marked for road review. The 15%
                trigger was an
                authored investigation threshold for this synthetic lesson, not a biological
                threshold.

                | Check | What the direct evidence says | What it supports |
                | --- | --- | --- |
                | Scenario completeness | All {ridgeway.complete_scenario_count} synthetic scenarios completed; no required connectivity check failed | Complete enough to compare under the authored rules |
                | Screening cost | ${fixture.cost_usd:,.0f} against an assumed ${fixture.budget_usd:,.0f} cap | Fits the synthetic screening budget; this is not a bid or construction estimate |
                | Safeguard connectivity | Modeled resistance improved {abs(fixture.resistance_change_percent):.4f}% in the least favorable safeguard case | The modeled safeguard connection did not worsen under the stated assumptions |
                | Road-area diagnostic | Total modeled current inside the predefined road-review area changed {ridgeway.safeguard_mask_min_percent:.2f}%–{ridgeway.safeguard_mask_max_percent:.2f}% relative to the no-action baseline, above the authored {ridgeway.review_trigger_percent:.0f}% trigger | Investigate the road area, modeled effect, and site assumptions; this is not animal movement |
                | Project assumption | One site in the broader project catalog still lacks a confirmed maintenance owner | Resolve the project record; this candidate proposes no work at that site |

                The outcome is therefore:

                > **Candidate to carry forward for ecological, field, and engineering feasibility
                > review.**

                Not “best.” Not “approved.”

                Carry forward for review.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="electrical-map",
            heading="The electrical map is not a deer",
            markdown=dedent(
                f"""
                [Circuitscape](https://docs.circuitscape.org/Circuitscape.jl/latest/usage/)
                models a landscape using an electrical-circuit analogy. Habitat areas act like
                endpoints. Each map cell receives a reviewed resistance value describing how
                difficult movement is assumed to be through that cell. The solver calculates
                effective resistance between habitat areas and a current pattern showing how
                modeled connectivity may spread across the landscape.

                This lets reviewers ask useful questions. Does an intervention help under several
                plausible resistance maps or only one? Does improving a target scenario create a
                concern for a safeguard scenario? Where does the modeled signal shift enough to
                warrant closer inspection?

                But the analogy remains an analogy.

                A current map does not show observed animal movement. It does not predict whether
                an animal will use a crossing. It does not estimate collisions avoided,
                population survival, biodiversity gained, or regulatory compliance achieved.

                Ridgeway matters because two things can be true at once:

                1. No required modeled connectivity check failed.
                2. Total modeled current inside the road-review area changed by {ridgeway.safeguard_mask_min_percent:.0f}%–{ridgeway.safeguard_mask_max_percent:.0f}% from the no-action baseline.

                The first statement says the candidate met the authored connectivity rules. The
                second says something changed in a sensitive place strongly enough that a reviewer
                should investigate. A single score would flatten both statements into one color.
                EcoCorridor keeps them separate and keeps the road-area result tied to its
                baseline, scenario, map, and assumptions.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="field-notebook",
            heading="What we actually built",
            markdown=dedent(
                """
                The product is not an automatic corridor designer. It is a decision workspace for
                an ecologist, transportation environmental team, or consultant who already owns
                the ecological judgment.

                The human supplies or approves the resistance hypotheses, habitat areas,
                intervention effects, safeguard scenarios, costs, and feasibility rules. The
                software refuses to quietly invent them. It then handles the tedious evidence work
                that is easy to perform inconsistently:

                - run every option against every required target and safeguard scenario;
                - withhold incomplete options from comparison;
                - keep incompatible ecological assumptions separate;
                - surface road-area and safeguard concerns beside favorable results;
                - preserve assumptions and unresolved questions;
                - stop at **survey first** when key evidence or feasibility is missing; and
                - connect each displayed claim to its direct run and source evidence.

                The important output is not a prettier connectivity map. It is a smaller,
                inspectable decision.

                Professional judgment does the important part. The software keeps its evidence
                organized enough to inspect.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="agent-clipboard",
            heading="Then we added an agent, because apparently we enjoy supervision",
            markdown=dedent(
                """
                The natural AI question was whether an agent could help prepare the field-review
                brief. This is plausible process work: a large evidence record may contain
                safeguard findings, model gaps, cost constraints, authorizations, and direct-run
                references scattered across several branches.

                So we designed a bounded Review Brief Agent.

                The agent gets a clipboard, not the keys.

                It may inspect only an authorized snapshot of the selected option and ask narrow,
                read-only questions. It cannot change the model, run a new analysis, approve an
                intervention, select a preferred option, accept a quality issue, alter costs,
                browse the filesystem, search the web, or create a new ecological value.

                It does not write the final story freestyle either. The model returns a structured
                selection of known findings. Deterministic local code supplies the visible
                numbers, units, limitations, and evidence links.

                Every real model turn and tool call in the recorded campaign was retained with
                [W&B Weave](https://docs.wandb.ai/weave/guides/tracking/tracing). If an output
                fails its local checks, cites evidence outside its authorization, or cannot be
                read back from the exact trace, no brief is saved.

                This is deliberately less ambitious than “AI plans a wildlife corridor.” The
                agent organizes authorized evidence; it does not acquire ecological authority.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="no-parade",
            heading=f"The agent saved {token_difference} tokens and received no parade",
            markdown=dedent(
                f"""
                The question was narrow: could the read-only agent inspect less evidence than a
                full-context method without losing the required findings?

                | Method | Model input |
                | --- | ---: |
                | Full-context comparison | {HISTORICAL_BASELINE_INPUT_TOKENS:,} tokens |
                | Read-only agent | {HISTORICAL_NATIVE_INPUT_TOKENS:,} tokens |
                | Difference | {token_difference:,} fewer tokens ({HISTORICAL_CONTEXT_REDUCTION_PERCENT:.2f}%) |

                That is enough to avoid one modest paragraph. It is not enough to reorganize the
                product around an agent.

                We declared a **25% reduction target** before reading the result. The agent missed
                it by a comfortable margin, so the rule-based checklist remains the default.

                No parade was scheduled.

                For the audit record, the campaign contained **{HISTORICAL_RECORD_COUNT} scored
                records**: **{HISTORICAL_PROVIDER_CONVERSATION_COUNT} real model conversations**
                captured in Weave, **{HISTORICAL_PRE_PROVIDER_BLOCK_COUNT} unsafe or invalid
                cases** stopped before any model call, and 14 deterministic local-control records.

                The comparison was also not fair enough to answer the larger agent question. The
                supposed agent was instructed to call the same tools in the same order, so it was
                not choosing what evidence to inspect or when to stop. The full-context method's
                prompt included a deterministic expected-answer contract describing the findings
                a valid answer should contain. In plain English: one side followed a script, and
                the other received hints about the answer.

                That campaign tested the plumbing. It did not test genuinely selective agent
                behavior.

                The supported conclusion is narrower:

                > This experiment established traceable, fail-closed execution under the recorded
                > conditions. It did not establish that the agent reduced review burden enough to
                > deserve promotion.

                That is a useful negative result. It stopped “we added an agent” from quietly
                becoming “the agent improved the product.”
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="current-value",
            heading="What is useful today",
            markdown=dedent(
                """
                EcoCorridor’s present value is not that it knows ecology better than an ecologist.
                It is that it makes several ordinary review failures harder:

                - treating an incomplete scenario set as a comparable result;
                - forgetting a safeguard while admiring a target improvement;
                - blending incompatible model assumptions into one reassuring average;
                - losing the link between a claim and the direct run that produced it;
                - allowing a cost-feasible option to outrun unresolved field evidence;
                - describing a modeled electrical pattern as observed animal behavior; and
                - converting “worth investigating” into “ready to build.”

                A conventional Ridgeway summary could stop at “under budget, no failed
                connectivity checks.” EcoCorridor keeps the road-area warning beside that good
                news and turns the warning into a next action.

                The result is a smaller, inspectable handoff: what looks promising, what remains
                unresolved, where every number came from, and what a qualified reviewer should
                investigate next.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="evidence-to-earn",
            heading="What we still have to earn",
            markdown=dedent(
                """
                Ridgeway is synthetic. It demonstrates software behavior against authored
                assumptions. It does not validate a real resistance map, habitat-area choice,
                intervention effect, or crossing design.

                The historical agent campaign shows that provider calls were traced, unsafe cases
                could be blocked, and outputs could be checked before release under those recorded
                conditions. It does not show autonomous ecological reasoning, a useful adaptive
                agent, or lower review burden.

                We have not yet shown that real practitioners using EcoCorridor miss fewer
                safeguards, reach correct screening dispositions faster, or reconstruct evidence
                more accurately than they do with a fair conventional evidence package. Those are
                empirical questions, not adjectives.

                We plan to test several structurally different synthetic landscapes, not endless
                costume changes of Ridgeway. We will compare a rule-based checklist, a model that
                receives the whole evidence packet, an agent allowed to choose what it reads, an
                agent required to read everything, and a best-case comparison given only the
                evidence it needs. We will also try to break the safety boundary with stale
                evidence, malicious instructions, sensitive-location leakage, altered tool
                results, malformed output, and trace outages.

                Only after those technical studies are frozen should practitioners compare the
                workflows in a controlled study. Authorized partner projects should be
                retrospective and de-identified. They should test workflow utility and handoff
                quality without influencing a live decision.

                No part of this work establishes observed animal movement, crossing use,
                collision reduction, population response, biodiversity benefit, regulatory
                approval, or a design-ready project.

                This source implementation is not a practitioner release either. The engine study
                still needs protected independent execution, cloud authorization still needs an
                officially supported verification path, installers still need signatures, and
                practitioners still need to complete acceptance testing.

                If the agent cannot reduce irrelevant reading without missing critical findings,
                it stays experimental. If EcoCorridor does not improve review quality or time, we
                say so.

                Auditability does not replace expertise. It gives expertise something better to
                inspect.
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="community",
            heading="Why the community should care",
            markdown=dedent(
                """
                Environmental decisions often combine uncertain science, costly interventions,
                fragmented evidence, and pressure to produce one clean recommendation. That is
                exactly where a confident summary can outrun what the underlying work supports.

                More AI does not solve that automatically. Neither do more plots or more decimal
                places. What helps is a disciplined division of labor:

                ```text
                model calculates
                  → software preserves and checks
                  → agent may organize
                  → practitioner judges
                ```

                The model calculates the declared scenario. The software keeps the record
                complete, comparable, and traceable. The agent may help navigate that record if
                it proves that it can do so selectively and safely. The practitioner decides what
                the evidence means, what must be investigated, and whether any option deserves the
                next dollar of real-world attention.

                In Ridgeway, the most important result was not the favorable budget or
                connectivity number. It was the moment the evidence said:

                > This still needs a human at the road.

                That is not the model failing to decide. That is the system remembering who
                should.

                **A green result is a reason to open the field notebook—not cut the ribbon.**
                """
            ).strip(),
        ),
        HumanReadableSection(
            section_id="audit-trail",
            heading="Inspect the record",
            markdown=dedent(
                f"""
                - [Historical campaign run]({source.historical_run_url})
                - [Versioned campaign artifact]({source.historical_artifact_url})
                - [Original technical Report]({source.historical_report_url})
                - [Implementation and review history]({IMPLEMENTATION_PR_URL})
                - [Circuitscape documentation](https://docs.circuitscape.org/Circuitscape.jl/latest/usage/)
                - [W&B Weave tracing](https://docs.wandb.ai/weave/guides/tracking/tracing)

                The current local Ridgeway package retains 1,536 direct matrix jobs and 60 direct
                current-map jobs. That is semantically verified local fixture evidence, not a
                signed or protected release attestation.

                The unresolved project-catalog assumption is maintenance ownership at `site 06`;
                the selected candidate uses no action at that site.

                Machine-verifiable identities:

                - campaign: `{source.historical_campaign_sha256}`
                - campaign artifact manifest: `{source.historical_artifact_manifest_sha256}`
                - retained Weave trace receipts: `{source.historical_trace_receipts_sha256}`
                - source Report blocks: `{source.historical_report_blocks_sha256}`
                - Ridgeway projection: `{source.ridgeway.evidence.projection_sha256}`

                The identifiers live here, behind the explanation, where they can support the
                story instead of attempting to become one.
                """
            ).strip(),
        ),
    )
    return HumanReadableArticle.create(
        summary=(
            "Ridgeway looked ready for a victory lap: the synthetic $400,000 option fit its "
            "$500,000 screening budget, and no required connectivity check failed across 12 "
            "scenarios. Then EcoCorridor's authored road-area check found that total modeled "
            "electrical current inside a predefined review area changed 47–66% relative to the "
            "no-action baseline. That exceeded the synthetic 15% investigation trigger; "
            "it was not observed animal movement. The answer became ‘bring a reviewer,’ not "
            "‘approve the project.’ The AI "
            f"agent later saved {token_difference} tokens—2.37% against a 25% promotion gate—and "
            "stayed on the bench."
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
        "A candid, human, and bounded interpretation of the verified EcoCorridor evidence. "
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
