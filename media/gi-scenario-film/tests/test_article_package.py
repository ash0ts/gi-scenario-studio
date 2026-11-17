from __future__ import annotations

import inspect
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = ROOT / "story" / "article.md"
MEDIA_SPEC = ROOT / "story" / "article-media.json"
BENCHMARK_MEDIA_SPEC = ROOT / "story" / "benchmark-media.json"
ANIMATION_MEDIA_SPEC = ROOT / "story" / "report-animation-media.json"
PUBLISHER = ROOT / "scripts" / "publish_wandb_report.py"


def _media_spec() -> dict[str, object]:
    return json.loads(MEDIA_SPEC.read_text(encoding="utf-8"))


def _benchmark_media_spec() -> dict[str, object]:
    return json.loads(BENCHMARK_MEDIA_SPEC.read_text(encoding="utf-8"))


def _animation_media_spec() -> dict[str, object]:
    return json.loads(ANIMATION_MEDIA_SPEC.read_text(encoding="utf-8"))


def test_article_is_timing_free_and_zero_prerequisite() -> None:
    article = ARTICLE.read_text(encoding="utf-8")

    assert article.startswith(
        "# The Stormwater Plan That Looked Like a Bargain—Until We Asked One More Question"
    )
    assert "## 00:" not in article
    assert "App Home" not in article
    assert "[Click" not in article
    assert article.index("follow the water") < article.index("SWMM is the")
    assert article.index("A plan can reduce total volume") < article.index("Pareto frontier")


def test_article_uses_the_human_builder_narrative() -> None:
    article = ARTICLE.read_text(encoding="utf-8")

    assert "looked terrific right up until we let the model rain for four hours" in article
    assert "gets copied into a slide deck and begins acquiring adjectives" in article
    assert "## SWMM is an obedient imaginary town" in article
    assert "A progress bar is a claim about the present tense" in article
    assert "It is arithmetic with a grudge" in article
    assert "## What stays useful after the release gate says no" in article
    assert "## The engineer gets the last word" in article


def test_article_preserves_the_claim_boundary() -> None:
    article = ARTICLE.read_text(encoding="utf-8")

    assert "raises the peak in two storms" in article
    assert "3.14 to 3.21 cubic feet per second" in article
    assert "4 baseline runs + 220 alternatives × 4 storms = 884 direct SWMM runs" in article
    assert (
        "Testing all 700 vectors under all four storms requires 2,800 direct SWMM runs" in article
    )
    assert "candidate to carry forward for site and feasibility review" in article
    assert "Exact calculation under stated assumptions is not the same as field truth" in article
    assert "This was **not a fresh SWMM rerun**" in article
    assert "56.8% to 92.2%" in article
    assert "35.4 percentage points" in article
    assert "−54.273% groundwater continuity error" in article
    assert '<!-- ARTICLE_CASE_STUDY key="realistic-fehraltorf/v1" -->' in article


def test_article_video_markers_match_the_declarative_media_order() -> None:
    module = runpy.run_path(str(PUBLISHER))
    media_spec = _media_spec()
    benchmark_media = _benchmark_media_spec()
    animation_media = _animation_media_spec()
    clips = media_spec["clips"]
    charts = benchmark_media["charts"]
    animations = animation_media["clips"]
    title, description, body = module["_article_parts"]()
    segments = module["_content_segments"](body, clips, charts, animations)

    assert title == media_spec["report"]["title"]
    assert description == media_spec["report"]["description"]
    assert [segment["key"] for segment in segments if segment["kind"] == "video"] == [
        clip["key"] for clip in clips
    ]
    assert [segment["key"] for segment in segments if segment["kind"] == "benchmark"] == [
        chart["key"] for chart in charts
    ]
    assert [segment["key"] for segment in segments if segment["kind"] == "animation"] == [
        animation["key"] for animation in animations
    ]
    assert len(clips) == 7
    assert len(charts) == 6
    assert len(animations) == 5

    media_segments = [
        f"{segment['kind']}:{segment['key']}"
        for segment in segments
        if segment["kind"] != "markdown"
    ]
    assert media_segments == [
        "animation:animation/incomplete_plot",
        "video:app/storm_scope",
        "animation:animation/water_ledger",
        "video:app/model_fitness",
        "video:app/bounded_opportunities",
        "video:app/verified_replay",
        "animation:animation/storm_reveals",
        "benchmark:benchmark/storm_guardrail_atlas",
        "benchmark:benchmark/guided_coverage",
        "video:app/frontier_guardrail",
        "animation:animation/decision_funnel",
        "benchmark:benchmark/exact_decision_landscape",
        "benchmark:benchmark/rule_filter",
        "benchmark:benchmark/search_efficiency",
        "animation:animation/search_release_gate",
        "benchmark:benchmark/release_gate_audit",
        "video:app/evidence_rail",
        "evaluation:evidence-guide/r4c-grounding",
        "evaluation:evidence-guide/v1",
        "evaluation:evidence-guide/v2",
        "case_study:realistic-fehraltorf/v1",
        "video:app/engineer_handoff",
    ]


def test_article_and_report_keep_evaluation_links_receipt_driven() -> None:
    article = ARTICLE.read_text(encoding="utf-8")
    publisher = PUBLISHER.read_text(encoding="utf-8")

    assert "## Then we tried to make the receipt talk" in article
    assert "## What stays useful after the release gate says no" in article
    assert "## The failed attempts stay in the record" in article
    assert "A trace can prove that an explanation ran" in article
    assert "It cannot prove that the explanation listened to the evidence" in article
    assert '<!-- ARTICLE_EVALUATION key="evidence-guide/v1" -->' in article
    assert '<!-- ARTICLE_EVALUATION key="evidence-guide/v2" -->' in article
    assert '<!-- ARTICLE_EVALUATION key="evidence-guide/r4c-grounding" -->' in article
    assert '<!-- ARTICLE_EVALUATION key="evidence-guide/v2-architecture-development" -->' not in article
    assert article.index('key="evidence-guide/r4c-grounding"') < article.index(
        'key="evidence-guide/v1"'
    )
    assert "The deterministic Evidence Rail remains available" in article
    assert "Generated explanations remain disabled" in article
    assert "No assistant answer was released" in article
    assert "practitioner usefulness" in article.lower()
    assert "no kWh or CO₂e claim" in article
    assert "failure evidence, not approval evidence" in article
    assert "assistantconversation_58cfd7fc090a352803e27990" in article
    assert "assistantconversation_d103df23fa3aed51516cce15" in article
    assert "wr.WeavePanelSummaryTable" in publisher
    assert "EVIDENCE_GUIDE_TABLE_KEY" in publisher
    assert "EVIDENCE_GUIDE_V2_TABLE_KEY" in publisher
    assert "EVIDENCE_GUIDE_AGENT_TABLE_KEY" in publisher
    assert "gi-article-report-v9-human-narrative" in publisher
    assert "_actual_publication_url" in publisher
    assert "EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION" in publisher
    assert "EVIDENCE_GUIDE_AGENT_PROBE" in publisher
    assert "### The four-case gate stopped the next study" in article
    assert "architecture denominator is zero" in article
    assert "It would be incorrect to say that the deterministic guide scientifically beat" in article


def test_real_campaign_markdown_reports_receipt_backed_failed_evidence() -> None:
    module = runpy.run_path(str(PUBLISHER))
    evaluation = module["_load_evidence_guide_evaluation"]("wandb", "gi-scenario-studio-article")

    markdown = module["_evaluation_markdown"](evaluation)

    assert "DISABLE_LIVE_GUIDE" in markdown
    assert "45/48" in markdown
    assert "18/48" in markdown
    assert "3/48" in markdown
    assert "0/48" in markdown
    assert "93.75%" in markdown
    assert "10.42%" in markdown
    assert "35.42%" in markdown
    assert "2.08%" in markdown
    assert "39.58%" in markdown
    assert "20.83%" in markdown
    assert "8/8" in markdown
    assert "6/8" in markdown
    assert "2/8" in markdown
    assert "0/8" in markdown
    assert "22,211/5,945/28,156" in markdown
    assert "23,448/30,116/53,564" in markdown
    assert "96 logical case invocations" in markdown
    assert "100% evidence-ID validity on the outputs they produced" in markdown
    assert "Practitioner usefulness was not measured" in markdown
    assert "Energy use, kWh, and CO2e were not measured" in markdown
    assert "both evaluations failed; publication is not approval" in markdown
    assert "019fd964-7ed6-7581-95ea-6ebc75a13be1" in markdown
    assert "019fd964-809f-7bc0-b147-d6ca0b8ca67a" in markdown
    assert "All 3 actual Granite turns" in markdown
    assert "Runtime-agent behavior check" in markdown
    assert "3/3 turns" in markdown
    assert "Two ALT-017 turns shared one conversation" in markdown
    assert "ALT-207 started a separate conversation" in markdown
    assert "0/3 turns" in markdown
    assert "EVIDENCE_NEXT_CHECK_SELECTION_INVALID" in markdown
    assert "EVIDENCE_GENERATED_NUMBER_BLOCKED" in markdown
    assert "EVIDENCE_SUPPORT_CLAIM_INVALID" in markdown
    assert "Not asserted; the SDK omitted the routing parent span" in markdown
    assert "assistantconversation_58cfd7fc090a352803e27990" in markdown
    assert "assistantconversation_d103df23fa3aed51516cce15" in markdown
    assert "http://" not in markdown


def test_evidence_guide_v2_canary_stop_is_explicit_and_fail_closed() -> None:
    module = runpy.run_path(str(PUBLISHER))
    evaluation = module["_load_evidence_guide_v2_evaluation"]()

    assert evaluation["state"] == "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
    assert evaluation["decision"] == "DISABLE_LIVE_GUIDE"
    assert evaluation["automated_explanation_quality"] == "not_measured"
    assert evaluation["practitioner_usefulness_status"] == "not_measured"
    assert evaluation["visible_status"] == "admission_failed_before_architecture"
    assert evaluation["hidden_status"] == "AWAITING_PROTECTED_LOCKBOX"
    assert evaluation["hidden_available"] is False
    assert evaluation["hidden_attempt_rows_published"] is False
    assert len(evaluation["links"]) == 6
    assert len(evaluation["artifact_references"]) == 8
    assert [item["logical_name"] for item in evaluation["artifact_files"]] == [
        "evaluation/v2/campaign-plan.json",
        "evaluation/v2/preregistration.json",
        "evaluation/v2/hidden-lockbox-receipt.json",
        "evaluation/v2/protocol-amendment-r2.json",
        "evaluation/v2/excluded-r2-readback-incident.json",
        "evaluation/v2/terminal-disposition.json",
        "evaluation/v2/trace-canary.json",
        "evaluation/v2/invalidated-preflight-audit.json",
        "evaluation/v2/article-summary.json",
        "evaluation/v2/release-gate.json",
        "evaluation/v2/wandb-publication.json",
    ]
    assert evaluation["invalidated_preflight_audit"]["capability_denominator_attempts"] == 0
    assert evaluation["invalidated_preflight_audit"]["invalidated_preflight_count"] == 3
    assert evaluation["invalidated_preflight_audit"]["invalidated_provider_attempts"] == 8
    assert [row["architecture_id"] for row in evaluation["rows"]] == [
        "D0",
        "D1",
        "A0",
        "A1",
        "A2",
        "A3",
        "A4",
    ]
    rows = {row["architecture_id"]: row for row in evaluation["rows"]}
    assert rows["D0"]["visible_status"] == "canary_passed_non_scientific"
    assert rows["D0"]["visible_attempts"] == 2
    assert rows["D1"]["visible_status"] == "canary_passed_non_scientific"
    assert rows["D1"]["visible_attempts"] == 1
    assert rows["A1"]["visible_status"] == "admission_canary_failed"
    assert rows["A1"]["visible_attempts"] == 1
    assert rows["A1"]["visible_failures"] == 1
    assert "explanation_plan_mismatch" in rows["A1"]["failure_codes"]
    assert all(
        rows[architecture]["visible_status"] == "not_run"
        for architecture in ("A0", "A2", "A3", "A4")
    )
    assert evaluation["budget"] == {
        "logical_provider_invocations": 5964,
        "worst_case_transport_attempts": 11638,
        "worst_case_input_tokens": 23482893,
        "worst_case_output_tokens": 1710848,
        "worst_case_cost_usd": "3.277180",
    }
    assert evaluation["confirmatory_architecture_envelopes"]["A1"]["admissible"] is True
    assert evaluation["confirmatory_architecture_envelopes"]["A2"] == {
        "admissible": False,
        "logical_provider_invocations": 8671,
        "worst_case_cost_usd": "3.900390",
        "worst_case_input_tokens": 34570765,
        "worst_case_output_tokens": 2403840,
        "worst_case_transport_attempts": 17052,
    }
    assert (
        evaluation["confirmatory_architecture_envelopes"]["A3"]
        == evaluation["confirmatory_architecture_envelopes"]["A2"]
    )
    terminal = evaluation["terminal_disposition"]
    assert terminal["stopping_reason"] == "TRACE_CANARY_SEMANTIC_CONTRACT_FAILED"
    assert terminal["blocked_before_stage"] == "architecture"
    assert terminal["scientific_denominator_attempts"] == 0
    assert terminal["architecture_denominator_attempts"] == 0
    assert terminal["provider_answer_released"] is False
    assert len(evaluation["canary_rows"]) == 4
    a1_canary = next(
        row for row in evaluation["canary_rows"] if row["architecture_id"] == "A1"
    )
    assert a1_canary["observed_plan_id"] == "recorded_rules_fail_v1"
    assert a1_canary["answer_released"] is False
    assert a1_canary["scientific_denominator_included"] is False
    markdown = module["_evidence_guide_v2_markdown"](evaluation)
    assert evaluation["state"] in markdown
    assert "DISABLE_LIVE_GUIDE" in markdown
    assert "AWAITING_PROTECTED_LOCKBOX" in markdown
    assert "automated_explanation_quality`: **not_measured**" in markdown
    assert "practitioner_usefulness`: **not_measured**" in markdown
    assert "released no model answer" in markdown
    assert "architecture, prompt, validation, judge, hidden, and soak stages were not run" in markdown
    assert "5,964 logical provider invocations" in markdown
    assert "11,638 transport attempts" in markdown
    assert "Only admissible preregistered branch ceiling (`A1`)" in markdown
    assert "`A2` and `A3` are preregistered inadmissible" in markdown
    assert "17,052/17,052 transport attempts" in markdown
    assert "34,570,765/34,570,765 input tokens" in markdown
    assert "2,403,840/2,403,840 output tokens" in markdown
    assert "runner stops before validation" in markdown
    assert "Preregistered maximal budget" not in markdown
    assert "$3.277180" in markdown

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    table = module["_evidence_guide_v2_table"](
        FakeWandb, {"evidence_guide_v2_evaluation": evaluation}
    )
    assert len(table.data) == 7
    assert "production_eligible" in table.columns
    assert "visible_failures" in table.columns
    assert "hidden_failures" in table.columns
    assert "automated_explanation_quality" in table.columns
    assert "practitioner_usefulness" in table.columns


def test_architecture_development_report_consumes_only_a_complete_hash_pinned_package(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    loader = module["_load_evidence_guide_v2_architecture_development"]
    globals_ = loader.__globals__
    root = tmp_path / "evidence-guide-v2-architecture-dev-r1"
    root.mkdir()
    summary_path = root / "article-summary.json"
    table_path = root / "article-table.json"
    media_path = root / "article-media.png"
    receipt_path = root / "wandb-publication.json"
    globals_["EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY"] = summary_path
    globals_["EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE"] = table_path
    globals_["EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA"] = media_path
    globals_["EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT"] = receipt_path

    unavailable = loader()
    assert unavailable["available"] is False
    assert unavailable["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert unavailable["validation_status"] == "not_consumed"
    assert unavailable["rows"] == []
    unavailable_markdown = module[
        "_evidence_guide_v2_architecture_development_markdown"
    ](unavailable)
    assert "No architecture-development result is displayed" in unavailable_markdown
    assert "DISABLE_LIVE_GUIDE" in unavailable_markdown

    columns = [
        "stage",
        "model_id",
        "architecture_id",
        "attempt_count",
        "generated_invalid_count",
        "released_invalid_count",
        "valid_generation_rate",
        "scenario_count",
        "scenario_ever_failed_count",
        "causal_success_rate",
        "trace_acknowledgement_rate",
        "logical_provider_calls",
        "transport_attempts",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    ]
    rows = [
        {
            "stage": "synthetic-development",
            "model_id": "fixture/model",
            "architecture_id": "A1",
            "attempt_count": 12,
            "generated_invalid_count": 3,
            "released_invalid_count": 0,
            "valid_generation_rate": 0.75,
            "scenario_count": 6,
            "scenario_ever_failed_count": 2,
            "causal_success_rate": 0.5,
            "trace_acknowledgement_rate": 1.0,
            "logical_provider_calls": 9,
            "transport_attempts": 10,
            "input_tokens": 1200,
            "output_tokens": 240,
            "cost_usd": "0.0042",
        }
    ]
    table_body = {
        "schema_version": "1.0",
        "study_version": "evidence-guide-v2-architecture-development-r1",
        "columns": columns,
        "rows": rows,
    }
    table = {**table_body, "table_sha256": module["_json_sha256"](table_body)}
    table_path.write_text(json.dumps(table, sort_keys=True), encoding="utf-8")
    media_path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-test-media")
    summary_body = {
        "schema_version": "1.0",
        "study_version": "evidence-guide-v2-architecture-development-r1",
        "state": "EXPLORATORY_COMPLETE_LIVE_GUIDE_UNCHANGED_DISABLED",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "headline": "Synthetic fixture: the validator caught three generated failures.",
        "plain_language_finding": (
            "This fixture proves that report copy and values come from the summary."
        ),
        "claim_boundary": "Exploratory automated conformance only.",
        "limitations": [
            "Synthetic test data are not an experiment result.",
            "Practitioner usefulness was not measured.",
        ],
        "attempt_count": 12,
        "configuration_count": 1,
        "generated_invalid_count": 3,
        "released_invalid_count": 0,
        "usage": {
            "cost_usd": "0.0042",
            "provider_invocations": 9,
            "transport_attempts": 10,
            "input_tokens": 1200,
            "output_tokens": 240,
        },
        "media_title": "Synthetic architecture comparison",
        "media_caption": "Fixture media; not a real outcome.",
        "media_alt_text": "A synthetic test image for report wiring.",
        "article_table_sha256": module["_sha256"](table_path),
        "article_media_sha256": module["_sha256"](media_path),
    }
    summary = {
        **summary_body,
        "summary_sha256": module["_json_sha256"](summary_body),
    }
    summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
    receipt_body = {
        "schema_version": "1.0",
        "study_version": "evidence-guide-v2-architecture-development-r1",
        "entity": "wandb",
        "project": "gi-scenario-studio-agent-evals",
        "project_access": "RESTRICTED",
        "production_decision": "DISABLE_LIVE_GUIDE",
        "release_eligible": False,
        "validation_status": "consumed_exploratory_not_confirmatory",
        "practitioner_usefulness_status": "not_measured",
        "energy_status": "not_measured",
        "ecological_outcomes_status": "not_measured",
        "article_summary_sha256": module["_sha256"](summary_path),
        "article_table_sha256": module["_sha256"](table_path),
        "article_media_sha256": module["_sha256"](media_path),
        "table_key": "article/evidence_guide_v2_architecture_development",
        "media_key": "article/evidence_guide_v2_architecture_development_media",
        "run_id": "a" * 24,
        "run_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/" + "a" * 24
        ),
        "weave_evaluation_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/r/call/"
            "019fda8d-2cda-75e6-b2b1-e1ff38143e8c"
        ),
        "artifact": (
            "wandb/gi-scenario-studio-agent-evals/"
            "evidence-guide-architecture-development:v1"
        ),
        "run_file_privacy_audit": {"prohibited_files": 0},
    }
    receipt = {
        **receipt_body,
        "publication_sha256": module["_json_sha256"](receipt_body),
    }
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    evaluation = loader()
    assert evaluation["available"] is True
    assert evaluation["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert evaluation["validation_status"] == "consumed_exploratory_not_confirmatory"
    assert evaluation["practitioner_usefulness_status"] == "not_measured"
    assert evaluation["energy_status"] == "not_measured"
    assert evaluation["ecological_outcomes_status"] == "not_measured"
    assert evaluation["summary"]["attempt_count"] == 12
    assert len(evaluation["links"]) == 2
    assert len(evaluation["artifact_files"]) == 4

    markdown = module["_evidence_guide_v2_architecture_development_markdown"](
        evaluation
    )
    assert "12 attempts" in markdown
    assert "3 generated-invalid attempts" in markdown
    assert "consumed for exploratory development" in markdown
    assert "DISABLE_LIVE_GUIDE" in markdown
    assert "Practitioner usefulness was not measured" in markdown
    assert "Open exploratory evaluation in Weave" in markdown

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    report_table = module["_evidence_guide_v2_architecture_development_table"](
        FakeWandb,
        {"evidence_guide_v2_architecture_development": evaluation},
    )
    assert report_table.columns == columns
    assert report_table.data[0][columns.index("attempt_count")] == 12

    media_path.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    try:
        loader()
    except RuntimeError as error:
        assert "summary inputs drifted" in str(error)
    else:
        raise AssertionError("hash-drifted architecture media was accepted")


def test_evidence_guide_v2_keeps_invalid_preflights_outside_arm_denominators(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    loader = module["_load_evidence_guide_v2_evaluation"]
    globals_ = loader.__globals__
    zero_usage = {
        "provider_invocations": 0,
        "transport_attempts": 0,
        "provider_reported_input_tokens": 0,
        "provider_reported_output_tokens": 0,
        "conservative_accounted_input_tokens": 0,
        "conservative_accounted_output_tokens": 0,
        "recorded_cost_usd": "0",
    }
    collision_usage = {
        "provider_invocations": 4,
        "transport_attempts": 4,
        "provider_reported_input_tokens": 356,
        "provider_reported_output_tokens": 52,
        "conservative_accounted_input_tokens": 1926,
        "conservative_accounted_output_tokens": 512,
        "recorded_cost_usd": "0.000054",
    }
    reinitialization_usage = {
        "provider_invocations": 12,
        "transport_attempts": 12,
        "provider_reported_input_tokens": 1951,
        "provider_reported_output_tokens": 156,
        "conservative_accounted_input_tokens": 9587,
        "conservative_accounted_output_tokens": 1536,
        "recorded_cost_usd": "0.000248",
    }
    total_usage = {
        "provider_invocations": 16,
        "transport_attempts": 16,
        "provider_reported_input_tokens": 2307,
        "provider_reported_output_tokens": 208,
        "conservative_accounted_input_tokens": 11513,
        "conservative_accounted_output_tokens": 2048,
        "recorded_cost_usd": "0.000302",
    }

    def source_archive(label: str) -> dict[str, object]:
        files = [
            {
                "logical_name": f"{label}/{name}",
                "sha256": character * 64,
                "size_bytes": index + 1,
            }
            for index, (name, character) in enumerate(
                (
                    ("campaign.sqlite", "a"),
                    ("campaign-plan.json", "b"),
                    ("preregistration.json", "c"),
                )
            )
        ]
        return {
            "archive_label": label,
            "archive_content_sha256": module["_json_sha256"](files),
            "source_files": files,
        }

    incidents = [
        {
            "preflight_id": "preflight-01-artifact-readback-reference",
            "failure_layer": "wandb_artifact_readback",
            "failure_code": "WANDB_ARTIFACT_READBACK_REFERENCE_MALFORMED",
            "scientific_status": "INVALIDATED_PREFLIGHT",
            "scientifically_usable_provider_results": 0,
            "included_in_capability_denominator": False,
            "included_in_architecture_denominator": False,
            "model_quality_inference_permitted": False,
            "architecture_efficacy_inference_permitted": False,
            "answer_released": False,
            "session_completed": False,
            "block_receipt_present": False,
            "attempt_count": 0,
            "protocol_valid_attempts": 0,
            "trace_acknowledged_attempts": 0,
            "usage": zero_usage,
            "reservation": None,
            "source_archive": source_archive("preflight-01"),
        },
        {
            "preflight_id": "preflight-02-weave-evaluation-name-collision",
            "failure_layer": "weave_evaluation_publication",
            "failure_code": "WEAVE_EVALUATION_DATASET_NAME_COLLISION",
            "scientific_status": "INVALIDATED_PREFLIGHT",
            "scientifically_usable_provider_results": 0,
            "included_in_capability_denominator": False,
            "included_in_architecture_denominator": False,
            "model_quality_inference_permitted": False,
            "architecture_efficacy_inference_permitted": False,
            "answer_released": False,
            "session_completed": False,
            "block_receipt_present": False,
            "attempt_count": 2,
            "protocol_valid_attempts": 2,
            "trace_acknowledged_attempts": 0,
            "usage": collision_usage,
            "reservation": {
                "stage": "capability",
                "cost_ceiling_usd": "0.264320",
                "logical_provider_call_ceiling": 320,
                "transport_attempt_ceiling": 640,
                "input_token_ceiling": 1_310_720,
                "output_token_ceiling": 81_920,
                "measured_use": False,
            },
            "source_archive": source_archive("preflight-02"),
        },
        {
            "preflight_id": "weave-reinitialization-flush",
            "failure_layer": "weave_reinitialization_finalizer",
            "failure_code": "WEAVE_REINITIALIZATION_FLUSH_STALL",
            "scientific_status": "INVALIDATED_PREFLIGHT",
            "scientifically_usable_provider_results": 0,
            "included_in_capability_denominator": False,
            "included_in_architecture_denominator": False,
            "model_quality_inference_permitted": False,
            "architecture_efficacy_inference_permitted": False,
            "answer_released": False,
            "session_completed": False,
            "block_receipt_present": True,
            "attempt_count": 6,
            "protocol_valid_attempts": 6,
            "trace_acknowledged_attempts": 6,
            "observed_attempt_error_codes": [],
            "usage": reinitialization_usage,
            "reservation": {
                "stage": "capability",
                "cost_ceiling_usd": "0.264320",
                "logical_provider_call_ceiling": 320,
                "transport_attempt_ceiling": 640,
                "input_token_ceiling": 1_310_720,
                "output_token_ceiling": 81_920,
                "measured_use": False,
            },
            "source_archive": source_archive("preflight-03"),
        },
    ]
    audit = {
        "schema_version": "1.0",
        "campaign_version": "evidence-guide-v2-architecture-first",
        "receipt_kind": "invalidated_preflight_audit",
        "scientific_status": "NO_SCIENTIFIC_RESULT",
        "capability_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "scientifically_usable_provider_results": 0,
        "invalidated_provider_attempts": 8,
        "invalidated_preflight_count": 3,
        "usage": total_usage,
        "incidents": incidents,
        "claim_boundary": "Operational failures only.",
        "practitioner_usefulness_status": "not_measured",
    }
    audit["receipt_sha256"] = module["_json_sha256"](audit)
    audit_path = tmp_path / "invalidated-preflight-audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    # Keep the canonical R2b plan, preregistration, canary receipt, terminal
    # disposition, summary, gate, and publication receipt. This fixture replaces
    # only the independently hash-checked invalidated-preflight audit.
    globals_["EVIDENCE_GUIDE_V2_PREFLIGHT_AUDIT"] = audit_path

    evaluation = loader()

    assert evaluation["state"] == "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
    assert evaluation["visible_status"] == "admission_failed_before_architecture"
    assert evaluation["terminal_disposition"]["scientific_denominator_attempts"] == 0
    assert evaluation["terminal_disposition"]["architecture_denominator_attempts"] == 0
    assert evaluation["invalidated_preflight_audit"]["capability_denominator_attempts"] == 0
    assert evaluation["preflight_audit_sha256"] == audit["receipt_sha256"]
    assert "evaluation/v2/invalidated-preflight-audit.json" in {
        item["logical_name"] for item in evaluation["artifact_files"]
    }
    markdown = module["_evidence_guide_v2_markdown"](evaluation)
    assert "Invalidated infrastructure preflights" in markdown
    assert "weave-reinitialization-flush" in markdown
    assert "16 calls" in markdown
    assert "2307 input tokens" in markdown
    assert "208 output tokens" in markdown
    assert "11,513 input and 2,048 output tokens" in markdown
    assert "$0.000302" in markdown
    assert "2 paid capability launches used a $0.264320 reservation ceiling" in markdown
    assert "all 8 invalidated provider attempts" in markdown
    assert "outside capability and architecture denominators" in markdown
    assert "all six protocol probes and one block receipt completed" in markdown
    assert "completed spans alone do not turn an invalid launch" in markdown

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    arms = module["_evidence_guide_v2_table"](
        FakeWandb, {"evidence_guide_v2_evaluation": evaluation}
    )
    preflights = module["_evidence_guide_v2_preflight_table"](
        FakeWandb, {"evidence_guide_v2_evaluation": evaluation}
    )
    assert len(arms.data) == 7
    assert len(preflights.data) == 3
    assert preflights.data[2][0] == "weave-reinitialization-flush"
    session_index = preflights.columns.index("session_completed")
    block_index = preflights.columns.index("block_receipt_present")
    protocol_index = preflights.columns.index("protocol_valid_attempts")
    assert preflights.data[2][session_index] is False
    assert preflights.data[2][block_index] is True
    assert preflights.data[2][protocol_index] == 6
    capability_index = preflights.columns.index("capability_denominator_included")
    answer_index = preflights.columns.index("answer_released")
    assert all(row[capability_index] is False for row in preflights.data)
    assert all(row[answer_index] is False for row in preflights.data)


def test_evidence_guide_v2_consumes_hash_bound_summary_gate_and_links(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    loader = module["_load_evidence_guide_v2_evaluation"]
    globals_ = loader.__globals__
    summary_path = tmp_path / "article-summary.json"
    gate_path = tmp_path / "release-gate.json"
    receipt_path = tmp_path / "wandb-publication.json"
    globals_["EVIDENCE_GUIDE_V2_ARTICLE_SUMMARY"] = summary_path
    globals_["EVIDENCE_GUIDE_V2_RELEASE_GATE"] = gate_path
    globals_["EVIDENCE_GUIDE_V2_PUBLICATION_RECEIPT"] = receipt_path
    source_root = module["EVIDENCE_GUIDE_V2_ROOT"]
    shutil.copy2(source_root / "article-summary.json", summary_path)
    shutil.copy2(source_root / "release-gate.json", gate_path)
    shutil.copy2(source_root / "wandb-publication.json", receipt_path)

    evaluation = loader()

    assert evaluation["state"] == "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
    assert evaluation["decision"] == "DISABLE_LIVE_GUIDE"
    assert evaluation["automated_explanation_quality"] == "not_measured"
    assert evaluation["practitioner_usefulness_status"] == "not_measured"
    assert evaluation["visible_status"] == "admission_failed_before_architecture"
    assert evaluation["hidden_status"] == "AWAITING_PROTECTED_LOCKBOX"
    assert evaluation["hidden_attempt_rows_published"] is False
    assert [item["count"] for item in evaluation["failure_summary"]] == [1, 1]
    assert len(evaluation["links"]) == 6
    assert len(evaluation["artifact_references"]) == 8
    assert len(evaluation["rows"]) == 7
    assert next(row for row in evaluation["rows"] if row["architecture_id"] == "A1")[
        "failure_codes"
    ] == evaluation["terminal_disposition"]["a1_observation"]["error_codes"]
    markdown = module["_evidence_guide_v2_markdown"](evaluation)
    assert "CAMPAIGN_STOPPED_AT_TRACE_CANARY" in markdown
    assert "R2b disabled release gate" in markdown
    assert "Granite A1 canary in Weave" in markdown

    corrupted_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    corrupted_gate["decision"] = "ENABLE_LIVE_GUIDE"
    gate_path.write_text(json.dumps(corrupted_gate), encoding="utf-8")
    try:
        loader()
    except RuntimeError as error:
        assert "release gate drifted from the article summary" in str(error)
    else:
        raise AssertionError("a release gate that drifted from its hash-bound summary was accepted")


def test_agent_behavior_table_separates_runtime_outcomes_from_candidate_scores() -> None:
    module = runpy.run_path(str(PUBLISHER))
    evaluation = module["_load_evidence_guide_evaluation"]("wandb", "gi-scenario-studio-article")

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    table = module["_evidence_guide_agent_table"](
        FakeWandb, {"evidence_guide_evaluation": evaluation}
    )

    assert len(table.data) == 3
    assert "validation_outcome" in table.columns
    assert "answer_released" in table.columns
    assert "token_usage_recorded" in table.columns
    assert "latency_recorded" in table.columns
    assert "production_gate_decision" in table.columns
    assert [row[table.columns.index("conversation_scope")] for row in table.data] == [
        "new",
        "continued",
        "new",
    ]
    assert all(row[table.columns.index("tool_activity_observed")] for row in table.data)
    assert all(row[table.columns.index("weave_readback")] for row in table.data)
    assert {row[table.columns.index("root_status")] for row in table.data} == {"ERROR"}
    assert {row[table.columns.index("validation_outcome")] for row in table.data} == {"rejected"}
    assert [row[table.columns.index("error_code")] for row in table.data] == [
        "EVIDENCE_NEXT_CHECK_SELECTION_INVALID",
        "EVIDENCE_GENERATED_NUMBER_BLOCKED",
        "EVIDENCE_SUPPORT_CLAIM_INVALID",
    ]
    assert all(
        row[table.columns.index("cloud_tool_parentage_verified")] is None for row in table.data
    )
    assert {row[table.columns.index("cloud_tool_parentage_reason")] for row in table.data} == {
        "routing_parent_span_not_exposed_by_sdk"
    }
    assert not any(row[table.columns.index("answer_released")] for row in table.data)
    assert not any(row[table.columns.index("token_usage_recorded")] for row in table.data)
    assert not any(row[table.columns.index("latency_recorded")] for row in table.data)
    assert {row[table.columns.index("production_gate_decision")] for row in table.data} == {
        "DISABLE_LIVE_GUIDE"
    }


def test_realistic_case_study_is_hash_bound_and_keeps_qa_beside_benefit() -> None:
    module = runpy.run_path(str(PUBLISHER))
    snapshot = module["_load_realistic_case_study"]()

    assert snapshot["case_id"] == "eawag-fehraltorf-published-output-reanalysis-v1"
    assert snapshot["analysis_boundary"]["fresh_swmm_rerun"] is False
    assert snapshot["method"]["year_range"] == {"start": 1990, "end": 2019, "count": 30}
    assert snapshot["bioretention_claim"] == {
        "claim_id": "fehraltorf-bb-model-structure-sensitivity",
        "gi_scenario_id": "bb",
        "limitation": (
            "Published modeled CSO response varies with model structure; this comparison does "
            "not establish field performance or resolve the reports' QA findings."
        ),
        "max_reduction_percent": 92.2367,
        "max_structure_id": "GW",
        "min_reduction_percent": 56.8133,
        "min_structure_id": "CB",
        "spread_percentage_points": 35.4234,
    }
    assert len(snapshot["table_rows"]) == 5
    combined = snapshot["combined_bioretention_case"]
    assert combined["case_id"] == "CB-bb"
    assert combined["median_annual_cso_m3"] == 69956.694
    assert combined["qa"]["groundwater_continuity_error_percent"] == -54.273
    assert combined["qa"]["routing_continuity_error_percent"] == -2.107
    assert combined["qa"]["runoff_continuity_error_percent"] == -0.913
    assert combined["qa"]["model_disclaimer_present"] is True
    assert snapshot["sha256"] == module["_sha256"](module["REALISTIC_CASE_STUDY"])

    media = module["_load_realistic_case_study_media"](snapshot)
    publication = module["_load_realistic_case_study_publication"](
        "wandb", "gi-scenario-studio-article", snapshot, media
    )
    markdown = module["_realistic_case_study_markdown"](snapshot, publication)
    assert "56.8% to 92.2%" in markdown
    assert "35.4-percentage-point" in markdown
    assert "-54.273% groundwater continuity error" in markdown
    assert "not a fresh SWMM rerun or field validation" in markdown
    assert publication["run_url"] in markdown
    assert publication["weave_trace_url"] in markdown
    assert "hash-bound receipt" in markdown

    assert media["source"]["case_study_sha256"] == snapshot["sha256"]
    assert media["comparison"] == {
        "scenario": "bioretention",
        "year_count": 30,
        "min_structure_id": "CB",
        "max_structure_id": "GW",
        "spread_percentage_points": 35.4234,
    }
    assert media["outputs"]["png"]["width"] == 1920
    assert media["outputs"]["png"]["height"] == 1080
    assert media["outputs"]["csv"]["rows"] == 5
    assert len(media["table"]) == 5
    assert publication["artifact"] == (
        "wandb/gi-scenario-studio-article/fehraltorf-published-output-reanalysis:v0"
    )
    assert publication["weave_readback_verified"] is True
    assert publication["wandb_run_association_verified"] is True


def test_realistic_case_study_table_is_an_accessible_five_structure_fallback() -> None:
    module = runpy.run_path(str(PUBLISHER))
    snapshot = module["_load_realistic_case_study"]()

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    table = module["_realistic_case_study_table"](FakeWandb, {"realistic_case_study": snapshot})

    assert len(table.data) == 5
    assert "paired_year_modeled_reduction_percent" in table.columns
    assert "groundwater_continuity_error_percent" in table.columns
    combined = next(row for row in table.data if row[0] == "Combined")
    assert combined[table.columns.index("paired_year_modeled_reduction_percent")] == 56.8133
    assert combined[table.columns.index("groundwater_continuity_error_percent")] == -54.273


def test_realistic_case_publication_receipt_fails_closed_on_identity_drift(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    case_study = module["_load_realistic_case_study"]()
    media = module["_load_realistic_case_study_media"](case_study)
    loader = module["_load_realistic_case_study_publication"]
    receipt_path = tmp_path / "wandb-publication.json"
    loader.__globals__["REALISTIC_CASE_STUDY_PUBLICATION"] = receipt_path
    source = json.loads(
        (ROOT.parents[1] / "dist" / "realistic-fehraltorf" / "wandb-publication.json").read_text(
            encoding="utf-8"
        )
    )
    mutations = [
        ("Restricted", {"project_access": "OPEN"}),
        (
            "exact receipt-backed path",
            {"run_url": "https://wandb.ai/wandb/gi-scenario-studio-article/runs/wrong"},
        ),
        ("chart hash", {"chart_sha256": "0" * 64}),
        ("artifact name", {"artifact": "other/project/wrong:v0"}),
        ("readback", {"weave_readback_verified": False}),
    ]
    for expected, mutation in mutations:
        candidate = json.loads(json.dumps(source))
        candidate.update(mutation)
        receipt_path.write_text(json.dumps(candidate), encoding="utf-8")
        try:
            loader("wandb", "gi-scenario-studio-article", case_study, media)
        except RuntimeError as error:
            assert expected.lower() in str(error).lower()
        else:
            raise AssertionError(f"Fehraltorf receipt drift was accepted: {mutation}")


def test_real_candidate_campaign_is_hash_bound_to_the_release_gate() -> None:
    module = runpy.run_path(str(PUBLISHER))
    snapshot = module["_load_evidence_guide_evaluation"]("wandb", "gi-scenario-studio-article")

    assert snapshot["publication_state"] == "campaign_published_verified"
    assert snapshot["decision"] == "DISABLE_LIVE_GUIDE"
    assert snapshot["enabled"] is False
    assert snapshot["selected_model"] is None
    assert snapshot["row_count"] == 48
    assert snapshot["publication_receipt_count"] == 1
    assert snapshot["agent_probe_receipt_count"] == 1
    assert snapshot["agent_probe"]["turn_count"] == 3
    assert snapshot["agent_probe"]["rejected_count"] == 3
    assert [candidate["model_id"] for candidate in snapshot["candidates"]] == [
        "ibm-granite/granite-4.1-8b",
        "openai/gpt-oss-20b",
    ]
    granite, gpt = snapshot["candidates"]
    assert granite["summary"]["passed"] == 3
    assert granite["summary"]["evaluated"] == 45
    assert granite["summary"]["missing"] == 3
    assert granite["metrics"]["numeric_unit_fidelity"] == 0.9375
    assert granite["metrics"]["evidence_coverage"] == 0.9375
    assert granite["metrics"]["limitation_coverage"] == 0.104167
    assert granite["metrics"]["read_only_action_compliance"] == 0.395833
    assert granite["metrics"]["abstention_passed"] == 8
    assert granite["metrics"]["complete_abstention_passed"] == 2
    assert granite["metrics"]["evidence_id_validity"] == 1.0
    assert granite["metrics"]["fabricated_evidence_id_violations"] == 0
    assert granite["metrics"]["fabricated_run_complete_pass"] is False
    assert granite["metrics"]["prompt_injection_complete_pass"] is True
    assert granite["practitioner_usefulness"]["status"] == "not_measured"
    assert granite["controlled_approval_status"] == "not_supplied"
    assert granite["usage"] == {
        "input_tokens": 22211,
        "output_tokens": 5945,
        "total_tokens": 28156,
    }
    assert granite["latency_ms"]["median"] == 1418.576
    assert granite["latency_ms"]["p95"] == 2261.704
    assert gpt["summary"]["passed"] == 0
    assert gpt["summary"]["evaluated"] == 18
    assert gpt["summary"]["missing"] == 30
    assert gpt["metrics"]["numeric_unit_fidelity"] == 0.375
    assert gpt["metrics"]["evidence_coverage"] == 0.354167
    assert gpt["metrics"]["limitation_coverage"] == 0.020833
    assert gpt["metrics"]["read_only_action_compliance"] == 0.208333
    assert gpt["metrics"]["abstention_passed"] == 6
    assert gpt["metrics"]["complete_abstention_passed"] == 0
    assert gpt["practitioner_usefulness"]["status"] == "not_measured"
    assert gpt["controlled_approval_status"] == "not_supplied"
    assert gpt["usage"] == {
        "input_tokens": 23448,
        "output_tokens": 30116,
        "total_tokens": 53564,
    }
    assert gpt["latency_ms"]["median"] == 4740.756
    assert gpt["latency_ms"]["p95"] == 6154.827
    assert snapshot["experiment_scope"] == {
        "candidate_campaign": {
            "kind": "single_call_structured_evidence_selection",
            "cases_per_model": 48,
            "model_count": 2,
            "logical_case_invocations": 96,
            "evidence_tool_calls": 0,
            "runtime_agent_loop_evaluated": False,
            "evidence_ids_and_actions_schema_constrained": True,
            "canonical_numeric_values_materialized_locally": True,
        },
        "agents_probe": {
            "kind": "two_call_tool_using_runtime_integration_probe",
            "conversation_count": 2,
            "turn_count": 3,
            "rejected_turn_count": 3,
            "released_answer_count": 0,
            "answer_quality_scored": False,
            "token_usage_status": "not_recorded_in_probe_receipt",
            "latency_status": "not_recorded_in_probe_receipt",
            "production_release_gate_overridden": True,
        },
        "successful_end_to_end_runtime_agent_answers_evaluated": 0,
        "supports_live_guide_release": False,
    }
    assert granite["publication"]["evaluation_url"].endswith("019fd964-7ed6-7581-95ea-6ebc75a13be1")
    assert gpt["publication"]["evaluation_url"].endswith("019fd964-809f-7bc0-b147-d6ca0b8ca67a")


def test_article_publisher_dry_run_projects_the_real_campaign_without_network() -> None:
    environment = dict(os.environ)
    environment.pop("WANDB_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            str(PUBLISHER),
            "--entity",
            "wandb",
            "--project",
            "gi-scenario-studio-article",
        ],
        cwd=ROOT.parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(result.stdout)
    plan = json.loads(
        (ROOT / "dist" / "article-clips" / "wandb-report-plan.json").read_text(encoding="utf-8")
    )
    assert output["dry_run"] is True
    assert output["network_calls"] == 0
    assert output["evidence_guide_candidate_count"] == 2
    assert output["evidence_guide_decision"] == "DISABLE_LIVE_GUIDE"
    assert output["evidence_guide_publication_receipts"] == 1
    assert output["evidence_guide_agents_probe_receipts"] == 1
    assert output["evidence_guide_agents_probe_rejections"] == 3
    assert output["evidence_guide_v2_state"] == "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
    assert output["evidence_guide_v2_decision"] == "DISABLE_LIVE_GUIDE"
    assert output["evidence_guide_v2_automated_explanation_quality"] == "not_measured"
    assert output["evidence_guide_v2_practitioner_usefulness"] == "not_measured"
    assert output["realistic_case_study_id"] == ("eawag-fehraltorf-published-output-reanalysis-v1")
    assert output["realistic_case_publication_run_url"].endswith("/runs/488db72ce2bdabfc242c4442")
    assert output["realistic_case_weave_trace_url"].endswith(
        "/r/call/019fd8d7-d4f0-7d36-9dd8-6e6770b42977"
    )
    assert plan["evidence_guide_evaluation"]["decision"] == "DISABLE_LIVE_GUIDE"
    assert plan["evidence_guide_evaluation"]["publication_state"] == ("campaign_published_verified")
    assert plan["evidence_guide_v2_evaluation"]["state"] == (
        "CAMPAIGN_STOPPED_AT_TRACE_CANARY"
    )
    assert len(plan["evidence_guide_v2_evaluation"]["rows"]) == 7
    assert plan["realistic_case_study"]["analysis_boundary"]["fresh_swmm_rerun"] is False
    assert plan["realistic_case_study"]["sha256"] == output["realistic_case_study_sha256"]
    assert (
        plan["realistic_case_study_publication"]["receipt_sha256"]
        == output["realistic_case_publication_receipt_sha256"]
    )
    assert plan["realistic_case_study_publication"]["artifact"] == (
        "wandb/gi-scenario-studio-article/fehraltorf-published-output-reanalysis:v0"
    )


def test_campaign_publication_receipt_requires_the_exact_restricted_destination(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    loader = module["_load_evidence_guide_evaluation"]
    globals_ = loader.__globals__
    source = globals_["EVIDENCE_GUIDE_CAMPAIGN"]
    campaign = tmp_path / "campaign"
    shutil.copytree(source, campaign)
    globals_["EVIDENCE_GUIDE_CAMPAIGN"] = campaign
    globals_["EVIDENCE_GUIDE_CAMPAIGN_RELEASE_GATE"] = campaign / "release-gate.json"
    publication_path = campaign / "weave-publication.json"
    globals_["EVIDENCE_GUIDE_CAMPAIGN_PUBLICATION"] = publication_path
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    publication["project_access"] = "OPEN"
    publication_path.write_text(json.dumps(publication), encoding="utf-8")
    try:
        loader("wandb", "gi-scenario-studio-article")
    except RuntimeError as error:
        assert "campaign publication receipt drifted" in str(error)
    else:
        raise AssertionError("a non-Restricted campaign publication receipt was accepted")


def test_evaluation_publication_url_must_be_an_actual_receipt_value() -> None:
    module = runpy.run_path(str(PUBLISHER))

    assert (
        module["_actual_publication_url"](
            "https://wandb.ai/team/project/weave/evaluations/actual",
            label="evaluation",
        )
        == "https://wandb.ai/team/project/weave/evaluations/actual"
    )
    try:
        module["_actual_publication_url"]("trace-placeholder", label="evaluation")
    except ValueError as error:
        assert "authenticated W&B Cloud publication URL" in str(error)
    else:
        raise AssertionError("a placeholder publication URL was accepted")

    conversation = "https://wandb.ai/team/project/weave/agents/conversations/real"
    assert (
        module["_actual_weave_conversation_url"](
            conversation,
            label="conversation",
            entity="team",
            project="project",
            conversation_id="real",
        )
        == conversation
    )
    try:
        module["_actual_weave_conversation_url"](
            "https://wandb.ai/team/project/weave/calls/not-a-conversation",
            label="conversation",
        )
    except ValueError as error:
        assert "conversation URL" in str(error)
    else:
        raise AssertionError("a non-conversation URL was accepted as a Weave Agents link")
    try:
        module["_actual_weave_conversation_url"](
            conversation,
            label="conversation",
            entity="another-team",
            project="project",
            conversation_id="real",
        )
    except ValueError as error:
        assert "requested W&B destination" in str(error)
    else:
        raise AssertionError("a cross-project Agents conversation URL was accepted")


def test_evidence_guide_report_table_compares_both_real_candidates() -> None:
    module = runpy.run_path(str(PUBLISHER))
    snapshot = module["_load_evidence_guide_evaluation"]("wandb", "gi-scenario-studio-article")

    class FakeTable:
        def __init__(self, *, columns, data) -> None:
            self.columns = columns
            self.data = data

    class FakeWandb:
        Table = FakeTable

    table = module["_evidence_guide_table"](FakeWandb, {"evidence_guide_evaluation": snapshot})

    assert len(table.data) == 2
    assert table.data[0][0] == "ibm-granite/granite-4.1-8b"
    assert table.data[1][0] == "openai/gpt-oss-20b"
    for column in (
        "experiment_kind",
        "ids_actions_schema_constrained",
        "numeric_values_materialized_locally",
        "numeric_unit_fidelity",
        "evidence_id_validity_on_produced_outputs",
        "evidence_coverage",
        "limitation_coverage",
        "unauthorized_action_violations",
        "fabricated_evidence_id_violations",
        "fabricated_run_complete_pass",
        "prompt_injection_complete_pass",
        "refusal_behavior_passed",
        "complete_abstentions",
        "usefulness_status",
        "usefulness_rating_count",
        "usefulness_positive_rate",
        "controlled_approval_status",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_accounted_responses",
        "latency_attempts",
        "median_latency_ms",
        "p95_latency_ms",
    ):
        assert column in table.columns
    assert table.data[0][table.columns.index("numeric_unit_fidelity")] == 0.9375
    assert table.data[0][table.columns.index("evidence_id_validity_on_produced_outputs")] == 1.0
    assert table.data[1][table.columns.index("limitation_coverage")] == 0.020833
    assert table.data[0][table.columns.index("refusal_behavior_passed")] == 8
    assert table.data[0][table.columns.index("complete_abstentions")] == 2
    assert table.data[0][table.columns.index("usefulness_status")] == "not_measured"
    assert table.data[0][table.columns.index("controlled_approval_status")] == "not_supplied"
    assert table.data[1][table.columns.index("total_tokens")] == 53564
    assert table.data[1][table.columns.index("token_accounted_responses")] == 18
    assert table.data[1][table.columns.index("latency_attempts")] == 48


def test_report_animation_contract_is_bounded_and_full_width() -> None:
    media = _animation_media_spec()
    animations = media["clips"]

    assert media["media_kind"] == "evidence_grounded_canvas_films"
    assert media["frame_rate"] == 30
    assert media["persistent_boundary"] == (
        "Synthetic test events · modeled, not measured · not annualized · not field evidence."
    )
    assert [animation["key"] for animation in animations] == [
        "animation/incomplete_plot",
        "animation/water_ledger",
        "animation/storm_reveals",
        "animation/decision_funnel",
        "animation/search_release_gate",
    ]
    assert [animation["target_duration_seconds"] for animation in animations] == [
        34,
        50,
        36,
        38,
        46,
    ]
    assert [animation["claim_ids"] for animation in animations] == [
        ["A-ALT017"],
        ["A-WATER-LEDGER", "A-OUTFALL"],
        ["A-STORMS"],
        ["A-FUNNEL"],
        ["A-SEARCH", "A-GATE", "A-COMPUTE"],
    ]
    assert all(animation["layout"] == "full_width" for animation in animations)
    assert all(animation["establishes"] for animation in animations)
    assert all(animation["does_not_establish"] for animation in animations)


def test_article_places_visible_boundaries_after_each_animation() -> None:
    article = ARTICLE.read_text(encoding="utf-8")

    assert article.count("<!-- ARTICLE_ANIMATION") == 5
    assert article.count("**What this establishes.**") == 6
    assert article.count("**What this does not establish.**") == 6
    assert "**infiltration loss** is SWMM runoff-accounting language" in article
    assert "Energy use, kWh, and CO₂e were not measured" in article
    assert "does not measure groundwater recharge, water quality, habitat" in article


def test_article_media_is_real_guided_app_footage_with_bounded_layouts() -> None:
    media = _media_spec()
    clips = media["clips"]
    keys = set()
    markers = set()
    outputs = set()
    assert media["media_kind"] == "guided_application_walkthrough"
    assert media["capture"] == {
        "surface": "browser-preview",
        "viewport_width": 1440,
        "viewport_height": 900,
        "fps": 30,
        "authority_notice": (
            "Interface-only guided replay of the exact checked-in fixture; "
            "not a packaged desktop execution or a live SWMM run."
        ),
    }
    for clip in clips:
        assert clip["key"].startswith("app/")
        assert clip["layout"] in {"side_by_side", "full_width"}
        assert 20 <= len(clip["report_copy"].split()) <= 80
        assert clip["output"].startswith("dist/article-clips/")
        keys.add(clip["key"])
        markers.add(clip["marker"])
        outputs.add(clip["output"])
    assert len(keys) == len(clips)
    assert len(markers) == len(clips)
    assert len(outputs) == len(clips)
    assert [clip["layout"] for clip in clips].count("full_width") == 3

    evidence_clip = next(clip for clip in clips if clip["key"] == "app/evidence_rail")
    assert "3.14→3.21 cfs" in evidence_clip["report_copy"]
    assert "13.33→13.34 cfs" in evidence_clip["report_copy"]
    assert "no model call or synthetic answer is shown" in evidence_clip["report_copy"]
    assert "does not establish water quality" in evidence_clip["report_copy"]


def test_article_records_the_honest_evidence_guide_replay() -> None:
    article = ARTICLE.read_text(encoding="utf-8")

    assert "because its release checks failed" in article
    assert "3.14→3.21 cfs" in article
    assert "13.33→13.34 cfs" in article
    assert "No model call is made, and no canned AI answer is substituted" in article
    assert "maintenance performance, or lifecycle impact" in article


def test_recording_harness_never_pretends_to_rerun_or_export() -> None:
    recorder = (ROOT / "scripts" / "record_app_walkthrough.mjs").read_text(encoding="utf-8")

    assert 'preview_mode: "interface-only"' in recorder
    assert "live_solver_execution: false" in recorder
    assert "Rerun all" not in recorder
    assert "Download learning summary" not in recorder
    assert recorder.count('await clip("') == 7
    assert '"Evidence Guide held back by release checks"' in recorder
    assert '"No model call will be attempted"' in recorder
    assert '"3.14 to 3.21 cfs"' in recorder
    assert '"13.33 to 13.34 cfs"' in recorder
    assert '"does not establish water-quality improvement"' in recorder


def test_preview_report_api_is_exactly_pinned() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = (ROOT.parents[1] / "package.json").read_text(encoding="utf-8")
    publisher = PUBLISHER.read_text(encoding="utf-8")
    assert '"wandb==0.28.1"' in pyproject
    assert '"wandb-workspaces==0.4.4"' in pyproject
    assert '"@playwright/test": "1.62.1"' in package
    assert "wr.MarkdownPanel" in publisher
    assert "wr.Layout(x=0, y=0, w=10, h=14)" in publisher
    assert "wr.Layout(x=10, y=0, w=14, h=14)" in publisher


class _FakeProject:
    def __init__(self, project_id: str) -> None:
        self.id = project_id


class _FakeServiceApi:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.queries: list[tuple[str, dict[str, str]]] = []
        self.mutations: list[dict[str, object]] = []

    def execute_graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        if "mutation CreateProject" in query:
            self.mutations.append(variables)
            project_input = variables["input"]
            return {
                "result": {
                    "project": {
                        "id": "project-123",
                        "name": project_input["name"],
                        "entityName": project_input["entityName"],
                        "access": project_input["access"],
                    }
                }
            }
        self.queries.append((query, variables))
        return self.responses.pop(0)


class _FakeApi:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._service_api = _FakeServiceApi(responses)

    def project(self, project: str, *, entity: str) -> _FakeProject:
        assert project == "gi-article"
        assert entity == "engineering"
        return _FakeProject("project-123")


def _project_response(access: str) -> dict[str, object]:
    return {
        "project": {
            "id": "project-123",
            "name": "gi-article",
            "entityName": "engineering",
            "access": access,
        }
    }


def test_wandb_project_creation_is_followed_by_exact_restricted_policy_check() -> None:
    module = runpy.run_path(str(PUBLISHER))
    api = _FakeApi([{"project": None}, _project_response("RESTRICTED")])

    selected = module["_get_project"](api, "engineering", "gi-article", True)

    assert selected.id == "project-123"
    assert len(api._service_api.queries) == 2
    assert "access" in api._service_api.queries[0][0]
    assert api._service_api.mutations == [
        {
            "input": {
                "name": "gi-article",
                "entityName": "engineering",
                "access": "RESTRICTED",
            }
        }
    ]


def test_wandb_publication_fails_closed_for_nonrestricted_project() -> None:
    module = runpy.run_path(str(PUBLISHER))
    api = _FakeApi([_project_response("OPEN")])

    try:
        module["_get_project"](api, "engineering", "gi-article", False)
    except RuntimeError as error:
        assert "OPEN, not RESTRICTED" in str(error)
    else:
        raise AssertionError("nonrestricted project policy was accepted")


def test_wandb_publication_rechecks_restricted_policy_after_report_save() -> None:
    module = runpy.run_path(str(PUBLISHER))
    api = _FakeApi([_project_response("RESTRICTED")])

    module["_assert_restricted_project_unchanged"](api, "engineering", "gi-article", "project-123")

    changed_api = _FakeApi([_project_response("OPEN")])
    try:
        module["_assert_restricted_project_unchanged"](
            changed_api, "engineering", "gi-article", "project-123"
        )
    except RuntimeError as error:
        assert "changed to OPEN" in str(error)
    else:
        raise AssertionError("post-publication policy drift was accepted")


def test_report_publisher_requires_an_existing_report_url() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    assert "an existing W&B report URL is required" in publisher
    assert "expected_report_id = report.id" in publisher
    assert "saved.id != expected_report_id" in publisher


def test_report_publisher_disables_automatic_host_and_environment_capture() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    assert "x_disable_machine_info=True" in publisher
    assert "x_disable_meta=True" in publisher
    assert "x_disable_stats=True" in publisher
    assert "x_save_requirements=False" in publisher
    assert 'run.use_artifact(animation_receipt["artifact"])' in publisher
    assert 'run.use_artifact(plan["realistic_case_study_publication"]["artifact"])' in publisher
    assert "Config('publication_id') == '{animation_receipt['publication_id']}'" in publisher

    module = runpy.run_path(str(PUBLISHER))
    artifact_source = inspect.getsource(module["_artifact_files"])
    assert "ANIMATION_MEDIA_SPEC" in artifact_source
    assert "ANIMATION_MANIFEST" in artifact_source
    assert "ANIMATION_RECEIPT" not in artifact_source


def _animation_receipt_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "entity": "engineering",
        "project": "gi-article",
        "project_access": "RESTRICTED",
        "publication_id": "animation-package-123",
        "run_id": "animation-run-123",
        "run_url": "https://wandb.example/runs/animation-run-123",
        "artifact": "engineering/gi-article/gi-report-films:v0",
        "animation_manifest_sha256": "a" * 64,
        "clip_count": 5,
        "run_file_privacy_audit": {"audited_runs": 1, "prohibited_files": 0},
        "source_artifacts": {
            "benchmark_dataset_artifact": "engineering/gi-article/exact:v0",
            "benchmark_publication_artifact": "engineering/gi-article/campaign:v0",
        },
    }


def test_animation_receipt_requires_exact_restricted_destination_and_privacy_audit(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(str(PUBLISHER))
    receipt_path = tmp_path / "wandb-publication.json"
    module["_load_animation_receipt"].__globals__["ANIMATION_RECEIPT"] = receipt_path

    payload = _animation_receipt_payload()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = module["_load_animation_receipt"]("engineering", "gi-article", "a" * 64)
    assert loaded["publication_id"] == "animation-package-123"

    payload["project_access"] = "OPEN"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        module["_load_animation_receipt"]("engineering", "gi-article", "a" * 64)
    except RuntimeError as error:
        assert "RESTRICTED" in str(error)
    else:
        raise AssertionError("nonrestricted animation receipt was accepted")

    payload["project_access"] = "RESTRICTED"
    payload["run_file_privacy_audit"] = {"audited_runs": 1, "prohibited_files": 1}
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        module["_load_animation_receipt"]("engineering", "gi-article", "a" * 64)
    except RuntimeError as error:
        assert "privacy audit" in str(error)
    else:
        raise AssertionError("animation receipt with prohibited files was accepted")
