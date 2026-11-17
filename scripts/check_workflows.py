#!/usr/bin/env python3
"""Small fail-fast workflow policy check; actionlint remains the syntax authority."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
UNPINNED_USE = re.compile(r"^\s*-?\s*uses:\s*([^#\s]+)", re.MULTILINE)
FULL_COMMIT = re.compile(r"^[^@]+@[0-9a-f]{40}$")
SECRET_EXPRESSION = re.compile(
    r"\$\{\{(?:(?!\}\}).)*\bsecrets\b",
    re.IGNORECASE | re.DOTALL,
)
EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
CONTEXT_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<context>github|needs|strategy|matrix|job|runner|env|vars|secrets|steps|inputs)"
    r"\s*(?:\.|\[)",
    re.IGNORECASE,
)
RUNTIME_ONLY_GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])github\s*(?:\.\s*token|\[\s*['\"]token['\"]\s*\])",
    re.IGNORECASE,
)
SHELL_EXPRESSION = re.compile(r"\$\{\{")
WRITE_PERMISSION = re.compile(r"^\s+[a-z-]+:\s*write\s*$", re.MULTILINE)
UNTRUSTED_EVENTS = ("pull_request", "pull_request_target", "push", "merge_group", "schedule")
ASSISTANT_EVALUATION_WORKFLOW = "ecocorridor-assistant-evaluation.yml"
ASSISTANT_FINALIZATION_WORKFLOW = "ecocorridor-observability-integration.yml"
ECOCORRIDOR_CI_WORKFLOW = "ecocorridor-ci.yml"
ECOCORRIDOR_RELEASE_WORKFLOW = "ecocorridor-release.yml"
ECOCORRIDOR_PAGES_WORKFLOW = "ecocorridor-pages.yml"
RIDGEWAY_EVIDENCE_WORKFLOW = "ecocorridor-ridgeway-evidence.yml"
REVIEW_BRIEF_EVALUATION_WORKFLOW = "ecocorridor-review-brief-evaluation.yml"
ASSISTANT_EVALUATION_NAME = "Eco protected assistant evaluation candidate"
RIDGEWAY_EVIDENCE_NAME = "EcoCorridor protected Ridgeway direct evidence"
REVIEW_BRIEF_EVALUATION_NAME = "EcoCorridor protected Review Brief evaluation"
ECOCORRIDOR_GEOSPATIAL_HELPER = "scripts/install_ecocorridor_geospatial_runtime.py"
ECOCORRIDOR_GEO_EXTRA = re.compile(r"(?<![A-Za-z0-9_-])--extra\s+geo(?=(?:\s|\\|['\";]|$))")
ECOCORRIDOR_GEOSPATIAL_INVOCATION = re.compile(
    r"(?<!\S)uv\s+run\s+--no-sync\s+python\s+"
    + re.escape(ECOCORRIDOR_GEOSPATIAL_HELPER)
    + r"(?=(?:\s|\\|['\";]|$))"
)
LOCKED_UV_SYNC = re.compile(r"(?<!\S)uv\s+sync\s+--locked(?=(?:\s|\\|['\";]|$))")
PACKAGECOMPILER_BUILD_SCRIPT = "scripts/build_ecocorridor_engine.jl"
PACKAGECOMPILER_HEARTBEAT_HELPER = "scripts/run_with_progress_heartbeat.sh"
PACKAGECOMPILER_HEARTBEAT_LABEL = "PackageCompiler application build"
CIRCUITSCAPE_MANIFEST_VERIFIER = "julia/EcoCorridorEngine/scripts/verify_manifest.jl"
CIRCUITSCAPE_PROJECT_ARGUMENT = "--project=julia/EcoCorridorEngine"
UPLOAD_ACTION = re.compile(
    r"^\s*-?\s*uses:\s*actions/upload(?:-pages)?-artifact@",
    re.MULTILINE,
)
BROAD_RUNNER_TEMP_UPLOAD = re.compile(
    r"^(?:"
    r"\$\{\{\s*runner\.temp\s*\}\}|"
    r"\$\{RUNNER_TEMP\}|"
    r"\$RUNNER_TEMP|"
    r"\$env:RUNNER_TEMP|"
    r"%RUNNER_TEMP%"
    r")(?:[/\\*?]+)?$",
    re.IGNORECASE,
)
CREDENTIAL_PATH_TOKENS = (
    "authkey.p8",
    "ci-resource-key.seed.hex",
    "ecocorridor-build.keychain",
    "ecocorridor-resource-signing-key.seed.hex",
    "ecocorridor-signing.p12",
    "ecocorridor-signing.pfx",
)


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


def _yaml_value_block(lines: list[str], start: int, indent: int) -> list[str]:
    """Return one YAML key/value and its indented continuation lines."""

    value_lines = [lines[start]]
    for candidate in lines[start + 1 :]:
        candidate_indent = len(candidate) - len(candidate.lstrip())
        if candidate.strip() and candidate_indent <= indent:
            break
        value_lines.append(candidate)
    return value_lines


def _expression_contexts(source: str) -> set[str]:
    contexts: set[str] = set()
    for expression in EXPRESSION.finditer(source):
        contexts.update(
            match.group("context").lower()
            for match in CONTEXT_REFERENCE.finditer(expression.group("body"))
        )
    return contexts


def _pre_job_context_findings(path: Path, lines: list[str]) -> list[str]:
    """Reject runtime-only contexts where GitHub evaluates before a step starts."""

    findings: list[str] = []

    def inspect(
        *,
        source_lines: list[str],
        line_number: int,
        location: str,
        allowed: set[str],
    ) -> None:
        source = "\n".join(source_lines)
        for context in sorted(_expression_contexts(source) - allowed):
            if context == "runner":
                findings.append(f"{path.name}:{line_number}: runner context used before step scope")
            else:
                findings.append(
                    f"{path.name}:{line_number}: {context} context is unavailable in {location}"
                )
        if any(
            RUNTIME_ONLY_GITHUB_TOKEN.search(expression.group("body"))
            for expression in EXPRESSION.finditer(source)
        ):
            findings.append(
                f"{path.name}:{line_number}: github.token is unavailable before step scope"
            )

    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if re.match(r"^env\s*:", line):
            inspect(
                source_lines=_yaml_value_block(lines, index, indent),
                line_number=index + 1,
                location="workflow env",
                allowed={"github", "inputs", "secrets", "vars"},
            )
        elif re.match(r"^concurrency\s*:", line):
            inspect(
                source_lines=_yaml_value_block(lines, index, indent),
                line_number=index + 1,
                location="workflow concurrency",
                allowed={"github", "inputs", "vars"},
            )

    job_allowed = {"github", "inputs", "matrix", "needs", "strategy", "vars"}
    for number, name, block_lines in _job_blocks(lines):
        for index, line in enumerate(block_lines):
            match = re.match(
                r"^    (?P<key>name|runs-on|timeout-minutes|continue-on-error|if|strategy|concurrency)\s*:",
                line,
            )
            if match is None:
                continue
            key = match.group("key")
            allowed = job_allowed
            if key in {"if", "strategy"}:
                allowed = {"github", "inputs", "needs", "vars"}
            inspect(
                source_lines=_yaml_value_block(block_lines, index, 4),
                line_number=number + index,
                location=f"jobs.{name}.{key}",
                allowed=allowed,
            )

        for index, line in enumerate(block_lines):
            if not re.match(r"^    env\s*:", line):
                continue
            inspect(
                source_lines=_yaml_value_block(block_lines, index, 4),
                line_number=number + index,
                location=f"jobs.{name}.env",
                allowed={"github", "inputs", "matrix", "needs", "secrets", "strategy", "vars"},
            )
    return findings


def _upload_path_values(step_lines: list[str]) -> list[str]:
    values: list[str] = []
    for with_index, with_line in enumerate(step_lines):
        with_match = re.match(r"^(?P<indent>\s+)with\s*:\s*(?:#.*)?$", with_line)
        if with_match is None:
            continue
        with_indent = len(with_match.group("indent"))
        path_match: re.Match[str] | None = None
        path_index = -1
        path_indent = -1
        for index in range(with_index + 1, len(step_lines)):
            line = step_lines[index]
            line_indent = len(line) - len(line.lstrip())
            if line.strip() and line_indent <= with_indent:
                break
            candidate_match = re.match(r"^\s+path\s*:\s*(?P<value>.*)$", line)
            if candidate_match is None or line_indent != with_indent + 2:
                continue
            path_match = candidate_match
            path_index = index
            path_indent = line_indent
            break
        if path_match is None:
            continue
        value = path_match.group("value").strip()
        if value not in {"|", "|-", "|+", ">", ">-", ">+"}:
            values.append(value.strip("'\""))
            continue
        for candidate in step_lines[path_index + 1 :]:
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and candidate_indent <= path_indent:
                break
            normalized = candidate.strip().removeprefix("- ").strip().strip("'\"")
            if normalized and not normalized.startswith("#"):
                values.append(normalized)
    return values


def _artifact_upload_findings(
    path: Path,
    *,
    job_line: int,
    step_start: int,
    step_lines: list[str],
) -> list[str]:
    step = "\n".join(step_lines)
    if UPLOAD_ACTION.search(step) is None:
        return []
    findings: list[str] = []
    for value in _upload_path_values(step_lines):
        line_number = job_line + step_start
        if BROAD_RUNNER_TEMP_UPLOAD.fullmatch(value):
            findings.append(
                f"{path.name}:{line_number}: artifact upload may not use the broad RUNNER_TEMP root"
            )
        lowered = value.lower()
        for token in CREDENTIAL_PATH_TOKENS:
            if token in lowered:
                findings.append(
                    f"{path.name}:{line_number}: artifact upload includes credential path {token}"
                )
    return findings


def _named_steps(text: str, step_name: str) -> list[str]:
    matches: list[str] = []
    for _, _, block_lines in _job_blocks(text.splitlines()):
        for start, end in _step_ranges(block_lines):
            step = "\n".join(block_lines[start:end])
            if re.search(
                rf"^(?:      -\s+|        )name:\s*{re.escape(step_name)}\s*$",
                step,
                re.MULTILINE,
            ):
                matches.append(step)
    return matches


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

    findings.extend(_pre_job_context_findings(path, lines))
    for number, line in enumerate(lines, 1):
        match = re.match(r"^(?P<indent>\s*)env\s*:", line)
        if match is None or len(match.group("indent")) != 0:
            continue
        env_lines = _yaml_value_block(lines, number - 1, 0)
        if SECRET_EXPRESSION.search("\n".join(env_lines)):
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
            step_lines = block_lines[start:end]
            step = "\n".join(step_lines)
            shell_source = _run_source(block_lines, start, end)
            findings.extend(
                _artifact_upload_findings(
                    path,
                    job_line=number,
                    step_start=start,
                    step_lines=step_lines,
                )
            )
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
        "scripts/publish_ridgeway_evidence.py",
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


def evaluate_review_brief_evaluation_topology(workflow: str) -> list[str]:
    """Lock the credentialed campaign, media, and append-only Report sequence."""

    findings: list[str] = []
    if f"name: {REVIEW_BRIEF_EVALUATION_NAME}" not in workflow:
        findings.append("Review Brief evaluation workflow has an unexpected identity")
    if not _declares_event(workflow, "workflow_dispatch") or any(
        _declares_event(workflow, event)
        for event in ("push", "pull_request", "pull_request_target", "merge_group", "schedule")
    ):
        findings.append("Review Brief evaluation workflow must be manual-only")
    required = (
        'test "${GITHUB_REF}" = "refs/heads/main"',
        'test "${GITHUB_WORKFLOW_SHA}" = "${GITHUB_SHA}"',
        "environment:",
        "ecocorridor-observability-integration",
        "uv sync --locked --extra ecocorridor --extra ecocorridor-agents",
        "tests/python/test_review_brief_evaluation_campaign.py",
        "scripts/run_ecocorridor_review_brief_evaluation.py",
        '--provider-cost-basis-ref "${PROVIDER_COST_BASIS_REF}"',
        '--provider-input-usd-per-million "${PROVIDER_INPUT_USD_PER_MILLION}"',
        '--provider-output-usd-per-million "${PROVIDER_OUTPUT_USD_PER_MILLION}"',
        '--wandb-api-base-url "${WANDB_BASE_URL}"',
        '--wandb-web-base-url "${WANDB_WEB_BASE_URL}"',
        "--authorize-synthetic-public-provider-evaluation",
        "scripts/render_ecocorridor_review_brief_animation.py",
        "--authorize-verified-provider-media-render",
        "scripts/publish_ecocorridor_review_brief_report.py",
        '--report-url "${WANDB_REPORT_URL}"',
        "--authorize-synthetic-public-upload",
        "name: ecocorridor-review-brief-evaluation",
        "if-no-files-found: error",
    )
    for token in required:
        if token not in workflow:
            findings.append(f"Review Brief evaluation workflow is missing {token}")
    for token in (
        '--wandb-api-base-url "${WANDB_BASE_URL}"',
        '--wandb-web-base-url "${WANDB_WEB_BASE_URL}"',
    ):
        if workflow.count(token) != 2:
            findings.append(
                f"Review Brief campaign and publisher must both receive the exact target: {token}"
            )
    ordered = (
        "Run network-free campaign and publisher tests",
        "Run the real three-method by fourteen-case campaign",
        "Render only the complete verified campaign",
        "Append only the verified owned section to the existing Report",
        "Preserve the exact campaign and publication receipt",
    )
    if all(f"- name: {name}" in workflow for name in ordered):
        positions = [workflow.index(f"- name: {name}") for name in ordered]
        if positions != sorted(positions):
            findings.append(
                "Review Brief evaluation must test, run, render, append, and preserve in order"
            )
    return findings


def _shell_without_comment_lines(source: str) -> str:
    """Normalize a YAML run body while excluding comment-only shell lines."""

    return " ".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def _logical_shell_commands(source: str) -> list[str]:
    """Join backslash continuations while retaining command boundaries."""

    commands: list[str] = []
    fragments: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            fragments.append(line[:-1].rstrip())
            continue
        fragments.append(line)
        commands.append(" ".join(fragments))
        fragments = []
    if fragments:
        commands.append(" ".join(fragments))
    return commands


def _circuitscape_julia_command_kind(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    prefix = [
        "julia",
        CIRCUITSCAPE_PROJECT_ARGUMENT,
        "--startup-file=no",
        "--history-file=no",
    ]
    if tokens[: len(prefix)] != prefix:
        return None
    arguments = tokens[len(prefix) :]
    if arguments == ["-e", "using Pkg; Pkg.instantiate()"]:
        return "instantiate"
    if arguments == [CIRCUITSCAPE_MANIFEST_VERIFIER]:
        return "verify"
    if arguments == ["-e", "using Pkg; Pkg.precompile()"]:
        return "precompile"
    return None


def evaluate_circuitscape_manifest_verification_topology(
    workflows: dict[str, str],
) -> list[str]:
    """Require runtime-manifest verification before tests, packaging, or evidence.

    Every approved Circuitscape environment must execute the same three-command
    sequence in one step: instantiate, verify the loaded package graph, then
    precompile.  Keeping the verifier adjacent prevents another command from
    using or transforming an unverified dependency environment first.
    """

    findings: list[str] = []
    expected: dict[str, dict[str, int]] = {
        ECOCORRIDOR_CI_WORKFLOW: {
            "connectivity-engine": 1,
            "packaged-scientific-boundary": 1,
        },
        ECOCORRIDOR_RELEASE_WORKFLOW: {"build-signed-installers": 1},
        RIDGEWAY_EVIDENCE_WORKFLOW: {"direct-ridgeway-evidence": 1},
    }
    missing = sorted(set(expected) - set(workflows))
    if missing:
        findings.append(
            "Circuitscape manifest-verification topology is incomplete: " + ", ".join(missing)
        )

    for workflow_name, text in sorted(workflows.items()):
        allowed_jobs = expected.get(workflow_name, {})
        observed_by_job: dict[str, int] = {}
        seen_jobs: set[str] = set()
        for number, job_name, block_lines in _job_blocks(text.splitlines()):
            seen_jobs.add(job_name)
            for start, end in _step_ranges(block_lines):
                source = _run_source(block_lines, start, end)
                commands = _logical_shell_commands(source)
                kinds = [_circuitscape_julia_command_kind(command) for command in commands]
                raw_instantiate_count = source.count("Pkg.instantiate()")
                recognized_instantiate_count = kinds.count("instantiate")
                if raw_instantiate_count != recognized_instantiate_count:
                    findings.append(
                        f"{workflow_name}:{number + start}: Pkg.instantiate must use the exact "
                        "pinned Circuitscape Julia command"
                    )
                if raw_instantiate_count and not re.search(
                    r"^\s*shell:\s*bash\s*$",
                    "\n".join(block_lines[start:end]),
                    re.MULTILINE,
                ):
                    findings.append(
                        f"{workflow_name}:{number + start}: Circuitscape instantiation must "
                        "declare shell: bash for cross-platform command continuations"
                    )
                for command_index, kind in enumerate(kinds):
                    if kind != "instantiate":
                        continue
                    if job_name not in allowed_jobs:
                        findings.append(
                            f"{workflow_name}:{number + start}: Circuitscape instantiation is "
                            f"not allowed in job {job_name}"
                        )
                    expected_sequence = ["instantiate", "verify", "precompile"]
                    if kinds[command_index : command_index + 3] != expected_sequence:
                        findings.append(
                            f"{workflow_name}:{number + start}: Circuitscape instantiation must "
                            "be followed immediately by verify_manifest.jl and precompile"
                        )
                        continue
                    observed_by_job[job_name] = observed_by_job.get(job_name, 0) + 1

                for command_index, kind in enumerate(kinds):
                    if kind == "verify" and (
                        command_index == 0 or kinds[command_index - 1] != "instantiate"
                    ):
                        findings.append(
                            f"{workflow_name}:{number + start}: verify_manifest.jl is not "
                            "immediately bound to an approved instantiation"
                        )

        for required_job, required_count in allowed_jobs.items():
            observed = observed_by_job.get(required_job, 0)
            if required_job not in seen_jobs:
                findings.append(
                    f"{workflow_name}: required Circuitscape job {required_job} is absent"
                )
            elif observed != required_count:
                findings.append(
                    f"{workflow_name}: job {required_job} must instantiate and immediately "
                    f"verify Circuitscape exactly {required_count} time(s); observed {observed}"
                )

    return findings


def evaluate_ecocorridor_geospatial_dependency_topology(
    workflows: dict[str, str],
) -> list[str]:
    """Keep the GeoPandas runtime additive, locked, and Eco-only.

    GeoPandas is intentionally projected from the committed lock by a helper
    instead of installing the GI-owned ``geo`` extra.  The helper is needed
    only by the two CI jobs that execute geospatial tests or package the
    scientific boundary and by the signed-installer build job.  Any new use is
    therefore an explicit policy change rather than dependency drift hidden in
    workflow YAML.
    """

    findings: list[str] = []
    expected: dict[str, dict[str, int]] = {
        ECOCORRIDOR_CI_WORKFLOW: {
            "isolation-and-python": 1,
            "packaged-scientific-boundary": 1,
        },
        ECOCORRIDOR_RELEASE_WORKFLOW: {"build-signed-installers": 1},
    }
    missing = sorted(set(expected) - set(workflows))
    if missing:
        findings.append(
            "EcoCorridor geospatial dependency topology is incomplete: " + ", ".join(missing)
        )

    for workflow_name, text in sorted(workflows.items()):
        if ECOCORRIDOR_GEO_EXTRA.search(text):
            findings.append(f"{workflow_name}: --extra geo crosses the GI-only dependency boundary")

        recognized_invocations = 0
        seen_jobs: set[str] = set()
        allowed_jobs = expected.get(workflow_name, {})
        for _, job_name, block_lines in _job_blocks(text.splitlines()):
            seen_jobs.add(job_name)
            count = 0
            sync_seen = False
            helper_before_sync = False
            for start, end in _step_ranges(block_lines):
                source = _shell_without_comment_lines(_run_source(block_lines, start, end))
                events = [
                    *((match.start(), "sync") for match in LOCKED_UV_SYNC.finditer(source)),
                    *(
                        (match.start(), "helper")
                        for match in ECOCORRIDOR_GEOSPATIAL_INVOCATION.finditer(source)
                    ),
                ]
                for _, event in sorted(events):
                    if event == "sync":
                        sync_seen = True
                    else:
                        count += 1
                        recognized_invocations += 1
                        if not sync_seen:
                            helper_before_sync = True

            required_count = allowed_jobs.get(job_name, 0)
            if required_count and count != required_count:
                findings.append(
                    f"{workflow_name}: job {job_name} must invoke "
                    f"{ECOCORRIDOR_GEOSPATIAL_HELPER} exactly {required_count} time(s); "
                    f"observed {count}"
                )
            elif not required_count and count:
                findings.append(
                    f"{workflow_name}: job {job_name} may not invoke "
                    f"{ECOCORRIDOR_GEOSPATIAL_HELPER}"
                )
            if count and helper_before_sync:
                findings.append(
                    f"{workflow_name}: job {job_name} invokes "
                    f"{ECOCORRIDOR_GEOSPATIAL_HELPER} before uv sync --locked"
                )

        for required_job, required_count in allowed_jobs.items():
            if required_job not in seen_jobs:
                findings.append(
                    f"{workflow_name}: job {required_job} must invoke "
                    f"{ECOCORRIDOR_GEOSPATIAL_HELPER} exactly {required_count} time(s); "
                    "job is absent"
                )

        raw_mentions = text.count(ECOCORRIDOR_GEOSPATIAL_HELPER)
        if raw_mentions != recognized_invocations:
            findings.append(
                f"{workflow_name}: {ECOCORRIDOR_GEOSPATIAL_HELPER} may appear only as "
                "an executable 'uv run --no-sync python' command"
            )

    return findings


def evaluate_packagecompiler_heartbeat_topology(
    workflows: dict[str, str],
) -> list[str]:
    """Require every long PackageCompiler build to emit bounded progress.

    GitHub-hosted builds can spend tens of minutes inside the nonincremental
    sysimage compiler without writing output.  Both the PR smoke build and the
    signed-installer matrix must therefore execute the same checked heartbeat
    wrapper.  Requiring an explicit Bash shell also keeps the release command
    identical on macOS and Windows runners.
    """

    findings: list[str] = []
    expected = {
        ECOCORRIDOR_CI_WORKFLOW: "packaged-scientific-boundary",
        ECOCORRIDOR_RELEASE_WORKFLOW: "build-signed-installers",
    }
    missing = sorted(set(expected) - set(workflows))
    if missing:
        findings.append("PackageCompiler heartbeat topology is incomplete: " + ", ".join(missing))

    expected_prefix = [
        "bash",
        PACKAGECOMPILER_HEARTBEAT_HELPER,
        "--interval-seconds",
        "60",
        "--label",
        PACKAGECOMPILER_HEARTBEAT_LABEL,
        "--",
        "julia",
    ]
    for workflow_name, text in sorted(workflows.items()):
        required_job = expected.get(workflow_name)
        recognized_builds = 0
        for number, job_name, block_lines in _job_blocks(text.splitlines()):
            for start, end in _step_ranges(block_lines):
                step = "\n".join(block_lines[start:end])
                source = _shell_without_comment_lines(_run_source(block_lines, start, end))
                has_build = PACKAGECOMPILER_BUILD_SCRIPT in source
                has_helper = PACKAGECOMPILER_HEARTBEAT_HELPER in source
                if not has_build and not has_helper:
                    continue
                if job_name != required_job:
                    findings.append(
                        f"{workflow_name}:{number + start}: PackageCompiler build helper is "
                        f"not allowed in job {job_name}"
                    )
                    continue
                if not has_build or not has_helper:
                    findings.append(
                        f"{workflow_name}:{number + start}: PackageCompiler build must use "
                        f"{PACKAGECOMPILER_HEARTBEAT_HELPER}"
                    )
                    continue
                recognized_builds += 1
                if not re.search(
                    r"^(?:      -\s+|        )shell:\s*bash\s*(?:#.*)?$",
                    step,
                    re.MULTILINE,
                ):
                    findings.append(
                        f"{workflow_name}:{number + start}: PackageCompiler heartbeat step "
                        "must declare shell: bash"
                    )
                try:
                    tokens = shlex.split(source.replace("\\", " "))
                except ValueError:
                    tokens = []
                if tokens[: len(expected_prefix)] != expected_prefix:
                    findings.append(
                        f"{workflow_name}:{number + start}: PackageCompiler heartbeat command "
                        "must use the reviewed 60-second invocation"
                    )
                if not tokens or tokens[-1] != PACKAGECOMPILER_BUILD_SCRIPT:
                    findings.append(
                        f"{workflow_name}:{number + start}: PackageCompiler build script must be "
                        "the wrapped command"
                    )

        if required_job is not None and recognized_builds != 1:
            findings.append(
                f"{workflow_name}: job {required_job} must contain exactly one heartbeat-wrapped "
                f"PackageCompiler build; observed {recognized_builds}"
            )

    return findings


def evaluate_release_signing_security_topology(
    workflows: dict[str, str],
) -> list[str]:
    """Pin reviewed key materialization, use, and exact-target cleanup boundaries."""

    findings: list[str] = []
    required_workflows = {ECOCORRIDOR_CI_WORKFLOW, ECOCORRIDOR_RELEASE_WORKFLOW}
    missing = sorted(required_workflows - set(workflows))
    if missing:
        findings.append("release signing security topology is incomplete: " + ", ".join(missing))
        return findings

    for workflow_name, text in sorted(workflows.items()):
        for forbidden in (
            "cryptography",
            "RESOURCE_SIGNING_KEY_PEM",
            "ECOCORRIDOR_RESOURCE_SIGNING_KEY_PEM",
            "ci-resource-key.pem",
        ):
            if forbidden in text:
                findings.append(
                    f"{workflow_name}: release signing contains forbidden legacy token {forbidden}"
                )

    ci = workflows[ECOCORRIDOR_CI_WORKFLOW]
    for token in (
        "from nacl.signing import SigningKey",
        'key.encode().hex() + "\\n"',
        "ci-resource-key.seed.hex",
        "umask 077",
    ):
        if token not in ci:
            findings.append(f"EcoCorridor CI release signing is missing {token}")
    ci_signing_steps = _named_steps(ci, "Sign resource manifest with an ephemeral CI key")
    if len(ci_signing_steps) != 1:
        findings.append(
            "EcoCorridor CI must contain exactly one ephemeral resource-signing step; "
            f"observed {len(ci_signing_steps)}"
        )
    else:
        ci_signing_step = ci_signing_steps[0]
        for token in (
            'seed_path="${RUNNER_TEMP}/ci-resource-key.seed.hex"',
            "cleanup_ci_resource_seed()",
            "trap cleanup_ci_resource_seed EXIT HUP INT TERM",
            '[[ -e "${seed_path}" || -L "${seed_path}" ]]',
            'rm -f -- "${seed_path}"',
            "Path(sys.argv[1]).write_text",
            '--signing-key "${seed_path}"',
            "cleanup_ci_resource_seed\n",
            "trap - EXIT HUP INT TERM",
        ):
            if token not in ci_signing_step:
                findings.append(
                    "EcoCorridor CI ephemeral resource-signing step is missing " + token
                )
    if ci.count("ci-resource-key.seed.hex") != 1:
        findings.append("ephemeral CI signing seed must be scoped to its canonical exact path")

    release = workflows[ECOCORRIDOR_RELEASE_WORKFLOW]
    for token in (
        "RESOURCE_SIGNING_SEED_HEX: ${{ secrets.ECOCORRIDOR_RESOURCE_SIGNING_SEED_HEX }}",
        '[[ ! "${RESOURCE_SIGNING_SEED_HEX:-}" =~ ^[0-9a-f]{64}$ ]]',
        "ecocorridor-resource-signing-key.seed.hex",
        "unset RESOURCE_SIGNING_SEED_HEX",
    ):
        if token not in release:
            findings.append(f"protected release signing is missing {token}")
    if release.count("secrets.ECOCORRIDOR_RESOURCE_SIGNING_SEED_HEX") != 1:
        findings.append("protected release signing seed must be scoped to exactly one step")
    if re.search(r"(?:echo|printf)[^\n]*\$\{RESOURCE_SIGNING_SEED_HEX", release):
        allowed_materialization = re.compile(
            r"printf '%s\\n' \"\$\{RESOURCE_SIGNING_SEED_HEX\}\"\s*\\\n"
            r"\s*> \"\$\{seed_path\}\""
        )
        if not allowed_materialization.search(release):
            findings.append("protected release signing seed may only enter its canonical seed file")

    def require_step(step_name: str, required_tokens: tuple[str, ...]) -> str:
        steps = _named_steps(release, step_name)
        if len(steps) != 1:
            findings.append(
                f"protected release must contain exactly one {step_name!r} step; observed {len(steps)}"
            )
            return ""
        step = steps[0]
        for token in required_tokens:
            if token not in step:
                findings.append(f"protected release step {step_name!r} is missing {token}")
        return step

    require_step(
        "Sign the exact native-resource inventory",
        (
            'seed_path="${RUNNER_TEMP}/ecocorridor-resource-signing-key.seed.hex"',
            "cleanup_resource_seed()",
            "trap cleanup_resource_seed EXIT HUP INT TERM",
            '> "${seed_path}"',
            '--signing-key "${seed_path}"',
            "cleanup_resource_seed\n",
            "trap - EXIT HUP INT TERM",
        ),
    )
    capture_apple_default_keychain = require_step(
        "Capture Apple default keychain",
        (
            "id: capture_apple_default_keychain",
            "security default-keychain -d user",
            'original_default_keychain="${BASH_REMATCH[1]}"',
            'user_keychain_root="$(python3 -c',
            ('"${original_default_keychain}" != "${user_keychain_root}/${original_keychain_name}"'),
            "^[A-Za-z0-9._-]+\\.keychain(-db)?$",
            '! -f "${original_default_keychain}"',
            '-L "${original_default_keychain}"',
            'printf \'path=%s\\n\' "${original_default_keychain}" >> "${GITHUB_OUTPUT}"',
        ),
    )
    if "${GITHUB_ENV}" in capture_apple_default_keychain:
        findings.append(
            "protected release must carry the original default keychain as a scoped step output"
        )
    if release.count("steps.capture_apple_default_keychain.outputs.path") != 1:
        findings.append(
            "protected release must consume the captured original default keychain path exactly once"
        )
    if (
        len(
            re.findall(
                r"^\s+ECOCORRIDOR_ORIGINAL_DEFAULT_KEYCHAIN:\s+",
                release,
                re.MULTILINE,
            )
        )
        != 1
    ):
        findings.append(
            "protected release must scope the original default keychain path to cleanup only"
        )
    require_step(
        "Import Apple signing identity",
        (
            'p12_path="${RUNNER_TEMP}/ecocorridor-signing.p12"',
            'keychain_path="${RUNNER_TEMP}/ecocorridor-build.keychain-db"',
            "cleanup_apple_certificate_file()",
            "trap cleanup_apple_certificate_file EXIT HUP INT TERM",
            "umask 077",
            'security default-keychain -d user -s "${keychain_path}"',
            'security import "${p12_path}"',
            '-k "${keychain_path}"',
            "cleanup_apple_certificate_file\n",
            "trap - EXIT HUP INT TERM",
        ),
    )
    require_step(
        "Import Windows signing identity",
        (
            '$certificatePath = Join-Path $env:RUNNER_TEMP "ecocorridor-signing.pfx"',
            "try {",
            "catch {",
            "finally {",
            "^[0-9A-F]{40}$",
            "Windows signing PFX must contain exactly one imported certificate.",
            'Remove-Item -LiteralPath "Cert:\\CurrentUser\\My\\$importedThumbprint"',
            "Remove-Item -LiteralPath $certificatePath -Force -ErrorAction SilentlyContinue",
            "Remove-Item Env:WINDOWS_CERTIFICATE_BASE64 -ErrorAction SilentlyContinue",
            "Remove-Item Env:WINDOWS_CERTIFICATE_PASSWORD -ErrorAction SilentlyContinue",
        ),
    )
    require_step(
        "Write Apple notarization key",
        (
            "umask 077",
            '> "${RUNNER_TEMP}/AuthKey.p8"',
            "unset APPLE_API_PRIVATE_KEY",
        ),
    )
    apple_cleanup = require_step(
        "Remove Apple signing material",
        (
            "if: always() && startsWith(matrix.target, 'macos')",
            (
                "ECOCORRIDOR_ORIGINAL_DEFAULT_KEYCHAIN: "
                "${{ steps.capture_apple_default_keychain.outputs.path }}"
            ),
            'auth_key_path="${RUNNER_TEMP}/AuthKey.p8"',
            'keychain_path="${RUNNER_TEMP}/ecocorridor-build.keychain-db"',
            'rm -f -- "${auth_key_path}"',
            'original_default_keychain="${ECOCORRIDOR_ORIGINAL_DEFAULT_KEYCHAIN:-}"',
            ('"${original_default_keychain}" != "${user_keychain_root}/${original_keychain_name}"'),
            "^[A-Za-z0-9._-]+\\.keychain(-db)?$",
            '! -f "${original_default_keychain}"',
            '-L "${original_default_keychain}"',
            'security default-keychain -d user -s "${original_default_keychain}"',
            '"${BASH_REMATCH[1]}" != "${original_default_keychain}"',
            'security delete-keychain "${keychain_path}"',
            'test ! -e "${auth_key_path}"',
            'test ! -e "${keychain_path}"',
        ),
    )
    restore_position = apple_cleanup.find(
        'security default-keychain -d user -s "${original_default_keychain}"'
    )
    delete_position = apple_cleanup.find('security delete-keychain "${keychain_path}"')
    if restore_position < 0 or delete_position < 0 or restore_position >= delete_position:
        findings.append(
            "protected release must restore the exact original default keychain before deleting "
            "the temporary Apple keychain"
        )
    require_step(
        "Remove Windows signing material",
        (
            "if: always() && matrix.target == 'windows-x64'",
            "^[0-9A-F]{40}$",
            '$certificatePath = "Cert:\\CurrentUser\\My\\$thumbprint"',
            "Remove-Item -LiteralPath $certificatePath -Force",
            '$pfxPath = Join-Path $env:RUNNER_TEMP "ecocorridor-signing.pfx"',
            "Remove-Item -LiteralPath $pfxPath -Force -ErrorAction SilentlyContinue",
        ),
    )

    ordered_steps = (
        "Capture Apple default keychain",
        "Import Apple signing identity",
        "Build signed and notarized macOS installer",
        "Build signed Windows installers",
        "Remove Apple signing material",
        "Remove Windows signing material",
        "Verify Apple signatures and notarization",
        "Verify Windows Authenticode signatures",
    )
    if all(f"- name: {name}" in release for name in ordered_steps):
        positions = [release.index(f"- name: {name}") for name in ordered_steps]
        if positions != sorted(positions):
            findings.append(
                "protected release signing cleanup must follow installer builds and precede "
                "signature verification"
            )
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
        REVIEW_BRIEF_EVALUATION_WORKFLOW,
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
        findings.extend(
            evaluate_review_brief_evaluation_topology(by_name[REVIEW_BRIEF_EVALUATION_WORKFLOW])
        )
    eco_workflows = {
        name: text for name, text in by_name.items() if name.startswith("ecocorridor-")
    }
    findings.extend(evaluate_ecocorridor_geospatial_dependency_topology(eco_workflows))
    findings.extend(evaluate_circuitscape_manifest_verification_topology(eco_workflows))
    findings.extend(evaluate_packagecompiler_heartbeat_topology(eco_workflows))
    findings.extend(evaluate_release_signing_security_topology(eco_workflows))
    if findings:
        for finding in findings:
            print(finding)
        return 1
    print(f"Workflow policy passed for {len(paths)} workflow(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
