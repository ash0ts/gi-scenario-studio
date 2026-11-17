from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ecocorridor_core.review_brief_evaluation import (
    REPORT_MEDIA_AUTHORITY_COPY,
    ProviderExecutionEvidence,
    ReviewBriefArtifactReceipt,
    _validate_provider_trace_identity_uniqueness,
)

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "review_brief_provenance_publisher",
    ROOT / "scripts" / "publish_ecocorridor_review_brief_report.py",
)
assert _SPEC is not None and _SPEC.loader is not None
publisher = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = publisher
_SPEC.loader.exec_module(publisher)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provider(*, root_call_id: str, trace_id: str, trace_url: str) -> ProviderExecutionEvidence:
    return ProviderExecutionEvidence(
        provider="provider",
        model="model-1",
        provider_call_id=f"sha256:{_sha(root_call_id)}",
        provider_call_identity_kind="WEAVE_GENERATION_CALL_ID",
        provider_response_sha256=_sha(f"response:{root_call_id}"),
        weave_call_id=root_call_id,
        weave_trace_id=trace_id,
        weave_trace_url=trace_url,
        trace_status="VERIFIED",
        trace_readback_verified=True,
        prompt_tokens=11,
        completion_tokens=7,
        latency_ms=13,
    )


def test_root_call_permalink_must_end_in_the_exact_root_call_id() -> None:
    root_call_id = "019fd920-0c1a-7fb7-a3f8-21ecd827c2a5"
    url = f"https://wandb.ai/entity/project/weave/traces/{root_call_id}"
    assert _provider(root_call_id=root_call_id, trace_id="trace-1", trace_url=url)

    with pytest.raises(ValueError, match="differs from its exact Weave root call"):
        _provider(
            root_call_id="019fd920-0c1a-7fb7-a3f8-21ecd827c2a6",
            trace_id="trace-2",
            trace_url=url,
        )
    with pytest.raises(ValueError, match="differs from its exact Weave root call"):
        _provider(
            root_call_id="opaque-call-one",
            trace_id="trace-3",
            trace_url="https://wandb.ai/entity/project/weave/r/call/opaque-call-two",
        )


def test_campaign_provider_rows_require_unique_root_calls_and_trace_ids() -> None:
    def record(call_id: str, trace_id: str) -> Any:
        provider = SimpleNamespace(weave_call_id=call_id, weave_trace_id=trace_id)
        return SimpleNamespace(execution=SimpleNamespace(provider_execution=provider))

    _validate_provider_trace_identity_uniqueness(
        [record("call-1", "trace-1"), record("call-2", "trace-2")]
    )
    with pytest.raises(ValueError, match="reuses a provider Weave root call"):
        _validate_provider_trace_identity_uniqueness(
            [record("call-1", "trace-1"), record("call-1", "trace-2")]
        )
    with pytest.raises(ValueError, match="reuses a provider Weave trace"):
        _validate_provider_trace_identity_uniqueness(
            [record("call-1", "trace-1"), record("call-2", "trace-1")]
        )


class _ReportsBlocks:
    @staticmethod
    def Runset(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="runset", **kwargs)

    @staticmethod
    def MarkdownBlock(*, text: str) -> Any:
        return SimpleNamespace(kind="markdown", text=text)

    @staticmethod
    def H1(text: str) -> Any:
        return SimpleNamespace(kind="h1", value=text)

    @staticmethod
    def H2(text: str) -> Any:
        return SimpleNamespace(kind="h2", value=text)

    @staticmethod
    def CalloutBlock(*, text: str) -> Any:
        return SimpleNamespace(kind="callout", text=text)

    @staticmethod
    def ScalarChart(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="scalar", **kwargs)

    @staticmethod
    def LinePlot(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="line", **kwargs)

    @staticmethod
    def PanelGrid(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="grid", **kwargs)

    @staticmethod
    def WeavePanelSummaryTable(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="table", **kwargs)

    @staticmethod
    def Layout(**kwargs: Any) -> Any:
        return SimpleNamespace(kind="layout", **kwargs)


def _plan(*, video_rendered: bool = False) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "campaign_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "plan_sha256": "c" * 64,
        "artifact_manifest_sha256": "d" * 64,
        "campaign_wandb_run_id": "campaign-run-1",
        "campaign_wandb_run_url": "https://wandb.example/entity/project/runs/campaign-run-1",
        "wandb_projection": {"run_id": "campaign-run-1"},
        "method_count": 3,
        "case_count": 14,
        "record_count": 42,
        "provider_conversation_count": 22,
        "pre_provider_block_count": 6,
        "video_rendered": video_rendered,
        "default_method_decision": {
            "decision": "KEEP_DETERMINISTIC",
            "candidate_status": "EXPERIMENTAL",
        },
        "report_copy": {
            "boundary": "Synthetic provider-backed evaluation.",
            "why_useful": "Tests exact evidence handling.",
            "media_authority": REPORT_MEDIA_AUTHORITY_COPY,
            "limitations": "Not ecological validation.",
        },
    }
    if video_rendered:
        plan["validated_media"] = {"media_manifest_sha256": "e" * 64}
    return plan


def _receipt(plan: dict[str, Any]) -> ReviewBriefArtifactReceipt:
    return ReviewBriefArtifactReceipt(
        campaign_sha256=plan["campaign_sha256"],
        dataset_sha256=plan["dataset_sha256"],
        run_id=plan["campaign_wandb_run_id"],
        run_url=plan["campaign_wandb_run_url"],
        artifact_ref="entity/project/review-brief:v1",
        artifact_url="https://wandb.example/entity/project/artifacts/review-brief/v1",
        artifact_manifest_sha256=plan["artifact_manifest_sha256"],
        provider_readback_verified=True,
    )


def test_report_section_labels_media_as_presentation_copy_and_marks_all_digests() -> None:
    plan = _plan()
    blocks = publisher.build_report_section_blocks(
        _ReportsBlocks,
        plan,
        _receipt(plan),
        entity="entity",
        project="project",
    )
    sections = publisher._owned_report_sections(blocks)
    assert len(sections) == 1
    marker, body = sections[0]
    body_sha256 = publisher._report_section_body_sha256(body)
    assert marker in [getattr(block, "text", None) for block in blocks]
    assert f"campaign={plan['campaign_sha256']}" in marker
    assert f"plan={plan['plan_sha256']}" in marker
    assert f"media={publisher.NO_MEDIA_MANIFEST_SHA256}" in marker
    assert f"body={body_sha256}" in marker
    article_text = "\n".join(
        str(block.text) for block in blocks if isinstance(getattr(block, "text", None), str)
    )
    assert REPORT_MEDIA_AUTHORITY_COPY in article_text
    assert "presentation copies" in article_text
    assert "byte authority" in article_text


def test_report_semantic_hash_survives_documented_wandb_roundtrip_normalization() -> None:
    panel_grid_type = type("PanelGrid", (), {})
    expected = panel_grid_type()
    expected._spec = {
        "panels": [
            {
                "metric": "review_brief/native_read_only_agent/passed_case_count",
                "table_name": "review_brief_method_results",
                "layout": {"w": 24, "h": 16, "x": 0, "y": 0},
            }
        ],
        "runsets": [
            {
                "filters": (
                    "ID = 'campaign-run-1' and Config('campaign_sha256') = '" + "a" * 64 + "'"
                ),
                "order": [{"ascending": False, "name": "CreatedTimestamp"}],
            }
        ],
    }
    observed = panel_grid_type()
    observed._spec = {
        "panels": [
            {
                "metric": {"name": "review_brief/native_read_only_agent/passed_case_count"},
                "table_name": "review_brief_method_results",
                "layout": {"w": 8, "h": 6, "x": 16, "y": 6},
            }
        ],
        "runsets": [
            {
                "filters": (
                    "Metric(\"ID\") == 'campaign-run-1' and "
                    'Config("campaign_sha256") == \'' + "a" * 64 + "'"
                ),
                "order": [{"ascending": False, "name": {"name": "CreatedTimestamp"}}],
            }
        ],
    }

    expected_raw_digest = publisher._report_section_body_sha256([expected])
    assert publisher._report_section_body_sha256([observed]) != expected_raw_digest
    expected_semantic_digest = publisher._report_section_semantic_sha256([expected])
    assert publisher._report_section_semantic_sha256([observed]) == expected_semantic_digest

    observed._spec["panels"][0]["metric"]["name"] = "review_brief/other_metric"
    assert publisher._report_section_semantic_sha256([observed]) != expected_semantic_digest


def test_report_semantic_projection_is_scoped_to_panelgrid_service_paths() -> None:
    unrelated_type = type("OtherBlock", (), {})
    first = unrelated_type()
    first._spec = {"layout": {"w": 24}, "payload": {"name": "first"}}
    second = unrelated_type()
    second._spec = {"layout": {"w": 8}, "payload": {"name": "first"}}
    assert publisher._report_section_semantic_sha256(
        [first]
    ) != publisher._report_section_semantic_sha256([second])

    third = unrelated_type()
    third._spec = {"layout": {"w": 24}, "payload": "first"}
    assert publisher._report_section_semantic_sha256(
        [first]
    ) != publisher._report_section_semantic_sha256([third])


def test_existing_semantically_equivalent_panelgrid_section_reuses_raw_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(video_rendered=True)
    panel_grid_type = type("PanelGrid", (), {})
    authored = panel_grid_type()
    authored._spec = {
        "panels": [
            {
                "metric": "review_brief/native_read_only_agent/passed_case_count",
                "layout": {"w": 24, "h": 16, "x": 0, "y": 0},
            }
        ],
        "runsets": [
            {
                "filters": "ID = 'campaign-run-1' and Config('campaign_sha256') = '"
                + "a" * 64
                + "'",
                "order": [{"ascending": False, "name": "CreatedTimestamp"}],
            }
        ],
    }
    readback = panel_grid_type()
    readback._spec = copy.deepcopy(authored._spec)
    readback._spec["panels"][0]["metric"] = {
        "name": "review_brief/native_read_only_agent/passed_case_count"
    }
    readback._spec["panels"][0]["layout"] = {"w": 8, "h": 6, "x": 16, "y": 6}
    readback._spec["runsets"][0]["filters"] = (
        'Metric("ID") == \'campaign-run-1\' and Config("campaign_sha256") == \'' + "a" * 64 + "'"
    )
    readback._spec["runsets"][0]["order"][0]["name"] = {"name": "CreatedTimestamp"}
    marker = publisher._report_section_marker(
        plan,
        body_sha256=publisher._report_section_body_sha256([authored]),
    )
    authored_section = [
        SimpleNamespace(text=marker),
        authored,
        SimpleNamespace(text=publisher.SECTION_END),
    ]
    existing_section = [
        SimpleNamespace(text=marker),
        readback,
        SimpleNamespace(text=publisher.SECTION_END),
    ]
    monkeypatch.setattr(
        publisher,
        "build_report_section_blocks",
        lambda *args, **kwargs: authored_section,
    )
    state: dict[str, Any] = {"blocks": existing_section}
    adapter = _adapter_with_report_state(state)

    adapter.upsert_owned_report_section(
        report_url="https://wandb.example/entity/project/reports/existing--id",
        plan=plan,
        receipt=_receipt(plan),
    )

    assert publisher._owned_section_markers(state["blocks"]) == [marker]


def test_report_filter_semantics_fail_closed_on_changed_or_unsupported_clause() -> None:
    expected = publisher._normalize_report_filter(
        "ID = 'campaign-run-1' and Config('campaign_sha256') = '" + "a" * 64 + "'"
    )
    assert expected == publisher._normalize_report_filter(
        'Metric("ID") == \'campaign-run-1\' and Config("campaign_sha256") == \'' + "a" * 64 + "'"
    )
    assert expected != publisher._normalize_report_filter(
        'Metric("ID") == \'another-run\' and Config("campaign_sha256") == \'' + "a" * 64 + "'"
    )
    with pytest.raises(ValueError, match="unsupported semantics"):
        publisher._normalize_report_filter("ID = 'campaign-run-1' or true")


class _StoredReport:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.entity = "entity"
        self.project = "project"
        self.url = "https://wandb.example/entity/project/reports/existing--id"
        self.blocks = list(state.get("blocks", []))

    def save(self) -> None:
        self._state["blocks"] = list(self.blocks)
        self._state["saved"] = True


def _adapter_with_report_state(state: dict[str, Any]) -> publisher.WandbSdkReviewBriefAdapter:
    class ReportApi:
        @staticmethod
        def from_url(_url: str) -> Any:
            state["read_count"] = int(state.get("read_count", 0)) + 1
            if state.get("unsupported") and state["read_count"] > 1:
                raise NotImplementedError("readback unavailable")
            report = _StoredReport(state)
            if state.get("stale_readbacks", 0) and state["read_count"] > 1:
                state["stale_readbacks"] -= 1
                report.blocks = list(state.get("before_save_blocks", []))
            if state.get("duplicate_on_readback") and state["read_count"] > 1:
                report.blocks = [*report.blocks, *report.blocks]
            if state.get("tamper_body_on_readback") and state["read_count"] > 1:
                report.blocks = [
                    SimpleNamespace(text="tampered article body")
                    if getattr(block, "text", None) == "article body"
                    else block
                    for block in report.blocks
                ]
            return report

    return publisher.WandbSdkReviewBriefAdapter(
        entity="entity",
        project="project",
        api_base_url="https://api.wandb.example",
        web_base_url="https://wandb.example",
        authorize_synthetic_public_upload=True,
        wandb_module=SimpleNamespace(),
        reports_module=SimpleNamespace(Report=ReportApi),
    )


def _owned(plan: dict[str, Any]) -> list[Any]:
    body = [SimpleNamespace(text="article body")]
    marker = publisher._report_section_marker(
        plan,
        body_sha256=publisher._report_section_body_sha256(body),
    )
    return [SimpleNamespace(text=marker), *body, SimpleNamespace(text=publisher.SECTION_END)]


def test_report_save_is_followed_by_exact_marker_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(video_rendered=True)
    owned = _owned(plan)
    monkeypatch.setattr(publisher, "build_report_section_blocks", lambda *args, **kwargs: owned)
    history = [SimpleNamespace(text="history")]
    state: dict[str, Any] = {"blocks": history, "before_save_blocks": history}
    adapter = _adapter_with_report_state(state)

    observed = adapter.upsert_owned_report_section(
        report_url="https://wandb.example/entity/project/reports/existing--id",
        plan=plan,
        receipt=_receipt(plan),
    )

    assert observed == "https://wandb.example/entity/project/reports/existing--id"
    assert state["saved"] is True
    assert state["read_count"] == 2
    assert publisher._owned_section_markers(state["blocks"]) == [
        publisher._owned_section_markers(owned)[0]
    ]


def test_report_readback_retries_bounded_eventual_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publisher, "REPORT_READBACK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(publisher, "REPORT_READBACK_TIMEOUT_SECONDS", 1.0)
    plan = _plan()
    owned = _owned(plan)
    monkeypatch.setattr(publisher, "build_report_section_blocks", lambda *args, **kwargs: owned)
    history = [SimpleNamespace(text="history")]
    state: dict[str, Any] = {
        "blocks": history,
        "before_save_blocks": history,
        "stale_readbacks": 1,
    }
    adapter = _adapter_with_report_state(state)

    adapter.upsert_owned_report_section(
        report_url="https://wandb.example/entity/project/reports/existing--id",
        plan=plan,
        receipt=_receipt(plan),
    )

    assert state["read_count"] == 3


@pytest.mark.parametrize(
    "failure",
    ["duplicate_on_readback", "tamper_body_on_readback", "unsupported"],
)
def test_report_readback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(publisher, "REPORT_READBACK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(publisher, "REPORT_READBACK_TIMEOUT_SECONDS", 0.0)
    plan = _plan()
    owned = _owned(plan)
    monkeypatch.setattr(publisher, "build_report_section_blocks", lambda *args, **kwargs: owned)
    state: dict[str, Any] = {"blocks": [], failure: True}
    adapter = _adapter_with_report_state(state)

    with pytest.raises((RuntimeError, NotImplementedError)):
        adapter.upsert_owned_report_section(
            report_url="https://wandb.example/entity/project/reports/existing--id",
            plan=plan,
            receipt=_receipt(plan),
        )
