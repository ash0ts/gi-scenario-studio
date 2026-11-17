"""Evaluate saved Evidence Guide outputs and optionally publish verified Weave evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gi_scenario_core.evidence_candidate_evaluation import (
    CANDIDATE_MODELS,
    CandidateRun,
    json_sha256,
    model_slug,
    release_decision,
)
from gi_scenario_core.evidence_evaluation import (
    SCORER_VERSION,
    behavior_summary,
    dataset_sha256,
    evaluate_outputs,
    load_dataset,
    load_outputs,
    reference_outputs,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks" / "evidence_guide" / "v1" / "dataset.json"
DATASET_MANIFEST = DATASET.with_name("manifest.json")
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-evaluation" / "evaluation.json"
DEFAULT_RECEIPT = ROOT / "dist" / "evidence-guide-evaluation" / "wandb-publication.json"
DEFAULT_CAMPAIGN_PLAN = (
    ROOT / "dist" / "evidence-guide-candidate-evaluation" / "weave-publication-plan.json"
)
DEFAULT_CAMPAIGN_RECEIPT = (
    ROOT / "dist" / "evidence-guide-candidate-evaluation" / "weave-publication.json"
)
PROJECT_POLICY_GQL = """
query GIEvidenceGuideProjectPolicy($name: String!, $entity: String!) {
  project(name: $name, entityName: $entity) {
    id
    name
    entityName
    access
  }
}
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _validated_dataset() -> tuple[dict[str, Any], dict[str, Any], str]:
    dataset = load_dataset(DATASET)
    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    digest = dataset_sha256(DATASET)
    required = {
        "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": digest,
        "row_count": dataset["row_count"],
        "license": dataset["license"],
        "scorer_version": SCORER_VERSION,
    }
    drifted = [name for name, value in required.items() if manifest.get(name) != value]
    if drifted:
        raise RuntimeError("Evidence Guide dataset manifest drifted: " + ", ".join(drifted))
    return dataset, manifest, digest


def build_evaluation(
    *,
    outputs_path: Path | None,
    use_reference: bool,
    candidate_id: str | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    dataset, _, digest = _validated_dataset()
    if use_reference:
        outputs = reference_outputs(dataset)
        source = {
            "kind": "synthetic_scorer_conformance_projection",
            "candidate_id": candidate_id or "synthetic-reference-projection-v1",
            "reference_projection": True,
        }
    else:
        if outputs_path is None:
            raise ValueError("an outputs path is required for a non-reference evaluation")
        outputs = load_outputs(outputs_path)
        source = {
            "kind": "saved_evidence_guide_outputs",
            "candidate_id": candidate_id or outputs_path.stem,
            "outputs_file": outputs_path.name,
            "outputs_sha256": _sha256(outputs_path),
            "reference_projection": False,
        }
    result = evaluate_outputs(dataset, outputs)
    result.update(
        {
            "dataset_sha256": digest,
            "dataset_license": dataset["license"],
            "source": source,
            "claim_boundary": (
                "This evaluation measures deterministic explanation fidelity on synthetic cases. "
                "It does not establish hydraulic, field, practitioner-usefulness, or service quality."
            ),
        }
    )
    return result, outputs


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} was not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object")
    return value


def _provider_usage_projection(candidate_receipt: dict[str, Any]) -> dict[str, Any]:
    """Project aggregate provider usage without publishing token-like field names.

    ``token`` remains a prohibited trace-key fragment because credential fields
    frequently use it.  The receipt is local evidence, so this boundary copies
    only the three non-secret aggregate counts under unambiguous safe names.
    """

    usage = candidate_receipt.get("usage")
    if not isinstance(usage, dict):
        raise TypeError("candidate receipt usage must be an object")
    projected: dict[str, Any] = {"unit": "tokens"}
    for source, destination in (
        ("input_tokens", "input_count"),
        ("output_tokens", "output_count"),
        ("total_tokens", "total_count"),
    ):
        value = usage.get(source)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TypeError(f"candidate receipt usage {source} must be a non-negative integer")
        projected[destination] = value
    return projected


def _provider_failure_projection(candidate_receipt: dict[str, Any]) -> list[dict[str, str]]:
    failures = candidate_receipt.get("provider_failures")
    if not isinstance(failures, list):
        raise TypeError("candidate receipt provider_failures must be a list")
    projected: list[dict[str, str]] = []
    for index, failure in enumerate(failures):
        if not isinstance(failure, dict):
            raise TypeError(f"candidate receipt provider_failures[{index}] must be an object")
        case_id = failure.get("case_id")
        error_code = failure.get("error_code")
        if not isinstance(case_id, str) or not case_id:
            raise TypeError(
                f"candidate receipt provider_failures[{index}].case_id must be a string"
            )
        if not isinstance(error_code, str) or not error_code:
            raise TypeError(
                f"candidate receipt provider_failures[{index}].error_code must be a string"
            )
        projected.append({"case_id": case_id, "error_code": error_code})
    return projected


def _latency_projection(candidate_receipt: dict[str, Any]) -> dict[str, int | float | None]:
    latency = candidate_receipt.get("latency_ms")
    if not isinstance(latency, dict):
        raise TypeError("candidate receipt latency_ms must be an object")
    projected: dict[str, int | float | None] = {}
    for field in ("count", "total", "minimum", "median", "p95", "maximum"):
        if field not in latency:
            continue
        value = latency[field]
        if value is None:
            projected[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"candidate receipt latency_ms.{field} must be numeric or null")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"candidate receipt latency_ms.{field} must be finite and non-negative")
        projected[field] = value
    return projected


def _candidate_receipt_projection(candidate_receipt: dict[str, Any]) -> dict[str, Any]:
    """Return the allowlisted cloud representation of a local candidate receipt."""

    scalar_fields = (
        "schema_version",
        "evaluation_version",
        "release_gate_version",
        "model_id",
        "dataset_id",
        "dataset_version",
        "dataset_sha256",
        "scorer_version",
        "prompt_version",
        "prompt_sha256",
        "schema_version_id",
        "provider",
        "provider_call_count",
        "skipped_case_count",
        "outputs_sha256",
        "evaluation_sha256",
        "started_at",
        "completed_at",
        "reference_projection",
        "publishable_as_reference_projection",
        "claim_boundary",
    )
    projection = {
        field: candidate_receipt[field]
        for field in scalar_fields
        if field in candidate_receipt
    }
    projection.update(
        {
            "provider_call_id_hashes": list(
                candidate_receipt.get("provider_call_id_hashes", [])
            ),
            "provider_failures": _provider_failure_projection(candidate_receipt),
            "provider_usage": _provider_usage_projection(candidate_receipt),
            "latency_ms": _latency_projection(candidate_receipt),
            "deterministic_summary": candidate_receipt.get("deterministic_summary", {}),
            "selection_summary": candidate_receipt.get("selection_summary", {}),
            "practitioner_usefulness": candidate_receipt.get(
                "practitioner_usefulness", {}
            ),
            "release_gate": candidate_receipt.get("release_gate", {}),
            "candidate_receipt_sha256": json_sha256(candidate_receipt),
        }
    )
    return projection


def _validate_evaluation_package(
    *,
    evaluation: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    evaluation_path: Path,
    candidate_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate local evidence without requiring the candidate to have passed."""

    dataset, _manifest, digest = _validated_dataset()
    source = evaluation.get("source")
    if not isinstance(source, dict) or source.get("reference_projection") is not False:
        raise RuntimeError("only provider or saved candidate outputs may be published")
    candidate_id = source.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("evaluation source requires a candidate_id")
    if evaluation.get("dataset_sha256") != digest:
        raise ValueError("evaluation dataset hash does not match the versioned corpus")
    if evaluation.get("scorer_version") != SCORER_VERSION:
        raise ValueError("evaluation scorer version does not match the local release")

    recomputed = evaluate_outputs(dataset, outputs)
    for field in (
        "schema_version",
        "dataset_id",
        "dataset_version",
        "scorer_version",
        "summary",
        "case_results",
    ):
        if evaluation.get(field) != recomputed[field]:
            raise ValueError(f"evaluation {field} does not match the saved candidate outputs")
    materialized = _load_json_object(evaluation_path, label="evaluation manifest")
    if materialized != evaluation:
        raise ValueError("evaluation file bytes do not match the publication payload")

    disposition = (
        "passed" if bool(evaluation["summary"].get("release_gate_passed")) else "failed"
    )
    provider_failure_count = 0
    if candidate_receipt is not None:
        if candidate_receipt.get("reference_projection") is not False:
            raise ValueError("candidate receipt cannot describe a reference projection")
        expected = {
            "model_id": candidate_id,
            "dataset_sha256": digest,
            "scorer_version": SCORER_VERSION,
            "outputs_sha256": json_sha256(outputs),
            "evaluation_sha256": json_sha256(evaluation),
            "deterministic_summary": evaluation["summary"],
        }
        drifted = [name for name, value in expected.items() if candidate_receipt.get(name) != value]
        if drifted:
            raise ValueError("candidate receipt integrity drifted: " + ", ".join(drifted))
        receipt_projection = _candidate_receipt_projection(candidate_receipt)
        failures = receipt_projection["provider_failures"]
        provider_failure_count = len(failures)
        release_gate = candidate_receipt.get("release_gate")
        if not isinstance(release_gate, dict) or not isinstance(release_gate.get("passed"), bool):
            raise TypeError("candidate receipt requires a boolean release-gate result")
        disposition = "passed" if release_gate["passed"] else "failed"

    return {
        "candidate_id": candidate_id,
        "disposition": disposition,
        "total_cases": int(evaluation["summary"]["total"]),
        "evaluated_cases": int(evaluation["summary"]["evaluated"]),
        "missing_cases": int(evaluation["summary"]["missing"]),
        "passed_cases": int(evaluation["summary"]["passed"]),
        "provider_failure_count": provider_failure_count,
        "evaluation_manifest_sha256": _sha256(evaluation_path),
    }


def load_candidate_campaign(directory: Path) -> dict[str, Any]:
    """Load and integrity-check a complete two-candidate campaign."""

    root = directory.resolve()
    dataset, _manifest, digest = _validated_dataset()
    release_gate_path = root / "release-gate.json"
    campaign_receipt = _load_json_object(release_gate_path, label="campaign release gate")
    packages: list[dict[str, Any]] = []
    runs: list[CandidateRun] = []
    for model_id in CANDIDATE_MODELS:
        candidate_root = root / model_slug(model_id)
        outputs_path = candidate_root / "outputs.json"
        evaluation_path = candidate_root / "evaluation.json"
        receipt_path = candidate_root / "receipt.json"
        outputs = load_outputs(outputs_path)
        evaluation = _load_json_object(evaluation_path, label=f"{model_id} evaluation")
        receipt = _load_json_object(receipt_path, label=f"{model_id} receipt")
        validation = _validate_evaluation_package(
            evaluation=evaluation,
            outputs=outputs,
            evaluation_path=evaluation_path,
            candidate_receipt=receipt,
        )
        receipt_hash = json_sha256(receipt)
        if campaign_receipt.get("candidate_receipt_sha256", {}).get(model_id) != receipt_hash:
            raise ValueError(f"campaign release gate does not pin the {model_id} receipt")
        packages.append(
            {
                "model_id": model_id,
                "outputs": outputs,
                "evaluation": evaluation,
                "candidate_receipt": receipt,
                "evaluation_path": evaluation_path,
                "receipt_path": receipt_path,
                "validation": validation,
            }
        )
        runs.append(
            CandidateRun(
                model_id=model_id,
                outputs=outputs,
                evaluation=evaluation,
                receipt=receipt,
            )
        )
    expected_gate = release_decision(runs, dataset=dataset, dataset_path=DATASET)
    if campaign_receipt != expected_gate:
        raise ValueError("campaign release gate does not match the candidate receipts")
    if campaign_receipt.get("dataset_sha256") != digest:
        raise ValueError("campaign dataset hash does not match the versioned corpus")
    return {
        "root": root,
        "release_gate_path": release_gate_path,
        "release_gate": campaign_receipt,
        "packages": packages,
    }


def campaign_publication_plan(campaign: dict[str, Any]) -> dict[str, Any]:
    gate = campaign["release_gate"]
    return {
        "schema_version": "1.0",
        "mode": "candidate_campaign",
        "dataset_id": gate["dataset_id"],
        "dataset_version": gate["dataset_version"],
        "dataset_sha256": gate["dataset_sha256"],
        "candidate_count": len(campaign["packages"]),
        "decision": gate["decision"],
        "selected_model": gate["selected_model"],
        "network_calls": 0,
        "publishable_failure_evidence": True,
        "candidate_evaluations": [item["validation"] for item in campaign["packages"]],
        "experiment_scope": {
            "kind": "single_call_structured_evidence_selection",
            "runtime_agent_loop_evaluated": False,
            "evidence_ids_and_actions_schema_constrained": True,
            "provider_generates_numeric_values": False,
            "canonical_numeric_values_materialized_locally": True,
        },
        "candidate_behavior": {
            item["model_id"]: {
                "behavior": behavior_summary(item["evaluation"]),
                "practitioner_usefulness": item["candidate_receipt"].get(
                    "practitioner_usefulness", {}
                ),
                "provider_usage": _provider_usage_projection(item["candidate_receipt"]),
                "provider_latency_ms": _latency_projection(item["candidate_receipt"]),
            }
            for item in campaign["packages"]
        },
        "campaign_release_gate_sha256": _sha256(campaign["release_gate_path"]),
        "claim_boundary": gate["claim_boundary"],
    }


def _project_policy(api: Any, entity: str, project: str) -> dict[str, str] | None:
    response = api._service_api.execute_graphql(
        PROJECT_POLICY_GQL,
        {"name": project, "entity": entity},
    )
    record = response.get("project")
    if record is None:
        return None
    required = ("id", "name", "entityName", "access")
    if any(not record.get(field) for field in required):
        raise RuntimeError("W&B returned an incomplete project policy")
    if record["name"] != project or record["entityName"] != entity:
        raise RuntimeError("W&B project policy did not match the requested destination")
    return {field: str(record[field]) for field in required}


def _actual_url(value: Any, *, label: str, entity: str, project: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} was not returned by the publication API")
    parsed = urlparse(value)
    expected_prefix = f"/{entity}/{project}/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "wandb.ai"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(expected_prefix)
        or parsed.fragment
    ):
        raise RuntimeError(f"{label} did not match the exact W&B Cloud destination")
    return value


def _defensive_environment() -> None:
    cloud_root = ROOT / "dist" / "evidence-guide-evaluation" / "wandb"
    for name, child in (
        ("WANDB_DIR", "run"),
        ("WANDB_CACHE_DIR", "cache"),
        ("WANDB_ARTIFACT_DIR", "artifacts"),
    ):
        path = cloud_root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    defaults = {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
    }
    for name, value in defaults.items():
        os.environ[name] = value


def _publication_context(*, entity: str, project: str) -> dict[str, Any]:
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    _defensive_environment()

    import wandb
    import weave

    api = wandb.Api(
        overrides={"base_url": os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")}
    )
    policy = _project_policy(api, entity, project)
    if policy is None:
        raise RuntimeError("the requested W&B project does not exist")
    if policy["access"].upper() != "RESTRICTED":
        raise RuntimeError(
            f"W&B project access is {policy['access'].upper()}, not RESTRICTED; nothing was published"
        )

    dataset = load_dataset(DATASET)
    client = weave.init(f"{entity}/{project}", settings={"redact_pii": False})
    from gi_scenario_core.observability import (
        _configure_weave_sensitive_key_redaction,
        _prepare_trace_value,
    )

    _configure_weave_sensitive_key_redaction()
    weave_dataset = weave.Dataset(
        name=f"{dataset['dataset_id']}-{dataset['dataset_version']}",
        description=_prepare_trace_value(
            dataset["description"], context="evaluation dataset description"
        ),
        rows=_prepare_trace_value(dataset["rows"], context="evaluation dataset rows"),
    )
    dataset_ref = weave.publish(
        weave_dataset,
        name=f"{dataset['dataset_id']}-{dataset['dataset_version']}",
    )
    published_dataset = dataset_ref.get()
    published_rows = {row["case_id"]: row for row in published_dataset.rows}
    if set(published_rows) != {row["case_id"] for row in dataset["rows"]}:
        raise RuntimeError("published Weave Dataset does not contain the versioned case set")
    return {
        "wandb": wandb,
        "weave": weave,
        "api": api,
        "policy": policy,
        "entity": entity,
        "project": project,
        "client": client,
        "dataset": dataset,
        "dataset_ref": dataset_ref,
        "published_rows": published_rows,
        "prepare_trace_value": _prepare_trace_value,
    }


def _publish_one_evaluation(
    context: dict[str, Any],
    *,
    evaluation: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    validation: dict[str, Any],
    candidate_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    weave = context["weave"]
    dataset = context["dataset"]
    dataset_ref = context["dataset_ref"]
    published_rows = context["published_rows"]
    prepare = context["prepare_trace_value"]
    receipt_projection = (
        _candidate_receipt_projection(candidate_receipt)
        if candidate_receipt is not None
        else None
    )
    logger = weave.EvaluationLogger(
        name=(
            f"GI Evidence Guide {dataset['dataset_version']} · "
            f"{evaluation['source']['candidate_id']}"
        ),
        model=str(evaluation["source"]["candidate_id"]),
        dataset=dataset_ref.uri(),
        scorers=[*evaluation["summary"]["score_rates"], "deterministic_overall"],
        eval_attributes={
            "dataset_sha256": evaluation["dataset_sha256"],
            "scorer_version": evaluation["scorer_version"],
            "synthetic_only": True,
            "candidate_disposition": validation["disposition"],
            "missing_case_count": validation["missing_cases"],
            "provider_failure_count": validation["provider_failure_count"],
        },
    )
    results = {item["case_id"]: item for item in evaluation["case_results"]}
    for case in dataset["rows"]:
        case_id = case["case_id"]
        output = outputs.get(case_id)
        if output is None:
            output = {
                "status": "not_produced",
                "reason": "provider_or_local_validation_failure",
                "case_id": case_id,
            }
        prediction = logger.log_prediction(
            inputs=published_rows[case_id],
            output=prepare(output, context="evaluation candidate output"),
            example_id=case_id,
        )
        for scorer_name, score in results[case_id]["scores"].items():
            prediction.log_score(scorer=scorer_name, score=score)
        prediction.log_score(
            scorer="deterministic_overall",
            score=bool(results[case_id]["passed"]),
        )
        prediction.finish()
    logger.log_summary(
        summary=prepare(
            {
            "deterministic": evaluation["summary"],
            "behavior": behavior_summary(evaluation),
            "dataset_sha256": evaluation["dataset_sha256"],
            "scorer_version": evaluation["scorer_version"],
            "candidate_disposition": validation["disposition"],
            "provider_failures": (
                receipt_projection["provider_failures"]
                if receipt_projection is not None
                else []
            ),
            "provider_usage": (
                receipt_projection["provider_usage"]
                if receipt_projection is not None
                else {}
            ),
            "provider_latency_ms": (
                receipt_projection["latency_ms"]
                if receipt_projection is not None
                else {}
            ),
            "practitioner_usefulness": (
                receipt_projection["practitioner_usefulness"]
                if receipt_projection is not None
                else {
                    "status": "not_measured",
                    "rating_count": 0,
                    "unique_reviewers": 0,
                    "case_coverage": 0,
                    "positive_rate": None,
                    "passed": False,
                }
            ),
            "candidate_receipt_sha256": (
                receipt_projection["candidate_receipt_sha256"]
                if receipt_projection is not None
                else None
            ),
            },
            context="evaluation summary",
        )
    )
    return {
        "candidate_id": validation["candidate_id"],
        "disposition": validation["disposition"],
        "evaluation_url": _actual_url(
            logger.ui_url,
            label="Weave evaluation URL",
            entity=context["entity"],
            project=context["project"],
        ),
        "summary": evaluation["summary"],
        "behavior_summary": behavior_summary(evaluation),
        "practitioner_usefulness": (
            receipt_projection["practitioner_usefulness"]
            if receipt_projection is not None
            else {
                "status": "not_measured",
                "rating_count": 0,
                "unique_reviewers": 0,
                "case_coverage": 0,
                "positive_rate": None,
                "passed": False,
            }
        ),
        "provider_failure_count": validation["provider_failure_count"],
        "evaluation_manifest_sha256": validation["evaluation_manifest_sha256"],
        "candidate_receipt_sha256": (
            json_sha256(candidate_receipt) if candidate_receipt is not None else None
        ),
    }


def _verify_final_policy(context: dict[str, Any], *, entity: str, project: str) -> None:
    final_policy = _project_policy(context["api"], entity, project)
    policy = context["policy"]
    if (
        final_policy is None
        or final_policy["id"] != policy["id"]
        or final_policy["access"].upper() != "RESTRICTED"
    ):
        raise RuntimeError("W&B project identity or Restricted policy changed during publication")


def publish_evaluation(
    *,
    evaluation: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    evaluation_path: Path,
    entity: str,
    project: str,
    candidate_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if evaluation["source"]["reference_projection"]:
        raise RuntimeError(
            "the scorer-conformance projection cannot be published as model evidence"
        )
    validation = _validate_evaluation_package(
        evaluation=evaluation,
        outputs=outputs,
        evaluation_path=evaluation_path,
        candidate_receipt=candidate_receipt,
    )
    context = _publication_context(entity=entity, project=project)
    published = _publish_one_evaluation(
        context,
        evaluation=evaluation,
        outputs=outputs,
        validation=validation,
        candidate_receipt=candidate_receipt,
    )
    context["client"].flush()
    _verify_final_policy(context, entity=entity, project=project)

    receipt = {
        "schema_version": "1.0",
        "publication_type": "candidate_evaluation",
        "entity": entity,
        "project": project,
        "project_access": "RESTRICTED",
        "dataset_id": context["dataset"]["dataset_id"],
        "dataset_version": context["dataset"]["dataset_version"],
        "dataset_sha256": evaluation["dataset_sha256"],
        "dataset_ref": context["dataset_ref"].uri(),
        "scorer_version": evaluation["scorer_version"],
        "candidate_id": evaluation["source"]["candidate_id"],
        "evaluation_manifest_sha256": _sha256(evaluation_path),
        "evaluation_url": published["evaluation_url"],
        "trace_urls": [],
        "summary": evaluation["summary"],
        "candidate_disposition": validation["disposition"],
        "failed_evaluation_published": validation["disposition"] == "failed",
        "provider_failure_count": validation["provider_failure_count"],
        "candidate_receipt_sha256": published["candidate_receipt_sha256"],
        "published_at": datetime.now(UTC).isoformat(),
        "claim_boundary": evaluation["claim_boundary"],
    }
    return receipt


def publish_candidate_campaign(
    *,
    campaign: dict[str, Any],
    entity: str,
    project: str,
) -> dict[str, Any]:
    context = _publication_context(entity=entity, project=project)
    publications = []
    for package in campaign["packages"]:
        publications.append(
            _publish_one_evaluation(
                context,
                evaluation=package["evaluation"],
                outputs=package["outputs"],
                validation=package["validation"],
                candidate_receipt=package["candidate_receipt"],
            )
        )

    campaign_evidence = context["prepare_trace_value"](
        {
            "schema_version": "1.0",
            "evidence_type": "evidence_guide_candidate_campaign",
            "release_gate": campaign["release_gate"],
            "candidate_receipt_projections": [
                _candidate_receipt_projection(package["candidate_receipt"])
                for package in campaign["packages"]
            ],
            "candidate_evaluations": publications,
            "experiment_scope": {
                "kind": "single_call_structured_evidence_selection",
                "runtime_agent_loop_evaluated": False,
                "evidence_ids_and_actions_schema_constrained": True,
                "provider_generates_numeric_values": False,
                "canonical_numeric_values_materialized_locally": True,
            },
            "dataset_ref": context["dataset_ref"].uri(),
            "campaign_release_gate_sha256": _sha256(campaign["release_gate_path"]),
        },
        context="candidate campaign evidence",
    )
    campaign_ref = context["weave"].publish(
        campaign_evidence,
        name=(
            "gi-evidence-guide-candidate-campaign-"
            f"{campaign['release_gate']['dataset_version']}-"
            f"{campaign['release_gate']['dataset_sha256'][:12]}"
        ),
    )
    if campaign_ref.get() is None:
        raise RuntimeError("published Weave campaign evidence could not be read back")
    context["client"].flush()
    _verify_final_policy(context, entity=entity, project=project)
    failed = sum(item["disposition"] == "failed" for item in publications)
    return {
        "schema_version": "1.0",
        "publication_type": "candidate_campaign",
        "entity": entity,
        "project": project,
        "project_access": "RESTRICTED",
        "dataset_id": campaign["release_gate"]["dataset_id"],
        "dataset_version": campaign["release_gate"]["dataset_version"],
        "dataset_sha256": campaign["release_gate"]["dataset_sha256"],
        "dataset_ref": context["dataset_ref"].uri(),
        "campaign_evidence_ref": campaign_ref.uri(),
        "campaign_release_gate_sha256": _sha256(campaign["release_gate_path"]),
        "decision": campaign["release_gate"]["decision"],
        "selected_model": campaign["release_gate"]["selected_model"],
        "candidate_evaluations": publications,
        "experiment_scope": {
            "kind": "single_call_structured_evidence_selection",
            "runtime_agent_loop_evaluated": False,
            "evidence_ids_and_actions_schema_constrained": True,
            "provider_generates_numeric_values": False,
            "canonical_numeric_values_materialized_locally": True,
        },
        "failed_candidate_count": failed,
        "failed_evaluations_published": failed > 0,
        "published_at": datetime.now(UTC).isoformat(),
        "claim_boundary": campaign["release_gate"]["claim_boundary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--validate-only", action="store_true")
    source.add_argument("--reference", action="store_true")
    source.add_argument("--outputs", type=Path)
    source.add_argument(
        "--campaign-dir",
        type=Path,
        help="validate or publish both candidate evaluations and their release-gate receipt",
    )
    parser.add_argument("--candidate-id")
    parser.add_argument("--candidate-receipt", type=Path)
    parser.add_argument("--output", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="validate publication inputs without credentials, SDK imports, or network calls",
    )
    action.add_argument("--publish", action="store_true")
    parser.add_argument("--entity", default=os.environ.get("WANDB_REPORT_ENTITY"))
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args()

    dataset, manifest, digest = _validated_dataset()
    if arguments.validate_only:
        if arguments.dry_run or arguments.publish:
            parser.error("--validate-only does not accept --dry-run or --publish")
        print(
            json.dumps(
                {
                    "dataset_id": dataset["dataset_id"],
                    "dataset_version": dataset["dataset_version"],
                    "dataset_sha256": digest,
                    "row_count": dataset["row_count"],
                    "license": manifest["license"],
                },
                sort_keys=True,
            )
        )
        return

    if arguments.campaign_dir is not None:
        if arguments.candidate_id or arguments.candidate_receipt:
            parser.error("--campaign-dir does not accept single-candidate options")
        campaign = load_candidate_campaign(arguments.campaign_dir)
        plan = campaign_publication_plan(campaign)
        output_path = arguments.output or DEFAULT_CAMPAIGN_PLAN
        _write_json_atomic(output_path, plan)
        response: dict[str, Any] = {
            "mode": "dry_run" if arguments.dry_run else "local_validation",
            "network_calls": 0,
            "publication_plan": str(output_path),
            "decision": plan["decision"],
            "candidate_count": plan["candidate_count"],
            "failed_candidate_count": sum(
                item["disposition"] == "failed" for item in plan["candidate_evaluations"]
            ),
        }
        if arguments.publish:
            if not arguments.entity or not arguments.project:
                parser.error("--publish requires --entity and --project")
            receipt = publish_candidate_campaign(
                campaign=campaign,
                entity=arguments.entity,
                project=arguments.project,
            )
            receipt_path = arguments.receipt or DEFAULT_CAMPAIGN_RECEIPT
            _write_json_atomic(receipt_path, receipt)
            response.update(
                {
                    "mode": "publish",
                    "receipt": str(receipt_path),
                    "network_calls": None,
                    "campaign_evidence_ref": receipt["campaign_evidence_ref"],
                }
            )
        print(json.dumps(response, sort_keys=True))
        return

    evaluation, outputs = build_evaluation(
        outputs_path=arguments.outputs,
        use_reference=arguments.reference,
        candidate_id=arguments.candidate_id,
    )
    output_path = arguments.output or DEFAULT_OUTPUT
    _write_json_atomic(output_path, evaluation)
    candidate_receipt = (
        _load_json_object(arguments.candidate_receipt, label="candidate receipt")
        if arguments.candidate_receipt is not None
        else None
    )
    validation = None
    if not arguments.reference:
        validation = _validate_evaluation_package(
            evaluation=evaluation,
            outputs=outputs,
            evaluation_path=output_path,
            candidate_receipt=candidate_receipt,
        )
    response: dict[str, Any] = {
        "mode": "dry_run" if arguments.dry_run else "local_evaluation",
        "network_calls": 0,
        "evaluation": str(output_path),
        "summary": evaluation["summary"],
        "publication_validation": validation,
    }
    if arguments.publish:
        if not arguments.entity or not arguments.project:
            parser.error("--publish requires --entity and --project")
        receipt = publish_evaluation(
            evaluation=evaluation,
            outputs=outputs,
            evaluation_path=output_path,
            entity=arguments.entity,
            project=arguments.project,
            candidate_receipt=candidate_receipt,
        )
        receipt_path = arguments.receipt or DEFAULT_RECEIPT
        _write_json_atomic(receipt_path, receipt)
        response["mode"] = "publish"
        response["network_calls"] = None
        response["receipt"] = str(receipt_path)
        response["evaluation_url"] = receipt["evaluation_url"]
    print(json.dumps(response, sort_keys=True))


if __name__ == "__main__":
    main()
