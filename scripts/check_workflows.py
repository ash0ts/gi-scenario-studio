#!/usr/bin/env python3
"""Small fail-fast workflow policy check; actionlint remains the syntax authority."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
UNPINNED_USE = re.compile(r"^\s*-?\s*uses:\s*([^#\s]+)", re.MULTILINE)
FULL_COMMIT = re.compile(r"^[^@]+@[0-9a-f]{40}$")
SECRET_EXPRESSION = re.compile(
    r"\$\{\{(?:(?!\}\}).)*\bsecrets\b",
    re.IGNORECASE | re.DOTALL,
)
SHELL_EXPRESSION = re.compile(r"\$\{\{")
WRITE_PERMISSION = re.compile(r"^\s+[a-z-]+:\s*write\s*$", re.MULTILINE)
UNTRUSTED_EVENTS = ("pull_request", "pull_request_target", "push", "merge_group", "schedule")
ASSISTANT_EVALUATION_WORKFLOW = "ecocorridor-assistant-evaluation.yml"
ASSISTANT_FINALIZATION_WORKFLOW = "ecocorridor-observability-integration.yml"
ECOCORRIDOR_RELEASE_WORKFLOW = "ecocorridor-release.yml"
ECOCORRIDOR_PAGES_WORKFLOW = "ecocorridor-pages.yml"
RIDGEWAY_EVIDENCE_WORKFLOW = "ecocorridor-ridgeway-evidence.yml"
ASSISTANT_EVALUATION_NAME = "Eco protected assistant evaluation candidate"
RIDGEWAY_EVIDENCE_NAME = "EcoCorridor protected Ridgeway direct evidence"


def _declares_event(text: str, event: str) -> bool:
    escaped = re.escape(event)
    if re.search(rf"^on:\s*{escaped}\s*(?:#.*)?$", text, re.MULTILINE):
        return True
    if match := re.search(r"^on:\s*\[([^\]\n]+)\]\s*(?:#.*)?$", text, re.MULTILINE):
        events = {item.strip().strip("'\"") for item in match.group(1).split(",")}
        if event in events:
            return True
    on_block = re.search(r"^on:\s*(?:#.*)?$", text, re.MULTILINE)
    if not on_block:
        return False
    remainder = text[on_block.end() :]
    block_lines: list[str] = []
    for line in remainder.splitlines():
        if line and not line.startswith((" ", "\t", "#")):
            break
        block_lines.append(line)
    return bool(re.search(rf"^  {escaped}(?:\s*:|\s*$)", "\n".join(block_lines), re.MULTILINE))


def _job_blocks(lines: list[str]) -> list[tuple[int, str, list[str]]]:
    jobs_index = next((index for index, line in enumerate(lines) if line == "jobs:"), None)
    if jobs_index is None:
        return []
    blocks: list[tuple[int, str, list[str]]] = []
    current_number: int | None = None
    current_name = ""
    current_lines: list[str] = []
    for index in range(jobs_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#")):
            break
        if match := re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line):
            if current_number is not None:
                blocks.append((current_number, current_name, current_lines))
            current_number = index + 1
            current_name = match.group(1)
            current_lines = [line]
        elif current_number is not None:
            current_lines.append(line)
    if current_number is not None:
        blocks.append((current_number, current_name, current_lines))
    return blocks


def _step_ranges(block_lines: list[str]) -> list[tuple[int, int]]:
    starts = [
        index for index, candidate in enumerate(block_lines) if re.match(r"^      -\s+", candidate)
    ]
    return [
        (start, starts[index + 1] if index + 1 < len(starts) else len(block_lines))
        for index, start in enumerate(starts)
    ]


def _run_source(block_lines: list[str], start: int, end: int) -> str:
    for candidate_index, candidate in enumerate(block_lines[start:end]):
        match = re.match(
            r"^(?:      -\s+|        )run\s*:\s*(.*)$",
            candidate,
        )
        if match is not None:
            value = match.group(1)
            if value not in {"|", "|-", "|+", ">", ">-", ">+"}:
                return value
            source_lines: list[str] = []
            for nested in block_lines[start + candidate_index + 1 : end]:
                nested_indent = len(nested) - len(nested.lstrip())
                if nested.strip() and nested_indent <= 8:
                    break
                source_lines.append(nested)
            return "\n".join(source_lines)
    return ""


def evaluate_workflow(path: Path, text: str) -> list[str]:
    """Return fail-closed policy findings for one workflow document."""

    findings: list[str] = []
    lines = text.splitlines()
    if not re.search(r"^permissions:\s*$", text, re.MULTILINE):
        findings.append(f"{path.name}: missing top-level permissions")
    for match in UNPINNED_USE.finditer(text):
        target = match.group(1)
        if target.startswith("./") or FULL_COMMIT.fullmatch(target):
            continue
        findings.append(f"{path.name}: action is not pinned to a full commit: {target}")

    for index, line in enumerate(lines):
        match = re.match(
            r"^(?P<indent>\s*)(?P<dash>-\s+)?uses:\s*actions/checkout@[0-9a-f]{40}(?:\s*#.*)?$",
            line,
        )
        if match is None:
            continue
        step_indent = len(match.group("indent")) - (0 if match.group("dash") else 2)
        checkout_block = [line]
        for candidate in lines[index + 1 :]:
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and candidate_indent <= step_indent:
                break
            checkout_block.append(candidate)
        if not re.search(
            r"^\s+persist-credentials:\s*false\s*(?:#.*)?$",
            "\n".join(checkout_block),
            re.MULTILINE,
        ):
            findings.append(
                f"{path.name}:{index + 1}: checkout must set persist-credentials: false"
            )

    for number, line in enumerate(lines, 1):
        # Neither workflow-level nor job-level env can use the runner context.
        # That context is available only after a job has started.
        match = re.match(r"^(?P<indent>\s*)env\s*:", line)
        if not match or len(match.group("indent")) not in {0, 4}:
            continue
        block_indent = len(match.group("indent"))
        if "${{ runner." in line:
            findings.append(f"{path.name}:{number}: runner context used before step scope")
        env_lines = [line]
        index = number
        while index < len(lines):
            candidate = lines[index]
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate and candidate_indent <= block_indent:
                break
            env_lines.append(candidate)
            if "${{ runner." in candidate:
                findings.append(f"{path.name}:{index + 1}: runner context used before step scope")
            index += 1
        if block_indent == 0 and SECRET_EXPRESSION.search("\n".join(env_lines)):
            findings.append(f"{path.name}:{number}: workflow-level env may not contain secrets")

    if _declares_event(text, "pull_request_target"):
        findings.append(f"{path.name}: pull_request_target is forbidden")
    if _declares_event(text, "pull_request"):
        if SECRET_EXPRESSION.search(text):
            findings.append(f"{path.name}: pull-request workflow references repository secrets")
        if re.search(r"^    environment\s*:", text, re.MULTILINE):
            findings.append(f"{path.name}: pull-request workflow enters a protected environment")
        if WRITE_PERMISSION.search(text):
            findings.append(f"{path.name}: pull-request workflow requests write permission")

    if SECRET_EXPRESSION.search(text):
        risky = [event for event in UNTRUSTED_EVENTS if _declares_event(text, event)]
        if risky:
            findings.append(
                f"{path.name}: secret-bearing workflow has non-manual trigger(s): "
                + ", ".join(risky)
            )
        if not _declares_event(text, "workflow_dispatch"):
            findings.append(f"{path.name}: secret-bearing workflow is not manual-only")
    job_blocks = _job_blocks(lines)
    if not job_blocks:
        findings.append(f"{path.name}: workflow has no jobs")
    for number, name, block_lines in job_blocks:
        block = "\n".join(block_lines)
        if not re.search(r"^    runs-on\s*:", block, re.MULTILINE):
            findings.append(f"{path.name}:{number}: job {name} has no runs-on")
        if not re.search(r"^    timeout-minutes\s*:", block, re.MULTILINE):
            findings.append(f"{path.name}:{number}: job {name} has no timeout-minutes")
        if SECRET_EXPRESSION.search(block) and not re.search(
            r"^    environment\s*:", block, re.MULTILINE
        ):
            findings.append(
                f"{path.name}:{number}: secret-bearing job {name} has no protected environment"
            )
        for env_index, candidate in enumerate(block_lines):
            if not re.match(r"^    env\s*:\s*(?:#.*)?$", candidate):
                continue
            env_end = env_index + 1
            while env_end < len(block_lines):
                nested = block_lines[env_end]
                nested_indent = len(nested) - len(nested.lstrip())
                if nested.strip() and nested_indent <= 4:
                    break
                env_end += 1
            if SECRET_EXPRESSION.search("\n".join(block_lines[env_index:env_end])):
                findings.append(
                    f"{path.name}:{number + env_index}: secrets in job {name} must be "
                    "scoped to the consuming step"
                )
        if SECRET_EXPRESSION.search(block) and re.search(r"^    if\s*:", block, re.MULTILINE):
            findings.append(
                f"{path.name}:{number}: secret-bearing job {name} may not be conditionally skipped"
            )
        if re.search(r"^    continue-on-error\s*:", block, re.MULTILINE):
            findings.append(f"{path.name}:{number}: job {name} may not continue on error")

        # A credential-consuming step cannot be advisory.  Release workflows
        # may collect diagnostics with continue-on-error only in steps that do
        # not receive credentials, followed by an explicit terminal gate.
        step_ranges = _step_ranges(block_lines)
        for step_index, (start, end) in enumerate(step_ranges):
            step = "\n".join(block_lines[start:end])
            shell_source = _run_source(block_lines, start, end)
            if SHELL_EXPRESSION.search(shell_source):
                findings.append(
                    f"{path.name}:{number + start}: GitHub expression in a shell command in "
                    f"job {name}; pass it through step env"
                )
            if SECRET_EXPRESSION.search(shell_source):
                findings.append(
                    f"{path.name}:{number + start}: secret expression in a shell command in "
                    f"job {name}; pass it through step env"
                )
            if SECRET_EXPRESSION.search(step) and re.search(
                r"^(?:      -\s+|        )continue-on-error\s*:\s*true\s*(?:#.*)?$",
                step,
                re.MULTILINE,
            ):
                findings.append(
                    f"{path.name}:{number + start}: credential-consuming step in job "
                    f"{name} may not continue on error"
                )
            step_continue = re.search(
                r"^(?:      -\s+|        )continue-on-error\s*:\s*([^#\n]+?)(?:\s+#.*)?$",
                step,
                re.MULTILINE,
            )
            if step_continue is not None and step_continue.group(1).strip() not in {
                "false",
                "true",
            }:
                findings.append(
                    f"{path.name}:{number + start}: step in job {name} may not use "
                    "a dynamic continue-on-error value"
                )
            if step_continue is not None and step_continue.group(1).strip() == "true":
                identifier = re.search(
                    r"^(?:      -\s+|        )id\s*:\s*([A-Za-z_][A-Za-z0-9_-]*)\s*$",
                    step,
                    re.MULTILINE,
                )
                if identifier is None:
                    findings.append(
                        f"{path.name}:{number + start}: continue-on-error step in job {name} "
                        "requires an ID and terminal outcome gate"
                    )
                    continue
                outcome = f"steps.{identifier.group(1)}.outcome"
                gated = False
                for future_start, future_end in step_ranges[step_index + 1 :]:
                    future = "\n".join(block_lines[future_start:future_end])
                    if (
                        outcome in future
                        and re.search(
                            r"^        if\s*:\s*always\(\)\s*(?:#.*)?$",
                            future,
                            re.MULTILINE,
                        )
                        and re.search(
                            r"(^|[;&|]\s*|\s)exit(?:\s|$)",
                            _run_source(block_lines, future_start, future_end),
                        )
                    ):
                        gated = True
                        break
                if not gated:
                    findings.append(
                        f"{path.name}:{number + start}: continue-on-error step "
                        f"{identifier.group(1)} in job {name} lacks an always-run terminal "
                        "outcome gate"
                    )
    return findings


def evaluate_assistant_release_topology(
    evaluation: str,
    finalization: str,
    release: str,
    pages: str,
) -> list[str]:
    """Reject a circular or unverifiable assistant usefulness release gate.

    Practitioner ratings can only be authored after the provider answers and
    Weave call identities exist.  The protected process must therefore preserve
    an immutable stage-one artifact and finalize that exact artifact in a later
    dispatch without invoking the provider again.
    """

    findings: list[str] = []
    if f"name: {ASSISTANT_EVALUATION_NAME}" not in evaluation:
        findings.append("assistant evaluation workflow has an unexpected identity")
    if "--evaluation-only" not in evaluation:
        findings.append("assistant evaluation workflow does not emit an unrated candidate")
    if "practitioner_ratings" in evaluation or "--practitioner-ratings" in evaluation:
        findings.append("assistant evaluation workflow consumes ratings before answers exist")
    if "name: ecocorridor-assistant-evaluation-candidate" not in evaluation:
        findings.append("assistant evaluation workflow does not preserve its immutable candidate")
    required_evaluation_identity = (
        'test "${GITHUB_REF}" = "refs/heads/main"',
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        "github_actions_producer_identity.py create",
        "ecocorridor-assistant-evaluation-producer-identity.json",
        '--run-attempt "${GITHUB_RUN_ATTEMPT}"',
        '--workflow-ref "${GITHUB_WORKFLOW_REF}"',
        '--workflow-sha "${GITHUB_WORKFLOW_SHA}"',
    )
    for token in required_evaluation_identity:
        if token not in evaluation:
            findings.append(f"assistant evaluation producer identity is missing {token}")
    if re.search(
        r"^      (?:WANDB_API_KEY|WANDB_ENTITY|ECOCORRIDOR_PROVIDER_API_KEY):\s*"
        r"\$\{\{\s*secrets\.",
        evaluation,
        re.MULTILINE,
    ):
        findings.append("assistant provider secrets may not be inherited by the whole job")

    required_finalization = (
        'test "${GITHUB_REF}" = "refs/heads/main"',
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        "evaluation_run_id:",
        "resolve_github_actions_provenance.py",
        ".github/workflows/ecocorridor-assistant-evaluation.yml",
        "artifact-ids: ${{ steps.resolve_candidate.outputs.artifact_id }}",
        "digest-mismatch: error",
        "ecocorridor-assistant-evaluation-candidate",
        "validate_ecocorridor_assistant_evaluation.py",
        "ECOCORRIDOR_ASSISTANT_RATINGS_B64",
        "ECOCORRIDOR_ASSISTANT_RATINGS_SIGNATURE_B64",
        "ecocorridor-assistant-ratings.json",
        "ecocorridor-assistant-ratings.sig",
        "ecocorridor-assistant-ratings-public-key.bin",
        "github_actions_producer_identity.py verify",
        "github_actions_producer_identity.py create",
        "ecocorridor-assistant-evaluation-producer-identity.json",
        "ecocorridor-observability-finalization-producer-identity.json",
        "name: ecocorridor-observability-release-evidence",
    )
    for token in required_finalization:
        if token not in finalization:
            findings.append(f"assistant finalization workflow is missing {token}")
    if "run_ecocorridor_assistant_evaluation.py" in finalization:
        findings.append("assistant finalization workflow reruns the provider evaluation")
    if "ECOCORRIDOR_PROVIDER_API_KEY" in finalization:
        findings.append("assistant finalization workflow receives a provider credential")
    if "practitioner_ratings_url" in finalization or "curl " in finalization:
        findings.append("assistant ratings may not be supplied through persistent URL inputs")
    if re.search(
        r"^      ECOCORRIDOR_ASSISTANT_RATINGS_(?:B64|SIGNATURE_B64):\s*"
        r"\$\{\{\s*secrets\.",
        finalization,
        re.MULTILINE,
    ):
        findings.append("assistant ratings secrets may not be inherited by the whole job")

    for filename in (
        "assistant-evaluation-refinalized.json",
        "assistant-ratings.json",
        "assistant-ratings.sig",
        "assistant-ratings-public-key.bin",
        "assistant-evaluation-source-provenance.json",
        "assistant-evaluation-producer-identity.json",
        "assistant-stage-one-recheck-provenance.json",
        "assistant-finalization-provenance.json",
        "assistant-finalization-producer-identity.json",
        "eco-ci-provenance.json",
        "general-ci-provenance.json",
        "public-replay-pages-provenance.json",
        "public-replay-pages-producer-identity.json",
    ):
        if f"release-assets/{filename}" not in release:
            findings.append(f"protected release does not publish {filename}")
    required_release_provenance = (
        ".github/workflows/ecocorridor-ci.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/ecocorridor-pages.yml",
        "--run-only",
        "artifact-ids: ${{ steps.resolve_pages_artifact.outputs.artifact_id }}",
        "run-id: ${{ inputs.pages_run_id }}",
        "merge-multiple: true",
        "digest-mismatch: error",
        "github_actions_producer_identity.py verify",
        "ecocorridor-pages-producer-identity.json",
        "stage-one-recheck/ecocorridor-assistant-evaluation-pending.json",
        "stage-one-recheck/ecocorridor-assistant-evaluation-cases.json",
        "--finalized-report",
        "assistant-evaluation-refinalized.json",
    )
    for token in required_release_provenance:
        if token not in release:
            findings.append(f"protected release provenance is missing {token}")
    if not re.search(
        r'cmp\s+\\\n\s+"\$\{RUNNER_TEMP\}/release-prerequisites/'
        r'assistant-evaluation-refinalized\.json"\s+\\\n\s+'
        r'"\$\{RUNNER_TEMP\}/release-prerequisites/observability/'
        r'ecocorridor-assistant-evaluation\.json"',
        release,
    ):
        findings.append(
            "protected release does not byte-compare the independently finalized report"
        )
    if "gh run view" in release or "gh run download" in release:
        findings.append("protected release may not use name-only gh run provenance")
    required_release_tag_binding = (
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        '"/repos/${GITHUB_REPOSITORY}/git/ref/tags/${RELEASE_TAG}"',
        '"/repos/${GITHUB_REPOSITORY}/git/refs"',
        '-f ref="refs/tags/${RELEASE_TAG}"',
        '-f sha="${GITHUB_SHA}"',
        "--verify-tag",
        "ecocorridor-published-tag-ref.json",
        'target.get("sha") == os.environ["EXPECTED_SHA"]',
        "ECOCORRIDOR_RELEASE_POLICY_TOKEN",
        "ECOCORRIDOR_RELEASE_TAG_RULESET_ID",
        '"/repos/${GITHUB_REPOSITORY}/immutable-releases"',
        '"/repos/${GITHUB_REPOSITORY}/rulesets/${RELEASE_TAG_RULESET_ID}?includes_parents=true"',
        "X-GitHub-Api-Version: 2026-03-10",
        "validate_github_release_policy.py",
        "verify_github_release_assets.py create",
        "verify_github_release_assets.py verify",
        'test "${PUBLISH_RELEASE}" = "true"',
        "--draft",
        "--draft=false",
        'release.get("immutable") is True',
    )
    for token in required_release_tag_binding:
        if token not in release:
            findings.append(f"protected release tag binding is missing {token}")
    if '--target "${GITHUB_SHA}"' in release:
        findings.append("protected release may not rely on gh release --target tag creation")
    if release.count("verify_github_release_assets.py verify") < 2:
        findings.append("protected release must verify exact assets before and after publication")
    if re.search(
        r"^      RELEASE_POLICY_TOKEN:\s*\$\{\{\s*secrets\.",
        release,
        re.MULTILINE,
    ):
        findings.append("release policy credential may not be inherited by the whole job")
    required_pages_identity = (
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        "scripts/github_actions_producer_identity.py",
        "github_actions_producer_identity.py create",
        "ecocorridor-pages-producer-identity.json",
        '--run-attempt "${GITHUB_RUN_ATTEMPT}"',
        '--workflow-ref "${GITHUB_WORKFLOW_REF}"',
        '--workflow-sha "${GITHUB_WORKFLOW_SHA}"',
    )
    for token in required_pages_identity:
        if token not in pages:
            findings.append(f"Pages producer identity is missing {token}")
    if pages.find("github_actions_producer_identity.py create") > pages.find(
        "actions/upload-pages-artifact@"
    ):
        findings.append("Pages producer identity must be embedded before artifact upload")
    return findings


def evaluate_ridgeway_release_topology(
    producer: str,
    pages: str,
    release: str,
) -> list[str]:
    """Require authenticated direct execution before replay or release promotion."""

    findings: list[str] = []
    if f"name: {RIDGEWAY_EVIDENCE_NAME}" not in producer:
        findings.append("Ridgeway producer workflow has an unexpected identity")
    if not _declares_event(producer, "workflow_dispatch") or any(
        _declares_event(producer, event)
        for event in ("push", "pull_request", "pull_request_target", "merge_group", "schedule")
    ):
        findings.append("Ridgeway producer workflow must be manual-only")
    required_producer = (
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        "source_ci_run_id:",
        "environment:",
        "ecocorridor-release-evidence",
        ".github/workflows/ecocorridor-ci.yml",
        "benchmarks/ecocorridor/ridgeway/run_direct_study.py",
        "scripts/package_ridgeway_evidence.py",
        "scripts/verify_ridgeway_release_evidence.py",
        "scripts/compare_ridgeway_protected_rerun.py",
        "scripts/validate_ridgeway_producer_attestation.py create",
        "scripts/github_actions_producer_identity.py create",
        "ecocorridor-ridgeway-protected-evidence",
        '--run-attempt "${GITHUB_RUN_ATTEMPT}"',
        '--workflow-ref "${GITHUB_WORKFLOW_REF}"',
        '--workflow-sha "${GITHUB_WORKFLOW_SHA}"',
    )
    for token in required_producer:
        if token not in producer:
            findings.append(f"Ridgeway producer workflow is missing {token}")
    if "--job-limit" in producer:
        findings.append("Ridgeway protected producer may not run a partial study")

    required_consumer = (
        "ridgeway_evidence_run_id:",
        ".github/workflows/ecocorridor-ridgeway-evidence.yml",
        "ecocorridor-ridgeway-protected-evidence",
        "artifact-ids: ${{ steps.resolve_ridgeway_evidence.outputs.artifact_id }}",
        "digest-mismatch: error",
        "scripts/github_actions_producer_identity.py verify",
        "scripts/compare_ridgeway_protected_rerun.py",
        "scripts/validate_ridgeway_producer_attestation.py verify",
    )
    for label, workflow in (("Pages", pages), ("release", release)):
        for token in required_consumer:
            if token not in workflow:
                findings.append(f"{label} does not require protected Ridgeway evidence: {token}")
    if _declares_event(pages, "push") or not _declares_event(pages, "workflow_dispatch"):
        findings.append("Pages must be manual-only after protected Ridgeway evidence exists")
    for filename in (
        "ridgeway-protected-producer-attestation.json",
        "ridgeway-protected-rerun-differential.json",
        "ridgeway-producer-identity.json",
        "ridgeway-producer-provenance.json",
    ):
        if f"release-assets/{filename}" not in release:
            findings.append(f"protected release does not publish {filename}")
    for token in (
        "release-assets/ridgeway-direct-evidence-manifest.json",
        "release-assets/ridgeway-ui-projection.json",
        "release-assets/ridgeway-release-trust.json",
        "release-assets/ridgeway-protected-rerun-direct-evidence-manifest.json",
        "release-assets/ridgeway-protected-rerun-ui-projection.json",
        "release-assets/ridgeway-protected-rerun-release-trust.json",
        'cp "${producer_root}/ridgeway/${producer_archive}"',
    ):
        if token not in release:
            findings.append(f"protected release omits durable Ridgeway evidence: {token}")
    return findings


def main() -> int:
    findings: list[str] = []
    paths = sorted({*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")})
    for path in paths:
        text = path.read_text(encoding="utf-8")
        findings.extend(evaluate_workflow(path, text))
    by_name = {path.name: path.read_text(encoding="utf-8") for path in paths}
    required = {
        ASSISTANT_EVALUATION_WORKFLOW,
        ASSISTANT_FINALIZATION_WORKFLOW,
        ECOCORRIDOR_RELEASE_WORKFLOW,
        ECOCORRIDOR_PAGES_WORKFLOW,
        RIDGEWAY_EVIDENCE_WORKFLOW,
    }
    missing = sorted(required - set(by_name))
    if missing:
        findings.append("assistant release workflow topology is incomplete: " + ", ".join(missing))
    else:
        findings.extend(
            evaluate_assistant_release_topology(
                by_name[ASSISTANT_EVALUATION_WORKFLOW],
                by_name[ASSISTANT_FINALIZATION_WORKFLOW],
                by_name[ECOCORRIDOR_RELEASE_WORKFLOW],
                by_name[ECOCORRIDOR_PAGES_WORKFLOW],
            )
        )
        findings.extend(
            evaluate_ridgeway_release_topology(
                by_name[RIDGEWAY_EVIDENCE_WORKFLOW],
                by_name[ECOCORRIDOR_PAGES_WORKFLOW],
                by_name[ECOCORRIDOR_RELEASE_WORKFLOW],
            )
        )
    if findings:
        for finding in findings:
            print(finding)
        return 1
    print(f"Workflow policy passed for {len(paths)} workflow(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
