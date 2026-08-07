from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from ecocorridor_core import human_readable_report as article
from ecocorridor_core.usefulness_narrative import (
    HISTORICAL_ARTIFACT_MANIFEST_SHA256,
    HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256,
    HISTORICAL_RUN_URL,
    RidgewayNarrativeEvidence,
    UsefulnessNarrativeReceipt,
    _blocks_sha256,
)

SOURCE_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
    "historical--VmlldzoxNzY1OTU0MQ=="
)
SOURCE_ID = "VmlldzoxNzY1OTU0MQ=="
SOURCE_TITLE = "EcoCorridor historical search benchmark · d43c377f · 2be48975"
SOURCE_COMMIT = "a" * 40


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text
        self._spec = {"text": text}


class H1(_Block):
    pass


class H2(_Block):
    pass


class CalloutBlock(_Block):
    pass


class MarkdownBlock(_Block):
    pass


class _FakeApi:
    def __init__(self) -> None:
        self.items: list[_FakeReport] = []
        self.report_calls = 0

    def reports(self, *, path: str, per_page: int) -> list[_FakeReport]:
        assert path == "wandb/ecocorridor-scenario-studio"
        assert per_page == 100
        self.report_calls += 1
        return list(self.items)


class _FakeReport:
    api: ClassVar[_FakeApi]
    registry: ClassVar[dict[str, _FakeReport]] = {}
    serial: ClassVar[int] = 0
    source_to_mutate: ClassVar[_FakeReport | None] = None
    mutate_source_on_save: ClassVar[bool] = False

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        title: str,
        description: str,
        blocks: list[Any],
        width: str,
    ) -> None:
        self.entity = entity
        self.project = project
        self.title = title
        self.description = description
        self.blocks = list(blocks)
        self.width = width
        self.id = ""
        self.url = ""
        self.saved = 0

    @classmethod
    def retained(
        cls,
        *,
        title: str,
        description: str,
        blocks: list[Any],
        report_id: str,
        url: str,
    ) -> _FakeReport:
        report = cls(
            entity="wandb",
            project="ecocorridor-scenario-studio",
            title=title,
            description=description,
            blocks=blocks,
            width="fluid",
        )
        report.id = report_id
        report.url = url
        cls.registry[url] = report
        cls.api.items.append(report)
        return report

    def save(self) -> None:
        assert not self.id
        self.saved += 1
        type(self).serial += 1
        self.id = f"new-report-{type(self).serial}"
        self.url = (
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
            f"new--{self.id}"
        )
        type(self).registry[self.url] = self
        type(self).api.items.append(self)
        if type(self).mutate_source_on_save and type(self).source_to_mutate is not None:
            type(self).source_to_mutate.blocks.append(MarkdownBlock("unexpected edit"))

    @classmethod
    def from_url(cls, url: str) -> _FakeReport:
        return cls.registry[url]


@pytest.fixture
def fake_cloud() -> tuple[Any, _FakeApi]:
    api = _FakeApi()
    _FakeReport.api = api
    _FakeReport.registry = {}
    _FakeReport.serial = 0
    _FakeReport.source_to_mutate = None
    _FakeReport.mutate_source_on_save = False
    reports = SimpleNamespace(
        Report=_FakeReport,
        H1=H1,
        H2=H2,
        CalloutBlock=CalloutBlock,
        MarkdownBlock=MarkdownBlock,
    )
    return reports, api


def _ridgeway() -> RidgewayNarrativeEvidence:
    return RidgewayNarrativeEvidence(
        resistance_change_percent=-1.6031418683724497,
        review_mask_change_percent=47.34703286028441,
    )


def _receipt(source_blocks: list[Any], *, source_commit: str = SOURCE_COMMIT):
    source_digest = _blocks_sha256(source_blocks)
    return UsefulnessNarrativeReceipt.create(
        source_commit=source_commit,
        historical_artifact_manifest_sha256=HISTORICAL_ARTIFACT_MANIFEST_SHA256,
        historical_downloaded_file_manifest_sha256=(
            HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256
        ),
        historical_provider_trace_receipts_sha256="b" * 64,
        historical_run_url=HISTORICAL_RUN_URL,
        report_url=SOURCE_URL,
        section_marker="verified-source-marker",
        section_body_sha256="c" * 64,
        historical_prefix_sha256=source_digest,
        original_block_count=len(source_blocks),
        final_block_count=len(source_blocks),
    )


def _plan(source_blocks: list[Any]) -> article.HumanReadableReportPlan:
    source = article.HumanReadableSourceSnapshot.from_verified_receipt(
        _receipt(source_blocks),
        ridgeway=_ridgeway(),
    )
    return article.HumanReadableReportPlan.create(source)


def _source_report(source_blocks: list[Any]) -> _FakeReport:
    return _FakeReport.retained(
        title=SOURCE_TITLE,
        description="hash-verified historical evidence",
        blocks=source_blocks,
        report_id=SOURCE_ID,
        url=SOURCE_URL,
    )


def test_article_leads_with_a_plain_decision_story_and_keeps_hashes_last() -> None:
    source_blocks = [MarkdownBlock("historical")]
    plan = _plan(source_blocks)
    copy = "\n".join(section.markdown for section in plan.article.sections)

    assert plan.article.title == "Passing the model is not the same as being ready to build"
    assert "$400,000" in plan.article.summary
    assert "$500,000" in plan.article.summary
    assert "47–66%" in plan.article.summary
    assert "This is not observed animal movement" in plan.article.summary
    assert "2.37%" in plan.article.summary
    assert "25% target" in plan.article.summary
    assert "simpler checklist remains the default" in plan.article.summary
    assert "All 12 synthetic scenarios completed" in copy
    assert "no required connectivity check failed" in copy
    assert (
        "Modeled difficulty connecting habitat areas (effective resistance) improved 1.60%"
        in copy
    )
    assert "47.35% to 65.65%" in copy
    assert "authored 15% trigger" in copy
    assert "carry forward for field and feasibility review—not approval" in copy
    assert "12/12" not in copy
    assert "rules passed" not in copy
    assert "safe enough" not in copy
    assert "does not predict crossing use" in copy
    assert plan.article.sections[-1].section_id == "audit-trail"
    assert plan.source.historical_report_blocks_sha256 in plan.article.sections[-1].markdown


def test_plan_bytes_are_canonical_and_tampering_is_rejected() -> None:
    plan = _plan([MarkdownBlock("historical")])
    payload = article.canonical_report_plan_bytes(plan)
    assert article.validate_report_plan_bytes(payload) == plan
    with pytest.raises(ValueError, match="noncanonical"):
        article.validate_report_plan_bytes(payload.rstrip())


def test_new_report_is_created_without_saving_the_historical_report(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source_blocks = [MarkdownBlock("historical"), H2("retained result")]
    source_report = _source_report(source_blocks)
    plan = _plan(source_blocks)
    reads = 0

    def source_reader():
        nonlocal reads
        reads += 1
        return _receipt(source_blocks), _ridgeway()

    publisher = article.VerifiedHumanReadableReportPublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        api=api,
        reports_module=reports,
        source_reader=source_reader,
    )
    receipt = publisher.publish(plan)

    assert receipt.status == "CREATED"
    assert receipt.report_url != SOURCE_URL
    assert receipt.source_report_preserved is True
    assert receipt.new_wandb_run_created is False
    assert receipt.new_artifact_created is False
    assert reads == 2
    assert source_report.saved == 0
    assert source_report.blocks == source_blocks
    destinations = [item for item in api.items if item.title == article.REPORT_TITLE]
    assert len(destinations) == 1
    assert destinations[0].saved == 1


def test_exact_existing_derivative_is_reused_without_any_save(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source_blocks = [MarkdownBlock("historical")]
    source_report = _source_report(source_blocks)
    plan = _plan(source_blocks)
    destination = _FakeReport.retained(
        title=article.REPORT_TITLE,
        description=article.report_description(plan),
        blocks=article.render_report_blocks(reports, plan.article),
        report_id="reader-report-1",
        url=(
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
            "reader--reader-report-1"
        ),
    )
    publisher = article.VerifiedHumanReadableReportPublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        api=api,
        reports_module=reports,
        source_reader=lambda: (_receipt(source_blocks), _ridgeway()),
    )

    receipt = publisher.publish(plan)

    assert receipt.status == "REUSED"
    assert receipt.report_id == "reader-report-1"
    assert destination.saved == 0
    assert source_report.saved == 0


def test_same_title_with_another_source_fails_without_save(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source_blocks = [MarkdownBlock("historical")]
    source_report = _source_report(source_blocks)
    occupied = _FakeReport.retained(
        title=article.REPORT_TITLE,
        description="unrelated",
        blocks=[MarkdownBlock("other")],
        report_id="occupied",
        url=(
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/occupied"
        ),
    )
    plan = _plan(source_blocks)
    publisher = article.VerifiedHumanReadableReportPublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        api=api,
        reports_module=reports,
        source_reader=lambda: (_receipt(source_blocks), _ridgeway()),
    )

    with pytest.raises(PermissionError, match="title is occupied"):
        publisher.publish(plan)
    assert source_report.saved == 0
    assert occupied.saved == 0


def test_source_drift_before_creation_blocks_without_save(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source_blocks = [MarkdownBlock("historical")]
    source_report = _source_report(source_blocks)
    plan = _plan(source_blocks)
    changed_blocks = [MarkdownBlock("changed")]
    calls = 0

    def changing_reader():
        nonlocal calls
        calls += 1
        current = source_blocks if calls == 1 else changed_blocks
        return _receipt(current), _ridgeway()

    publisher = article.VerifiedHumanReadableReportPublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        api=api,
        reports_module=reports,
        source_reader=changing_reader,
    )
    with pytest.raises(RuntimeError, match="source changed before"):
        publisher.publish(plan)
    assert source_report.saved == 0
    assert all(item.title != article.REPORT_TITLE for item in api.items)


def test_source_report_mutation_during_save_prevents_success_receipt(
    fake_cloud: tuple[Any, _FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    reports, api = fake_cloud
    source_blocks = [MarkdownBlock("historical")]
    source_report = _source_report(source_blocks)
    _FakeReport.source_to_mutate = source_report
    _FakeReport.mutate_source_on_save = True
    plan = _plan(source_blocks)
    publisher = article.VerifiedHumanReadableReportPublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        api=api,
        reports_module=reports,
        source_reader=lambda: (_receipt(source_blocks), _ridgeway()),
    )
    monkeypatch.setattr(article, "REPORT_READBACK_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="failed exact readback"):
        publisher.publish(plan)


def test_explicit_authorization_and_exact_project_are_required(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    kwargs = {
        "entity": "wandb",
        "project": "ecocorridor-scenario-studio",
        "web_base_url": "https://wandb.ai",
        "api": api,
        "reports_module": reports,
        "source_reader": lambda: (_receipt([MarkdownBlock("source")]), _ridgeway()),
    }
    with pytest.raises(PermissionError, match="explicit authorization"):
        article.VerifiedHumanReadableReportPublisher(
            **kwargs,
            authorize_new_report=False,
        )
    with pytest.raises(PermissionError, match="bound to the EcoCorridor"):
        article.VerifiedHumanReadableReportPublisher(
            **{**kwargs, "project": "another-project"},
            authorize_new_report=True,
        )
