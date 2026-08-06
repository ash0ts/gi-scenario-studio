from __future__ import annotations

import importlib.util
import subprocess
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


def test_runtime_contexts_are_rejected_in_other_pre_job_locations(policy: ModuleType) -> None:
    workflow = """\
permissions:
  contents: read
concurrency:
  group: unsafe-${{ needs.prepare.result }}
jobs:
  test:
    name: ${{ steps.prepare.outputs.label }}
    runs-on: ${{ runner.os }}
    if: ${{ matrix.enabled }}
    timeout-minutes: 5
    strategy:
      matrix:
        os: [${{ job.status }}]
    steps: []
"""

    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("needs context is unavailable in workflow concurrency" in item for item in findings)
    assert any("steps context is unavailable in jobs.test.name" in item for item in findings)
    assert any("runner context used before step scope" in item for item in findings)
    assert any("matrix context is unavailable in jobs.test.if" in item for item in findings)
    assert any("job context is unavailable in jobs.test.strategy" in item for item in findings)


def test_supported_pre_job_contexts_do_not_create_false_positives(policy: ModuleType) -> None:
    workflow = """\
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ inputs.channel }}
jobs:
  prepare:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps: []
  test:
    needs: prepare
    name: ${{ github.workflow }} / ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
    if: ${{ needs.prepare.result == 'success' }}
    timeout-minutes: ${{ inputs.timeout }}
    concurrency:
      group: ${{ github.workflow }}-${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-24.04]
    env:
      CACHE_KEY: ${{ needs.prepare.result }}-${{ vars.CACHE_VERSION }}
    steps: []
"""

    assert policy.evaluate_workflow(Path("safe.yml"), workflow) == []


def test_github_token_is_rejected_before_step_scope(policy: ModuleType) -> None:
    workflow = """\
permissions:
  contents: read
env:
  TOKEN: ${{ github.token }}
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps: []
"""

    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("github.token is unavailable before step scope" in item for item in findings)


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


def test_review_brief_evaluation_workflow_is_locked_to_exact_target_and_order(
    policy: ModuleType,
) -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / policy.REVIEW_BRIEF_EVALUATION_WORKFLOW).read_text(
        encoding="utf-8"
    )

    assert policy.evaluate_review_brief_evaluation_topology(workflow) == []
    assert workflow.count('--wandb-api-base-url "${WANDB_BASE_URL}"') == 2
    assert workflow.count('--wandb-web-base-url "${WANDB_WEB_BASE_URL}"') == 2

    missing_publisher_target = workflow.replace(
        '            --wandb-web-base-url "${WANDB_WEB_BASE_URL}" \\\n'
        '            --report-url "${WANDB_REPORT_URL}"',
        '            --report-url "${WANDB_REPORT_URL}"',
        1,
    )
    findings = policy.evaluate_review_brief_evaluation_topology(missing_publisher_target)
    assert any("wandb-web-base-url" in item for item in findings)

    wrong_order = (
        workflow.replace(
            "- name: Render only the complete verified campaign",
            "- name: __ORDER_SENTINEL__",
            1,
        )
        .replace(
            "- name: Append only the verified owned section to the existing Report",
            "- name: Render only the complete verified campaign",
            1,
        )
        .replace(
            "- name: __ORDER_SENTINEL__",
            "- name: Append only the verified owned section to the existing Report",
            1,
        )
    )
    assert any(
        "test, run, render, append, and preserve in order" in item
        for item in policy.evaluate_review_brief_evaluation_topology(wrong_order)
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


@pytest.mark.parametrize(
    ("upload_path", "message"),
    (
        ("${{ runner.temp }}", "broad RUNNER_TEMP root"),
        ("${{ runner.temp }}/**/*", "broad RUNNER_TEMP root"),
        (
            "${{ runner.temp }}/ecocorridor-resource-signing-key.seed.hex",
            "credential path ecocorridor-resource-signing-key.seed.hex",
        ),
        (
            "${{ runner.temp }}/ci-resource-key.seed.hex",
            "credential path ci-resource-key.seed.hex",
        ),
        ("${{ runner.temp }}/AuthKey.p8", "credential path authkey.p8"),
        (
            "${{ runner.temp }}/ecocorridor-build.keychain-db",
            "credential path ecocorridor-build.keychain",
        ),
        ("${{ runner.temp }}/ecocorridor-signing.p12", "credential path ecocorridor-signing.p12"),
        ("${{ runner.temp }}/ecocorridor-signing.pfx", "credential path ecocorridor-signing.pfx"),
    ),
)
def test_artifact_upload_rejects_broad_temp_and_credential_paths(
    policy: ModuleType,
    upload_path: str,
    message: str,
) -> None:
    workflow = f"""\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  upload:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f
        with:
          name: unsafe
          path: {upload_path}
"""

    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any(message in item for item in findings)


def test_artifact_upload_allows_a_narrow_noncredential_temp_directory(
    policy: ModuleType,
) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  upload:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Safe upload
        uses: actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f
        env:
          path: ${{ runner.temp }}
        with:
          name: release-assets
          path: ${{ runner.temp }}/release-assets
"""

    assert policy.evaluate_workflow(Path("safe.yml"), workflow) == []


def test_artifact_upload_checks_each_multiline_path(policy: ModuleType) -> None:
    workflow = """\
on: workflow_dispatch
permissions:
  contents: read
jobs:
  upload:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Unsafe upload
        uses: actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f
        with:
          name: unsafe
          path: |
            ${{ runner.temp }}/release-assets
            ${{ runner.temp }}/ecocorridor-signing.pfx
"""

    findings = policy.evaluate_workflow(Path("unsafe.yml"), workflow)

    assert any("credential path ecocorridor-signing.pfx" in item for item in findings)


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
    frontend_job = workflow.split("  eco-frontend:", 1)[1].split("\n  native-host:", 1)[0]

    assert "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b" in frontend_job
    assert "uv sync --locked --no-dev --no-install-project" in frontend_job
    assert "uv run --no-sync python scripts/check_ecocorridor_public_replay.py" in frontend_job
    assert "python3 scripts/check_ecocorridor_public_replay.py" not in frontend_job
    assert "--extra" not in frontend_job
    assert "PYTHONPATH: python" in frontend_job
    assert '--source-sha "${GITHUB_SHA}"' in frontend_job
    assert "--root apps/ecocorridor-replay/dist" in frontend_job
    assert (
        "--trust-record apps/ecocorridor-desktop/src/data/ridgewayReleaseTrust.json" in frontend_job
    )
    assert "--require-verified-evidence" not in frontend_job
    assert "pnpm --dir apps/ecocorridor-replay test:rendered-copy" in frontend_job
    assert frontend_job.index("pnpm build:eco") < frontend_job.index(
        "pnpm --dir apps/ecocorridor-replay test:rendered-copy"
    )


def test_pages_crawls_rendered_copy_before_uploading_the_public_replay() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/ecocorridor-pages.yml").read_text(encoding="utf-8")
    build_job = workflow.split("  build-public-replay:", 1)[1].split(
        "\n  deploy-public-replay:", 1
    )[0]

    assert "uv sync --locked --no-dev --no-install-project" in build_job
    assert (
        "pnpm --dir apps/ecocorridor-replay exec playwright install --with-deps chromium"
        in build_job
    )
    assert "pnpm --dir apps/ecocorridor-replay test:rendered-copy" in build_job
    assert build_job.index("pnpm --dir apps/ecocorridor-replay build") < build_job.index(
        "pnpm --dir apps/ecocorridor-replay test:rendered-copy"
    )
    assert build_job.index(
        "pnpm --dir apps/ecocorridor-replay test:rendered-copy"
    ) < build_job.index("uses: actions/upload-pages-artifact")


def test_packaged_boundary_preserves_verifiable_success_evidence_without_masking_failure() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/ecocorridor-ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  packaged-scientific-boundary:", 1)[1].split("\n  eco-frontend:", 1)[0]
    smoke = job.split("- name: Execute staged scientific boundary offline", 1)[1].split(
        "- name: Preserve packaged-boundary evidence", 1
    )[0]
    upload = job.split("- name: Preserve packaged-boundary evidence", 1)[1].split(
        "- name: Remove PackageCompiler swap", 1
    )[0]

    assert '--disposition-output "${RUNNER_TEMP}/ecocorridor-packaged-boundary-smoke.json"' in smoke
    assert "${{ runner.temp }}/ci-resource-public-key.hex" in upload
    assert "${{ runner.temp }}/ecocorridor-packaged-boundary-smoke.json" in upload
    assert "release-generated/dependency-inventory.json" in upload
    assert "if: always()" not in upload


def test_packaged_boundary_provisions_bounded_swap_for_nonincremental_sysimage() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ecocorridor-ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  packaged-scientific-boundary:", 1)[1].split("\n  eco-frontend:", 1)[0]
    provision = job.split("- name: Provision bounded PackageCompiler swap", 1)[1].split(
        "- name: Build target-native engine application", 1
    )[0]
    cleanup = job.split("- name: Remove PackageCompiler swap", 1)[1]

    assert 'swap_path="${RUNNER_TEMP}/ecocorridor-packagecompiler.swap"' in provision
    assert '[[ -e "${swap_path}" || -L "${swap_path}" ]]' in provision
    assert 'sudo fallocate --length 4G "${swap_path}"' in provision
    assert 'sudo chmod 600 "${swap_path}"' in provision
    assert 'sudo mkswap "${swap_path}"' in provision
    assert 'sudo swapon "${swap_path}"' in provision
    assert 'JULIA_NUM_PRECOMPILE_TASKS: "1"' in job
    assert "if: always()" in cleanup
    assert 'sudo swapoff "${swap_path}" || true' in cleanup
    assert 'sudo rm -f -- "${swap_path}"' in cleanup

    release = (root / ".github" / "workflows" / "ecocorridor-release.yml").read_text(
        encoding="utf-8"
    )
    package_job = release.split("  build-signed-installers:", 1)[1].split(
        "\n  installed-artifact-smoke:", 1
    )[0]
    assert package_job.count('JULIA_NUM_PRECOMPILE_TASKS: "1"') == 2


def test_packagecompiler_builds_are_heartbeat_wrapped_in_ci_and_release(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()

    assert policy.evaluate_packagecompiler_heartbeat_topology(workflows) == []
    for workflow_name in (
        policy.ECOCORRIDOR_CI_WORKFLOW,
        policy.ECOCORRIDOR_RELEASE_WORKFLOW,
    ):
        workflow = workflows[workflow_name]
        assert workflow.count(policy.PACKAGECOMPILER_BUILD_SCRIPT) == 1
        assert workflow.count(policy.PACKAGECOMPILER_HEARTBEAT_HELPER) == 1


def test_packagecompiler_heartbeat_policy_rejects_unwrapped_or_wrong_shell(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    unsafe = dict(workflows)
    unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = unsafe[
        policy.ECOCORRIDOR_RELEASE_WORKFLOW
    ].replace(
        "        shell: bash\n        env:\n          ECOCORRIDOR_ENGINE_OUTPUT:",
        "        env:\n          ECOCORRIDOR_ENGINE_OUTPUT:",
        1,
    )
    findings = policy.evaluate_packagecompiler_heartbeat_topology(unsafe)

    assert any("heartbeat step must declare shell: bash" in item for item in findings)

    unsafe = dict(workflows)
    unsafe[policy.ECOCORRIDOR_CI_WORKFLOW] = unsafe[policy.ECOCORRIDOR_CI_WORKFLOW].replace(
        "bash scripts/run_with_progress_heartbeat.sh \\\n"
        "            --interval-seconds 60 \\\n"
        '            --label "PackageCompiler application build" \\\n'
        "            -- \\\n"
        "            ",
        "",
        1,
    )
    findings = policy.evaluate_packagecompiler_heartbeat_topology(unsafe)

    assert any("must use scripts/run_with_progress_heartbeat.sh" in item for item in findings)


def test_progress_heartbeat_preserves_wrapped_command_failure_status() -> None:
    root = Path(__file__).resolve().parents[2]
    helper = root / "scripts" / "run_with_progress_heartbeat.sh"
    completed = subprocess.run(
        [
            "bash",
            str(helper),
            "--interval-seconds",
            "1",
            "--label",
            "test compiler",
            "--",
            "bash",
            "-c",
            "sleep 2; exit 23",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 23
    assert "test compiler is still running" in completed.stdout
    assert "test compiler exited with status 23" in completed.stderr


def _ecocorridor_workflows() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((root / ".github" / "workflows").glob("ecocorridor-*.yml"))
    }


def test_circuitscape_instantiation_is_immediately_verified_everywhere(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()

    assert policy.evaluate_circuitscape_manifest_verification_topology(workflows) == []
    expected_counts = {
        policy.ECOCORRIDOR_CI_WORKFLOW: 2,
        policy.ECOCORRIDOR_RELEASE_WORKFLOW: 1,
        policy.RIDGEWAY_EVIDENCE_WORKFLOW: 1,
    }
    for workflow_name, expected_count in expected_counts.items():
        workflow = workflows[workflow_name]
        assert workflow.count("Pkg.instantiate()") == expected_count
        assert workflow.count(policy.CIRCUITSCAPE_MANIFEST_VERIFIER) == expected_count


def test_circuitscape_manifest_policy_rejects_missing_or_delayed_verification(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    unsafe = dict(workflows)
    unsafe[policy.ECOCORRIDOR_CI_WORKFLOW] = unsafe[policy.ECOCORRIDOR_CI_WORKFLOW].replace(
        f"          julia {policy.CIRCUITSCAPE_PROJECT_ARGUMENT} \\\n"
        "            --startup-file=no --history-file=no \\\n"
        f"            {policy.CIRCUITSCAPE_MANIFEST_VERIFIER}\n",
        "",
        1,
    )

    findings = policy.evaluate_circuitscape_manifest_verification_topology(unsafe)

    assert any("followed immediately by verify_manifest.jl" in item for item in findings)

    unsafe = dict(workflows)
    verified = (
        f"          julia {policy.CIRCUITSCAPE_PROJECT_ARGUMENT} \\\n"
        "            --startup-file=no --history-file=no \\\n"
        f"            {policy.CIRCUITSCAPE_MANIFEST_VERIFIER}\n"
    )
    precompile = (
        f"          julia {policy.CIRCUITSCAPE_PROJECT_ARGUMENT} \\\n"
        "            --startup-file=no --history-file=no \\\n"
        "            -e 'using Pkg; Pkg.precompile()'\n"
    )
    release = unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    assert verified + precompile in release
    unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = release.replace(
        verified + precompile,
        precompile + verified,
        1,
    )

    findings = policy.evaluate_circuitscape_manifest_verification_topology(unsafe)

    assert any("followed immediately by verify_manifest.jl" in item for item in findings)
    assert any("not immediately bound" in item for item in findings)

    unsafe = dict(workflows)
    unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = unsafe[
        policy.ECOCORRIDOR_RELEASE_WORKFLOW
    ].replace(
        "      - name: Instantiate pinned Circuitscape environment\n        shell: bash\n",
        "      - name: Instantiate pinned Circuitscape environment\n",
        1,
    )

    findings = policy.evaluate_circuitscape_manifest_verification_topology(unsafe)

    assert any("declare shell: bash" in item for item in findings)


def test_release_signing_policy_requires_canonical_pynacl_seed_boundary(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()

    assert policy.evaluate_release_signing_security_topology(workflows) == []


@pytest.mark.parametrize(
    "removed_token",
    (
        "trap cleanup_ci_resource_seed EXIT HUP INT TERM",
        'rm -f -- "${seed_path}"',
        "cleanup_ci_resource_seed\n",
        "trap - EXIT HUP INT TERM",
    ),
)
def test_ci_ephemeral_signing_seed_requires_failure_safe_exact_cleanup(
    policy: ModuleType,
    removed_token: str,
) -> None:
    workflows = _ecocorridor_workflows()
    ci = workflows[policy.ECOCORRIDOR_CI_WORKFLOW]
    assert removed_token in ci
    workflows[policy.ECOCORRIDOR_CI_WORKFLOW] = ci.replace(
        removed_token,
        "REMOVED_CI_SEED_CLEANUP_BOUNDARY",
        1,
    )

    findings = policy.evaluate_release_signing_security_topology(workflows)

    assert any("ephemeral resource-signing step" in item for item in findings)


def test_release_signing_policy_rejects_legacy_or_weakened_key_input(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    unsafe = dict(workflows)
    unsafe[policy.ECOCORRIDOR_CI_WORKFLOW] = unsafe[policy.ECOCORRIDOR_CI_WORKFLOW].replace(
        "from nacl.signing import SigningKey", "import cryptography"
    )
    release = unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    unsafe[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = release.replace(
        '[[ ! "${RESOURCE_SIGNING_SEED_HEX:-}" =~ ^[0-9a-f]{64}$ ]]',
        '[[ -z "${RESOURCE_SIGNING_SEED_HEX:-}" ]]',
    )

    findings = policy.evaluate_release_signing_security_topology(unsafe)

    assert any("forbidden legacy token cryptography" in item for item in findings)
    assert any("protected release signing is missing" in item for item in findings)


@pytest.mark.parametrize(
    ("removed_token", "expected_finding"),
    (
        (
            "trap cleanup_resource_seed EXIT HUP INT TERM",
            "Sign the exact native-resource inventory",
        ),
        (
            "trap cleanup_apple_certificate_file EXIT HUP INT TERM",
            "Import Apple signing identity",
        ),
        (
            'printf \'path=%s\\n\' "${original_default_keychain}" >> "${GITHUB_OUTPUT}"',
            "Capture Apple default keychain",
        ),
        ("finally {", "Import Windows signing identity"),
        (
            'security delete-keychain "${keychain_path}"',
            "Remove Apple signing material",
        ),
        (
            'security default-keychain -d user -s "${original_default_keychain}"',
            "Remove Apple signing material",
        ),
        (
            "if: always() && matrix.target == 'windows-x64'",
            "Remove Windows signing material",
        ),
    ),
)
def test_release_signing_policy_requires_failure_safe_exact_cleanup(
    policy: ModuleType,
    removed_token: str,
    expected_finding: str,
) -> None:
    workflows = _ecocorridor_workflows()
    release = workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    assert removed_token in release
    workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = release.replace(
        removed_token,
        "REMOVED_SECURITY_BOUNDARY",
        1,
    )

    findings = policy.evaluate_release_signing_security_topology(workflows)

    assert any(expected_finding in item for item in findings)


def test_release_signing_cleanup_occurs_after_build_and_before_verification(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    release = workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    apple_cleanup = release.split("      - name: Remove Apple signing material", 1)[1].split(
        "      - name: Remove Windows signing material", 1
    )[0]
    apple_capture = release.split("      - name: Capture Apple default keychain", 1)[1].split(
        "      - name: Import Apple signing identity", 1
    )[0]
    windows_cleanup = release.split("      - name: Remove Windows signing material", 1)[1].split(
        "      - name: Verify Apple signatures and notarization", 1
    )[0]

    assert "if: always() && startsWith(matrix.target, 'macos')" in apple_cleanup
    assert "id: capture_apple_default_keychain" in apple_capture
    assert "security default-keychain -d user" in apple_capture
    assert "${GITHUB_OUTPUT}" in apple_capture
    assert "${GITHUB_ENV}" not in apple_capture
    assert release.count("steps.capture_apple_default_keychain.outputs.path") == 1
    assert release.count("          ECOCORRIDOR_ORIGINAL_DEFAULT_KEYCHAIN: ") == 1
    assert (
        "ECOCORRIDOR_ORIGINAL_DEFAULT_KEYCHAIN: "
        "${{ steps.capture_apple_default_keychain.outputs.path }}"
    ) in apple_cleanup
    restore_index = apple_cleanup.index(
        'security default-keychain -d user -s "${original_default_keychain}"'
    )
    delete_index = apple_cleanup.index('security delete-keychain "${keychain_path}"')
    assert restore_index < delete_index
    assert 'security delete-keychain "${keychain_path}"' in apple_cleanup
    assert "if: always() && matrix.target == 'windows-x64'" in windows_cleanup
    assert "^[0-9A-F]{40}$" in windows_cleanup
    assert policy.evaluate_release_signing_security_topology(workflows) == []


def test_release_signing_policy_rejects_keychain_deletion_before_restore(
    policy: ModuleType,
) -> None:
    workflows = _ecocorridor_workflows()
    release = workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW]
    restore = 'security default-keychain -d user -s "${original_default_keychain}"'
    delete = 'security delete-keychain "${keychain_path}"'
    assert restore in release and delete in release
    unsafe_release = release.replace(restore, "RESTORE_MOVED_AFTER_DELETE", 1).replace(
        delete,
        f"{delete}\n          {restore}",
        1,
    )
    workflows[policy.ECOCORRIDOR_RELEASE_WORKFLOW] = unsafe_release

    findings = policy.evaluate_release_signing_security_topology(workflows)

    assert any("before deleting the temporary Apple keychain" in item for item in findings)


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
        "uv sync --locked --extra science --extra ecocorridor --extra ecocorridor-agents "
        "--extra ecocorridor-report --extra export "
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
    assert "scripts/publish_ridgeway_evidence.py" in producer
    assert "scripts/package_ridgeway_evidence.py" not in producer
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
