from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from ecocorridor_core.assistant_runtime import ProviderBackedAssistantRuntime
from ecocorridor_core.assistants import AssistantProviderConfiguration
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.observability import (
    RIDGEWAY_SYNTHETIC_TRACE_CONSENT,
    RIDGEWAY_SYNTHETIC_TRACE_PAYLOAD_CLASSES,
    EcoRpcObservabilityAdapter,
    SyntheticTraceConsentBinding,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.rpc_response_contracts import ObservabilityBindingData
from ecocorridor_core.rpc_worker import (
    ObservabilityPreflightParams,
    RPCDispatcher,
    RPCFault,
    SyntheticTracePreflightParams,
)
from pydantic import ValidationError
from scenario_platform.cloud_sync import InMemoryCredentialSession
from scenario_platform.observability import (
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
)

SHA = "a" * 64


class _ExactScopeProvider:
    def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
        return ProjectScopeEvidence(
            entity=request["entity"],
            project=request["project"],
            external_project_id="external-project-1",
            deployment=request["deployment"],
            api_base_url=request["api_base_url"],
            scope="RESTRICTED",
            evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
            provider_field="documented_admin.project_scope",
        )


class _ObservabilityAdapter:
    def validate(self, *, binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "external_project_id": binding["scope_evidence"]["external_project_id"],
            "profile_namespace": "ecocorridor",
            "verified": True,
        }


class _AssistantRuntime:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def configure(
        self,
        *,
        project_root: Path,
        configuration: AssistantProviderConfiguration,
        api_key: str,
    ) -> dict[str, Any]:
        del api_key
        self.calls.append(project_root)
        return {
            "configured": True,
            "provider": {
                "base_url": configuration.base_url,
                "model": configuration.model,
                "timeout_seconds": configuration.timeout_seconds,
            },
            "project_id": project_root.name,
            "wandb_external_project_id": "external-project-1",
            "trace_required": True,
        }


def _project(root: Path, mode: str) -> None:
    store = EcoProjectStore(root)
    try:
        store.initialize(
            project_id=root.name,
            title=f"{mode.title()} project",
            mode=mode,
            skeleton={"test": True},
        )
    finally:
        store.close()


def _professional_preflight_payload(**updates: Any) -> dict[str, Any]:
    payload = {
        "entity": "eco-team",
        "project": "restricted-project",
        "deployment": "WANDB_CLOUD",
        "api_base_url": "https://api.wandb.ai",
        "web_base_url": "https://wandb.ai",
        "api_key": "not-a-real-wandb-key-value",
    }
    payload.update(updates)
    return payload


def _synthetic_preflight_payload(**updates: Any) -> dict[str, Any]:
    payload = {
        "project_directory": "/tmp/guided",
        "entity": "eco-team",
        "project": "trace-project",
        "deployment": "WANDB_CLOUD",
        "api_base_url": "https://api.wandb.ai",
        "web_base_url": "https://wandb.ai",
        "consent": RIDGEWAY_SYNTHETIC_TRACE_CONSENT,
        "api_key": "not-a-real-wandb-key-value",
    }
    payload.update(updates)
    return payload


def _synthetic_binding(**updates: Any) -> SyntheticTraceConsentBinding:
    payload = {
        "entity": "eco-team",
        "project": "trace-project",
        "external_project_id": "external-project-1",
        "deployment": "WANDB_CLOUD",
        "api_base_url": "https://api.wandb.ai",
        "web_base_url": "https://wandb.ai",
        "wandb_run_id": "ecocorridor-guided-session-guided-ridgeway",
        "projection_sha256": SHA,
        "manifest_sha256": "b" * 64,
        "archive_sha256": "c" * 64,
        "consent_sha256": canonical_sha256(RIDGEWAY_SYNTHETIC_TRACE_CONSENT),
        "allowed_payload_classes": RIDGEWAY_SYNTHETIC_TRACE_PAYLOAD_CLASSES,
        "first_connected_at": datetime(2026, 8, 6, tzinfo=UTC),
    }
    payload.update(updates)
    return SyntheticTraceConsentBinding.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ObservabilityPreflightParams,
            _professional_preflight_payload(api_base_url="https://api.wandb.example"),
        ),
        (
            ObservabilityPreflightParams,
            _professional_preflight_payload(web_base_url="https://api.wandb.ai"),
        ),
        (
            SyntheticTracePreflightParams,
            _synthetic_preflight_payload(api_base_url="https://api.wandb.example"),
        ),
        (
            SyntheticTracePreflightParams,
            _synthetic_preflight_payload(web_base_url="https://api.wandb.ai"),
        ),
    ],
)
def test_wandb_cloud_params_require_official_api_and_web_origins(
    model: type[Any], payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="official https://api.wandb.ai"):
        model.model_validate(payload)


def test_cloud_defaults_official_web_origin_and_custom_deployments_remain_supported() -> None:
    cloud = ObservabilityPreflightParams.model_validate(
        _professional_preflight_payload(web_base_url=None)
    )
    assert cloud.api_base_url == "https://api.wandb.ai"
    assert cloud.web_base_url == "https://wandb.ai"

    dedicated = ObservabilityPreflightParams.model_validate(
        _professional_preflight_payload(
            deployment="DEDICATED",
            api_base_url="https://api.wandb.example/platform",
            web_base_url="https://wandb.example/app",
        )
    )
    assert dedicated.api_base_url == "https://api.wandb.example/platform"
    assert dedicated.web_base_url == "https://wandb.example/app"


def test_persisted_professional_and_synthetic_contracts_reject_cloud_lookalikes() -> None:
    scope = ProjectScopeEvidence(
        entity="eco-team",
        project="restricted-project",
        external_project_id="external-project-1",
        deployment="WANDB_CLOUD",
        api_base_url="https://api.wandb.example",
        scope="RESTRICTED",
        evidence_kind=ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
        provider_field="documented.project.scope",
    )
    with pytest.raises(ValidationError, match="official https://api.wandb.ai"):
        ObservabilityBindingData.model_validate(
            {
                "state": "CONNECTED",
                "binding_id": "binding-1",
                "entity": scope.entity,
                "project": scope.project,
                "deployment": scope.deployment,
                "api_base_url": scope.api_base_url,
                "web_base_url": "https://wandb.example",
                "profile_namespace": "ecocorridor",
                "scope_evidence": scope.model_dump(mode="json"),
                "verified": True,
                "external_project_id": scope.external_project_id,
            }
        )

    with pytest.raises(ValidationError, match="official https://api.wandb.ai"):
        _synthetic_binding(
            api_base_url="https://api.wandb.example",
            web_base_url="https://wandb.example",
        )


def test_adapters_reject_cloud_lookalikes_before_credentials_or_transport() -> None:
    scope = ProjectScopeEvidence(
        entity="eco-team",
        project="restricted-project",
        external_project_id="external-project-1",
        deployment="WANDB_CLOUD",
        api_base_url="https://api.wandb.example",
        scope="RESTRICTED",
        evidence_kind=ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
        provider_field="documented.project.scope",
    )
    with pytest.raises(ValueError, match="official https://api.wandb.ai"):
        EcoRpcObservabilityAdapter.target_from_binding(
            {
                "entity": scope.entity,
                "project": scope.project,
                "deployment": scope.deployment,
                "api_base_url": scope.api_base_url,
                "web_base_url": "https://wandb.example",
                "external_project_id": scope.external_project_id,
                "scope_evidence": scope.model_dump(mode="json"),
            }
        )

    adapter = EcoRpcObservabilityAdapter(
        scope_resolver=ProjectScopeResolver(),
        store_factory=cast(Any, lambda _path: None),
    )
    synthetic_candidate = _synthetic_binding(
        deployment="DEDICATED",
        api_base_url="https://api.wandb.example",
        web_base_url="https://wandb.example",
    ).model_dump(mode="json", exclude={"external_project_id", "first_connected_at"})
    synthetic_candidate["deployment"] = "WANDB_CLOUD"
    with pytest.raises(RuntimeError, match="WANDB_DEPLOYMENT_ORIGIN_MISMATCH"):
        adapter.validate_synthetic_trace_target(binding=synthetic_candidate)


def test_professional_preflight_and_connect_are_blocked_for_guided_projects(
    tmp_path: Path,
) -> None:
    guided = tmp_path / "guided"
    _project(guided, "GUIDED")
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=_ObservabilityAdapter(),
        scope_resolver=ProjectScopeResolver(_ExactScopeProvider()),
    )

    with pytest.raises(RPCFault) as preflight_error:
        dispatcher.dispatch(
            "observability.preflight",
            _professional_preflight_payload(
                project_directory=str(guided),
                deployment="DEDICATED",
                api_base_url="https://api.wandb.example",
                web_base_url="https://wandb.example",
            ),
        )
    assert preflight_error.value.error.code == "PROFESSIONAL_CAPABILITY_GUIDED_PROJECT_BLOCKED"

    destination = dispatcher.dispatch(
        "observability.preflight",
        _professional_preflight_payload(
            deployment="DEDICATED",
            api_base_url="https://api.wandb.example",
            web_base_url="https://wandb.example",
        ),
    )["data"]
    with pytest.raises(RPCFault) as connect_error:
        dispatcher.dispatch(
            "observability.connect",
            {
                "project_directory": str(guided),
                "preflight_id": destination["preflight_id"],
            },
        )
    assert connect_error.value.error.code == "PROFESSIONAL_CAPABILITY_GUIDED_PROJECT_BLOCKED"


@pytest.mark.parametrize("mode", ["IMPORTED", "PLANNING"])
def test_professional_observability_remains_available_for_professional_modes(
    tmp_path: Path, mode: str
) -> None:
    root = tmp_path / mode.casefold()
    _project(root, mode)
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        observability_adapter=_ObservabilityAdapter(),
        scope_resolver=ProjectScopeResolver(_ExactScopeProvider()),
    )
    destination = dispatcher.dispatch(
        "observability.preflight",
        _professional_preflight_payload(
            project_directory=str(root),
            deployment="DEDICATED",
            api_base_url="https://api.wandb.example",
            web_base_url="https://wandb.example",
        ),
    )["data"]
    connected = dispatcher.dispatch(
        "observability.connect",
        {
            "project_directory": str(root),
            "preflight_id": destination["preflight_id"],
        },
    )["data"]
    assert connected["binding"]["deployment"] == "DEDICATED"
    assert connected["binding"]["external_project_id"] == "external-project-1"


def test_professional_assistant_is_blocked_for_guided_but_unchanged_for_professional_modes(
    tmp_path: Path,
) -> None:
    runtime = _AssistantRuntime()
    dispatcher = RPCDispatcher(data_root=tmp_path, assistant_runtime=cast(Any, runtime))
    configuration = {
        "base_url": "https://provider.example/v1",
        "model": "reviewed-model",
    }

    guided = tmp_path / "guided"
    _project(guided, "GUIDED")
    with pytest.raises(RPCFault) as guided_error:
        dispatcher.dispatch(
            "assistant.configure",
            {
                "project_directory": str(guided),
                "configuration": configuration,
                "api_key": "not-a-real-provider-key",
            },
        )
    assert guided_error.value.error.code == "PROFESSIONAL_CAPABILITY_GUIDED_PROJECT_BLOCKED"
    assert runtime.calls == []

    for mode in ("IMPORTED", "PLANNING"):
        root = tmp_path / mode.casefold()
        _project(root, mode)
        configured = dispatcher.dispatch(
            "assistant.configure",
            {
                "project_directory": str(root),
                "configuration": configuration,
                "api_key": "not-a-real-provider-key",
            },
        )["data"]
        assert configured["configured"] is True
    assert runtime.calls == [tmp_path / "imported", tmp_path / "planning"]


def test_synthetic_and_professional_runtime_sessions_cannot_coexist(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    runtime = ProviderBackedAssistantRuntime(
        scope_resolver=ProjectScopeResolver(),
        observability_credentials=InMemoryCredentialSession(environment_key="WANDB_API_KEY"),
    )
    runtime._sessions[root.resolve()] = cast(Any, object())

    with pytest.raises(PermissionError, match="ASSISTANT_CAPABILITY_SESSION_CONFLICT"):
        runtime.configure_synthetic_review_brief(
            project_root=root,
            target=cast(Any, None),
            configuration=AssistantProviderConfiguration(
                base_url="https://provider.example/v1",
                model="reviewed-model",
            ),
            api_key="not-a-real-provider-key",
        )


def test_runtime_professional_verification_rejects_guided_projects(tmp_path: Path) -> None:
    root = tmp_path / "guided"
    _project(root, "GUIDED")
    runtime = ProviderBackedAssistantRuntime(
        scope_resolver=ProjectScopeResolver(),
        observability_credentials=InMemoryCredentialSession(environment_key="WANDB_API_KEY"),
    )
    with pytest.raises(PermissionError, match="PROFESSIONAL_CAPABILITY_GUIDED_PROJECT_BLOCKED"):
        runtime.verify_project(root)
