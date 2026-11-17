#!/usr/bin/env python3
"""Publish a frozen EcoCorridor usefulness protocol and append its Report section.

This command publishes protocol and current evidence status, not study outcomes.
It never creates a W&B Report and never logs result media.  Online publication
is allowed only from the exact clean source commit named by the frozen plan,
after explicit authorization to upload the CC0 synthetic protocol payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from ecocorridor_core.contracts import ContractModel, canonical_sha256
from ecocorridor_core.usefulness_reporting import UsefulnessPublicationPlan
from pydantic import Field, model_validator

MAX_PLAN_BYTES = 16 * 1024 * 1024
REPORT_READBACK_TIMEOUT_SECONDS = 8.0
REPORT_READBACK_INTERVAL_SECONDS = 0.4
SECTION_ID = "usefulness-evidence-program/v1"
SECTION_START_PREFIX = f"<!-- ecocorridor-owned-section:{SECTION_ID}:start"
SECTION_END = f"<!-- ecocorridor-owned-section:{SECTION_ID}:end -->"
APPROVED_PROTOCOL_PAYLOADS = frozenset(
    {
        "claim-ledger.json",
        "cost-basis.json",
        "historical-review-brief.json",
        "historical-github-actions-provenance.json",
        "historical-provider-trace-receipts.json",
        "historical-wandb-readback-receipt.json",
        "landscapes.json",
        "program-status.json",
        "protocol.json",
        "safety-attack-recipes.json",
    }
)


class UsefulnessCloudReceipt(ContractModel):
    """Exact immutable W&B run and artifact identity before Report mutation."""

    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=128)
    run_url: str
    artifact_ref: str = Field(min_length=1, max_length=512)
    artifact_url: str
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_readback_verified: Literal[True] = True
    artifact_readback_verified: Literal[True] = True


class UsefulnessPublicationReceipt(ContractModel):
    """Success receipt returned only after run, artifact, and Report readback."""

    schema_version: Literal["ecocorridor-usefulness-publication-receipt/v1"] = (
        "ecocorridor-usefulness-publication-receipt/v1"
    )
    status: Literal["COMPLETE"] = "COMPLETE"
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=128)
    run_url: str
    artifact_ref: str = Field(min_length=1, max_length=512)
    artifact_url: str
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_url: str
    run_readback_verified: Literal[True] = True
    artifact_readback_verified: Literal[True] = True
    report_readback_verified: Literal[True] = True
    observed_effectiveness_result_count: Literal[0] = 0
    new_result_media_logged: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        expected = canonical_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if self.receipt_sha256 != expected:
            raise ValueError("usefulness publication receipt digest differs")
        return self

    @classmethod
    def create(
        cls,
        cloud: UsefulnessCloudReceipt,
        *,
        report_url: str,
    ) -> UsefulnessPublicationReceipt:
        payload = {
            "schema_version": "ecocorridor-usefulness-publication-receipt/v1",
            "status": "COMPLETE",
            **cloud.model_dump(mode="json"),
            "report_url": report_url,
            "report_readback_verified": True,
            "observed_effectiveness_result_count": 0,
            "new_result_media_logged": False,
        }
        return cls(**payload, receipt_sha256=canonical_sha256(payload))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _safe_https_url(value: object, *, label: str) -> str:
    rendered = str(value or "")
    parsed = urlsplit(rendered)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    return rendered


def _https_origin(value: object, *, label: str) -> tuple[str, str, int]:
    rendered = _safe_https_url(value, label=label)
    parsed = urlsplit(rendered)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    return parsed.scheme, str(parsed.hostname).casefold(), port


def _require_web_origin(value: object, *, web_base_url: str, label: str) -> str:
    rendered = _safe_https_url(value, label=label)
    if _https_origin(rendered, label=label) != _https_origin(
        web_base_url, label="W&B web base URL"
    ):
        raise PermissionError(f"{label} belongs to another W&B deployment")
    return rendered


def _git_source(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("repository HEAD is not a full Git commit")
    return commit, not bool(status)


def load_publication_plan(path: str | Path) -> UsefulnessPublicationPlan:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("usefulness publication plan is missing, symlinked, or oversized")
    plan = UsefulnessPublicationPlan.model_validate_json(source.read_bytes())
    if plan.media_status != "HISTORICAL_MEDIA_ONLY":
        raise ValueError("protocol publication cannot attach result media")
    completed_result_stages = {
        name
        for name, status in plan.program_manifest.get("stages", {}).items()
        if name != "protocol" and status == "COMPLETE"
    }
    if completed_result_stages:
        raise ValueError("protocol publisher cannot publish a completed study as planned evidence")
    _validate_tables(plan)
    _validate_artifact_payloads(plan)
    return plan


def require_online_source(
    plan: UsefulnessPublicationPlan,
    *,
    repository_root: Path,
) -> None:
    commit, clean = _git_source(repository_root)
    if commit != plan.source_commit:
        raise PermissionError("publication plan source commit differs from checked-out HEAD")
    if not plan.source_tree_clean or not clean:
        raise PermissionError("online usefulness publication requires an exact clean source tree")


def _validate_tables(plan: UsefulnessPublicationPlan) -> None:
    if not plan.tables:
        raise ValueError("usefulness plan must contain typed W&B Tables")
    for name, table in plan.tables.items():
        if not re.fullmatch(r"usefulness_[a-z0-9_]+", name):
            raise ValueError("usefulness Table name is outside the approved namespace")
        columns = table.get("columns")
        rows = table.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or len(columns) != len(set(columns))
            or any(not isinstance(column, str) or not column for column in columns)
            or not isinstance(rows, list)
            or table.get("row_count") != len(rows)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
        ):
            raise ValueError(f"invalid typed W&B Table projection: {name}")


def _validate_artifact_payloads(plan: UsefulnessPublicationPlan) -> None:
    if set(plan.artifact_payloads) != APPROVED_PROTOCOL_PAYLOADS:
        raise ValueError("usefulness artifact payload set differs from the approved protocol")
    total_bytes = 0
    for name, payload in plan.artifact_payloads.items():
        if (
            name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            raise ValueError("usefulness artifact payload name is unsafe")
        total_bytes += len(_json_bytes(payload))
    if total_bytes > MAX_PLAN_BYTES:
        raise ValueError("usefulness artifact payloads exceed the publication limit")


def _expected_run_url(
    *,
    web_base_url: str,
    entity: str,
    project: str,
    run_id: str,
) -> str:
    for value, label in ((entity, "entity"), (project, "project"), (run_id, "run ID")):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError(f"W&B {label} contains unsupported characters")
    return f"{web_base_url.rstrip('/')}/{entity}/{project}/runs/{run_id}"


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    return (
        getattr(error, "status_code", None) == 404 or getattr(response, "status_code", None) == 404
    )


@contextmanager
def _wandb_base_environment(api_base_url: str):
    previous = os.environ.get("WANDB_BASE_URL")
    os.environ["WANDB_BASE_URL"] = api_base_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WANDB_BASE_URL", None)
        else:
            os.environ["WANDB_BASE_URL"] = previous


def _report_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _report_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_report_value(nested) for nested in value]
    spec = getattr(value, "_spec", None)
    if isinstance(spec, Mapping):
        return _report_value(spec)
    public = getattr(value, "__dict__", None)
    if isinstance(public, Mapping):
        return {
            str(key): _report_value(nested)
            for key, nested in public.items()
            if not str(key).startswith("_")
        }
    raise TypeError("W&B Report block contains unsupported state")


def _block_representation(block: Any) -> dict[str, Any]:
    spec = getattr(block, "_spec", None)
    value = _report_value(spec) if isinstance(spec, Mapping) else _report_value(block)
    return {"block_type": type(block).__name__, "value": value}


def _body_sha256(body: Sequence[Any]) -> str:
    return canonical_sha256([_block_representation(block) for block in body])


def _block_text(block: Any) -> str | None:
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _owned_sections(blocks: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    sections: list[tuple[str, list[Any]]] = []
    marker: str | None = None
    body: list[Any] = []
    for block in blocks:
        text = _block_text(block)
        if text is not None and text.startswith(SECTION_START_PREFIX):
            if marker is not None:
                raise ValueError("W&B Report contains nested usefulness section markers")
            marker = text
            body = []
            continue
        if text == SECTION_END:
            if marker is None:
                raise ValueError("W&B Report contains an unmatched usefulness end marker")
            sections.append((marker, body))
            marker = None
            body = []
            continue
        if marker is not None:
            body.append(block)
    if marker is not None:
        raise ValueError("W&B Report contains an unterminated usefulness section")
    return sections


def append_owned_section(existing: Sequence[Any], owned: Sequence[Any]) -> list[Any]:
    owned_sections = _owned_sections(owned)
    if len(owned_sections) != 1:
        raise ValueError("publisher must supply exactly one bounded usefulness section")
    marker = owned_sections[0][0]
    if any(existing_marker == marker for existing_marker, _ in _owned_sections(existing)):
        return list(existing)
    return [*existing, *owned]


def _report_marker(plan: UsefulnessPublicationPlan, *, body_sha256: str) -> str:
    return (
        f"{SECTION_START_PREFIX} plan={plan.plan_sha256} "
        f"artifacts={plan.artifact_manifest_sha256} body={body_sha256} -->"
    )


def build_report_section_blocks(
    reports: Any,
    plan: UsefulnessPublicationPlan,
    cloud: UsefulnessCloudReceipt,
) -> list[Any]:
    historical = plan.historical_review_brief
    if historical.evidence_status == "VERIFIED_HISTORICAL":
        reduction = 100.0 * float(historical.context_reduction_fraction or 0.0)
        historical_copy = (
            f"The prior campaign remains historical evidence: **{historical.record_count} records**, "
            f"**{historical.provider_conversation_count} provider-backed conversations**, and "
            f"**{historical.pre_provider_block_count} pre-provider blocks**. Its measured input-token "
            f"reduction was **{reduction:.2f}%**, so the recorded decision remains "
            f"**{historical.decision}**. It demonstrated trace integrity and fail-closed execution; "
            "it did not prove adaptive-agent or practitioner usefulness."
        )
    elif historical.evidence_status == "UNVERIFIED_HISTORICAL":
        historical_copy = (
            "Historical campaign bytes were supplied, but the exact GitHub producer "
            "provenance and prior W&B run/artifact readback receipt did not jointly "
            "verify. They are retained only as **UNVERIFIED_HISTORICAL** input. No "
            "trace-integrity, fail-closed execution, efficiency, or usefulness claim "
            "is made from those bytes."
        )
    else:
        historical_copy = (
            "No historical campaign bytes were supplied to this frozen publication plan. "
            "Any earlier Report section remains unchanged and is not reinterpreted here."
        )
    stages = plan.program_manifest.get("stages", {})
    stage_lines = "\n".join(f"- `{name}` — **{status}**" for name, status in sorted(stages.items()))
    claim_lines = "\n".join(
        f"- **{claim.level.value}** — `{claim.status.value}`: {claim.bounded_claim} "
        f"({claim.reason})"
        for claim in plan.claim_ledger.claims
    )
    landscape_lines = "\n".join(
        f"- **{item['title']}** — {item['direct_job_count']} planned direct jobs; "
        f"evidence state `{plan.landscape_manifest['evidence_state']}`; definition "
        f"`{item['definition_sha256']}`"
        for item in plan.landscape_manifest["landscapes"]
    )
    body = [
        reports.H1("EcoCorridor usefulness evidence program"),
        reports.CalloutBlock(text=plan.report_copy["boundary"]),
        reports.H2("What the historical campaign established"),
        reports.MarkdownBlock(text=historical_copy),
        reports.H2("Independent-landscape truth program"),
        reports.MarkdownBlock(
            text=(
                f"Five CC0 synthetic landscape definitions are frozen under protocol "
                f"`{plan.protocol.protocol_sha256}`. **These are study inputs, not observed "
                f"usefulness results.**\n\n{landscape_lines}"
            )
        ),
        reports.H2("Agent complexity, safety, and practitioner evidence"),
        reports.MarkdownBlock(
            text=(
                f"The fair agent matrix contains **{plan.protocol.agent_complexity_task_count} "
                "planned task instances** before model-family repeats. Each of the ten safety "
                f"classes requires **{plan.protocol.safety_variants_per_class} planned variants**. "
                "Practitioner crossover, reviewer handoff, and partner shadow evidence remain "
                "external gates. None of these planned rows is presented as an observation."
            )
        ),
        reports.H2("Frozen stage status"),
        reports.MarkdownBlock(text=stage_lines),
        reports.H2("Claim ladder"),
        reports.MarkdownBlock(text=claim_lines),
        reports.H2("Audit trail and media boundary"),
        reports.MarkdownBlock(
            text=(
                f"[Open the exact W&B run]({cloud.run_url}) · immutable protocol artifact "
                f"`{cloud.artifact_ref}` · artifact manifest "
                f"`{cloud.artifact_manifest_sha256}`. The run logs typed Tables for the protocol, "
                "landscapes, method arms, task matrix, safety classes, and claim ledger. "
                "**No new effectiveness animation or result media was rendered or logged**, "
                "because the preregistered campaigns are not complete. "
                f"{plan.report_copy['external']} {plan.report_copy['forbidden']}"
            )
        ),
    ]
    marker = _report_marker(plan, body_sha256=_body_sha256(body))
    return [
        reports.MarkdownBlock(text=marker),
        *body,
        reports.MarkdownBlock(text=SECTION_END),
    ]


class WandbUsefulnessPublisher:
    """Replaceable SDK boundary for exact protocol, artifact, run, and Report I/O."""

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        api_base_url: str,
        web_base_url: str,
        authorize_synthetic_public_upload: bool,
        wandb_module: Any | None = None,
        reports_module: Any | None = None,
    ) -> None:
        if not authorize_synthetic_public_upload:
            raise PermissionError("publishing requires explicit CC0 synthetic upload authorization")
        if wandb_module is None:
            import wandb as wandb_module  # type: ignore[import-not-found,no-redef]
        if reports_module is None:
            import wandb_workspaces.reports.v2 as reports_module  # type: ignore[import-not-found,no-redef]
        self.entity = entity
        self.project = project
        self.api_base_url = _safe_https_url(api_base_url, label="W&B API base URL").rstrip("/")
        self.web_base_url = _safe_https_url(web_base_url, label="W&B web base URL").rstrip("/")
        self.run_path = f"{entity}/{project}"
        _expected_run_url(
            web_base_url=self.web_base_url,
            entity=entity,
            project=project,
            run_id="validation",
        )
        self.wandb = wandb_module
        self.reports = reports_module

    def _api(self) -> Any:
        return self.wandb.Api(overrides={"base_url": self.api_base_url})

    def _expected_artifact_metadata(self, plan: UsefulnessPublicationPlan) -> dict[str, Any]:
        return {
            "schema_version": plan.schema_version,
            "protocol_sha256": plan.protocol.protocol_sha256,
            "plan_sha256": plan.plan_sha256,
            "artifact_manifest_sha256": plan.artifact_manifest_sha256,
            "source_commit": plan.source_commit,
            "data_classification": "CC0_SYNTHETIC",
            "observed_effectiveness_result_count": 0,
            "new_result_media_logged": False,
        }

    def _verify_artifact(
        self,
        plan: UsefulnessPublicationPlan,
        *,
        artifact_ref: str,
    ) -> tuple[bool, str]:
        artifact = self._api().artifact(artifact_ref)
        metadata = dict(getattr(artifact, "metadata", {}) or {})
        expected_metadata = self._expected_artifact_metadata(plan)
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            return False, ""
        artifact_url = _require_web_origin(
            getattr(artifact, "url", ""),
            web_base_url=self.web_base_url,
            label="W&B artifact URL",
        )
        with tempfile.TemporaryDirectory(prefix="ecocorridor-usefulness-readback-") as temporary:
            downloaded = Path(artifact.download(root=temporary)).resolve()
            observed: set[str] = set()
            for path in downloaded.rglob("*"):
                if path.is_dir():
                    continue
                relative = path.relative_to(downloaded).as_posix()
                payload = plan.artifact_payloads.get(relative)
                if (
                    path.is_symlink()
                    or payload is None
                    or path.read_bytes() != _json_bytes(payload)
                ):
                    return False, ""
                observed.add(relative)
            if observed != set(plan.artifact_payloads):
                return False, ""
        return True, artifact_url

    def _readback_run(
        self,
        plan: UsefulnessPublicationPlan,
        *,
        artifact_ref: str,
    ) -> tuple[Any, str]:
        run_id = str(plan.wandb_projection["run_id"])
        expected_url = _expected_run_url(
            web_base_url=self.web_base_url,
            entity=self.entity,
            project=self.project,
            run_id=run_id,
        )
        deadline = time.monotonic() + REPORT_READBACK_TIMEOUT_SECONDS
        while True:
            public_run: Any | None = None
            try:
                public_run = self._api().run(f"{self.run_path}/{run_id}")
                config = dict(getattr(public_run, "config", {}) or {})
                summary = dict(getattr(public_run, "summary", {}) or {})
                valid = (
                    str(getattr(public_run, "id", "")) == run_id
                    and _require_web_origin(
                        getattr(public_run, "url", ""),
                        web_base_url=self.web_base_url,
                        label="W&B run URL",
                    )
                    == expected_url
                    and str(getattr(public_run, "state", "")).casefold() == "finished"
                    and config.get("run_kind") == "usefulness_report"
                    and config.get("plan_sha256") == plan.plan_sha256
                    and config.get("protocol_sha256") == plan.protocol.protocol_sha256
                    and config.get("source_commit") == plan.source_commit
                    and summary.get("usefulness/status") == "COMPLETE"
                    and summary.get("usefulness/plan_sha256") == plan.plan_sha256
                    and summary.get("usefulness/artifact_ref") == artifact_ref
                    and summary.get("usefulness/artifact_manifest_sha256")
                    == plan.artifact_manifest_sha256
                    and summary.get("usefulness/observed_effectiveness_result_count") == 0
                    and summary.get("usefulness/new_result_media_logged") == 0
                )
                if valid:
                    return public_run, expected_url
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry does not expose remote text
                public_run = None
            if time.monotonic() >= deadline:
                raise RuntimeError("W&B usefulness run failed exact readback")
            time.sleep(REPORT_READBACK_INTERVAL_SECONDS)

    def _reuse_completed(self, plan: UsefulnessPublicationPlan) -> UsefulnessCloudReceipt | None:
        run_id = str(plan.wandb_projection["run_id"])
        try:
            run = self._api().run(f"{self.run_path}/{run_id}")
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise RuntimeError("unable to determine deterministic W&B run identity") from exc
        config = dict(getattr(run, "config", {}) or {})
        summary = dict(getattr(run, "summary", {}) or {})
        if (
            config.get("plan_sha256") != plan.plan_sha256
            or summary.get("usefulness/status") != "COMPLETE"
        ):
            raise RuntimeError("deterministic W&B run ID is occupied by nonmatching state")
        artifact_ref = str(summary.get("usefulness/artifact_ref") or "")
        if not re.fullmatch(
            rf"{re.escape(self.entity)}/{re.escape(self.project)}/[^:\s]+:v[0-9]+",
            artifact_ref,
        ):
            raise RuntimeError("completed W&B run lacks an immutable usefulness artifact")
        artifact_verified, artifact_url = self._verify_artifact(plan, artifact_ref=artifact_ref)
        if not artifact_verified:
            raise RuntimeError("completed usefulness artifact failed exact readback")
        _, run_url = self._readback_run(plan, artifact_ref=artifact_ref)
        return UsefulnessCloudReceipt(
            plan_sha256=plan.plan_sha256,
            run_id=run_id,
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=plan.artifact_manifest_sha256,
            run_readback_verified=True,
            artifact_readback_verified=True,
        )

    def publish_plan(self, plan: UsefulnessPublicationPlan) -> UsefulnessCloudReceipt:
        if not plan.source_tree_clean:
            raise PermissionError("plan was not frozen from a clean source tree")
        if plan.media_status != "HISTORICAL_MEDIA_ONLY":
            raise ValueError("planned usefulness publication cannot log result media")
        _validate_tables(plan)
        _validate_artifact_payloads(plan)
        reused = self._reuse_completed(plan)
        if reused is not None:
            return reused
        projection = plan.wandb_projection
        run_id = str(projection["run_id"])
        expected_url = _expected_run_url(
            web_base_url=self.web_base_url,
            entity=self.entity,
            project=self.project,
            run_id=run_id,
        )
        with tempfile.TemporaryDirectory(prefix="ecocorridor-usefulness-wandb-") as temporary:
            settings = self.wandb.Settings(
                base_url=self.api_base_url,
                console="off",
                disable_code=True,
                disable_git=True,
                x_disable_machine_info=True,
                x_disable_meta=True,
                x_save_requirements=False,
                silent=True,
            )
            run = self.wandb.init(
                entity=self.entity,
                project=self.project,
                id=run_id,
                name=str(projection["run_name"]),
                job_type=str(projection["job_type"]),
                tags=["usefulness_protocol", "cc0_synthetic", "planned_not_observed"],
                config={
                    "run_kind": "usefulness_report",
                    "plan_sha256": plan.plan_sha256,
                    "protocol_sha256": plan.protocol.protocol_sha256,
                    "artifact_manifest_sha256": plan.artifact_manifest_sha256,
                    "source_repository": plan.source_repository,
                    "source_commit": plan.source_commit,
                    "data_classification": "CC0_SYNTHETIC",
                    "evidence_state": plan.landscape_manifest["evidence_state"],
                    "observed_effectiveness_result_count": 0,
                    "new_result_media_logged": False,
                },
                resume="never",
                save_code=False,
                dir=temporary,
                settings=settings,
            )
            try:
                if (
                    _require_web_origin(
                        getattr(run, "url", ""),
                        web_base_url=self.web_base_url,
                        label="W&B run URL",
                    )
                    != expected_url
                ):
                    raise RuntimeError("W&B created a different usefulness run identity")
                run.log(
                    {
                        name: self.wandb.Table(columns=table["columns"], data=table["rows"])
                        for name, table in sorted(plan.tables.items())
                    }
                )
                artifact = self.wandb.Artifact(
                    str(projection["artifact_name"]),
                    type=str(projection["artifact_type"]),
                    metadata=self._expected_artifact_metadata(plan),
                )
                for name, payload in sorted(plan.artifact_payloads.items()):
                    with artifact.new_file(name, mode="wb") as stream:
                        stream.write(_json_bytes(payload))
                logged = run.log_artifact(artifact).wait()
                artifact_ref = str(getattr(logged, "qualified_name", "") or "")
                if not re.fullmatch(
                    rf"{re.escape(self.entity)}/{re.escape(self.project)}/[^:\s]+:v[0-9]+",
                    artifact_ref,
                ):
                    raise RuntimeError("W&B did not return an immutable usefulness artifact")
                run.summary["usefulness/status"] = "COMPLETE"
                run.summary["usefulness/plan_sha256"] = plan.plan_sha256
                run.summary["usefulness/protocol_sha256"] = plan.protocol.protocol_sha256
                run.summary["usefulness/artifact_ref"] = artifact_ref
                run.summary["usefulness/artifact_manifest_sha256"] = plan.artifact_manifest_sha256
                run.summary["usefulness/observed_effectiveness_result_count"] = 0
                run.summary["usefulness/new_result_media_logged"] = 0
                run.finish(exit_code=0)
            except Exception:
                run.finish(exit_code=1)
                raise
        artifact_verified, artifact_url = self._verify_artifact(plan, artifact_ref=artifact_ref)
        if not artifact_verified:
            raise RuntimeError("W&B usefulness artifact failed exact byte readback")
        _, run_url = self._readback_run(plan, artifact_ref=artifact_ref)
        return UsefulnessCloudReceipt(
            plan_sha256=plan.plan_sha256,
            run_id=run_id,
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=plan.artifact_manifest_sha256,
            run_readback_verified=True,
            artifact_readback_verified=True,
        )

    def append_report_section(
        self,
        *,
        report_url: str,
        plan: UsefulnessPublicationPlan,
        cloud: UsefulnessCloudReceipt,
    ) -> str:
        target = _require_web_origin(
            report_url,
            web_base_url=self.web_base_url,
            label="existing W&B Report URL",
        )
        with _wandb_base_environment(self.api_base_url):
            report = self.reports.Report.from_url(target)
            if (
                str(getattr(report, "entity", "")) != self.entity
                or str(getattr(report, "project", "")) != self.project
            ):
                raise PermissionError("existing W&B Report belongs to another destination")
            existing = list(report.blocks)
            existing_digest = canonical_sha256([_block_representation(block) for block in existing])
            owned = build_report_section_blocks(self.reports, plan, cloud)
            expected_marker, expected_body = _owned_sections(owned)[0]
            expected_body_sha256 = _body_sha256(expected_body)
            appended = append_owned_section(existing, owned)
            if len(appended) != len(existing):
                report.blocks = appended
                report.save()
            updated_url = _require_web_origin(
                getattr(report, "url", ""),
                web_base_url=self.web_base_url,
                label="updated W&B Report URL",
            )
            deadline = time.monotonic() + REPORT_READBACK_TIMEOUT_SECONDS
            while True:
                verified = False
                try:
                    readback = self.reports.Report.from_url(updated_url)
                    if (
                        str(getattr(readback, "entity", "")) != self.entity
                        or str(getattr(readback, "project", "")) != self.project
                    ):
                        raise PermissionError("read-back W&B Report belongs to another destination")
                    readback_blocks = list(readback.blocks)
                    sections = _owned_sections(readback_blocks)
                    matches = [body for marker, body in sections if marker == expected_marker]
                    preserved = (
                        canonical_sha256(
                            [
                                _block_representation(block)
                                for block in readback_blocks[: len(existing)]
                            ]
                        )
                        == existing_digest
                    )
                    verified = bool(
                        preserved
                        and len(matches) == 1
                        and _body_sha256(matches[0]) == expected_body_sha256
                    )
                except PermissionError:
                    raise
                except Exception:  # noqa: BLE001 - finite retry hides remote content
                    verified = False
                if verified:
                    return updated_url
                if time.monotonic() >= deadline:
                    raise RuntimeError("W&B Report failed exact append-only section readback")
                time.sleep(REPORT_READBACK_INTERVAL_SECONDS)


def publish_usefulness_report(
    *,
    plan: UsefulnessPublicationPlan,
    report_url: str,
    publisher: WandbUsefulnessPublisher,
) -> UsefulnessPublicationReceipt:
    cloud = publisher.publish_plan(plan)
    updated_report = publisher.append_report_section(
        report_url=report_url,
        plan=plan,
        cloud=cloud,
    )
    return UsefulnessPublicationReceipt.create(cloud, report_url=updated_report)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--entity")
    parser.add_argument("--project")
    parser.add_argument("--wandb-api-base-url")
    parser.add_argument("--wandb-web-base-url")
    parser.add_argument("--report-url")
    parser.add_argument("--authorize-synthetic-public-upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    plan = load_publication_plan(arguments.plan)
    commit, clean = _git_source(root)
    if commit != plan.source_commit:
        raise SystemExit("usefulness plan source commit differs from checked-out HEAD")
    if arguments.dry_run:
        result = {
            "status": "DRY_RUN",
            "plan_sha256": plan.plan_sha256,
            "protocol_sha256": plan.protocol.protocol_sha256,
            "source_commit": commit,
            "current_source_tree_clean": clean,
            "plan_source_tree_clean": plan.source_tree_clean,
            "typed_table_count": len(plan.tables),
            "observed_effectiveness_result_count": 0,
            "new_result_media_logged": False,
        }
        _atomic_json(arguments.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    require_online_source(plan, repository_root=root)
    if not all(
        (
            arguments.entity,
            arguments.project,
            arguments.wandb_api_base_url,
            arguments.wandb_web_base_url,
            arguments.report_url,
        )
    ):
        raise SystemExit(
            "online publication requires --entity, --project, --wandb-api-base-url, "
            "--wandb-web-base-url, and --report-url"
        )
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("online publication requires WANDB_API_KEY in process memory")
    publisher = WandbUsefulnessPublisher(
        entity=arguments.entity,
        project=arguments.project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        authorize_synthetic_public_upload=arguments.authorize_synthetic_public_upload,
    )
    receipt = publish_usefulness_report(
        plan=plan,
        report_url=arguments.report_url,
        publisher=publisher,
    )
    _atomic_json(arguments.output, receipt.model_dump(mode="json"))
    observed = UsefulnessPublicationReceipt.model_validate_json(arguments.output.read_bytes())
    if observed != receipt:
        raise RuntimeError("usefulness publication receipt failed exact local readback")
    print(
        json.dumps(
            {
                "status": receipt.status,
                "run_url": receipt.run_url,
                "report_url": receipt.report_url,
                "observed_effectiveness_result_count": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
