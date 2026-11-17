from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from gi_scenario_core.evidence_guide import AGENT_ID, AGENT_VERSION
from gi_scenario_core.rpc_worker import RPCFault

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_evidence_guide_agents_probe.py"
SPEC = importlib.util.spec_from_file_location("run_evidence_guide_agents_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeAgentTurnClient:
    def __init__(
        self,
        values: dict[str, dict[str, Any]],
        spans: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.values = values
        self.spans = spans or {}
        self.requested: list[str] = []

    def get_agent_turn(self, *, trace_id: str) -> dict[str, Any]:
        self.requested.append(trace_id)
        return self.values[trace_id]

    def get_agent_spans(self, *, query: Any, limit: int | None = None) -> list[dict[str, Any]]:
        del limit
        trace_id = query["$expr"]["$eq"][1]["$literal"]
        return self.spans[trace_id]


class FakeDispatcher:
    def __init__(self) -> None:
        self.project = {
            "id": "project-synthetic-probe",
            "projectDirectory": "/temporary/synthetic-project",
        }
        self.conversations: dict[str, dict[str, Any]] = {}
        self.references: dict[str, dict[str, Any]] = {}
        self.connected = False

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if method == "project.create":
            assert params == {"kind": "demo"}
            return self.project
        if method == "observability.connect":
            assert params["policy"]["classification"] == "standard_engineering"
            binding = params["binding"]
            assert binding["access"] == "Restricted"
            assert binding["deployment"] == "wandb_cloud"
            self.connected = True
            return {
                "binding": {
                    **binding,
                    "external_project_id": "restricted-project-id",
                    "verified": True,
                }
            }
        if method == "assistant.get_capabilities":
            assert self.connected
            return {"available": True, "state": "CONSENT_REQUIRED"}
        if method == "assistant.start_conversation":
            anchor = params["anchor"]["identifier"]
            conversation_id = f"conversation-{anchor.casefold()}"
            self.conversations[conversation_id] = {
                "conversation_id": conversation_id,
                "turns": [],
            }
            return {"conversation": {"conversation_id": conversation_id}}
        if method == "assistant.get_conversation":
            conversation = self.conversations[params["conversation_id"]]
            return {"conversation": conversation, "turns": list(conversation["turns"])}
        if method == "assistant.ask":
            conversation = self.conversations[params["conversation_id"]]
            sequence = len(conversation["turns"])
            turn_id = f"turn-{len(self.references) + 1}"
            trace_id = f"trace-{len(self.references) + 1}"
            turn = {
                "turn_id": turn_id,
                "conversation_id": params["conversation_id"],
                "sequence": sequence,
                "state": "blocked",
                "error_code": "EVIDENCE_NEXT_CHECK_SELECTION_INVALID",
            }
            conversation["turns"].append(turn)
            self.references[turn_id] = {
                "conversation_id": params["conversation_id"],
                "turn_id": turn_id,
                "trace_id": trace_id,
                "root_span_id": f"root-{len(self.references) + 1}",
                "url": (
                    "https://wandb.ai/test-team/test-project/weave/agents/conversations/"
                    f"{params['conversation_id']}"
                ),
            }
            raise RPCFault(
                -32031,
                "The explanation was not released.",
                {"code": "EVIDENCE_NEXT_CHECK_SELECTION_INVALID"},
            )
        raise AssertionError(f"unexpected RPC method: {method}")


def _readback(trace_id: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "status_code": "ERROR",
        "agent_name": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "root_span_name": AGENT_ID,
        "messages": [
            {"type": "user_message"},
            {"type": "tool_call"},
            {"type": "assistant_message"},
        ],
    }


def _spans(trace_id: str, index: int) -> list[dict[str, Any]]:
    root_span_id = f"root-{index}"
    chat_span_id = f"chat-{index}"
    return [
        {
            "trace_id": trace_id,
            "span_id": root_span_id,
            "parent_span_id": "",
            "operation_name": "invoke_agent",
            "tool_name": "",
        },
        {
            "trace_id": trace_id,
            "span_id": chat_span_id,
            "parent_span_id": root_span_id,
            "operation_name": "chat",
            "tool_name": "",
        },
        {
            "trace_id": trace_id,
            "span_id": f"tool-{index}",
            "parent_span_id": chat_span_id,
            "operation_name": "execute_tool",
            "tool_name": "decision.get_rule_matrix",
        },
    ]


def test_test_only_gate_is_fully_pinned_without_mutating_production() -> None:
    before = probe.PRODUCTION_GATE.read_bytes()

    gate = probe._test_only_enabled_release_gate("ibm-granite/granite-4.1-8b")

    assert gate["enabled"] is True
    assert gate["decision"] == "ENABLE_LIVE_GUIDE"
    assert gate["selected_prompt_version"] == probe.PROMPT_VERSION
    assert gate["selected_response_schema_version"] == probe.RESPONSE_SCHEMA_VERSION
    assert gate["selected_response_schema_sha256"] == probe.RESPONSE_SCHEMA_SHA256
    assert gate["selected_tool_contract_version"] == probe.TOOL_CONTRACT_VERSION
    assert gate["selected_validator_version"] == probe.VALIDATOR_VERSION
    assert "TEST-ONLY" in gate["claim_boundary"]
    assert probe.PRODUCTION_GATE.read_bytes() == before


def test_probe_preserves_conversation_boundaries_and_rejected_prose(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    client = FakeAgentTurnClient(
        {f"trace-{index}": _readback(f"trace-{index}") for index in range(1, 4)},
        {f"trace-{index}": _spans(f"trace-{index}", index) for index in range(1, 4)},
    )

    receipt = probe.run_probe_flow(
        dispatcher,
        entity="test-team",
        external_project="test-project",
        model_id="ibm-granite/granite-4.1-8b",
        readback_client=client,
        reference_reader=lambda _directory, _project_id, turn_id: dispatcher.references[
            turn_id
        ],
    )

    alt_017, alt_207 = receipt["investigations"]
    assert len(alt_017["turns"]) == 2
    assert {turn["sequence"] for turn in alt_017["turns"]} == {0, 1}
    assert len(alt_207["turns"]) == 1
    assert alt_017["conversation_id"] != alt_207["conversation_id"]
    assert client.requested == ["trace-1", "trace-2", "trace-3"]
    assert receipt["verification"]["rejected_turn_count"] == 3
    assert receipt["verification"]["rejected_answer_release_count"] == 0
    assert receipt["verification"]["all_turns_observed_tool_activity"] is True
    assert receipt["verification"]["all_turns_tool_parentage_verified"] is True
    assert receipt["verification"]["provider_output_stored_in_receipt"] is False
    assert all("answer" not in turn for item in receipt["investigations"] for turn in item["turns"])
    assert "plain_language" not in json.dumps(receipt).casefold()

    output = tmp_path / "nested" / "receipt.json"
    probe._write_json_atomic(output, receipt)
    assert json.loads(output.read_text(encoding="utf-8")) == receipt
    assert not list(output.parent.glob("*.tmp"))


def test_readback_fails_closed_on_agent_identity_drift() -> None:
    client = FakeAgentTurnClient(
        {
            "trace-1": {
                **_readback("trace-1"),
                "agent_version": "unexpected-version",
            }
        }
    )

    with pytest.raises(RuntimeError, match="AGENT_VERSION_MISMATCH"):
        probe._agent_readback(client, "trace-1", "root-1")


def test_readback_requires_message_types() -> None:
    client = FakeAgentTurnClient(
        {
            "trace-1": {
                **_readback("trace-1"),
                "messages": [],
            }
        }
    )

    with pytest.raises(RuntimeError, match="MESSAGES_UNAVAILABLE"):
        probe._agent_readback(client, "trace-1", "root-1")


def test_readback_rejects_tool_span_that_is_a_root_sibling() -> None:
    trace_id = "trace-1"
    spans = _spans(trace_id, 1)
    spans[-1]["parent_span_id"] = "root-1"
    client = FakeAgentTurnClient(
        {trace_id: _readback(trace_id)},
        {trace_id: spans},
    )

    with pytest.raises(RuntimeError, match="TOOL_PARENT_NOT_ROUTING_CHAT"):
        probe._agent_readback(client, trace_id, "root-1")


def test_readback_does_not_claim_unexposed_routing_parent() -> None:
    trace_id = "trace-1"
    spans = _spans(trace_id, 1)
    spans = [span for span in spans if span["operation_name"] != "chat"]
    client = FakeAgentTurnClient(
        {trace_id: _readback(trace_id)},
        {trace_id: spans},
    )

    readback = probe._agent_readback(client, trace_id, "root-1")

    assert readback["tool_parentage_verified"] is None
    assert readback["tool_parentage_reason"] == "routing_parent_span_not_exposed_by_sdk"
