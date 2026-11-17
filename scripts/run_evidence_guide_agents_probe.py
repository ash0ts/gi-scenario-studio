"""Run the synthetic Evidence Guide against real W&B Inference and Weave Agents.

This is a manual, secret-bearing integration probe.  It never changes the
checked-in production release gate: an enabled gate is assembled in memory and
is explicitly labelled test-only in the resulting receipt.  Credentials are
read only from the process environment.

The receipt intentionally excludes provider output and validated answer text.
It records whether an answer was released, and proves rejected turns remained
fail-closed, without copying rejected model prose into another artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from gi_scenario_core.contracts import ObservabilityBinding, ProjectDataPolicy
from gi_scenario_core.evidence_guide import (
    AGENT_ID,
    AGENT_VERSION,
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    SUPPORTED_MODELS,
    TOOL_CONTRACT_VERSION,
    VALIDATOR_VERSION,
)
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_GATE = ROOT / "benchmarks" / "evidence_guide" / "v1" / "release-gate.json"
WANDB_CLOUD_API = "https://api.wandb.ai"
WANDB_CLOUD_WEB = "https://wandb.ai"


class Dispatcher(Protocol):
    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


class AgentTurnClient(Protocol):
    def get_agent_turn(self, *, trace_id: str) -> Any: ...

    def get_agent_spans(
        self,
        *,
        query: Any,
        limit: int | None = None,
    ) -> Any: ...


ReferenceReader = Callable[[str, str, str], dict[str, Any] | None]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write one credential-free receipt without exposing a partial document."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("the integration-probe receipt path cannot be a symbolic link")
    payload = json.dumps(value, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _test_only_enabled_release_gate(model_id: str) -> dict[str, Any]:
    """Build an exact, pinned in-memory override without mutating production data."""

    if model_id not in SUPPORTED_MODELS:
        raise ValueError(f"unsupported Evidence Guide candidate: {model_id}")
    payload = json.loads(PRODUCTION_GATE.read_text(encoding="utf-8"))
    payload.update(
        {
            "enabled": True,
            "selected_model": model_id,
            "decision": "ENABLE_LIVE_GUIDE",
            "reasons": [],
            "selected_prompt_version": PROMPT_VERSION,
            "selected_response_schema_version": RESPONSE_SCHEMA_VERSION,
            "selected_response_schema_sha256": RESPONSE_SCHEMA_SHA256,
            "selected_tool_contract_version": TOOL_CONTRACT_VERSION,
            "selected_validator_version": VALIDATOR_VERSION,
            "claim_boundary": (
                "TEST-ONLY synthetic integration override. It does not alter or satisfy "
                "the production Evidence Guide release gate."
            ),
        }
    )
    return payload


def _field(value: Any, name: str) -> tuple[bool, Any]:
    if isinstance(value, dict):
        return name in value, value.get(name)
    if hasattr(value, name):
        return True, getattr(value, name)
    return False, None


def _agent_span_parentage(
    client: AgentTurnClient,
    *,
    trace_id: str,
    root_span_id: str,
) -> dict[str, Any]:
    """Verify each tool is a direct child of a routing chat span when exposed."""

    get_spans = getattr(client, "get_agent_spans", None)
    if not callable(get_spans):
        return {
            "tool_parentage_verified": None,
            "tool_parentage_reason": "agent_span_parent_ids_unavailable",
            "agent_span_count": None,
            "tool_span_count": None,
            "routing_parent_count": None,
        }
    query = {
        "$expr": {
            "$eq": [
                {"$getField": "trace_id"},
                {"$literal": trace_id},
            ]
        }
    }
    spans: list[Any] = []
    for attempt in range(3):
        spans = list(get_spans(query=query, limit=100))
        if spans:
            break
        if attempt < 2:
            time.sleep(0.15)
    if not spans:
        raise RuntimeError("WEAVE_PROBE_AGENT_SPANS_UNAVAILABLE")

    def text(span: Any, field_name: str) -> str:
        return str(_field(span, field_name)[1] or "")

    by_id = {text(span, "span_id"): span for span in spans if text(span, "span_id")}
    root = by_id.get(root_span_id)
    if root is None or text(root, "parent_span_id"):
        raise RuntimeError("WEAVE_PROBE_ROOT_SPAN_MISMATCH")
    tool_spans = [
        span
        for span in spans
        if text(span, "operation_name") == "execute_tool" or text(span, "tool_name")
    ]
    if not tool_spans:
        raise RuntimeError("WEAVE_PROBE_TOOL_SPANS_MISSING")
    parent_ids = {text(span, "parent_span_id") for span in tool_spans}
    parent_spans = [by_id.get(parent_id) for parent_id in parent_ids]
    if (
        parent_ids
        and root_span_id not in parent_ids
        and all(parent is None for parent in parent_spans)
    ):
        # Weave 0.53 can expose the tool's non-root parent ID while omitting
        # a routing-only chat span from both Agent Spans and Agent Turn
        # projections. Do not infer its kind: retain tool activity and report
        # that exact cloud parentage could not be independently established.
        return {
            "tool_parentage_verified": None,
            "tool_parentage_reason": "routing_parent_span_not_exposed_by_sdk",
            "agent_span_count": len(spans),
            "tool_span_count": len(tool_spans),
            "routing_parent_count": len(parent_ids),
        }
    parentage_verified = bool(
        all(parent_spans)
        and all(text(parent, "operation_name") == "chat" for parent in parent_spans)
        and all(text(parent, "parent_span_id") == root_span_id for parent in parent_spans)
    )
    if not parentage_verified:
        raise RuntimeError("WEAVE_PROBE_TOOL_PARENT_NOT_ROUTING_CHAT")
    return {
        "tool_parentage_verified": True,
        "tool_parentage_reason": None,
        "agent_span_count": len(spans),
        "tool_span_count": len(tool_spans),
        "routing_parent_count": len(parent_ids),
    }


def _agent_readback(
    client: AgentTurnClient,
    trace_id: str,
    root_span_id: str,
) -> dict[str, Any]:
    """Read and validate the root turn independently through the Weave SDK."""

    value = client.get_agent_turn(trace_id=trace_id)
    if value is None:
        raise RuntimeError("WEAVE_PROBE_TRACE_NOT_ACKNOWLEDGED")
    present, returned_trace_id = _field(value, "trace_id")
    if not present or str(returned_trace_id or "") != trace_id:
        raise RuntimeError("WEAVE_PROBE_TRACE_ID_MISMATCH")

    required_identity: dict[str, str] = {
        "agent_name": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "root_span_name": AGENT_ID,
    }
    identity: dict[str, str] = {}
    for key, expected in required_identity.items():
        identity_present, actual = _field(value, key)
        if not identity_present or str(actual or "") != expected:
            raise RuntimeError(f"WEAVE_PROBE_{key.upper()}_MISMATCH")
        identity[key] = str(actual)

    status_present, status = _field(value, "status_code")
    if not status_present or str(status or "") not in {"ERROR", "OK", "UNSET"}:
        raise RuntimeError("WEAVE_PROBE_ROOT_STATUS_UNAVAILABLE")
    messages_present, messages = _field(value, "messages")
    if not messages_present or not isinstance(messages, list) or not messages:
        raise RuntimeError("WEAVE_PROBE_MESSAGES_UNAVAILABLE")
    message_types = sorted(
        {
            str(_field(message, "type")[1] or "")
            for message in messages
            if str(_field(message, "type")[1] or "")
        }
    )
    if "user_message" not in message_types:
        raise RuntimeError("WEAVE_PROBE_USER_MESSAGE_MISSING")
    parentage = _agent_span_parentage(
        client,
        trace_id=trace_id,
        root_span_id=root_span_id,
    )
    return {
        "trace_id": trace_id,
        "root_status": str(status),
        "agent_identity": identity,
        "message_types": message_types,
        "message_count": len(messages),
        "tool_activity_observed": "tool_call" in message_types,
        **parentage,
    }


def _repository_reference_reader(
    project_directory: str,
    project_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    repository = ProjectRepository(project_directory)
    try:
        return repository.get_weave_conversation_reference(project_id, turn_id)
    finally:
        repository.close()


def _connect_demo(
    dispatcher: Dispatcher,
    *,
    entity: str,
    external_project: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project = dispatcher.dispatch("project.create", {"kind": "demo"})
    project_id = str(project["id"])
    policy = ProjectDataPolicy(
        project_id=project_id,
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="manual synthetic Evidence Guide integration probe",
    )
    binding = ObservabilityBinding(
        project_id=project_id,
        data_policy_id=policy.policy_id,
        entity=entity,
        external_project=external_project,
        external_project_id="pending-validation",
        api_base_url=WANDB_CLOUD_API,
        web_base_url=WANDB_CLOUD_WEB,
        deployment="wandb_cloud",
        mode="online",
    )
    state = dispatcher.dispatch(
        "observability.connect",
        {
            "scope": "project",
            "project_directory": project["projectDirectory"],
            "policy": policy.model_dump(mode="json"),
            "binding": binding.model_dump(mode="json"),
        },
    )
    verified = state.get("binding") if isinstance(state, dict) else None
    if (
        not isinstance(verified, dict)
        or verified.get("verified") is not True
        or verified.get("access") != "Restricted"
        or verified.get("deployment") != "wandb_cloud"
        or verified.get("entity") != entity
        or verified.get("external_project") != external_project
    ):
        raise RuntimeError("WANDB_PROBE_RESTRICTED_BINDING_UNVERIFIED")
    return project, verified


def _base_request(project: dict[str, Any], alternative_id: str) -> dict[str, Any]:
    return {
        "project_directory": project["projectDirectory"],
        "project_id": project["id"],
        "anchor": {"kind": "alternative", "identifier": alternative_id},
    }


def _start_conversation(
    dispatcher: Dispatcher,
    project: dict[str, Any],
    alternative_id: str,
) -> tuple[str, str]:
    base = _base_request(project, alternative_id)
    capability = dispatcher.dispatch("assistant.get_capabilities", base)
    response = dispatcher.dispatch(
        "assistant.start_conversation",
        {
            **base,
            "inference_consent": True,
            "content_logging_consent": True,
        },
    )
    return str(response["conversation"]["conversation_id"]), str(capability["state"])


def _run_turn(
    dispatcher: Dispatcher,
    *,
    project: dict[str, Any],
    conversation_id: str,
    question_key: str,
    question: str,
    reference_reader: ReferenceReader,
    readback_client: AgentTurnClient,
) -> dict[str, Any]:
    base = {
        "project_directory": project["projectDirectory"],
        "project_id": project["id"],
        "conversation_id": conversation_id,
    }
    before = dispatcher.dispatch("assistant.get_conversation", base)
    prior_count = len(before["turns"])
    response: dict[str, Any] | None = None
    rpc_error: str | None = None
    try:
        response = dispatcher.dispatch("assistant.ask", {**base, "question": question})
    except RPCFault as exc:
        data = exc.data if isinstance(exc.data, dict) else {}
        rpc_error = str(data.get("code") or "EVIDENCE_GUIDE_RPC_FAILED")

    after = dispatcher.dispatch("assistant.get_conversation", base)
    turns = after["turns"]
    if len(turns) != prior_count + 1:
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_TURN_COUNT_MISMATCH")
    turn = turns[-1]
    if turn.get("conversation_id") != conversation_id:
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_CONVERSATION_CHANGED")
    turn_id = str(turn["turn_id"])
    reference = reference_reader(
        str(project["projectDirectory"]),
        str(project["id"]),
        turn_id,
    )
    if not isinstance(reference, dict):
        raise TypeError("EVIDENCE_GUIDE_PROBE_WEAVE_REFERENCE_MISSING")
    for key in ("trace_id", "root_span_id", "url"):
        if not isinstance(reference.get(key), str) or not reference[key]:
            raise RuntimeError(f"EVIDENCE_GUIDE_PROBE_{key.upper()}_MISSING")
    if reference.get("conversation_id") != conversation_id:
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_REFERENCE_CONVERSATION_MISMATCH")

    readback = _agent_readback(
        readback_client,
        str(reference["trace_id"]),
        str(reference["root_span_id"]),
    )
    local_state = str(turn.get("state") or "unknown")
    answer_released = bool(response and isinstance(response.get("answer"), dict))
    if local_state in {"blocked", "failed"}:
        if answer_released:
            raise RuntimeError("EVIDENCE_GUIDE_PROBE_REJECTED_ANSWER_RELEASED")
        if readback["root_status"] != "ERROR":
            raise RuntimeError("EVIDENCE_GUIDE_PROBE_REJECTED_ROOT_NOT_ERROR")
    elif local_state == "completed":
        if not answer_released or readback["root_status"] not in {"OK", "UNSET"}:
            raise RuntimeError("EVIDENCE_GUIDE_PROBE_COMPLETED_TURN_INVALID")
    else:
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_TURN_NOT_TERMINAL")

    error_code = str(turn.get("error_code") or rpc_error or "") or None
    return {
        "sequence": int(turn["sequence"]),
        "question_key": question_key,
        "local_state": local_state,
        "validation_outcome": "rejected" if local_state in {"blocked", "failed"} else "passed",
        "error_code": error_code,
        "answer_released": answer_released,
        "weave_acknowledged": True,
        "trace_id": reference["trace_id"],
        "root_span_id": reference["root_span_id"],
        "weave_url": reference["url"],
        "root_status": readback["root_status"],
        "agent_identity": readback["agent_identity"],
        "message_types": readback["message_types"],
        "message_count": readback["message_count"],
        "tool_activity_observed": readback["tool_activity_observed"],
        "tool_parentage_verified": readback["tool_parentage_verified"],
        "tool_parentage_reason": readback["tool_parentage_reason"],
        "agent_span_count": readback["agent_span_count"],
        "tool_span_count": readback["tool_span_count"],
        "routing_parent_count": readback["routing_parent_count"],
    }


def run_probe_flow(
    dispatcher: Dispatcher,
    *,
    entity: str,
    external_project: str,
    model_id: str,
    readback_client: AgentTurnClient,
    reference_reader: ReferenceReader = _repository_reference_reader,
) -> dict[str, Any]:
    """Execute three turns while preserving the intended conversation boundaries."""

    project, verified_binding = _connect_demo(
        dispatcher,
        entity=entity,
        external_project=external_project,
    )
    alt_017_conversation, alt_017_capability = _start_conversation(
        dispatcher, project, "ALT-017"
    )
    alt_017_turns = [
        _run_turn(
            dispatcher,
            project=project,
            conversation_id=alt_017_conversation,
            question_key="tradeoff_vs_acceptance",
            question="Why can this be nondominated and still fail?",
            reference_reader=reference_reader,
            readback_client=readback_client,
        ),
        _run_turn(
            dispatcher,
            project=project,
            conversation_id=alt_017_conversation,
            question_key="controlling_storm",
            question="Which selected storm controls this failure?",
            reference_reader=reference_reader,
            readback_client=readback_client,
        ),
    ]

    alt_207_conversation, alt_207_capability = _start_conversation(
        dispatcher, project, "ALT-207"
    )
    if alt_207_conversation == alt_017_conversation:
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_CLEAN_CONVERSATION_REQUIRED")
    alt_207_turns = [
        _run_turn(
            dispatcher,
            project=project,
            conversation_id=alt_207_conversation,
            question_key="ecological_boundary",
            question="Does ALT-207 prove ecological benefit?",
            reference_reader=reference_reader,
            readback_client=readback_client,
        )
    ]

    all_turns = [*alt_017_turns, *alt_207_turns]
    rejected = [item for item in all_turns if item["validation_outcome"] == "rejected"]
    if any(item["answer_released"] for item in rejected):
        raise RuntimeError("EVIDENCE_GUIDE_PROBE_REJECTED_ANSWER_RELEASED")
    receipt = {
        "schema_version": "1.0",
        "receipt_type": "evidence_guide_agents_manual_integration_probe",
        "environment": "temporary_synthetic_cedar_trace",
        "test_only": True,
        "production_release_gate": {
            "path": "benchmarks/evidence_guide/v1/release-gate.json",
            "sha256": _sha256(PRODUCTION_GATE),
            "decision": json.loads(PRODUCTION_GATE.read_text(encoding="utf-8"))["decision"],
            "overridden_in_memory": True,
            "unchanged_on_disk": True,
        },
        "test_only_gate": {
            "model_id": model_id,
            "prompt_version": PROMPT_VERSION,
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "response_schema_sha256": RESPONSE_SCHEMA_SHA256,
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "purpose": "manual synthetic W&B Inference and Weave Agents integration probe only",
        },
        "destination": {
            "entity": entity,
            "project": external_project,
            "access": "RESTRICTED",
            "deployment": "wandb_cloud",
            "binding_verified": bool(verified_binding["verified"]),
        },
        "investigations": [
            {
                "anchor": "ALT-017",
                "capability_state": alt_017_capability,
                "conversation_id": alt_017_conversation,
                "turns": alt_017_turns,
            },
            {
                "anchor": "ALT-207",
                "capability_state": alt_207_capability,
                "conversation_id": alt_207_conversation,
                "turns": alt_207_turns,
            },
        ],
        "verification": {
            "conversation_count": 2,
            "turn_count": len(all_turns),
            "alt_017_turns_share_conversation": True,
            "alt_207_uses_clean_conversation": True,
            "all_turns_cloud_acknowledged": all(
                item["weave_acknowledged"] for item in all_turns
            ),
            "all_turns_read_back_from_weave": len(all_turns) == 3,
            "all_turns_observed_tool_activity": all(
                item["tool_activity_observed"] for item in all_turns
            ),
            "all_turns_tool_parentage_verified": (
                None
                if any(item["tool_parentage_verified"] is None for item in all_turns)
                else all(item["tool_parentage_verified"] for item in all_turns)
            ),
            "rejected_turn_count": len(rejected),
            "rejected_answer_release_count": sum(
                1 for item in rejected if item["answer_released"]
            ),
            "provider_output_stored_in_receipt": False,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument(
        "--project",
        default=os.environ.get("WANDB_REPORT_PROJECT") or os.environ.get("WANDB_PROJECT"),
    )
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default=SUPPORTED_MODELS[0])
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    api_key = os.environ.get("WANDB_API_KEY")
    if not api_key:
        raise SystemExit("WANDB_API_KEY must be present in the process environment")
    if not arguments.entity or not arguments.project:
        raise SystemExit("--entity and --project are required (or set their W&B environment values)")
    configured_base = os.environ.get("WANDB_BASE_URL", WANDB_CLOUD_API).rstrip("/")
    if configured_base != WANDB_CLOUD_API:
        raise SystemExit("this probe requires W&B Cloud at https://api.wandb.ai")

    gate = _test_only_enabled_release_gate(arguments.model)
    with tempfile.TemporaryDirectory(prefix="gi-evidence-guide-agents-probe-") as temporary:
        dispatcher = RPCDispatcher(
            data_root=temporary,
            allowed_roots=[temporary],
            observability_adapters={"evidence_guide_release_gate": gate},
        )
        try:
            # This mirrors the native host's in-memory credential initialization.
            # The key is never placed in an RPC payload, command argument, receipt,
            # project database, or diagnostic output.
            dispatcher.observability.initialize_credentials(api_key, WANDB_CLOUD_API)
            try:
                import weave  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("WEAVE_SDK_UNAVAILABLE") from exc
            with dispatcher.observability.credentials.environment():
                readback_client = weave.init(
                    f"{arguments.entity}/{arguments.project}",
                    settings={"redact_pii": False},
                )
                receipt = run_probe_flow(
                    dispatcher,
                    entity=arguments.entity,
                    external_project=arguments.project,
                    model_id=arguments.model,
                    readback_client=readback_client,
                )
        finally:
            dispatcher.observability.clear_credentials()
            dispatcher._executor.shutdown(wait=True)

    _write_json_atomic(arguments.output, receipt)
    print(
        json.dumps(
            {
                "mode": "manual_real_integration_probe",
                "receipt": str(arguments.output.expanduser().resolve()),
                "destination": f"{arguments.entity}/{arguments.project}",
                "turn_count": receipt["verification"]["turn_count"],
                "rejected_turn_count": receipt["verification"]["rejected_turn_count"],
                "rejected_answer_release_count": receipt["verification"][
                    "rejected_answer_release_count"
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
