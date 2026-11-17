"""Protected Eco W&B/Weave release-gate entry point.

This script intentionally stops before importing either SDK unless a supported
exact-scope resolver exists in source.  W&B's current Public Project API does
not expose authoritative Restricted visibility, so a service-account upload,
successful login, or legacy ``PRIVATE`` value is not acceptable evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from ecocorridor_core.assistants import APPROVED_TRACE_STEPS, AssistantRunAssociation
from ecocorridor_core.observability import (
    ECOCORRIDOR_APP_PROFILE,
    WeaveNestedTraceSink,
    protected_integration_scope_resolver,
)
from scenario_platform.cloud_sync import (
    ArtifactProjection,
    ProjectCloudDirectories,
    TableProjection,
    TelemetryProjection,
    WandbSdkLineageUploader,
    WandbTarget,
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"PROTECTED_INTEGRATION_CONFIGURATION_MISSING:{name}")
    return value


def _write_report(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    entity = _required("WANDB_ENTITY")
    project = _required("WANDB_PROJECT")
    deployment = _required("WANDB_DEPLOYMENT")
    api_base_url = _required("WANDB_BASE_URL")
    _required("WANDB_API_KEY")
    resolver = protected_integration_scope_resolver(deployment)
    try:
        evidence = resolver.resolve(
            entity=entity,
            project=project,
            deployment=deployment,
            api_base_url=api_base_url,
        )
    except RuntimeError as exc:
        report = {
            "status": "BLOCKED",
            "code": "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE",
            "deployment": deployment,
            "entity": entity,
            "project": project,
            "next_action": (
                "Implement and review a provider backed by a documented exact-scope "
                "W&B endpoint; do not substitute access, PRIVATE, membership, or a "
                "manual checkbox."
            ),
        }
        _write_report(arguments.report, report)
        print(json.dumps(report, sort_keys=True))
        raise SystemExit(78) from exc
    web_base_url = os.environ.get("WANDB_WEB_BASE_URL", api_base_url).strip()
    target = WandbTarget(
        entity=entity,
        project=project,
        external_project_id=evidence.external_project_id,
        deployment=deployment,
        api_base_url=api_base_url,
        web_base_url=web_base_url,
        scope_evidence=evidence,
    )
    with tempfile.TemporaryDirectory(prefix="ecocorridor-observability-gate-") as temporary:
        root = Path(temporary)
        source = root / "evidence" / "probe.json"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps(
                {
                    "fixture": "CC0 synthetic observability probe",
                    "scientific_claim": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        import hashlib

        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        projection = TelemetryProjection(
            profile_namespace=ECOCORRIDOR_APP_PROFILE.namespace,
            project_id="protected_integration",
            run_kind="release_gate",
            run_subject_id="protected_observability_roundtrip",
            event_type="release_gate",
            event_sequence=1,
            config={"synthetic_probe": True},
            summary={"scientific_claim": False},
            tables=[
                TableProjection(
                    logical_name="release_gate",
                    columns=["check", "passed"],
                    rows=[["local_source_hash", True]],
                )
            ],
            artifacts=[
                ArtifactProjection(
                    local_artifact_id="protected_probe",
                    artifact_type="decision_evidence",
                    logical_id="protected_observability_probe",
                    logical_name="probe/probe.json",
                    source_relative_path="evidence/probe.json",
                    sha256=digest,
                    bytes=source.stat().st_size,
                    media_type="application/json",
                )
            ],
        )
        directories = ProjectCloudDirectories.create(root, ECOCORRIDOR_APP_PROFILE)
        receipt = WandbSdkLineageUploader(
            profile=ECOCORRIDOR_APP_PROFILE,
            scope_resolver=resolver,
        ).upload(
            envelope_id="protected_observability_roundtrip",
            idempotency_key="protected_observability_roundtrip",
            projection=projection,
            target=target,
            project_root=root,
            directories=directories,
        )
        trace = WeaveNestedTraceSink(
            target=target,
            scope_resolver=resolver,
            project_root=root,
        )
        call_id = "protected_assistant_trace_roundtrip"
        association = AssistantRunAssociation.create(
            project_id=projection.project_id,
            wandb_external_project_id=target.external_project_id,
            wandb_run_id=receipt.run_id,
            wandb_run_kind=projection.run_kind,
            run_subject_id=projection.run_subject_id,
        )
        trace.start(
            call_id=call_id,
            operation="assistant.request",
            inputs={
                "synthetic_probe": True,
                "wandb_association": association.model_dump(mode="json"),
            },
        )
        for step in APPROVED_TRACE_STEPS:
            trace.step(
                call_id=call_id,
                name=step,
                inputs={"synthetic_probe": True},
                output={"passed": True},
            )
        trace.finish(call_id=call_id, output={"scientific_claim": False})
        trace_id = trace.flush(call_id=call_id)
        readback = trace.readback(trace_id=trace_id)
        if (
            readback.steps != list(APPROVED_TRACE_STEPS)
            or not readback.completed
            or readback.association != association
        ):
            raise SystemExit("protected Weave trace readback failed")
    report = {
        "status": "PASSED",
        "entity": entity,
        "project": project,
        "run_id": receipt.run_id,
        "artifact_count": len(receipt.artifacts),
        "trace_readback": True,
    }
    _write_report(arguments.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
