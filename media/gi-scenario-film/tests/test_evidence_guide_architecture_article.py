from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

FILM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FILM_ROOT.parents[1]
SCRIPT = FILM_ROOT / "scripts" / "build_evidence_guide_architecture_article.py"
PUBLISHER = FILM_ROOT / "scripts" / "publish_wandb_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_evidence_guide_architecture_article", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_publisher():
    spec = importlib.util.spec_from_file_location("publish_wandb_report_arch_test", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _publication_digest(module, publication_root: Path) -> str:
    files = {path.name: _sha256(path) for path in sorted(publication_root.glob("*.json"))}
    row_counts = {}
    for path in sorted(publication_root.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value.get("rows"), list):
            row_counts[path.stem] = len(value["rows"])
    manifest = {
        "schema_version": "1.0",
        "campaign_version": module.CAMPAIGN_VERSION,
        "kind": "architecture_development_study",
        "files": files,
        "row_counts": row_counts,
        "release_eligible": False,
        "release_gate_consumed": False,
        "assistant_release_basis_emitted": False,
        "hidden_attempt_rows_included": False,
    }
    return module._json_sha256(manifest)


def _parent(
    attempt_id: str,
    stage: str,
    architecture: str,
    *,
    calls: int,
    transports: int | None = None,
) -> dict:
    return {
        "attempt_id": attempt_id,
        "stage": stage,
        "architecture_id": architecture,
        "provider_invocations": calls,
        "transport_attempts": calls if transports is None else transports,
    }


def _branch(
    parent: str,
    stage: str,
    arm: str,
    case_id: str,
    *,
    conformant: bool = True,
    source_architecture: str | None = None,
    causal: bool | None = None,
    cost: str = "0.001",
    latency_ms: int = 750,
) -> dict:
    return {
        "parent_attempt_id": parent,
        "stage": stage,
        "split": "validation" if stage == "transfer" else "development",
        "case_id": case_id,
        "project_family": "fixture-project",
        "scenario_type": "causal_retrieval" if causal is not None else "supported",
        "model_id": "repository" if arm == "D1" else "ibm-granite/granite-4.1-8b",
        "arm": arm,
        "source_architecture_id": source_architecture or arm,
        "raw_invalid": not conformant,
        "validator_contained": not conformant,
        "validator_missed_raw_invalid": False,
        "candidate_conformant": conformant,
        "hypothetical_release_eligible": conformant,
        "actual_answer_released": False,
        "actual_released_invalid": False,
        "trace_acknowledged": True,
        "causal": causal,
        "input_tokens": 0 if arm == "D1" else 120,
        "output_tokens": 0 if arm == "D1" else 12,
        "cost_usd": "0" if arm == "D1" else cost,
        "latency_ms": 4 if arm == "D1" else latency_ms,
    }


def _fixture(module, root: Path, *, release_one: bool = False) -> Path:
    attempts = [
        _parent("dev-d1", "development", "D1", calls=0),
        _parent("dev-a1", "development", "A1", calls=1),
        _parent("dev-a3x", "development", "A3X", calls=3, transports=4),
        _parent("val-d1", "transfer", "D1", calls=0),
        _parent("val-a1", "transfer", "A1", calls=1),
        _parent("val-a3x", "transfer", "A3X", calls=3),
        _parent("causal-a1", "causal_perturbation", "A1", calls=1),
        _parent("causal-a3p", "causal_perturbation", "A3P", calls=1),
        _parent("causal-a0xf", "causal_perturbation", "A0xF", calls=1),
    ]
    branches = [
        _branch("dev-d1", "development", "D1", "dev-1"),
        _branch("dev-a1", "development", "A1", "dev-1"),
        _branch(
            "dev-a3x",
            "development",
            "A3P",
            "dev-1",
            source_architecture="A3X",
        ),
        _branch(
            "dev-a3x",
            "development",
            "A0xF",
            "dev-1",
            source_architecture="A3X",
        ),
        _branch("val-d1", "transfer", "D1", "val-1"),
        _branch("val-a1", "transfer", "A1", "val-1"),
        _branch(
            "val-a3x",
            "transfer",
            "A3P",
            "val-1",
            source_architecture="A3X",
        ),
        _branch(
            "val-a3x",
            "transfer",
            "A0xF",
            "val-1",
            conformant=False,
            source_architecture="A3X",
        ),
        _branch(
            "causal-a1",
            "causal_perturbation",
            "A1",
            "causal-1",
            causal=True,
        ),
        _branch(
            "causal-a3p",
            "causal_perturbation",
            "A3P",
            "causal-1",
            causal=True,
        ),
        _branch(
            "causal-a0xf",
            "causal_perturbation",
            "A0xF",
            "causal-1",
            causal=True,
        ),
    ]
    if release_one:
        branches[0]["actual_answer_released"] = True

    flow = {
        "scheduled_branches": len(branches),
        "raw_invalid": 1,
        "validator_contained": 1,
        "candidate_conformant": len(branches) - 1,
        "actual_responses_released": int(release_one),
        "actual_released_invalid": 0,
    }
    structural_body = {
        "schema_version": "1.0",
        "analysis_version": "finite-plan-choice-audit-1",
        "scenario_count": 240,
        "finite_plan_cardinality": {"1": 240},
        "all_scenarios_have_one_allowed_plan": True,
        "splits": {},
        "interpretation": "All visible finite-plan choices are repository singletons.",
        "model_reasoning_attribution_permitted": False,
    }
    structural = {
        **structural_body,
        "analysis_sha256": module._json_sha256(structural_body),
    }
    prior = {
        "cost_usd": "0.01",
        "provider_invocations": 10,
        "transport_attempts": 10,
        "input_tokens": 1000,
        "output_tokens": 100,
    }
    usage = {
        "cost_usd": "0.02",
        "provider_invocations": 21,
        "transport_attempts": 22,
        "input_tokens": 2000,
        "output_tokens": 200,
    }
    summary_body = {
        "schema_version": "1.0",
        "study_version": module.STUDY_VERSION,
        "attempt_count": len(attempts),
        "branch_count": len(branches),
        "generated_invalid_count": 1,
        "validator_contained_count": 1,
        "validator_missed_raw_invalid_count": 0,
        "candidate_conformant_count": len(branches) - 1,
        "hypothetical_release_eligible_count": len(branches) - 1,
        "actual_response_released_count": int(release_one),
        "actual_released_invalid_count": 0,
        "production_non_release_enforced": not release_one,
        "release_eligible": False,
        "conformance_flow": flow,
        "usage": usage,
        "prior_r2b_usage": prior,
        "practitioner_usefulness_status": "not_measured",
        "structural_plan_analysis": structural,
    }
    summary = {**summary_body, "summary_sha256": module._json_sha256(summary_body)}
    terminal_body = {
        "schema_version": "1.0",
        "study_version": module.STUDY_VERSION,
        "disposition": module.TERMINAL_DISPOSITION,
        "production_decision": module.PRODUCTION_DECISION,
        "assistant_release_basis_created": False,
        "release_eligible": False,
        "preregistration_sha256": "a" * 64,
        "schedule_sha256": "b" * 64,
        "summary_sha256": summary["summary_sha256"],
        "attempt_count": len(attempts),
        "usage": usage,
        "claim_boundary": "Exploratory only.",
        "practitioner_usefulness_status": "not_measured",
    }
    terminal = {**terminal_body, "receipt_sha256": module._json_sha256(terminal_body)}
    _write(root / "research-summary.json", summary)
    _write(root / "terminal-disposition.json", terminal)

    triads = [
        {
            "model_id": "ibm-granite/granite-4.1-8b",
            "split": "development",
            "case_id": "causal-1",
            "presentation": 0,
            "arm": arm,
            "complete": True,
            "counterfactual_causal": True,
            "counterfactual_plan_changed": True,
            "withheld_safely_contained_or_conformant": True,
        }
        for arm in ("A1", "A3P", "A0xF")
    ]
    tables = {
        "attempts": {"schema_version": "1.0", "rows": attempts},
        "branch-outcomes": {"schema_version": "1.0", "rows": branches},
        "causal-triads": {"schema_version": "1.0", "rows": triads},
        "metric-breakdowns": {"schema_version": "1.0", "rows": []},
        "configuration-summary": summary,
        "structural-plan-analysis": structural,
        "cost-ledger": {"schema_version": "1.0", "rows": [usage]},
    }
    table_hashes = {}
    for name, value in tables.items():
        path = root / "tables" / f"{name}.json"
        _write(path, value)
        table_hashes[name] = _sha256(path)
    manifest_body = {
        "schema_version": "1.0",
        "study_version": module.STUDY_VERSION,
        "release_eligible": False,
        "table_sha256": table_hashes,
    }
    _write(
        root / "tables" / "manifest.json",
        {**manifest_body, "manifest_sha256": module._json_sha256(manifest_body)},
    )

    publication_digest = _publication_digest(module, root / "tables")

    receipt_path = root / "wandb-study-publication.json"
    receipt = {
        "schema_version": "1.0",
        "campaign_version": module.CAMPAIGN_VERSION,
        "kind": "architecture_development_study",
        "entity": module.TARGET_ENTITY,
        "project": module.TARGET_PROJECT,
        "project_access": "RESTRICTED",
        "run_id": "1234567890abcdef12345678",
        "run_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/"
            "1234567890abcdef12345678"
        ),
        "artifact_reference": (
            "wandb/gi-scenario-studio-agent-evals/"
            "evidence-guide-v2-architecture-development-deadbeef:v1"
        ),
        "source_sha256": publication_digest,
        "readback_sha256": publication_digest,
        "weave_evaluation_url": (
            "https://wandb.ai/wandb/gi-scenario-studio-agent-evals/r/call/"
            "12345678-1234-1234-1234-123456789abc"
        ),
        "run_file_privacy_audit": {"prohibited_files": 0, "files_checked": 3},
    }
    _write(receipt_path, receipt)
    return receipt_path


def test_builds_deterministic_receipt_bound_article_package(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    receipt = _fixture(module, source)
    first = tmp_path / "first"
    second = tmp_path / "second"

    result = module.build_article_package(REPO_ROOT, source, receipt, first)
    module.build_article_package(REPO_ROOT, source, receipt, second)

    for name in (
        "article-summary.json",
        "article-table.json",
        "article-media.png",
        "article-claim-ledger.json",
    ):
        assert _sha256(first / name) == _sha256(second / name)
        assert result["outputs"][name] == _sha256(first / name)

    with Image.open(first / "article-media.png") as image:
        assert image.size == (1920, 1080)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (245, 247, 246)
        half = image.resize((960, 540))
        assert half.getbbox() == (0, 0, 960, 540)

    summary = json.loads((first / "article-summary.json").read_text(encoding="utf-8"))
    summary_body = {key: value for key, value in summary.items() if key != "summary_sha256"}
    assert summary["summary_sha256"] == module._report_json_sha256(summary_body)
    assert summary["attempt_count"] == 11
    assert summary["generated_invalid_count"] == 1
    assert summary["released_invalid_count"] == 0
    assert summary["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert summary["practitioner_usefulness_status"] == "not_measured"
    assert summary["energy_status"] == "not_measured"
    assert summary["ecological_outcomes_status"] == "not_measured"
    assert summary["usage"] == {
        "cost_usd": "0.01",
        "provider_invocations": 11,
        "transport_attempts": 12,
        "input_tokens": 1000,
        "output_tokens": 100,
    }
    forbidden = " ".join(
        [summary["headline"], summary["plain_language_finding"], summary["media_caption"]]
    ).lower()
    assert "practitioner usefulness improved" not in forbidden
    assert "ecological benefit" not in forbidden
    assert "model reasoning" not in forbidden

    table = json.loads((first / "article-table.json").read_text(encoding="utf-8"))
    assert table["columns"] == list(module.ARTICLE_TABLE_COLUMNS)
    assert sum(row["attempt_count"] for row in table["rows"]) == 11
    assert sum(row["generated_invalid_count"] for row in table["rows"]) == 1
    assert {row["architecture_id"] for row in table["rows"]} >= {
        "D1",
        "A1",
        "A3P",
        "A0xF",
    }
    assert sum(row["logical_provider_calls"] for row in table["rows"]) == 11
    assert sum(row["transport_attempts"] for row in table["rows"]) == 12

    ledger = json.loads((first / "article-claim-ledger.json").read_text(encoding="utf-8"))
    assert ledger["human_question"].startswith("Did model assistance")
    assert "model reasoning for singleton finite plans" in ledger["forbidden_overclaims"]
    assert all(not Path(path).is_absolute() for path in ledger["source_file_sha256"])
    assert any(
        claim["claim_id"] == "agent-study.actual-releases" and claim["value"] == 0
        for claim in ledger["claims"]
    )


def test_final_publication_wrapper_is_post_content_and_self_hashed(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    raw_receipt = _fixture(module, source)
    output = tmp_path / "article"
    module.build_article_package(REPO_ROOT, source, raw_receipt, output)

    final_path = output / "wandb-publication.json"
    final = module.finalize_publication_receipt(raw_receipt, output, final_path)
    body = {key: value for key, value in final.items() if key != "publication_sha256"}

    assert final["publication_sha256"] == module._report_json_sha256(body)
    assert final["article_summary_sha256"] == _sha256(output / "article-summary.json")
    assert final["article_table_sha256"] == _sha256(output / "article-table.json")
    assert final["article_media_sha256"] == _sha256(output / "article-media.png")
    assert final["project_access"] == "RESTRICTED"
    assert final["release_eligible"] is False
    assert final["artifact"].endswith(":v1")
    assert _sha256(raw_receipt) == final["source_publication_receipt_file_sha256"]

    publisher = _load_publisher()
    publisher.EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_SUMMARY = output / "article-summary.json"
    publisher.EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_TABLE = output / "article-table.json"
    publisher.EVIDENCE_GUIDE_V2_ARCH_DEV_ARTICLE_MEDIA = output / "article-media.png"
    publisher.EVIDENCE_GUIDE_V2_ARCH_DEV_PUBLICATION_RECEIPT = final_path
    projected = publisher._load_evidence_guide_v2_architecture_development()
    assert projected["available"] is True
    assert projected["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert projected["release_eligible"] is False
    assert projected["rows"]


def test_rejects_source_table_hash_drift(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    receipt = _fixture(module, source)
    branch_path = source / "tables" / "branch-outcomes.json"
    branch_table = json.loads(branch_path.read_text(encoding="utf-8"))
    branch_table["rows"][0]["candidate_conformant"] = False
    _write(branch_path, branch_table)

    with pytest.raises(module.ArticlePackageError, match="source table hash drifted"):
        module.build_article_package(REPO_ROOT, source, receipt, tmp_path / "article")


def test_accepts_real_cli_publication_layout_and_nested_receipt(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    raw_receipt_path = _fixture(module, source)
    publication_root = source / "development-publication"
    (source / "tables").rename(publication_root)
    _write(
        publication_root / "research-summary.json",
        json.loads((source / "research-summary.json").read_text(encoding="utf-8")),
    )
    _write(
        publication_root / "terminal-disposition.json",
        json.loads((source / "terminal-disposition.json").read_text(encoding="utf-8")),
    )
    raw = json.loads(raw_receipt_path.read_text(encoding="utf-8"))
    digest = _publication_digest(module, publication_root)
    raw["source_sha256"] = digest
    raw["readback_sha256"] = digest
    execution_receipt = source / "execution-receipt.json"
    _write(
        execution_receipt,
        {
            "mode": "execute",
            "release_eligible": False,
            "development_publication": raw,
        },
    )

    result = module.build_article_package(
        REPO_ROOT, source, execution_receipt, tmp_path / "article"
    )

    assert result["release_eligible"] is False
    assert (tmp_path / "article" / "article-media.png").is_file()


def test_rejects_any_actual_answer_release(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    receipt = _fixture(module, source, release_one=True)

    with pytest.raises(module.ArticlePackageError, match="actual released answer"):
        module.build_article_package(REPO_ROOT, source, receipt, tmp_path / "article")


def test_rejects_singleton_plan_attribution_drift(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "study"
    receipt = _fixture(module, source)
    structural_path = source / "tables" / "structural-plan-analysis.json"
    structural = json.loads(structural_path.read_text(encoding="utf-8"))
    structural["model_reasoning_attribution_permitted"] = True
    structural_body = {
        key: value for key, value in structural.items() if key != "analysis_sha256"
    }
    structural["analysis_sha256"] = module._json_sha256(structural_body)
    _write(structural_path, structural)
    manifest_path = source / "tables" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["table_sha256"]["structural-plan-analysis"] = _sha256(structural_path)
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = module._json_sha256(manifest_body)
    _write(manifest_path, manifest)
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    publication_digest = _publication_digest(module, source / "tables")
    receipt_value["source_sha256"] = publication_digest
    receipt_value["readback_sha256"] = publication_digest
    _write(receipt, receipt_value)

    with pytest.raises(module.ArticlePackageError, match="singleton boundary"):
        module.build_article_package(REPO_ROOT, source, receipt, tmp_path / "article")
