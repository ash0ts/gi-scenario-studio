from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def policy() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_workflows.py"
    spec = importlib.util.spec_from_file_location("workflow_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_temp_is_rejected_at_workflow_and_job_env_scope(policy: ModuleType) -> None:
    workflow = """\
permissions:\n  contents: read\nenv:\n  CACHE: ${{ runner.temp }}/cache\njobs:\n  test:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    env:\n      DATA: ${{ runner.temp }}/data\n    steps: []\n"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)
    assert sum("runner context used before step scope" in item for item in findings) == 2


def test_pull_request_workflow_cannot_receive_secrets_or_write_scope(
    policy: ModuleType,
) -> None:
    workflow = """\
on:\n  pull_request:\npermissions:\n  contents: write\njobs:\n  test:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment:\n      name: release\n    env:\n      TOKEN: ${{ secrets.RELEASE_TOKEN }}\n    steps: []\n"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)
    assert any("pull-request workflow references repository secrets" in item for item in findings)
    assert any("pull-request workflow enters a protected environment" in item for item in findings)
    assert any("pull-request workflow requests write permission" in item for item in findings)
    assert any("secret-bearing workflow has non-manual trigger" in item for item in findings)


def test_manual_protected_secret_workflow_is_allowed(policy: ModuleType) -> None:
    workflow = """\
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  integration:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment:
      name: protected-integration
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - env:
          TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
        run: ./integration
"""
    assert policy.evaluate_workflow(Path("protected.yml"), workflow) == []


def test_assistant_release_topology_separates_evaluation_from_signed_feedback(
    policy: ModuleType,
) -> None:
    root = Path(__file__).resolve().parents[2]
    evaluation = (
        root / ".github" / "workflows" / "ecocorridor-assistant-evaluation.yml"
    ).read_text(encoding="utf-8")
    finalization = (
        root / ".github" / "workflows" / "ecocorridor-observability-integration.yml"
    ).read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )
    pages = (root / ".github" / "workflows" / "ecocorridor-pages.yml").read_text(encoding="utf-8")

    assert (
        policy.evaluate_assistant_release_topology(
            evaluation,
            finalization,
            release,
            pages,
        )
        == []
    )
    assert "practitioner_ratings" not in evaluation
    assert "\n      WANDB_API_KEY: ${{ secrets." not in evaluation
    assert "\n      ECOCORRIDOR_PROVIDER_API_KEY: ${{ secrets." not in evaluation
    provider_step = evaluation.split(
        "- name: Generate provider-backed case records without usefulness ratings", 1
    )[1]
    assert "WANDB_API_KEY: ${{ secrets." in provider_step
    assert "ECOCORRIDOR_PROVIDER_API_KEY: ${{ secrets." in provider_step
    assert "run_ecocorridor_assistant_evaluation.py" not in finalization
    assert "practitioner_ratings_url" not in finalization
    assert "ECOCORRIDOR_ASSISTANT_RATINGS_B64" in finalization
    assert "\n      ECOCORRIDOR_ASSISTANT_RATINGS_B64: ${{ secrets." not in finalization
    materialization = finalization.split(
        "- name: Materialize checksum-pinned exact-answer-bound usefulness ratings", 1
    )[1]
    assert "ECOCORRIDOR_ASSISTANT_RATINGS_B64: ${{ secrets." in materialization
    assert ".github/workflows/ecocorridor-assistant-evaluation.yml" in finalization
    assert "artifact-ids: ${{ steps.resolve_candidate.outputs.artifact_id }}" in finalization
    assert "digest-mismatch: error" in finalization
    assert "ecocorridor-assistant-evaluation-producer-identity.json" in evaluation
    assert "ecocorridor-observability-finalization-producer-identity.json" in finalization
    assert "github_actions_producer_identity.py verify" in finalization
    assert "ecocorridor-assistant-evaluation-source-provenance.json" in release
    assert "assistant-stage-one-recheck-provenance.json" in release
    assert ".github/workflows/ecocorridor-observability-integration.yml" in release
    assert "ECOCORRIDOR_PRACTITIONER_ACCEPTANCE_B64" in release
    assert "acceptance_evidence_url" not in release
    assert "ecocorridor-pages-producer-identity.json" in pages
    assert pages.count('test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"') == 1
    assert "public-replay-pages-producer-identity.json" in release
    assert "stage-one-recheck/ecocorridor-assistant-evaluation-pending.json" in release
    assert "stage-one-recheck/ecocorridor-assistant-evaluation-cases.json" in release
    assert "--finalized-report" in release
    assert "release-assets/assistant-evaluation-refinalized.json" in release
    assert '"/repos/${GITHUB_REPOSITORY}/git/ref/tags/${RELEASE_TAG}"' in release
    assert '-f sha="${GITHUB_SHA}"' in release
    assert "--verify-tag" in release
    assert '--target "${GITHUB_SHA}"' not in release
    assert "ECOCORRIDOR_RELEASE_POLICY_TOKEN" in release
    assert "ECOCORRIDOR_RELEASE_TAG_RULESET_ID" in release
    assert '"/repos/${GITHUB_REPOSITORY}/immutable-releases"' in release
    assert "validate_github_release_policy.py" in release
    assert release.count("verify_github_release_assets.py verify") == 2
    assert 'test "${PUBLISH_RELEASE}" = "true"' in release
    publish_step = release.split("- name: Publish immutable GitHub Release", 1)[1]
    assert not publish_step.lstrip().startswith("if:")
    assert "--draft" in release
    assert "--draft=false" in release
    assert 'release.get("immutable") is True' in release
    assert "byte-compare the independently finalized report" not in "\n".join(
        policy.evaluate_assistant_release_topology(
            evaluation,
            finalization,
            release,
            pages,
        )
    )


def test_assistant_release_topology_rejects_a_single_stage_rating_cycle(
    policy: ModuleType,
) -> None:
    evaluation = f"""\
name: {policy.ASSISTANT_EVALUATION_NAME}
on: workflow_dispatch
jobs:
  evaluate:
    steps:
      - run: python scripts/run_ecocorridor_assistant_evaluation.py --evaluation-only
      - run: echo ${{{{ inputs.practitioner_ratings_url }}}}
      - name: preserve
        with:
          name: ecocorridor-assistant-evaluation-candidate
"""
    findings = policy.evaluate_assistant_release_topology(evaluation, "", "", "")

    assert any("consumes ratings before answers exist" in item for item in findings)
    assert any("evaluation_run_id" in item for item in findings)


def test_assistant_release_topology_rejects_job_scoped_rating_secrets(
    policy: ModuleType,
) -> None:
    finalization = """\
jobs:
  finalization:
    env:
      ECOCORRIDOR_ASSISTANT_RATINGS_B64: ${{ secrets.RATINGS }}
      ECOCORRIDOR_ASSISTANT_RATINGS_SIGNATURE_B64: ${{ secrets.SIGNATURE }}
"""

    findings = policy.evaluate_assistant_release_topology("", finalization, "", "")

    assert any("inherited by the whole job" in item for item in findings)


def test_assistant_release_topology_rejects_job_scoped_provider_secrets(
    policy: ModuleType,
) -> None:
    evaluation = """\
jobs:
  evaluation:
    env:
      WANDB_API_KEY: ${{ secrets.WANDB_KEY }}
      WANDB_ENTITY: ${{ secrets.WANDB_ENTITY }}
      ECOCORRIDOR_PROVIDER_API_KEY: ${{ secrets.PROVIDER_KEY }}
"""

    findings = policy.evaluate_assistant_release_topology(evaluation, "", "", "")

    assert any("provider secrets may not be inherited" in item for item in findings)


def test_checkout_credentials_must_be_disabled(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)
    assert any("checkout must set persist-credentials: false" in item for item in findings)


def test_inline_pull_request_trigger_cannot_bypass_secret_policy(policy: ModuleType) -> None:
    workflow = """\
on: [workflow_dispatch, pull_request]
permissions:
  contents: read
jobs:
  unsafe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment: protected
    env:
      TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
    steps: []
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)
    assert any("pull-request workflow references repository secrets" in item for item in findings)
    assert any("secret-bearing workflow has non-manual trigger" in item for item in findings)


@pytest.mark.parametrize(
    "expression",
    (
        "${{ secrets['INTEGRATION_TOKEN'] }}",
        "${{ toJSON(secrets) }}",
    ),
)
def test_alternate_secret_expressions_cannot_bypass_policy(
    policy: ModuleType, expression: str
) -> None:
    workflow = f"""\
on:
  pull_request:
permissions:
  contents: read
env:
  LEAK: {expression}
jobs:
  unsafe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps: []
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("pull-request workflow references repository secrets" in item for item in findings)
    assert any("workflow-level env may not contain secrets" in item for item in findings)


def test_each_job_requires_timeout_and_secret_jobs_require_environment(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  missing-timeout:
    runs-on: ubuntu-24.04
    steps: []
  exposed-secret:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    env:
      TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
    steps: []
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)
    assert any("job missing-timeout has no timeout-minutes" in item for item in findings)
    assert any(
        "secret-bearing job exposed-secret has no protected environment" in item
        for item in findings
    )


def test_secret_must_be_scoped_to_its_consuming_step(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  overbroad-secret:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment: protected
    env:
      TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
    steps:
      - run: ./integration
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("must be scoped to the consuming step" in item for item in findings)


def test_secret_expression_must_not_be_interpolated_into_shell(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  unsafe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment: protected
    steps:
      - run: command --token \"${{ secrets.INTEGRATION_TOKEN }}\"
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("secret expression in a shell command" in item for item in findings)


def test_credential_job_cannot_report_green_by_skip_or_continue(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  skipped-secret-job:
    if: false
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment: protected
    env:
      TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
    steps: []
  advisory-secret-job:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    continue-on-error: true
    environment: protected
    env:
      TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
    steps: []
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("may not be conditionally skipped" in item for item in findings)
    assert any("job advisory-secret-job may not continue" in item for item in findings)


def test_credential_step_cannot_continue_on_error(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  integration:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    environment: protected
    steps:
      - name: Credential roundtrip
        continue-on-error: true
        env:
          TOKEN: ${{ secrets.INTEGRATION_TOKEN }}
        run: ./integration
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("credential-consuming step" in item for item in findings)


def test_dynamic_continue_on_error_cannot_bypass_terminal_gate(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  dynamic-job:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    continue-on-error: ${{ inputs.allow_failure }}
    steps: []
  dynamic-step:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - continue-on-error: ${{ inputs.allow_failure }}
        run: ./release-gate
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("job dynamic-job may not continue" in item for item in findings)
    assert any("dynamic continue-on-error value" in item for item in findings)


@pytest.mark.parametrize(
    "expression",
    (
        "${{ inputs.release_tag }}",
        "${{ github.event.pull_request.title }}",
        "${{ github.event['issue']['title'] }}",
    ),
)
def test_user_controlled_context_must_reach_shell_through_step_env(
    policy: ModuleType, expression: str
) -> None:
    workflow = f"""\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  unsafe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Unsafe interpolation
        run: command --value \"{expression}\"
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("GitHub expression in a shell command" in item for item in findings)


def test_user_controlled_context_is_allowed_through_step_env(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  safe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Safe interpolation
        env:
          RELEASE_TAG: ${{ inputs.release_tag }}
        run: command --value \"${RELEASE_TAG}\"
"""

    assert policy.evaluate_workflow(Path("safe.yml"), workflow) == []


def test_advisory_failure_requires_an_always_run_terminal_outcome_gate(
    policy: ModuleType,
) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  unsafe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Advisory scan without a gate
        id: scan
        continue-on-error: true
        run: ./scan
      - run: echo done
"""
    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("lacks an always-run terminal outcome gate" in item for item in findings)


def test_advisory_failure_with_terminal_outcome_gate_is_allowed(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  safe:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Advisory scan
        id: scan
        continue-on-error: true
        run: ./scan
      - name: Terminal outcome gate
        if: always()
        env:
          SCAN_OUTCOME: ${{ steps.scan.outcome }}
        run: |
          test \"${SCAN_OUTCOME}\" = success || exit 1
"""

    assert policy.evaluate_workflow(Path("safe.yml"), workflow) == []


def test_eco_pr_ci_validates_the_built_public_replay() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-ci.yml").read_text(encoding="utf-8")

    assert "python3 scripts/check_ecocorridor_public_replay.py" in workflow
    assert '--source-sha "${GITHUB_SHA}"' in workflow
    assert "--root apps/ecocorridor-replay/dist" in workflow
    assert "--trust-record apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json" in workflow
    assert "--require-verified-evidence" not in workflow


def _ecocorridor_workflows() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((root / ".github" / "workflows").glob("ecocorridor-*.yml"))
    }


def test_eco_geospatial_runtime_is_confined_to_the_three_post_sync_jobs(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()

    assert policy.evaluate_ecocorridor_geospatial_dependency_topology(workflows) == []
    assert (
        workflows[policy.ECOCORRIDOR_CI_WORKFLOW].count(policy.ECOCORRIDOR_GEOSPATIAL_HELPER) == 2
    )
    assert (
        workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW].count(policy.ECOCORRIDOR_GEOSPATIAL_HELPER)
        == 1
    )
    assert all("--extra geo" not in workflow for workflow in workflows.values())


def test_eco_workflow_policy_rejects_the_gi_geo_extra(policy: ModuleType) -> None:
    workflows = _ecocorridor_workflows()
    workflows[policy.ECOCORRIDOR_CI_WORKFLOW] = workflows[policy.ECOCORRIDOR_CI_WORKFLOW].replace(
        "uv sync --locked", "uv sync --locked --extra geo", 1
    )

    findings = policy.evaluate_ecocorridor_geospatial_dependency_topology(workflows)

    assert any("--extra geo crosses the GI-only dependency boundary" in item for item in findings)


def test_eco_workflow_policy_requires_each_approved_helper_once(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    workflows[policy.ECOCORRIDOR_CI_WORKFLOW] = workflows[policy.ECOCORRIDOR_CI_WORKFLOW].replace(
        policy.ECOCORRIDOR_GEOSPATIAL_HELPER, "scripts/not-the-helper.py", 1
    )

    findings = policy.evaluate_ecocorridor_geospatial_dependency_topology(workflows)

    assert any(
        "job isolation-and-python must invoke" in item and "observed 0" in item for item in findings
    )


def test_eco_workflow_policy_rejects_duplicate_or_unapproved_helper_use(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    invocation = f"uv run --no-sync python {policy.ECOCORRIDOR_GEOSPATIAL_HELPER}"
    workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = workflows[
        policy.ECOCORRIDOR_RELEASE_WORKFLOW
    ].replace(invocation, f"{invocation}\n          {invocation}", 1)
    pages = workflows[policy.ECOCORRIDOR_PAGES_WORKFLOW]
    workflows[policy.ECOCORRIDOR_PAGES_WORKFLOW] = pages.replace(
        "run: |", f"run: |\n          {invocation}", 1
    )

    findings = policy.evaluate_ecocorridor_geospatial_dependency_topology(workflows)

    assert any(
        "job build-signed-installers must invoke" in item and "observed 2" in item
        for item in findings
    )
    assert any("ecocorridor-pages.yml" in item and "may not invoke" in item for item in findings)


def test_eco_workflow_policy_requires_locked_sync_before_helper(policy: ModuleType) -> None:
    workflows = _ecocorridor_workflows()
    invocation = f"uv run --no-sync python {policy.ECOCORRIDOR_GEOSPATIAL_HELPER}"
    release = workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    sync = (
        "uv sync --locked --extra science --extra ecocorridor --extra export "
        "--extra observability --extra release"
    )
    assert f"{sync}\n          {invocation}" in release
    workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = release.replace(
        f"{sync}\n          {invocation}", f"{invocation}\n          {sync}", 1
    )

    findings = policy.evaluate_ecocorridor_geospatial_dependency_topology(workflows)

    assert any("before uv sync --locked" in item for item in findings)


def test_pages_deployment_requires_current_verified_ridgeway_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-pages.yml").read_text(
        encoding="utf-8"
    )

    assert "--trust-record apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json" in workflow
    assert "--require-verified-evidence" in workflow
    assert "ridgeway_evidence_run_id:" in workflow
    assert ".github/workflows/ecocorridor-ridgeway-evidence.yml" in workflow
    assert not policy_event_declared(workflow, "push")


def policy_event_declared(workflow: str, event: str) -> bool:
    # Keep tests independent of YAML 1.1 parser behavior around the `on` key.
    if f"on: {event}" in workflow:
        return True
    return f"\n  {event}:" in workflow.split("\npermissions:", 1)[0]


def test_protected_ridgeway_execution_is_required_by_pages_and_release(
    policy: ModuleType,
) -> None:
    root = Path(__file__).resolve().parents[2]
    producer = (root / ".github" / "workflows" / "ecocorridor-ridgeway-evidence.yml").read_text(
        encoding="utf-8"
    )
    pages = (root / ".github" / "workflows" / "ecocorridor-pages.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )

    assert policy.evaluate_ridgeway_release_topology(producer, pages, release) == []
    assert "benchmarks/ecocorridor/ridgeway/run_direct_study.py" in producer
    assert "--job-limit" not in producer
    assert "source_ci_run_id:" in producer
    assert producer.count('test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"') == 1
    assert "ridgeway_evidence_run_id:" in pages
    assert "ridgeway_evidence_run_id:" in release
    assert "artifact-ids: ${{ steps.resolve_ridgeway_evidence.outputs.artifact_id }}" in pages
    assert "artifact-ids: ${{ steps.resolve_ridgeway_evidence.outputs.artifact_id }}" in release
    assert "ridgeway-protected-rerun-direct-evidence-manifest.json" in release
    assert 'cp "${producer_root}/ridgeway/${producer_archive}"' in release


def test_ridgeway_topology_rejects_semantic_only_self_attestation(policy: ModuleType) -> None:
    producer = f"""\
name: {policy.RIDGEWAY_EVIDENCE_NAME}
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  package:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - run: python scripts/verify_ridgeway_release_evidence.py
"""
    pages = "on:\n  push:\n"
    findings = policy.evaluate_ridgeway_release_topology(producer, pages, "")

    assert any("run_direct_study.py" in finding for finding in findings)
    assert any("protected Ridgeway evidence" in finding for finding in findings)
    assert any("Pages must be manual-only" in finding for finding in findings)


def test_ridgeway_semantic_recomputation_is_strict_at_publication_boundaries() -> None:
    root = Path(__file__).resolve().parents[2]
    source_ci = (root / ".github" / "workflows" / "ecocorridor-ci.yml").read_text(encoding="utf-8")
    pages = (root / ".github" / "workflows" / "ecocorridor-pages.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )

    assert source_ci.count("scripts/verify_ridgeway_release_evidence.py") == 1
    assert "--allow-historical-blocked" in source_ci
    # Pages independently verifies both the committed reference and the
    # authenticated producer package.
    assert pages.split("jobs:", 1)[1].count("scripts/verify_ridgeway_release_evidence.py") == 2
    assert "--allow-historical-blocked" not in pages
    assert release.count("scripts/verify_ridgeway_release_evidence.py") == 2
    assert "--allow-historical-blocked" not in release


def test_eco_pr_ci_has_a_target_only_fail_closed_julia_vulnerability_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-ci.yml").read_text(encoding="utf-8")
    gate = workflow.split("julia-dependency-security:", 1)[1].split("connectivity-engine:", 1)[0]

    assert "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25" in gate
    assert "version: v0.70.0" in gate
    assert 'cache: "false"' in gate
    assert "TRIVY_DB_REPOSITORY: ghcr.io/aquasecurity/trivy-db:2" in gate
    assert "TRIVY_CACHE_DIR: ${{ github.workspace }}/.cache/trivy" in gate
    assert "scan-ref: julia/EcoCorridorEngine" in gate
    assert "scanners: vuln" in gate
    assert "severity: UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in gate
    assert "continue-on-error: true" in gate
    assert "validate_julia_vulnerability_report.py" in gate
    assert '--metadata "${RUNNER_TEMP}/ecocorridor-trivy-version.txt"' in gate
    assert "Fail closed on scanner, metadata, or policy failure" in gate


def test_protected_release_rescans_julia_with_the_same_fail_closed_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )
    prerequisites = workflow.split("release-prerequisites:", 1)[1].split(
        "build-signed-installers:", 1
    )[0]

    assert "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25" in prerequisites
    assert "version: v0.70.0" in prerequisites
    assert 'cache: "false"' in prerequisites
    assert "TRIVY_DB_REPOSITORY: ghcr.io/aquasecurity/trivy-db:2" in prerequisites
    assert "scan-ref: julia/EcoCorridorEngine" in prerequisites
    assert "severity: UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in prerequisites
    assert "validate_julia_vulnerability_report.py" in prerequisites
    assert "ecocorridor-release-julia-vulnerability-evidence" in prerequisites
    assert "Block release on scanner, metadata, or policy failure" in prerequisites


def test_multiplatform_release_staging_declares_bash() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )
    stage = workflow.split("- name: Stage target resources", 1)[1].split(
        "- name: Sign the exact native-resource inventory", 1
    )[0]

    assert "shell: bash" in stage


def test_protected_release_requires_same_commit_eco_ci_and_signed_smoke_identity() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )

    assert "source_ci_run_id:" in workflow
    eco_ci = workflow.split("- name: Verify same-commit EcoCorridor CI run identity", 1)[1].split(
        "- name: Verify same-commit general CI", 1
    )[0]
    general_ci = workflow.split(
        "- name: Verify same-commit general CI and dependency-policy identity", 1
    )[1].split("- name: Resolve same-commit public replay", 1)[0]
    pages = workflow.split(
        "- name: Resolve same-commit public replay deployment and exact artifact", 1
    )[1].split("- name: Recheck local source boundaries", 1)[0]
    assert ".github/workflows/ecocorridor-ci.yml" in eco_ci
    assert ".github/workflows/ci.yml" in general_ci
    assert eco_ci.count("--run-only") == 1
    assert general_ci.count("--run-only") == 1
    assert "gh run view" not in eco_ci + general_ci + pages
    assert ".github/workflows/ecocorridor-pages.yml" in pages
    assert "artifact-ids: ${{ steps.resolve_pages_artifact.outputs.artifact_id }}" in pages
    assert "run-id: ${{ inputs.pages_run_id }}" in pages
    assert "merge-multiple: true" in pages
    assert "digest-mismatch: error" in pages
    for filename in (
        "release-assets/eco-ci-provenance.json",
        "release-assets/general-ci-provenance.json",
        "release-assets/public-replay-pages-provenance.json",
    ):
        assert filename in workflow
    assert "--public-key" in workflow
    assert "--expected-source-commit" in workflow
    assert "--expected-target" in workflow


def test_signed_installer_gate_launches_actual_native_host_twice_before_resource_smoke() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )
    installed = workflow.split(
        "- name: Install macOS disk image and rerun the scientific boundary offline", 1
    )[1].split("- name: Collect uniquely named target artifacts", 1)[0]

    # Two explicit macOS launches and one Windows launch command inside a
    # two-receipt loop.
    assert installed.count("--release-health-check") == 3
    assert "validate_ecocorridor_installed_health.py" in installed
    assert 'spctl --assess --type execute --verbose=2 "${installed_application}"' in installed
    assert workflow.count("TimeStamperCertificate") >= 2
    assert "Installed native host lacks a valid timestamped Authenticode signature" in installed
    assert installed.count("--installed-health-receipt") == 0
    assert installed.index("validate_ecocorridor_installed_health.py") < installed.index(
        "smoke_ecocorridor_release_bundle.py"
    )
    collection = workflow.split("- name: Collect uniquely named target artifacts", 1)[1]
    assert collection.count("--installed-health-receipt") == 2
