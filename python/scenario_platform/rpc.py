"""Strict JSONL / JSON-RPC 2.0 framing and runtime contract validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import ContractModel

MAX_JSONL_BYTES = 8 * 1024 * 1024


class RpcProtocolError(ValueError):
    def __init__(self, code: int, message: str, *, request_id: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


class JsonRpcRequest(ContractModel):
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any]
    id: str | int

    def _validate_contract(self) -> None:
        if self.jsonrpc != "2.0" or not self.method or isinstance(self.id, bool):
            raise ValueError("invalid JSON-RPC 2.0 request")


class JsonRpcEvent(ContractModel):
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any]

    def _validate_contract(self) -> None:
        if self.jsonrpc != "2.0" or not self.method:
            raise ValueError("invalid JSON-RPC 2.0 event")


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is prohibited: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def decode_jsonl(line: bytes | str, *, max_bytes: int = MAX_JSONL_BYTES) -> dict[str, Any]:
    encoded = line.encode("utf-8") if isinstance(line, str) else line
    if len(encoded) > max_bytes:
        raise RpcProtocolError(-32600, "JSONL frame exceeds the configured byte limit")
    if b"\x00" in encoded:
        raise RpcProtocolError(-32700, "JSONL frame contains a null byte")
    stripped = encoded.rstrip(b"\r\n")
    if b"\n" in stripped or b"\r" in stripped:
        raise RpcProtocolError(-32700, "JSONL frame must contain exactly one JSON value")
    try:
        value = json.loads(
            stripped.decode("utf-8"),
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RpcProtocolError(-32700, "invalid JSONL frame") from exc
    if not isinstance(value, dict):
        raise RpcProtocolError(-32600, "JSON-RPC frame must be an object")
    return value


def encode_jsonl(
    value: ContractModel | dict[str, Any], *, max_bytes: int = MAX_JSONL_BYTES
) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, ContractModel) else value
    try:
        rendered = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise RpcProtocolError(-32603, "RPC payload is not strict JSON") from exc
    if len(rendered) > max_bytes:
        raise RpcProtocolError(-32600, "JSONL frame exceeds the configured byte limit")
    return rendered


@dataclass(frozen=True, slots=True)
class _MethodContract:
    request: type[ContractModel]
    response: type[ContractModel]


class RuntimeContractRegistry:
    """Fail-closed registry for every request, response, and event payload."""

    def __init__(self, *, allowed_prefixes: tuple[str, ...] = ()) -> None:
        self.allowed_prefixes = allowed_prefixes
        self._methods: dict[str, _MethodContract] = {}
        self._events: dict[str, type[ContractModel]] = {}

    def _validate_name(self, name: str) -> None:
        if not name or (self.allowed_prefixes and not name.startswith(self.allowed_prefixes)):
            raise ValueError(f"RPC name is outside the configured allowlist: {name}")

    def register_method(
        self,
        method: str,
        request: type[ContractModel],
        response: type[ContractModel],
    ) -> None:
        self._validate_name(method)
        if method in self._methods:
            raise ValueError(f"RPC method is already registered: {method}")
        self._methods[method] = _MethodContract(request, response)

    def register_event(self, event: str, payload: type[ContractModel]) -> None:
        self._validate_name(event)
        if event in self._events:
            raise ValueError(f"RPC event is already registered: {event}")
        self._events[event] = payload

    def validate_request(
        self, frame: bytes | str | dict[str, Any]
    ) -> tuple[JsonRpcRequest, ContractModel]:
        raw = decode_jsonl(frame) if isinstance(frame, (bytes, str)) else frame
        if isinstance(raw.get("id"), bool) or not isinstance(raw.get("params"), dict):
            raise RpcProtocolError(-32600, "invalid JSON-RPC request")
        try:
            request = JsonRpcRequest.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise RpcProtocolError(-32600, "invalid JSON-RPC request") from exc
        contract = self._methods.get(request.method)
        if contract is None:
            raise RpcProtocolError(-32601, "method not found", request_id=request.id)
        try:
            params = contract.request.model_validate(request.params)
        except (TypeError, ValueError) as exc:
            raise RpcProtocolError(
                -32602, "invalid method parameters", request_id=request.id
            ) from exc
        return request, params

    def validate_response(self, method: str, payload: Any) -> ContractModel:
        contract = self._methods.get(method)
        if contract is None:
            raise RpcProtocolError(-32601, "method not found")
        try:
            return contract.response.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise RpcProtocolError(-32603, "invalid method response") from exc

    def validate_event(
        self, frame: bytes | str | dict[str, Any]
    ) -> tuple[JsonRpcEvent, ContractModel]:
        raw = decode_jsonl(frame) if isinstance(frame, (bytes, str)) else frame
        try:
            event = JsonRpcEvent.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise RpcProtocolError(-32600, "invalid JSON-RPC event") from exc
        contract = self._events.get(event.method)
        if contract is None:
            raise RpcProtocolError(-32601, "event not found")
        try:
            payload = contract.model_validate(event.params)
        except (TypeError, ValueError) as exc:
            raise RpcProtocolError(-32602, "invalid event payload") from exc
        return event, payload
