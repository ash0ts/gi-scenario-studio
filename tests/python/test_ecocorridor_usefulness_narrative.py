from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from ecocorridor_core import usefulness_narrative as narrative

SOURCE_COMMIT = "a" * 40
ROOT = Path(__file__).resolve().parents[2]
REPORT_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
    "EcoCorridor-historical-search-benchmark--VmlldzoxNzY1OTU0MQ=="
)


def _load_cli() -> Any:
    path = ROOT / "scripts" / "append_ecocorridor_usefulness_narrative.py"
    spec = importlib.util.spec_from_file_location("append_usefulness_narrative", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


class _Node:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._spec = {"args": list(args), **kwargs}
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Report:
    reports: ClassVar[dict[str, _Report]] = {}

    def __init__(self, *, url: str, blocks: list[Any]) -> None:
        self.url = url
        self.entity = "wandb"
        self.project = "ecocorridor-scenario-studio"
        self.id = narrative.EXPECTED_REPORT_ID
        self.title = narrative.EXPECTED_REPORT_TITLE
        self.blocks = blocks
        self.saved = 0
        self.updated_at = "2026-08-07T12:00:00+00:00"
        self.model_reads = 0
        self.concurrent_on_second_model_read = False
        self.mutate_prefix_on_save = False
        self.mutate_body_on_save = False
        type(self).reports[url] = self

    @classmethod
    def from_url(cls, url: str, *, as_model: bool = False) -> Any:
        report = cls.reports[url]
        if as_model:
            report.model_reads += 1
            if report.concurrent_on_second_model_read and report.model_reads == 2:
                report.updated_at = "2026-08-07T12:00:01+00:00"
                report.blocks.append(_Node(text="concurrent editor block"))
            return SimpleNamespace(updated_at=report.updated_at)
        return report

    def save(self) -> _Report:
        self.saved += 1
        self.updated_at = "2026-08-07T12:00:02+00:00"
        if self.mutate_prefix_on_save:
            self.blocks[0] = _Node(text="mutated historical prefix")
        if self.mutate_body_on_save:
            for block in self.blocks:
                if getattr(block, "text", "") == "What is now implemented":
                    block.text = "mutated body"
                    block._spec["text"] = "mutated body"
                    break
        return self


class _Reports(SimpleNamespace):
    def __init__(self) -> None:
        _Report.reports.clear()
        super().__init__(
            Report=_Report,
            H1=lambda text: _Node(text=text),
            H2=lambda text: _Node(text=text),
            CalloutBlock=lambda **kwargs: _Node(**kwargs),
            MarkdownBlock=lambda **kwargs: _Node(**kwargs),
        )


def _summary() -> dict[str, Any]:
    return {
        "review_brief/record_count": narrative.HISTORICAL_RECORD_COUNT,
        "review_brief/provider_conversation_count": (
            narrative.HISTORICAL_PROVIDER_CONVERSATION_COUNT
        ),
        "review_brief/pre_provider_block_count": narrative.HISTORICAL_PRE_PROVIDER_BLOCK_COUNT,
        "review_brief/baseline_role_input_tokens": narrative.HISTORICAL_BASELINE_INPUT_TOKENS,
        "review_brief/native_role_input_tokens": narrative.HISTORICAL_NATIVE_INPUT_TOKENS,
        "review_brief/default_method_decision": narrative.HISTORICAL_DECISION,
        "review_brief/current_default_method": narrative.HISTORICAL_DEFAULT_METHOD,
        "review_brief/promotion_gate_passed": 0,
        "review_brief/status": "COMPLETE",
        "review_brief/artifact_ref": narrative.HISTORICAL_ARTIFACT_REF,
    }


class _Api:
    def __init__(self, run: Any) -> None:
        self.run_value = run
        self.paths: list[str] = []

    def run(self, path: str) -> Any:
        self.paths.append(path)
        return self.run_value

    def artifact(self, reference: str) -> Any:
        assert reference == narrative.HISTORICAL_ARTIFACT_REF
        return SimpleNamespace(
            qualified_name=narrative.HISTORICAL_ARTIFACT_REF,
            type="evaluation-bundle",
            url=narrative.HISTORICAL_ARTIFACT_URL,
            metadata={
                "schema_version": "ecocorridor-review-brief-publication/v1",
                "campaign_sha256": narrative.HISTORICAL_CAMPAIGN_SHA256,
                "dataset_sha256": narrative.HISTORICAL_DATASET_SHA256,
                "source_commit": narrative.HISTORICAL_SOURCE_COMMIT,
                "campaign_wandb_run_id": narrative.HISTORICAL_RUN_ID,
                "artifact_manifest_sha256": narrative.HISTORICAL_ARTIFACT_MANIFEST_SHA256,
                "provider_cost_basis_sha256": (
                    narrative.HISTORICAL_PROVIDER_COST_BASIS_SHA256
                ),
                "provider_backed": True,
                "video_rendered": True,
                "video_logged": True,
            },
            download=lambda *, root: root,
        )


class _Wandb:
    def __init__(self) -> None:
        self.run = SimpleNamespace(
            id=narrative.HISTORICAL_RUN_ID,
            state="finished",
            url=narrative.HISTORICAL_RUN_URL,
            config={
                "campaign_sha256": narrative.HISTORICAL_CAMPAIGN_SHA256,
                "dataset_sha256": narrative.HISTORICAL_DATASET_SHA256,
                "source_commit": narrative.HISTORICAL_SOURCE_COMMIT,
                "provider_model": narrative.HISTORICAL_MODEL,
                "default_method_decision": narrative.HISTORICAL_DECISION,
            },
            summary=_summary(),
        )
        self.api = _Api(self.run)

    def Api(self, **_kwargs: Any) -> _Api:
        return self.api

    def init(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("narrative publisher must not create a W&B run")

    def Artifact(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("narrative publisher must not create a W&B artifact")


def _historical_blocks() -> list[Any]:
    return [
        _Node(text="historical report title"),
        _Node(
            text=(
                f"{narrative.HISTORICAL_SECTION_START} "
                f"campaign={narrative.HISTORICAL_CAMPAIGN_SHA256} -->"
            )
        ),
        _Node(text="historical result remains unchanged"),
        _Node(text=narrative.HISTORICAL_SECTION_END),
    ]


def _publisher(
    wandb: _Wandb,
    reports: _Reports,
    *,
    authorized: bool = True,
) -> narrative.WandbUsefulnessNarrativePublisher:
    return narrative.WandbUsefulnessNarrativePublisher(
        entity="wandb",
        project="ecocorridor-scenario-studio",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        authorize_report_update=authorized,
        authorize_exclusive_report_update=authorized,
        wandb_module=wandb,
        reports_module=reports,
        artifact_download_verifier=lambda _path: narrative.HistoricalCampaignReadback(
            provider_trace_receipts_sha256="d" * 64
        ),
        historical_section_verifier=lambda _blocks: None,
    )


def _ridgeway() -> narrative.RidgewayNarrativeEvidence:
    return narrative.RidgewayNarrativeEvidence(
        resistance_change_percent=-1.6031418683724497,
        review_mask_change_percent=47.34703286028441,
    )


def test_narrative_is_explicitly_no_result_and_preserves_bounded_story() -> None:
    reports = _Reports()
    blocks = narrative.build_checkpoint_body(
        reports,
        source_commit=SOURCE_COMMIT,
        ridgeway=_ridgeway(),
    )
    text = "\n".join(str(getattr(block, "text", "")) for block in blocks)

    assert "NOT A USEFULNESS RESULT" in text
    assert "42 scored records" in text
    assert "22 provider-backed Weave" in text
    assert "2.37% reduction" in text
    assert "KEEP_DETERMINISTIC" in text
    assert "fixed tool sequence" in text
    assert "expected answer contract" in text
    assert "160-job plan" in text
    assert "TEST_ONLY_UNPUBLISHABLE" in text
    assert "No new animation" in text
    assert "animal movement" in text
    assert "USD 400,000" in text
    assert "47.3470%" in text
    assert SOURCE_COMMIT in text

    with pytest.raises(ValueError, match="full Git commit"):
        narrative.build_checkpoint_body(reports, source_commit="main", ridgeway=_ridgeway())


def test_first_append_preserves_history_and_exact_retry_is_idempotent() -> None:
    reports = _Reports()
    wandb = _Wandb()
    historical = _historical_blocks()
    report = _Report(url=REPORT_URL, blocks=list(historical))
    publisher = _publisher(wandb, reports)

    first = publisher.append(
        report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
    )
    first_count = len(report.blocks)
    second = publisher.append(
        report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
    )

    assert report.blocks[: len(historical)] == historical
    assert report.saved == 1
    assert first_count == len(report.blocks)
    assert first.section_marker == second.section_marker
    assert first.section_body_sha256 == second.section_body_sha256
    assert first.observed_effectiveness_result_count == 0
    assert first.corrected_campaign_result_count == 0
    assert first.new_wandb_run_created is False
    assert first.new_artifact_created is False
    assert first.new_result_media_logged is False
    assert len(narrative._checkpoint_sections(report.blocks)) == 1
    assert wandb.api.paths == [narrative.HISTORICAL_RUN_PATH] * 2


def test_historical_metric_mismatch_blocks_before_report_save() -> None:
    reports = _Reports()
    wandb = _Wandb()
    wandb.run.summary["review_brief/record_count"] = 41
    report = _Report(url=REPORT_URL, blocks=_historical_blocks())

    with pytest.raises(RuntimeError, match="records"):
        _publisher(wandb, reports).append(
            report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
        )

    assert report.saved == 0


def test_historical_section_requires_exact_marker_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = [_Node(text="retained historical body")]
    monkeypatch.setattr(
        narrative,
        "HISTORICAL_SECTION_SEMANTIC_SHA256",
        narrative._semantic_blocks_sha256(body),
    )
    exact = [
        _Node(text=narrative.HISTORICAL_SECTION_MARKER),
        *body,
        _Node(text=narrative.HISTORICAL_SECTION_END),
    ]
    narrative._require_historical_report_section(exact)

    with pytest.raises(PermissionError, match="historical campaign section"):
        narrative._require_historical_report_section([_Node(text="unrelated report")])
    with pytest.raises(PermissionError, match="historical campaign section"):
        narrative._require_historical_report_section(
            [
                _Node(text=narrative.HISTORICAL_SECTION_MARKER),
                _Node(text="tampered body"),
                _Node(text=narrative.HISTORICAL_SECTION_END),
            ]
        )
    with pytest.raises(PermissionError, match="unmatched historical end marker"):
        narrative._require_historical_report_section(
            [_Node(text=narrative.HISTORICAL_SECTION_END)]
        )


@pytest.mark.parametrize("mutation", ["prefix", "body"])
def test_readback_mutation_fails_closed(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    monkeypatch.setattr(narrative, "REPORT_READBACK_TIMEOUT_SECONDS", 0.0)
    reports = _Reports()
    wandb = _Wandb()
    report = _Report(url=REPORT_URL, blocks=_historical_blocks())
    if mutation == "prefix":
        report.mutate_prefix_on_save = True
    else:
        report.mutate_body_on_save = True

    with pytest.raises(RuntimeError, match="append-only narrative readback"):
        _publisher(wandb, reports).append(
            report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
        )


def test_concurrent_report_revision_blocks_before_save() -> None:
    reports = _Reports()
    wandb = _Wandb()
    report = _Report(url=REPORT_URL, blocks=_historical_blocks())
    report.concurrent_on_second_model_read = True

    with pytest.raises(RuntimeError, match="changed during the exclusive update window"):
        _publisher(wandb, reports).append(
            report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
        )

    assert report.saved == 0


def test_conflicting_v1_checkpoint_fails_without_rewrite() -> None:
    reports = _Reports()
    wandb = _Wandb()
    conflicting = [
        _Node(text=f"{narrative.SECTION_START} source={'b' * 40} body={'c' * 64} -->"),
        _Node(text="conflicting checkpoint"),
        _Node(text=narrative.SECTION_END),
    ]
    report = _Report(url=REPORT_URL, blocks=[*_historical_blocks(), *conflicting])

    with pytest.raises(RuntimeError, match="different v1"):
        _publisher(wandb, reports).append(
            report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
        )

    assert report.saved == 0


def test_wrong_destination_and_missing_authorization_are_rejected() -> None:
    reports = _Reports()
    wandb = _Wandb()
    with pytest.raises(PermissionError, match="explicit authorization"):
        _publisher(wandb, reports, authorized=False)
    with pytest.raises(PermissionError, match="exclusive window"):
        narrative.WandbUsefulnessNarrativePublisher(
            entity="wandb",
            project="ecocorridor-scenario-studio",
            api_base_url="https://api.wandb.ai",
            web_base_url="https://wandb.ai",
            authorize_report_update=True,
            authorize_exclusive_report_update=False,
            wandb_module=wandb,
            reports_module=reports,
        )

    report = _Report(url=REPORT_URL, blocks=_historical_blocks())
    report.project = "different-project"
    with pytest.raises(PermissionError, match="Report identity"):
        _publisher(wandb, reports).append(
            report_url=REPORT_URL, source_commit=SOURCE_COMMIT, ridgeway=_ridgeway()
        )

    with pytest.raises(PermissionError, match="existing article project"):
        narrative.WandbUsefulnessNarrativePublisher(
            entity="wandb",
            project="different-project",
            api_base_url="https://api.wandb.ai",
            web_base_url="https://wandb.ai",
            authorize_report_update=True,
            authorize_exclusive_report_update=True,
            wandb_module=wandb,
            reports_module=reports,
        )


def test_receipt_exact_local_readback(tmp_path: Path) -> None:
    reports = _Reports()
    wandb = _Wandb()
    _Report(url=REPORT_URL, blocks=_historical_blocks())
    receipt = _publisher(wandb, reports).append(
        report_url=REPORT_URL,
        source_commit=SOURCE_COMMIT,
        ridgeway=_ridgeway(),
    )
    output = tmp_path / "receipt.json"
    narrative.atomic_write_receipt(output, receipt)

    assert narrative.UsefulnessNarrativeReceipt.model_validate_json(output.read_bytes()) == receipt


def test_ridgeway_values_require_checksum_classified_committed_projection(
    tmp_path: Path,
) -> None:
    evidence = narrative.load_ridgeway_narrative_evidence(ROOT)
    assert evidence.pass_count == evidence.required_count == 12
    assert evidence.rule_status == "review"
    assert evidence.review_mask_change_percent == pytest.approx(47.34703286028441)

    destination = tmp_path / "repository"
    (destination / "benchmarks/ecocorridor/ridgeway").mkdir(parents=True)
    shutil.copy2(
        ROOT / "benchmarks/ecocorridor/data-manifest.json",
        destination / "benchmarks/ecocorridor/data-manifest.json",
    )
    shutil.copy2(
        ROOT / "benchmarks/ecocorridor/ridgeway/ui_projection.json",
        destination / "benchmarks/ecocorridor/ridgeway/ui_projection.json",
    )
    shutil.copy2(
        ROOT / "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
        destination / "benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json",
    )
    projection = destination / "benchmarks/ecocorridor/ridgeway/ui_projection.json"
    projection.write_bytes(projection.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="projection digest differs"):
        narrative.load_ridgeway_narrative_evidence(destination)


def test_historical_artifact_requires_all_verified_trace_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Dataset:
        dataset_sha256 = narrative.HISTORICAL_DATASET_SHA256

        @staticmethod
        def model_dump(*, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {"dataset_sha256": narrative.HISTORICAL_DATASET_SHA256}

    class _Campaign:
        campaign_sha256 = narrative.HISTORICAL_CAMPAIGN_SHA256
        dataset = _Dataset()
        source_repository = narrative.SOURCE_REPOSITORY
        source_commit = narrative.HISTORICAL_SOURCE_COMMIT
        campaign_wandb_run_id = narrative.HISTORICAL_RUN_ID
        campaign_wandb_run_url = narrative.HISTORICAL_RUN_URL
        provider_conversation_count = narrative.HISTORICAL_PROVIDER_CONVERSATION_COUNT
        pre_provider_block_count = narrative.HISTORICAL_PRE_PROVIDER_BLOCK_COUNT
        default_method_decision = SimpleNamespace(
            baseline_role_input_tokens=narrative.HISTORICAL_BASELINE_INPUT_TOKENS,
            native_role_input_tokens=narrative.HISTORICAL_NATIVE_INPUT_TOKENS,
            decision=narrative.HISTORICAL_DECISION,
            current_default_method=narrative.HISTORICAL_DEFAULT_METHOD,
            promotion_gate_passed=False,
        )

        def __init__(self) -> None:
            self.records = []
            for index in range(narrative.HISTORICAL_PROVIDER_CONVERSATION_COUNT):
                provider = SimpleNamespace(
                    provider_call_id=f"provider-{index}",
                    provider_response_sha256=f"{index + 1:064x}",
                    weave_call_id=f"weave-call-{index}",
                    weave_trace_id=f"weave-trace-{index}",
                    weave_trace_url=f"https://wandb.ai/trace/{index}",
                    trace_status="VERIFIED",
                    trace_readback_verified=True,
                )
                self.records.append(
                    SimpleNamespace(
                        case_id=f"case-{index}",
                        method_id="native_read_only_agent",
                        execution=SimpleNamespace(provider_execution=provider),
                    )
                )
            self.records.extend(
                SimpleNamespace(
                    case_id=f"blocked-{index}",
                    method_id="deterministic_exhaustive",
                    execution=SimpleNamespace(provider_execution=None),
                )
                for index in range(
                    narrative.HISTORICAL_RECORD_COUNT
                    - narrative.HISTORICAL_PROVIDER_CONVERSATION_COUNT
                )
            )

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {"campaign": narrative.HISTORICAL_CAMPAIGN_SHA256}

    campaign = _Campaign()
    campaign_bytes = (
        json.dumps(campaign.model_dump(mode="json"), indent=2, sort_keys=True).encode()
        + b"\n"
    )
    dataset_bytes = (
        json.dumps(campaign.dataset.model_dump(mode="json"), indent=2, sort_keys=True).encode()
        + b"\n"
    )
    for name in narrative.HISTORICAL_ARTIFACT_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (tmp_path / "review-brief-campaign.json").write_bytes(campaign_bytes)
    (tmp_path / "review-brief-dataset.json").write_bytes(dataset_bytes)
    monkeypatch.setattr(
        narrative, "HISTORICAL_CAMPAIGN_FILE_SHA256", hashlib.sha256(campaign_bytes).hexdigest()
    )
    monkeypatch.setattr(
        narrative, "HISTORICAL_DATASET_FILE_SHA256", hashlib.sha256(dataset_bytes).hexdigest()
    )
    monkeypatch.setattr(
        narrative,
        "ReviewBriefCampaignArtifact",
        SimpleNamespace(model_validate_json=lambda _payload: campaign),
    )
    monkeypatch.setattr(narrative, "verify_review_brief_campaign", lambda value: value)

    receipt = narrative.verify_downloaded_historical_artifact(tmp_path)
    assert receipt.provider_trace_receipt_count == 22

    campaign.records[0].execution.provider_execution.trace_readback_verified = False
    with pytest.raises(ValueError, match="trace receipt is incomplete"):
        narrative.verify_downloaded_historical_artifact(tmp_path)


class _SourceVerifier:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def verify(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(source_commit=kwargs["source_commit"])


def test_source_gate_requires_authoritative_remote_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): SOURCE_COMMIT,
        ("branch", "--show-current"): cli.EXPECTED_BRANCH,
    }
    monkeypatch.setattr(cli, "_git", lambda _root, *arguments: values[arguments])
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    verifier = _SourceVerifier()
    assert cli.require_publishable_source(ROOT, source_verifier=verifier) == SOURCE_COMMIT
    assert verifier.calls[0]["source_repository"] == cli.SOURCE_REPOSITORY_URL


def test_source_gate_rejects_remote_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        ("rev-parse", "HEAD"): SOURCE_COMMIT,
        ("branch", "--show-current"): cli.EXPECTED_BRANCH,
    }
    monkeypatch.setattr(cli, "_git", lambda _root, *arguments: values[arguments])

    with pytest.raises(PermissionError, match="current pushed upstream HEAD"):
        cli.require_publishable_source(
            ROOT,
            source_verifier=_SourceVerifier(
                error=PermissionError("package source commit is not the current pushed upstream HEAD")
            ),
        )
