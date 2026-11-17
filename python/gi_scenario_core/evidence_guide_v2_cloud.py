"""W&B and Weave publication boundary for the Evidence Guide v2 campaign.

This module is imported only by the explicitly approved real-campaign CLI.
Pull-request tests use :class:`LocalCampaignPublisher` and never import either
cloud SDK.  Hidden attempt rows are accepted only by a separate lockbox
publisher; the main evaluation project receives their aggregate receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .evidence_guide_v2_campaign import (
    CAMPAIGN_VERSION,
    TARGET_PROJECT,
    AttemptObserver,
    AttemptOutcome,
    AttemptSpec,
    Corpus,
    file_sha256,
    json_sha256,
    write_json_atomic,
)
from .observability import (
    CredentialSession,
    WeaveAdapter,
    validate_binding_from_environment,
)

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
WEAVE_EVALUATION_READBACK_TIMEOUT_SECONDS = 30
_PROHIBITED_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|credential(?:[_-]?id)?|cookie|"
    r"local[_-]?(?:absolute[_-]?)?path|password|private[_-]?key|secret|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token)"
)


def _campaign_run_id(
    *,
    campaign_instance_sha256: str,
    kind: str,
    entity: str,
    project: str,
    identity: str,
) -> str:
    """Create a run identity that cannot resume across preregistrations."""

    if not re.fullmatch(r"[0-9a-f]{64}", campaign_instance_sha256):
        raise ValueError("campaign instance must be a lowercase SHA-256 digest")
    return hashlib.sha256(
        (
            f"{CAMPAIGN_VERSION}|{campaign_instance_sha256}|{kind}|"
            f"{entity}|{project}|{identity}"
        ).encode()
    ).hexdigest()[:24]


def _safe(value: Any, *, hidden: bool = False) -> Any:
    """Reject accidental credential/path leakage before a cloud SDK sees data."""

    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if _PROHIBITED_KEY.search(str(key)):
                raise ValueError("cloud record contains a prohibited key")
            result[str(key)] = _safe(item, hidden=hidden)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item, hidden=hidden) for item in value]
    if isinstance(value, Path):
        raise TypeError("cloud record contains a local path")
    if isinstance(value, str):
        if "/Users/" in value or "\\\\" in value or "Bearer " in value:
            raise ValueError("cloud record contains local or credential material")
        if hidden and len(value) > 64:
            # Hidden summaries may contain stable hashes, counts, and short
            # enum labels, but never questions or generated answer text.
            raise ValueError("hidden aggregate contains unapproved free text")
        return value
    return value


def _logged_artifact_reference(logged: Any, *, entity: str, project: str) -> str:
    """Return the SDK's acknowledged ``collection:version`` reference once.

    In current W&B SDKs ``Artifact.name`` already includes the logged alias. Appending
    ``Artifact.version`` a second time produces ``collection:v0:v0`` and makes readback
    fail after upload. Prefer the SDK's qualified name and validate it against the
    intended Restricted destination before it crosses the local/cloud boundary.
    """

    qualified = str(getattr(logged, "qualified_name", "") or "")
    version = str(getattr(logged, "version", "") or "")
    if not qualified:
        name = str(getattr(logged, "name", "") or "")
        if ":" not in name:
            name = f"{name}:{version}"
        qualified = f"{entity}/{project}/{name}"
    prefix = f"{entity}/{project}/"
    if not qualified.startswith(prefix):
        raise RuntimeError("WANDB_ARTIFACT_DESTINATION_MISMATCH")
    leaf = qualified.removeprefix(prefix)
    if leaf.count(":") != 1 or not version or leaf.rsplit(":", 1)[1] != version:
        raise RuntimeError("WANDB_ARTIFACT_VERSION_REFERENCE_INVALID")
    return qualified


def _quiet_settings(root: Path) -> dict[str, str]:
    cloud = root.resolve() / "wandb"
    values = {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
        "WANDB_SILENT": "true",
        "WANDB_DIR": str(cloud / "run"),
        "WANDB_CACHE_DIR": str(cloud / "cache"),
        "WANDB_ARTIFACT_DIR": str(cloud / "artifacts"),
        "WEAVE_CAPTURE_CODE": "false",
        "WEAVE_PRINT_CALL_LINK": "false",
        # Weave retries HTTP calls. Keep each attempt bounded so two attempts
        # plus backoff cannot hold a campaign block for minutes during outage.
        "WEAVE_HTTP_TIMEOUT": "10",
        "WEAVE_RETRY_MAX_ATTEMPTS": "2",
        "WEAVE_RETRY_MAX_INTERVAL": "5",
    }
    for key in ("WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_ARTIFACT_DIR"):
        Path(values[key]).mkdir(parents=True, exist_ok=True)
    return values


def _wandb_run_settings(wandb: Any) -> Any:
    """Bound SDK finalization so an observability outage cannot hang a stage."""

    return wandb.Settings(
        save_code=False,
        disable_git=True,
        finish_timeout=120,
        finish_timeout_raises=True,
        init_timeout=60,
        summary_timeout=60,
    )


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wandb_viewer_username() -> str:
    """Resolve the authenticated account identity without logging credentials."""

    import wandb  # type: ignore[import-not-found]

    api = wandb.Api()
    viewer = getattr(api, "viewer", None)
    if callable(viewer):
        viewer = viewer()
    if isinstance(viewer, Mapping):
        username = viewer.get("username") or viewer.get("name")
    else:
        username = getattr(viewer, "username", None) or getattr(viewer, "name", None)
    if not isinstance(username, str) or not username.strip():
        raise RuntimeError("WANDB_SERVICE_ACCOUNT_IDENTITY_UNVERIFIABLE")
    return username.strip()


@dataclass(frozen=True)
class CloudReceipt:
    kind: str
    entity: str
    project: str
    run_id: str
    run_url: str
    artifact_id: str | None
    artifact_reference: str | None
    source_sha256: str
    readback_sha256: str | None
    weave_uri: str | None = None
    dataset_readback_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "schema_version": "1.0",
            "campaign_version": CAMPAIGN_VERSION,
        }


class LocalBlockObserver(AttemptObserver):
    def __init__(self, output: Path, *, group: Mapping[str, Any]) -> None:
        self.output = output
        self.group = dict(group)
        self.rows: list[dict[str, Any]] = []

    @contextmanager
    def attempt(self, spec: AttemptSpec, public_input: Mapping[str, Any]) -> Iterator[None]:
        del spec, public_input
        yield None

    def log_outcome(self, spec: AttemptSpec, outcome: AttemptOutcome) -> None:
        self.rows.append(outcome.as_record(spec))

    def finish(self) -> dict[str, Any]:
        payload = {
            "schema_version": "1.0",
            "campaign_version": CAMPAIGN_VERSION,
            "group": self.group,
            "attempts": self.rows,
        }
        write_json_atomic(self.output, payload)
        return {
            "mode": "local",
            "path": str(self.output),
            "sha256": file_sha256(self.output),
            "attempt_count": len(self.rows),
            "group_sha256": json_sha256(self.group),
        }


class LocalCampaignPublisher:
    """Credential-free publication target used by dry-run and fake-provider CI."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def publish_protocol_amendment(self, path: Path) -> CloudReceipt:
        return self.seal_preregistration(path)

    def reuse_protocol_amendment(
        self, receipt: Mapping[str, Any], *, path: Path
    ) -> str | None:
        if receipt.get("source_sha256") != file_sha256(path):
            raise RuntimeError("PROTOCOL_AMENDMENT_RECEIPT_MISMATCH")
        return None

    def publish_operational_incident(self, path: Path) -> CloudReceipt:
        receipt = self.seal_preregistration(path)
        return replace(receipt, kind="local_operational_incident")

    def reuse_operational_incident(
        self, receipt: Mapping[str, Any], *, path: Path
    ) -> str | None:
        if receipt.get("source_sha256") != file_sha256(path):
            raise RuntimeError("OPERATIONAL_INCIDENT_RECEIPT_MISMATCH")
        return None

    def seal_preregistration(
        self,
        path: Path,
        *,
        amendment_reference: str | None = None,
        incident_reference: str | None = None,
    ) -> CloudReceipt:
        del amendment_reference, incident_reference
        digest = file_sha256(path)
        destination = self.root / f"preregistration-{digest}.json"
        if destination.exists() and file_sha256(destination) != digest:
            raise RuntimeError("local preregistration seal drifted")
        if not destination.exists():
            destination.write_bytes(path.read_bytes())
        return CloudReceipt(
            kind="local_preregistration",
            entity="local",
            project=TARGET_PROJECT,
            run_id=digest[:24],
            run_url="",
            artifact_id=None,
            artifact_reference=None,
            source_sha256=digest,
            readback_sha256=file_sha256(destination),
        )

    def start_block(self, *, group: Mapping[str, Any]) -> LocalBlockObserver:
        digest = json_sha256(group)
        return LocalBlockObserver(self.root / "blocks" / f"{digest}.json", group=group)

    def start_time_block(
        self, specs: Sequence[AttemptSpec], *, time_block: str
    ) -> CompositeBlockObserver:
        observers = []
        for group, selected in group_specs(specs, time_block=time_block):
            observer = self.start_block(group=group)
            observers.append((observer, selected))
        return CompositeBlockObserver(observers)

    def publish_release_gate(self, path: Path) -> CloudReceipt:
        digest = file_sha256(path)
        destination = self.root / f"release-gate-{digest}.json"
        destination.write_bytes(path.read_bytes())
        return CloudReceipt(
            kind="local_release_gate",
            entity="local",
            project=TARGET_PROJECT,
            run_id=digest[:24],
            run_url="",
            artifact_id=None,
            artifact_reference=None,
            source_sha256=digest,
            readback_sha256=file_sha256(destination),
        )

    def publish_architecture_development_study(self, directory: Path) -> CloudReceipt:
        """Seal a public development package without any release-gate dependency."""

        paths = sorted(directory.glob("*.json"))
        if not paths:
            raise ValueError("architecture-development package is empty")
        if any(
            "release-gate" in path.name.lower() or "release_basis" in path.name.lower()
            for path in paths
        ):
            raise ValueError("development package may not contain release-gate material")
        files = {path.name: file_sha256(path) for path in paths}
        digest = json_sha256(
            {
                "kind": "architecture_development_study",
                "files": files,
                "release_eligible": False,
                "release_gate_consumed": False,
            }
        )
        destination = self.root / f"architecture-development-{digest}"
        destination.mkdir(parents=True, exist_ok=True)
        for path in paths:
            target = destination / path.name
            if not target.exists():
                target.write_bytes(path.read_bytes())
        readback = {path.name: file_sha256(destination / path.name) for path in paths}
        if readback != files:
            raise RuntimeError("LOCAL_ARCHITECTURE_DEVELOPMENT_READBACK_HASH_MISMATCH")
        return CloudReceipt(
            kind="local_architecture_development_study",
            entity="local",
            project=TARGET_PROJECT,
            run_id=digest[:24],
            run_url="",
            artifact_id=None,
            artifact_reference=str(destination),
            source_sha256=digest,
            readback_sha256=json_sha256(
                {
                    "kind": "architecture_development_study",
                    "files": readback,
                    "release_eligible": False,
                    "release_gate_consumed": False,
                }
            ),
        )


class WandbBlockObserver(AttemptObserver):
    """One W&B Run plus one pre-call Weave EvaluationLogger for a replicate block."""

    def __init__(
        self,
        *,
        publisher: WandbCampaignPublisher,
        group: Mapping[str, Any],
        hidden: bool,
    ) -> None:
        self.publisher = publisher
        self.group = dict(group)
        self.hidden = hidden
        self.rows: list[dict[str, Any]] = []
        self._trace_acknowledged: dict[str, bool] = {}
        self._input_by_attempt: dict[str, Any] = {}
        self._run = None
        self._logger = None
        self._weave = None
        self._evaluation_uri = None
        self._evaluation_run_url = None
        self._weave_client = None
        self._wandb_context_set = False
        self._run_finished = False
        self._lock = threading.RLock()

    def _cleanup_cloud_context(self) -> BaseException | None:
        """Best-effort cleanup that never masks an earlier block failure."""

        errors: list[BaseException] = []
        if self._wandb_context_set and self._weave_client is not None:
            try:
                self._weave_client.clear_wandb_run_context()
            except Exception as exc:  # noqa: BLE001 - defensive SDK cleanup boundary
                errors.append(exc)
            finally:
                self._wandb_context_set = False
        if self._run is not None and not self._run_finished:
            try:
                self._run.finish()
            except Exception as exc:  # noqa: BLE001 - defensive SDK cleanup boundary
                errors.append(exc)
            finally:
                self._run_finished = True
        if not errors:
            return None
        first = errors[0]
        for additional in errors[1:]:
            first.add_note(f"additional cleanup failure: {type(additional).__name__}")
        return first

    def start(self) -> WandbBlockObserver:
        import wandb  # type: ignore[import-not-found]
        import weave  # type: ignore[import-not-found]

        project = self.publisher.lockbox_project if self.hidden else self.publisher.project
        if self.hidden and not project:
            raise RuntimeError("HIDDEN_LOCKBOX_PROJECT_REQUIRED")
        group_hash = json_sha256(self.group)
        run_id = _campaign_run_id(
            campaign_instance_sha256=self.publisher.campaign_instance_sha256,
            kind="evaluation_block",
            entity=self.publisher.entity,
            project=project,
            identity=group_hash,
        )
        config = _safe(
            {
                **self.group,
                "campaign_version": CAMPAIGN_VERSION,
                "campaign_instance_sha256": self.publisher.campaign_instance_sha256,
                (
                    "preregistration_reference_sha256"
                    if self.hidden
                    else "preregistration_reference"
                ): (
                    hashlib.sha256(self.publisher.preregistration_reference.encode()).hexdigest()
                    if self.hidden
                    else self.publisher.preregistration_reference
                ),
                "hidden": self.hidden,
            },
            hidden=self.hidden,
        )
        self._run = wandb.init(
            entity=self.publisher.entity,
            project=project,
            id=run_id,
            resume="allow",
            name=(
                f"{self.publisher.campaign_instance_sha256[:8]}-"
                f"{self.group['stage']}-{self.group['architecture_id']}-"
                f"{self.group['model_slug']}-{self.group['repeat']}-"
                f"{self.group['time_block']}"
            ),
            job_type="evidence_guide_v2_evaluation_block",
            config=config,
            reinit="create_new",
            settings=_wandb_run_settings(wandb),
        )
        try:
            self._run.use_artifact(self.publisher.preregistration_reference)
            self._weave_client = self.publisher.ensure_weave_client(project)
            dataset_uri = self.publisher.dataset_reference(
                str(self.group["split"]), hidden=self.hidden
            )
            dataset = weave.ref(dataset_uri).get()
            evaluation = weave.Evaluation(
                name=(
                    "GI Evidence Guide v2 component · "
                    f"{self.publisher.campaign_instance_sha256[:8]} · {group_hash[:12]}"
                ),
                description=(
                    "Preregistered component-arm specification; actual bounded runtime calls "
                    "are recorded by the EvaluationLogger initialized immediately below."
                ),
                dataset=dataset,
                scorers=[],
                trials=1,
                metadata=config,
            )
            evaluation_ref = weave.publish(
                evaluation,
                name=(
                    "gi-evidence-guide-v2-component-"
                    f"{self.publisher.campaign_instance_sha256[:8]}-{group_hash[:12]}"
                ),
            )
            if evaluation_ref.get() is None:
                raise RuntimeError("WEAVE_EVALUATION_SPEC_READBACK_FAILED")
            self._evaluation_uri = evaluation_ref.uri()
            # Important: construct EvaluationLogger before any runtime model call so
            # provider usage and trace association are not reconstructed post hoc.
            # Weave's W&B run context is client-global rather than a contextvar,
            # so hold it only for this synchronous root-call creation. Keeping it
            # across attempts would let another block overwrite the association.
            self._weave_client.set_wandb_run_context(str(self._run.id))
            self._wandb_context_set = True
            try:
                self._logger = weave.EvaluationLogger(
                    name=(
                        "GI Evidence Guide v2 · "
                        f"{self.publisher.campaign_instance_sha256[:8]} · {group_hash[:12]}"
                    ),
                    model=str(self.group["model_id"]),
                    # In Weave 0.53.4 a string is treated as a new Dataset *name*, not
                    # resolved as an existing object reference. Passing the long
                    # ``weave:///...`` URI therefore creates a second Dataset and an
                    # auto-named Evaluation whose sanitized/truncated object IDs can
                    # collide. Reuse the resolved, ref-bearing Dataset object instead.
                    dataset=dataset,
                    scorers=[
                        "protocol_valid",
                        "safe",
                        "evidence_valid",
                        "repository_fidelity",
                        "causal",
                        "intent_correct",
                        "trace_acknowledged",
                    ],
                    eval_attributes=config,
                )
            finally:
                self._weave_client.clear_wandb_run_context()
                self._wandb_context_set = False
            self._evaluation_run_url = str(self._logger.ui_url or "")
            if not self._evaluation_run_url:
                raise RuntimeError("WEAVE_EVALUATION_RUN_URL_UNAVAILABLE")
            trace_credentials = (
                self.publisher.lockbox_credentials if self.hidden else self.publisher.credentials
            )
            if trace_credentials is None:
                raise RuntimeError("HIDDEN_LOCKBOX_DISTINCT_SERVICE_ACCOUNT_REQUIRED")
            self._weave = WeaveAdapter(
                trace_credentials,
                preinitialized_client=self._weave_client,
            )
        except BaseException as exc:
            cleanup_error = self._cleanup_cloud_context()
            if cleanup_error is not None:
                exc.add_note(f"cleanup failure preserved: {type(cleanup_error).__name__}")
            raise
        return self

    @contextmanager
    def attempt(self, spec: AttemptSpec, public_input: Mapping[str, Any]) -> Iterator[Any]:
        if self._logger is None or self._weave is None:
            raise RuntimeError("evaluation observer was not initialized before inference")
        safe_input = (
            {
                "case_sha256": hashlib.sha256(spec.case_id.encode()).hexdigest(),
                "packet_sha256": hashlib.sha256(
                    json.dumps(public_input, sort_keys=True, default=str).encode()
                ).hexdigest(),
                "hidden_content_logged": False,
            }
            if self.hidden
            else _safe(public_input)
        )
        with self._lock:
            self._input_by_attempt[spec.attempt_id] = safe_input
        binding = self.publisher.lockbox_binding if self.hidden else self.publisher.binding
        conversation_id = (
            f"eval-{self.publisher.campaign_instance_sha256[:12]}-{spec.attempt_id}"
        )
        try:
            with self._weave.agent_turn(
                binding,
                conversation_id=conversation_id,
                conversation_name=f"Evidence Guide evaluation {spec.attempt_id[:12]}",
                question=(
                    "[sealed hidden evaluation question]"
                    if self.hidden
                    else str(public_input.get("question") or "Evidence Guide protocol case")
                ),
                model=spec.model_id,
                include_content=not self.hidden,
                attributes={
                    "campaign_version": CAMPAIGN_VERSION,
                    "campaign_instance_sha256": self.publisher.campaign_instance_sha256,
                    "attempt_id": spec.attempt_id,
                    "stage": spec.stage,
                    "architecture_id": spec.architecture_id,
                    "hidden": spec.hidden,
                    # Explicit cross-linking avoids the shared client's mutable
                    # W&B context while concurrent turns are in flight.
                    "wandb_run_id": str(self._run.id),
                    "wandb_run_url": str(getattr(self._run, "url", "") or ""),
                },
            ) as recorder:
                yield recorder
            with self._lock:
                self._trace_acknowledged[spec.attempt_id] = True
        except Exception:
            with self._lock:
                self._trace_acknowledged[spec.attempt_id] = False
            raise

    def log_outcome(self, spec: AttemptSpec, outcome: AttemptOutcome) -> None:
        if self._logger is None:
            raise RuntimeError("evaluation logger is unavailable")
        row = outcome.as_record(spec)
        if self.hidden:
            row["case_id"] = hashlib.sha256(spec.case_id.encode()).hexdigest()
            row.pop("details", None)
            row["explanation_plan_id"] = None
        safe_row = _safe(row, hidden=self.hidden)
        with self._lock:
            self.rows.append(safe_row)
            prediction = self._logger.log_prediction(
                inputs=self._input_by_attempt.pop(spec.attempt_id),
                output=safe_row,
                example_id=(
                    hashlib.sha256(spec.case_id.encode()).hexdigest()
                    if self.hidden
                    else spec.case_id
                ),
            )
            for scorer in (
                "protocol_valid",
                "safe",
                "evidence_valid",
                "repository_fidelity",
                "causal",
                "intent_correct",
                "trace_acknowledged",
            ):
                value = safe_row.get(scorer)
                if value is not None:
                    prediction.log_score(scorer=scorer, score=bool(value))
            prediction.finish()

    def finish(self) -> dict[str, Any]:
        if self._run is None or self._logger is None:
            raise RuntimeError("cloud block was not started")
        import wandb  # type: ignore[import-not-found]

        try:
            summary = {
                "attempt_count": len(self.rows),
                "completed": sum(row["status"] == "completed" for row in self.rows),
                "provider_invocations": sum(row["provider_invocations"] for row in self.rows),
                "transport_attempts": sum(row["transport_attempts"] for row in self.rows),
                "input_tokens": sum(row["input_tokens"] for row in self.rows),
                "output_tokens": sum(row["output_tokens"] for row in self.rows),
                "cost_usd": str(sum(float(row["cost_usd"]) for row in self.rows)),
                "all_safe": all(row["safe"] for row in self.rows),
                "all_trace_acknowledged": all(row["trace_acknowledged"] for row in self.rows),
                "hidden": self.hidden,
                ("weave_evaluation_sha256" if self.hidden else "weave_evaluation_uri"): (
                    hashlib.sha256(str(self._evaluation_uri).encode()).hexdigest()
                    if self.hidden
                    else self._evaluation_uri
                ),
            }
            self._logger.log_summary(summary=_safe(summary, hidden=self.hidden))
            if self._weave_client is None:
                raise RuntimeError("WEAVE_EVALUATION_CLIENT_UNAVAILABLE")
            deadline = time.monotonic() + 60
            while self._weave_client.num_outstanding_jobs:
                if time.monotonic() >= deadline:
                    raise RuntimeError("WEAVE_EVALUATION_FLUSH_TIMEOUT")
                time.sleep(0.1)
            evaluation_call = getattr(self._logger, "_evaluate_call", None)
            evaluation_call_id = str(getattr(evaluation_call, "id", "") or "")
            if not evaluation_call_id:
                raise RuntimeError("WEAVE_EVALUATION_RUN_READBACK_FAILED")
            readback_deadline = (
                time.monotonic() + WEAVE_EVALUATION_READBACK_TIMEOUT_SECONDS
            )
            evaluation_readback = None
            while evaluation_readback is None:
                try:
                    evaluation_readback = self._weave_client.get_call(evaluation_call_id)
                except ValueError:
                    # An empty local SDK queue can precede read-API visibility.
                    # Only a successful readback acknowledges the evaluation.
                    evaluation_readback = None
                if evaluation_readback is not None:
                    break
                if time.monotonic() >= readback_deadline:
                    raise RuntimeError("WEAVE_EVALUATION_RUN_READBACK_FAILED")
                time.sleep(0.25)
            columns = [
                "attempt_id",
                "status",
                "stage",
                "model_id",
                "architecture_id",
                "repeat",
                "time_block",
                "provider_invocations",
                "transport_attempts",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "latency_ms",
                "protocol_valid",
                "safe",
                "evidence_valid",
                "repository_fidelity",
                "causal",
                "intent_correct",
                "trace_acknowledged",
                "error_codes",
            ]
            table = wandb.Table(columns=columns)
            for row in self.rows:
                table.add_data(*(row.get(column) for column in columns))
            self._run.log({"attempts": table})
            self._run.summary.update(summary)
            self._run.summary["weave_evaluation_run_url"] = self._evaluation_run_url
            result = {
                "mode": "wandb_weave",
                "run_id": str(self._run.id),
                "run_url": str(getattr(self._run, "url", "") or ""),
                "attempt_count": len(self.rows),
                "group_sha256": json_sha256(self.group),
                "hidden": self.hidden,
                "weave_evaluation_uri": self._evaluation_uri,
                "weave_evaluation_run_url": self._evaluation_run_url,
            }
        except BaseException as exc:
            cleanup_error = self._cleanup_cloud_context()
            if cleanup_error is not None:
                exc.add_note(f"cleanup failure preserved: {type(cleanup_error).__name__}")
            raise
        cleanup_error = self._cleanup_cloud_context()
        if cleanup_error is not None:
            raise cleanup_error
        return result


class WandbCampaignPublisher:
    def __init__(
        self,
        *,
        entity: str,
        project: str,
        workspace: Path,
        lockbox_project: str | None = None,
        credentials: CredentialSession | None = None,
        lockbox_credentials: CredentialSession | None = None,
        lockbox_expected_username: str | None = None,
        campaign_instance_sha256: str,
    ) -> None:
        if project != TARGET_PROJECT:
            raise ValueError(f"v2 campaign requires exact project {TARGET_PROJECT}")
        if not _SAFE_NAME.fullmatch(entity) or not _SAFE_NAME.fullmatch(project):
            raise ValueError("invalid W&B destination")
        self.entity = entity
        self.project = project
        if not re.fullmatch(r"[0-9a-f]{64}", campaign_instance_sha256):
            raise ValueError("campaign instance must be a lowercase SHA-256 digest")
        self.campaign_instance_sha256 = campaign_instance_sha256
        self.workspace = workspace.resolve()
        self.lockbox_project = lockbox_project
        self.credentials = credentials or CredentialSession()
        self.lockbox_credentials = lockbox_credentials
        self.lockbox_expected_username = lockbox_expected_username
        self.environment = _quiet_settings(self.workspace)
        self._weave_clients: dict[str, Any] = {}
        self._weave_client_lock = threading.RLock()
        with _temporary_environment(self.environment), self.credentials.environment():
            self.binding = validate_binding_from_environment(
                entity=entity,
                project=project,
                project_id="evidence-guide-v2-evaluations",
            )
            if not self.binding.verified or self.binding.access != "Restricted":
                raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
            self.lockbox_binding = None
            if lockbox_project:
                if lockbox_project == project:
                    raise ValueError("hidden lockbox must not use the report/evaluation project")
                if lockbox_credentials is None or not lockbox_expected_username:
                    raise RuntimeError("HIDDEN_LOCKBOX_DISTINCT_SERVICE_ACCOUNT_REQUIRED")
                if lockbox_credentials is self.credentials:
                    raise RuntimeError("HIDDEN_LOCKBOX_CREDENTIAL_REUSE_PROHIBITED")
                with lockbox_credentials.environment():
                    actual_username = _wandb_viewer_username()
                    if actual_username != lockbox_expected_username:
                        raise RuntimeError("HIDDEN_LOCKBOX_SERVICE_ACCOUNT_IDENTITY_MISMATCH")
                    self.lockbox_binding = validate_binding_from_environment(
                        entity=entity,
                        project=lockbox_project,
                        project_id="evidence-guide-v2-hidden-lockbox",
                    )
                    if (
                        not self.lockbox_binding.verified
                        or self.lockbox_binding.access != "Restricted"
                        or self.lockbox_binding.entity != entity
                    ):
                        raise RuntimeError("HIDDEN_LOCKBOX_RESTRICTED_SCOPE_UNVERIFIABLE")
        self.preregistration_reference = ""
        self.dataset_references: dict[str, dict[str, str]] = {}

    def ensure_weave_client(self, project: str) -> Any:
        """Initialize Weave at most once per process and reject project switching.

        Repeated ``weave.init`` calls finish the current client first. With an
        EvaluationLogger and concurrent Agent turns that can wait on retryable
        background jobs for hours. One campaign process owns exactly one Weave
        project, so reusing the verified client is both safer and deterministic.
        """

        import weave  # type: ignore[import-not-found]

        expected = f"{self.entity}/{project}"
        with self._weave_client_lock:
            known = self._weave_clients.get(project)
            current = weave.get_client()
            if known is not None:
                if current is not known or str(known.project_id) != expected:
                    raise RuntimeError("WEAVE_CAMPAIGN_CLIENT_CHANGED")
                return known
            if current is not None:
                if str(current.project_id) != expected:
                    raise RuntimeError("WEAVE_PROJECT_SWITCH_REQUIRES_NEW_PROCESS")
                client = current
            else:
                client = weave.init(
                    expected,
                    settings={
                        "redact_pii": False,
                        "print_call_link": False,
                        "client_parallelism": 8,
                    },
                )
            self._weave_clients[project] = client
            return client

    @contextmanager
    def cloud_environment(self, *, hidden: bool = False) -> Iterator[None]:
        credentials = self.lockbox_credentials if hidden else self.credentials
        if credentials is None:
            raise RuntimeError("HIDDEN_LOCKBOX_DISTINCT_SERVICE_ACCOUNT_REQUIRED")
        with _temporary_environment(self.environment), credentials.environment():
            yield

    def _publish_file(
        self,
        path: Path,
        *,
        kind: str,
        project: str,
        artifact_type: str,
        input_artifact_references: Sequence[str] = (),
    ) -> CloudReceipt:
        import wandb  # type: ignore[import-not-found]

        digest = file_sha256(path)
        run_id = _campaign_run_id(
            campaign_instance_sha256=self.campaign_instance_sha256,
            kind=kind,
            entity=self.entity,
            project=project,
            identity=digest,
        )
        with self.cloud_environment(hidden=project == self.lockbox_project):
            run = wandb.init(
                entity=self.entity,
                project=project,
                id=run_id,
                resume="allow",
                name=f"{kind}-{digest[:12]}",
                job_type=kind,
                config={
                    "campaign_version": CAMPAIGN_VERSION,
                    "campaign_instance_sha256": self.campaign_instance_sha256,
                    "source_sha256": digest,
                    "required_access": "Restricted",
                },
                reinit="create_new",
                settings=_wandb_run_settings(wandb),
            )
            try:
                for reference in input_artifact_references:
                    run.use_artifact(reference)
                artifact = wandb.Artifact(
                    name=f"evidence-guide-v2-{kind}-{digest[:16]}",
                    type=artifact_type,
                    metadata={
                        "campaign_version": CAMPAIGN_VERSION,
                        "campaign_instance_sha256": self.campaign_instance_sha256,
                        "source_sha256": digest,
                        "immutable_identity": digest,
                    },
                )
                artifact.add_file(str(path), name=path.name)
                logged = run.log_artifact(artifact)
                logged.wait()
                qualified = _logged_artifact_reference(
                    logged,
                    entity=self.entity,
                    project=project,
                )
                with tempfile.TemporaryDirectory(prefix="gi-evidence-v2-readback-") as temporary:
                    downloaded = run.use_artifact(qualified).download(root=temporary)
                    readback = Path(downloaded) / path.name
                    readback_sha = file_sha256(readback)
                if readback_sha != digest:
                    raise RuntimeError("WANDB_ARTIFACT_READBACK_HASH_MISMATCH")
                run.summary.update(
                    {
                        "artifact_reference": qualified,
                        "artifact_id": str(logged.id),
                        "readback_sha256": readback_sha,
                    }
                )
                run_url = str(getattr(run, "url", "") or "")
                receipt = CloudReceipt(
                    kind=kind,
                    entity=self.entity,
                    project=project,
                    run_id=str(run.id),
                    run_url=run_url,
                    artifact_id=str(logged.id),
                    artifact_reference=qualified,
                    source_sha256=digest,
                    readback_sha256=readback_sha,
                )
            finally:
                run.finish()
        return receipt

    def publish_protocol_amendment(self, path: Path) -> CloudReceipt:
        receipt = self._publish_file(
            path,
            kind="protocol_amendment",
            project=self.project,
            artifact_type="experiment-protocol-amendment",
        )
        if receipt.readback_sha256 != receipt.source_sha256:
            raise RuntimeError("PROTOCOL_AMENDMENT_NOT_ACKNOWLEDGED")
        return receipt

    def publish_operational_incident(self, path: Path) -> CloudReceipt:
        receipt = self._publish_file(
            path,
            kind="operational_incident",
            project=self.project,
            artifact_type="experiment-operational-incident",
        )
        if receipt.readback_sha256 != receipt.source_sha256:
            raise RuntimeError("OPERATIONAL_INCIDENT_NOT_ACKNOWLEDGED")
        return receipt

    def reuse_operational_incident(
        self, receipt: Mapping[str, Any], *, path: Path
    ) -> str:
        if (
            receipt.get("project") != self.project
            or receipt.get("entity") != self.entity
            or receipt.get("source_sha256") != file_sha256(path)
            or receipt.get("readback_sha256") != file_sha256(path)
            or not receipt.get("artifact_reference")
        ):
            raise RuntimeError("OPERATIONAL_INCIDENT_RECEIPT_MISMATCH")
        return str(receipt["artifact_reference"])

    def reuse_protocol_amendment(
        self, receipt: Mapping[str, Any], *, path: Path
    ) -> str:
        if (
            receipt.get("project") != self.project
            or receipt.get("entity") != self.entity
            or receipt.get("source_sha256") != file_sha256(path)
            or receipt.get("readback_sha256") != file_sha256(path)
            or not receipt.get("artifact_reference")
        ):
            raise RuntimeError("PROTOCOL_AMENDMENT_RECEIPT_MISMATCH")
        return str(receipt["artifact_reference"])

    def seal_preregistration(
        self,
        path: Path,
        *,
        amendment_reference: str | None = None,
        incident_reference: str | None = None,
    ) -> CloudReceipt:
        input_references = tuple(
            reference
            for reference in (amendment_reference, incident_reference)
            if reference
        )
        receipt = self._publish_file(
            path,
            kind="preregistration",
            project=self.project,
            artifact_type="experiment-preregistration",
            input_artifact_references=input_references,
        )
        if receipt.readback_sha256 != receipt.source_sha256:
            raise RuntimeError("PREREGISTRATION_NOT_ACKNOWLEDGED")
        self.preregistration_reference = str(receipt.artifact_reference)
        return receipt

    def reuse_preregistration(self, receipt: Mapping[str, Any], *, path: Path) -> None:
        if (
            receipt.get("project") != self.project
            or receipt.get("entity") != self.entity
            or receipt.get("source_sha256") != file_sha256(path)
            or receipt.get("readback_sha256") != file_sha256(path)
            or not receipt.get("artifact_reference")
        ):
            raise RuntimeError("PREREGISTRATION_RECEIPT_MISMATCH")
        self.preregistration_reference = str(receipt["artifact_reference"])

    def publish_hidden_lockbox(self, path: Path, *, expected_sha256: str) -> CloudReceipt:
        if not self.lockbox_project or self.lockbox_binding is None:
            raise RuntimeError("HIDDEN_LOCKBOX_PROJECT_REQUIRED")
        if file_sha256(path) != expected_sha256:
            raise RuntimeError("HIDDEN_LOCKBOX_HASH_MISMATCH")
        receipt = self._publish_file(
            path,
            kind="hidden_lockbox",
            project=self.lockbox_project,
            artifact_type="sealed-evaluation-lockbox",
        )
        import weave  # type: ignore[import-not-found]

        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("rows") if isinstance(raw, Mapping) else None
        if not isinstance(rows, list) or len(rows) != 300:
            raise RuntimeError("HIDDEN_LOCKBOX_SHAPE_MISMATCH")
        rows_digest = json_sha256(rows)
        with self.cloud_environment(hidden=True):
            self.ensure_weave_client(self.lockbox_project)
            dataset = weave.Dataset(
                name=f"gi-evidence-guide-v2-hidden-{expected_sha256[:12]}",
                description="Service-account-only sealed Evidence Guide v2 lockbox.",
                rows=rows,
            )
            dataset_ref = weave.publish(dataset, name=dataset.name)
            readback_rows = [dict(row) for row in dataset_ref.get().rows]
            if json_sha256(readback_rows) != rows_digest:
                raise RuntimeError("WEAVE_HIDDEN_DATASET_READBACK_MISMATCH")
        receipt = replace(
            receipt,
            weave_uri=dataset_ref.uri(),
            dataset_readback_sha256=rows_digest,
        )
        self.dataset_references["hidden"] = {
            "wandb_artifact": str(receipt.artifact_reference),
            "weave_uri": str(receipt.weave_uri),
            "sha256": receipt.source_sha256,
            "dataset_readback_sha256": rows_digest,
        }
        return receipt

    def reuse_hidden_lockbox(
        self,
        receipt: Mapping[str, Any],
        *,
        path: Path,
        expected_sha256: str,
    ) -> None:
        if (
            self.lockbox_project is None
            or receipt.get("project") != self.lockbox_project
            or receipt.get("source_sha256") != expected_sha256
            or receipt.get("readback_sha256") != expected_sha256
            or file_sha256(path) != expected_sha256
            or not receipt.get("artifact_reference")
            or not receipt.get("weave_uri")
            or not receipt.get("dataset_readback_sha256")
        ):
            raise RuntimeError("HIDDEN_LOCKBOX_RECEIPT_MISMATCH")
        self.dataset_references["hidden"] = {
            "wandb_artifact": str(receipt["artifact_reference"]),
            "weave_uri": str(receipt["weave_uri"]),
            "sha256": expected_sha256,
            "dataset_readback_sha256": str(receipt["dataset_readback_sha256"]),
        }

    def publish_visible_datasets(self, corpus: Corpus) -> dict[str, dict[str, str]]:
        """Publish development/validation/calibration before their first model call."""

        import wandb  # type: ignore[import-not-found]
        import weave  # type: ignore[import-not-found]

        for split in ("development", "validation", "judge_calibration"):
            if split not in corpus.splits:
                raise RuntimeError(f"visible v2 split is unavailable: {split}")
        identity = json_sha256(
            {
                split: corpus.split_sha256[split]
                for split in ("development", "validation", "judge_calibration")
            }
        )
        run_id = _campaign_run_id(
            campaign_instance_sha256=self.campaign_instance_sha256,
            kind="visible_datasets",
            entity=self.entity,
            project=self.project,
            identity=identity,
        )
        references: dict[str, dict[str, str]] = {}
        with self.cloud_environment():
            run = wandb.init(
                entity=self.entity,
                project=self.project,
                id=run_id,
                resume="allow",
                name=f"visible-datasets-{identity[:12]}",
                job_type="evidence_guide_v2_dataset_registration",
                config={
                    "campaign_version": CAMPAIGN_VERSION,
                    "campaign_instance_sha256": self.campaign_instance_sha256,
                    "dataset_identity": identity,
                    "hidden_content_included": False,
                },
                reinit="create_new",
                settings=_wandb_run_settings(wandb),
            )
            try:
                run.use_artifact(self.preregistration_reference)
                self.ensure_weave_client(self.project)
                for split in ("development", "validation", "judge_calibration"):
                    source = Path(corpus.source_paths[split])
                    expected = corpus.split_sha256[split]
                    artifact = wandb.Artifact(
                        name=f"evidence-guide-v2-{split}-{expected[:16]}",
                        type="benchmark-dataset",
                        metadata={
                            "campaign_version": CAMPAIGN_VERSION,
                            "split": split,
                            "sha256": expected,
                            "hidden": False,
                        },
                    )
                    artifact.add_file(str(source), name=f"{split}.json")
                    logged = run.log_artifact(artifact)
                    logged.wait()
                    artifact_ref = _logged_artifact_reference(
                        logged,
                        entity=self.entity,
                        project=self.project,
                    )
                    with tempfile.TemporaryDirectory(
                        prefix=f"gi-evidence-v2-{split}-readback-"
                    ) as temporary:
                        downloaded = Path(run.use_artifact(artifact_ref).download(root=temporary))
                        readback = file_sha256(downloaded / f"{split}.json")
                    if readback != expected:
                        raise RuntimeError("WANDB_VISIBLE_DATASET_READBACK_HASH_MISMATCH")
                    weave_dataset = weave.Dataset(
                        name=f"gi-evidence-guide-v2-{split}-{expected[:12]}",
                        description=(
                            "Synthetic/redistributable Evidence Guide v2 evaluation split; "
                            "practitioner usefulness is not measured."
                        ),
                        rows=[_safe(dict(row)) for row in corpus.splits[split]],
                    )
                    weave_ref = weave.publish(weave_dataset, name=weave_dataset.name)
                    published = weave_ref.get()
                    published_rows = [dict(row) for row in published.rows]
                    if len(published_rows) != len(corpus.splits[split]) or json_sha256(
                        published_rows
                    ) != json_sha256([dict(row) for row in corpus.splits[split]]):
                        raise RuntimeError("WEAVE_VISIBLE_DATASET_READBACK_MISMATCH")
                    references[split] = {
                        "wandb_artifact": artifact_ref,
                        "weave_uri": weave_ref.uri(),
                        "sha256": expected,
                    }
                run.summary.update(
                    {
                        "dataset_references": references,
                        "split_count": len(references),
                    }
                )
            finally:
                run.finish()
        self.dataset_references.update(references)
        return references

    def reuse_visible_datasets(self, receipt: Mapping[str, Any], *, corpus: Corpus) -> None:
        references = receipt.get("references")
        if not isinstance(references, Mapping):
            raise TypeError("VISIBLE_DATASET_RECEIPT_INVALID")
        for split in ("development", "validation", "judge_calibration"):
            reference = references.get(split)
            if (
                not isinstance(reference, Mapping)
                or reference.get("sha256") != corpus.split_sha256[split]
                or not reference.get("weave_uri")
                or not reference.get("wandb_artifact")
            ):
                raise RuntimeError("VISIBLE_DATASET_RECEIPT_MISMATCH")
        self.dataset_references = {
            str(split): {str(key): str(value) for key, value in reference.items()}
            for split, reference in references.items()
        }

    def dataset_reference(self, split: str, *, hidden: bool) -> str:
        expected = "hidden" if hidden else split
        reference = self.dataset_references.get(expected)
        if not reference:
            raise RuntimeError("EVALUATION_DATASET_MUST_BE_PUBLISHED_BEFORE_PROVIDER_CALLS")
        return str(reference["weave_uri"])

    def start_block(self, *, group: Mapping[str, Any], hidden: bool = False) -> WandbBlockObserver:
        if not self.preregistration_reference:
            raise RuntimeError("PREREGISTRATION_MUST_BE_UPLOADED_BEFORE_PROVIDER_CALLS")
        observer = WandbBlockObserver(publisher=self, group=group, hidden=hidden)
        with self.cloud_environment(hidden=hidden):
            return observer.start()

    def start_time_block(
        self, specs: Sequence[AttemptSpec], *, time_block: str
    ) -> CompositeBlockObserver:
        observers = []
        for group, selected in group_specs(specs, time_block=time_block):
            observer = self.start_block(group=group, hidden=bool(group["hidden"]))
            observers.append((observer, selected))
        return CompositeBlockObserver(observers)

    def publish_release_gate(self, path: Path) -> CloudReceipt:
        return self._publish_file(
            path,
            kind="release_gate",
            project=self.project,
            artifact_type="assistant-release-basis",
        )

    def publish_campaign_tables(
        self,
        directory: Path,
        *,
        release_gate_reference: str,
    ) -> CloudReceipt:
        """Publish all arms and failures as typed Tables plus one evidence artifact."""

        import wandb  # type: ignore[import-not-found]

        paths = sorted(directory.glob("*.json"))
        if not paths:
            raise ValueError("campaign table package is empty")
        manifest = {
            "schema_version": "1.0",
            "campaign_version": CAMPAIGN_VERSION,
            "files": {path.name: file_sha256(path) for path in paths},
            "release_gate_reference": release_gate_reference,
            "hidden_attempt_rows_included": False,
        }
        digest = json_sha256(manifest)
        run_id = _campaign_run_id(
            campaign_instance_sha256=self.campaign_instance_sha256,
            kind="campaign_tables",
            entity=self.entity,
            project=self.project,
            identity=digest,
        )
        with self.cloud_environment():
            run = wandb.init(
                entity=self.entity,
                project=self.project,
                id=run_id,
                resume="allow",
                name=f"campaign-tables-{digest[:12]}",
                job_type="evidence_guide_v2_campaign_summary",
                config={
                    "campaign_version": CAMPAIGN_VERSION,
                    "campaign_instance_sha256": self.campaign_instance_sha256,
                    "package_sha256": digest,
                    "release_gate_reference": release_gate_reference,
                },
                reinit="create_new",
                settings=_wandb_run_settings(wandb),
            )
            try:
                run.use_artifact(self.preregistration_reference)
                run.use_artifact(release_gate_reference)
                for path in paths:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    rows = value.get("rows") if isinstance(value, Mapping) else None
                    if not isinstance(rows, list) or not rows:
                        continue
                    columns = sorted({str(key) for row in rows for key in row})
                    table = wandb.Table(columns=columns)
                    for row in rows:
                        table.add_data(*(_safe(row.get(column)) for column in columns))
                    run.log({f"campaign/{path.stem}": table})
                artifact = wandb.Artifact(
                    name=f"evidence-guide-v2-campaign-{digest[:16]}",
                    type="assistant-evaluation-campaign",
                    metadata=manifest,
                )
                for path in paths:
                    artifact.add_file(str(path), name=path.name)
                logged = run.log_artifact(artifact)
                logged.wait()
                reference = _logged_artifact_reference(
                    logged,
                    entity=self.entity,
                    project=self.project,
                )
                with tempfile.TemporaryDirectory(
                    prefix="gi-evidence-v2-tables-readback-"
                ) as temporary:
                    downloaded = Path(run.use_artifact(reference).download(root=temporary))
                    readback_files = {
                        path.name: file_sha256(downloaded / path.name) for path in paths
                    }
                if readback_files != manifest["files"]:
                    raise RuntimeError("WANDB_CAMPAIGN_TABLE_READBACK_HASH_MISMATCH")
                run.summary.update(
                    {
                        "package_sha256": digest,
                        "artifact_reference": reference,
                        "hidden_attempt_rows_included": False,
                    }
                )
                receipt = CloudReceipt(
                    kind="campaign_tables",
                    entity=self.entity,
                    project=self.project,
                    run_id=str(run.id),
                    run_url=str(getattr(run, "url", "") or ""),
                    artifact_id=str(logged.id),
                    artifact_reference=reference,
                    source_sha256=digest,
                    readback_sha256=json_sha256({**manifest, "files": readback_files}),
                )
            finally:
                run.finish()
        return receipt

    def publish_architecture_development_study(self, directory: Path) -> CloudReceipt:
        """Publish exploratory Tables/artifacts with no release-gate input or output."""

        import wandb  # type: ignore[import-not-found]

        if not self.preregistration_reference:
            raise RuntimeError("PREREGISTRATION_MUST_BE_UPLOADED_BEFORE_DEVELOPMENT_PUBLICATION")
        paths = sorted(directory.glob("*.json"))
        if not paths:
            raise ValueError("architecture-development package is empty")
        if any(
            "release-gate" in path.name.lower() or "release_basis" in path.name.lower()
            for path in paths
        ):
            raise ValueError("development package may not contain release-gate material")
        files = {path.name: file_sha256(path) for path in paths}
        row_counts: dict[str, int] = {}
        loaded: dict[str, Mapping[str, Any]] = {}
        for path in paths:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise TypeError("development package JSON must contain objects")
            safe_value = _safe(value)
            loaded[path.name] = safe_value
            rows = safe_value.get("rows")
            if isinstance(rows, list):
                row_counts[path.stem] = len(rows)
        manifest = {
            "schema_version": "1.0",
            "campaign_version": CAMPAIGN_VERSION,
            "kind": "architecture_development_study",
            "files": files,
            "row_counts": row_counts,
            "release_eligible": False,
            "release_gate_consumed": False,
            "assistant_release_basis_emitted": False,
            "hidden_attempt_rows_included": False,
        }
        digest = json_sha256(manifest)
        run_id = _campaign_run_id(
            campaign_instance_sha256=self.campaign_instance_sha256,
            kind="architecture_development_study",
            entity=self.entity,
            project=self.project,
            identity=digest,
        )
        with self.cloud_environment():
            run = wandb.init(
                entity=self.entity,
                project=self.project,
                id=run_id,
                resume="allow",
                name=f"architecture-development-{digest[:12]}",
                job_type="evidence_guide_v2_architecture_development",
                config={
                    "campaign_version": CAMPAIGN_VERSION,
                    "campaign_instance_sha256": self.campaign_instance_sha256,
                    "package_sha256": digest,
                    "release_eligible": False,
                    "release_gate_consumed": False,
                },
                reinit="create_new",
                settings=_wandb_run_settings(wandb),
            )
            try:
                run.use_artifact(self.preregistration_reference)
                for path in paths:
                    rows = loaded[path.name].get("rows")
                    if not isinstance(rows, list) or not rows:
                        continue
                    if any(not isinstance(row, Mapping) for row in rows):
                        raise TypeError("development Table rows must contain objects")
                    columns = sorted({str(key) for row in rows for key in row})
                    table = wandb.Table(columns=columns)
                    for row in rows:
                        table.add_data(*(_safe(row.get(column)) for column in columns))
                    run.log({f"architecture-development/{path.stem}": table})
                artifact = wandb.Artifact(
                    name=f"evidence-guide-v2-architecture-development-{digest[:16]}",
                    type="research-development-evidence",
                    metadata=manifest,
                )
                for path in paths:
                    artifact.add_file(str(path), name=path.name)
                logged = run.log_artifact(artifact)
                logged.wait()
                reference = _logged_artifact_reference(
                    logged,
                    entity=self.entity,
                    project=self.project,
                )
                with tempfile.TemporaryDirectory(
                    prefix="gi-evidence-v2-development-readback-"
                ) as temporary:
                    downloaded = Path(run.use_artifact(reference).download(root=temporary))
                    readback_files = {
                        path.name: file_sha256(downloaded / path.name) for path in paths
                    }
                if readback_files != files:
                    raise RuntimeError(
                        "WANDB_ARCHITECTURE_DEVELOPMENT_READBACK_HASH_MISMATCH"
                    )
                receipt_payload = {
                    **manifest,
                    "files": readback_files,
                }
                run.summary.update(
                    {
                        "package_sha256": digest,
                        "artifact_reference": reference,
                        "release_eligible": False,
                        "release_gate_consumed": False,
                        "table_count": len(row_counts),
                    }
                )
                receipt = CloudReceipt(
                    kind="architecture_development_study",
                    entity=self.entity,
                    project=self.project,
                    run_id=str(run.id),
                    run_url=str(getattr(run, "url", "") or ""),
                    artifact_id=str(logged.id),
                    artifact_reference=reference,
                    source_sha256=digest,
                    readback_sha256=json_sha256(receipt_payload),
                )
            finally:
                run.finish()
        return receipt


def group_specs(
    specs: Sequence[AttemptSpec], *, time_block: str
) -> list[tuple[dict[str, Any], list[AttemptSpec]]]:
    groups: dict[tuple[Any, ...], list[AttemptSpec]] = {}
    for spec in specs:
        if spec.time_block != time_block:
            continue
        key = (
            spec.stage,
            spec.split,
            spec.model_id,
            spec.architecture_id,
            spec.prompt_id,
            spec.max_output_tokens,
            spec.repeat,
            spec.time_block,
            spec.hidden,
        )
        groups.setdefault(key, []).append(spec)
    output = []
    # ``stage_attempts`` is shuffled once with the preregistered seed.  Preserve
    # each group's first appearance in that frozen schedule instead of sorting
    # configurations alphabetically, which would systematically put the same
    # model/arm first in every campaign block.  Dict insertion order is stable,
    # so resumes reconstruct the identical randomized configuration order.
    for key, values in groups.items():
        stage, split, model, architecture, prompt, token_limit, repeat, block, hidden = key
        output.append(
            (
                {
                    "stage": stage,
                    "split": split,
                    "model_id": model,
                    "model_slug": re.sub(r"[^A-Za-z0-9._-]", "--", model),
                    "architecture_id": architecture,
                    "prompt_id": prompt,
                    "max_output_tokens": token_limit,
                    "repeat": repeat,
                    "time_block": block,
                    "hidden": hidden,
                },
                values,
            )
        )
    return output


class CompositeBlockObserver(AttemptObserver):
    """Route each attempt to its exact variant/model/replicate W&B Run."""

    def __init__(
        self,
        observers: Sequence[tuple[LocalBlockObserver | WandbBlockObserver, Sequence[AttemptSpec]]],
    ) -> None:
        self.observers = [item[0] for item in observers]
        self.by_attempt: dict[str, LocalBlockObserver | WandbBlockObserver] = {}
        for observer, specs in observers:
            for spec in specs:
                if spec.attempt_id in self.by_attempt:
                    raise RuntimeError("attempt is assigned to multiple observability blocks")
                self.by_attempt[spec.attempt_id] = observer

    @contextmanager
    def attempt(self, spec: AttemptSpec, public_input: Mapping[str, Any]) -> Iterator[Any]:
        with self.by_attempt[spec.attempt_id].attempt(spec, public_input) as recorder:
            yield recorder

    def log_outcome(self, spec: AttemptSpec, outcome: AttemptOutcome) -> None:
        self.by_attempt[spec.attempt_id].log_outcome(spec, outcome)

    def finish(self) -> list[dict[str, Any]]:
        return [observer.finish() for observer in self.observers]
