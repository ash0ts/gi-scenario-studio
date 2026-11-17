"""Bounded W&B Serverless Inference adapter for the Evidence Guide.

The adapter deliberately implements only the OpenAI-compatible chat-completions
surface needed by the read-only Evidence Guide.  It never owns project data,
never persists credentials, and never selects a fallback model.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_INFERENCE_BASE_URL = "https://api.inference.wandb.ai/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def build_inference_request_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_schema: dict[str, Any] | None = None,
    max_tokens: int,
    seed: int = 17,
) -> bytes:
    """Serialize the exact OpenAI-compatible request used by the adapter."""

    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "seed": seed,
        "max_tokens": max_tokens,
    }
    if tools:
        request.update(
            {
                "tools": tools,
                "tool_choice": "auto",
                # The Evidence Guide router may require more than one bounded
                # read-only lookup. Make the OpenAI-compatible capability
                # explicit instead of depending on a provider default.
                "parallel_tool_calls": True,
            }
        )
    if response_schema is not None:
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "GiEvidenceGuideAnswer",
                "strict": True,
                "schema": response_schema,
            },
        }
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")


class CredentialProvider(Protocol):
    def reveal(self): ...


@dataclass(frozen=True)
class InferenceUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class InferenceToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class InferenceCompletion:
    provider_call_id: str
    model: str
    content: str
    tool_calls: list[InferenceToolCall] = field(default_factory=list)
    usage: InferenceUsage = field(default_factory=InferenceUsage)
    finish_reason: str = ""
    transport_attempts: int = 1
    request_bytes: int = 0
    response_bytes: int = 0
    latency_ms: float = 0.0


class InferenceRequestError(RuntimeError):
    """Safe provider error carrying only metering required by the campaign."""

    def __init__(
        self,
        code: str,
        *,
        transport_attempts: int,
        request_bytes: int = 0,
        response_bytes: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.transport_attempts = transport_attempts
        self.request_bytes = request_bytes
        self.response_bytes = response_bytes
        self.latency_ms = latency_ms


class InferenceTransport(Protocol):
    def __call__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout_seconds: float,
    ) -> tuple[int, bytes]: ...


class WandbInferenceAdapter:
    """Small, injectable client for W&B's OpenAI-compatible endpoint."""

    def __init__(
        self,
        credentials: CredentialProvider,
        *,
        base_url: str = DEFAULT_INFERENCE_BASE_URL,
        transport: InferenceTransport | None = None,
        timeout_seconds: float = 30.0,
        max_request_bytes: int = 64 * 1024,
        max_transport_attempts: int = 2,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.inference.wandb.ai"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("W&B Inference must use the approved HTTPS service origin")
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.transport = transport or _urlopen_transport
        self.timeout_seconds = timeout_seconds
        if not 1_024 <= max_request_bytes <= 256 * 1024:
            raise ValueError("W&B Inference request-byte limit is outside the safe range")
        self.max_request_bytes = max_request_bytes
        if max_transport_attempts not in {1, 2}:
            raise ValueError("W&B Inference transport-attempt limit is outside the safe range")
        self.max_transport_attempts = max_transport_attempts

    def complete(
        self,
        *,
        entity: str,
        project: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        max_tokens: int = 700,
        seed: int = 17,
        timeout_seconds: float | None = None,
    ) -> InferenceCompletion:
        if not entity or not project:
            raise ValueError("W&B Inference requires an explicit entity and project")
        if not model:
            raise ValueError("W&B Inference requires a pinned model identifier")
        if not 1 <= max_tokens <= 1_200:
            raise ValueError("Evidence Guide output token limit is outside the approved range")
        if not messages or any(
            item.get("role") not in {"system", "user", "assistant", "tool"} for item in messages
        ):
            raise ValueError("Evidence Guide messages use an unsupported role")

        if tools and len(tools) > 6:
            raise ValueError("Evidence Guide exposes at most six read-only tools")
        payload = build_inference_request_payload(
            model=model,
            messages=messages,
            tools=tools,
            response_schema=response_schema,
            max_tokens=max_tokens,
            seed=seed,
        )
        # This is a transport-payload safety limit, not a token estimate. Token
        # accounting remains provider-reported (or conservatively reserved by
        # the campaign when no provider report exists).
        if len(payload) > self.max_request_bytes:
            raise InferenceRequestError(
                "INFERENCE_INPUT_ENVELOPE_EXCEEDED",
                transport_attempts=0,
                request_bytes=len(payload),
            )
        call_started = time.perf_counter()
        with self.credentials.reveal() as api_key:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Project": f"{entity}/{project}",
                "User-Agent": "gi-scenario-studio/0.2 evidence-guide",
            }
            try:
                status, body, transport_attempts = self._request(
                    headers=headers,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                )
            except InferenceRequestError as exc:
                raise InferenceRequestError(
                    exc.code,
                    transport_attempts=exc.transport_attempts,
                    request_bytes=len(payload),
                    latency_ms=(time.perf_counter() - call_started) * 1_000,
                ) from exc
        if status != 200:
            raise InferenceRequestError(
                _status_code(status),
                transport_attempts=transport_attempts,
                request_bytes=len(payload),
                response_bytes=len(body),
                latency_ms=(time.perf_counter() - call_started) * 1_000,
            )
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("INFERENCE_RESPONSE_TOO_LARGE")
        try:
            decoded = json.loads(body)
            choice = decoded["choices"][0]
            message = choice["message"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("INFERENCE_RESPONSE_INVALID") from exc

        tool_calls: list[InferenceToolCall] = []
        for item in message.get("tool_calls") or []:
            try:
                function = item["function"]
                raw_arguments = function.get("arguments") or "{}"
                arguments = (
                    raw_arguments
                    if isinstance(raw_arguments, dict)
                    else json.loads(str(raw_arguments))
                )
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be an object")
                tool_calls.append(
                    InferenceToolCall(
                        call_id=str(item.get("id") or ""),
                        name=str(function["name"]),
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("INFERENCE_TOOL_CALL_INVALID") from exc

        usage = decoded.get("usage") or {}
        return InferenceCompletion(
            provider_call_id=str(decoded.get("id") or ""),
            model=str(decoded.get("model") or model),
            content=str(message.get("content") or ""),
            tool_calls=tool_calls,
            usage=InferenceUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
            finish_reason=str(choice.get("finish_reason") or ""),
            transport_attempts=transport_attempts,
            request_bytes=len(payload),
            response_bytes=len(body),
            latency_ms=(time.perf_counter() - call_started) * 1_000,
        )

    def _request(
        self,
        *,
        headers: dict[str, str],
        payload: bytes,
        timeout_seconds: float | None,
    ) -> tuple[int, bytes, int]:
        url = f"{self.base_url}/chat/completions"
        last_status = 0
        budget = (
            self.timeout_seconds
            if timeout_seconds is None
            else min(self.timeout_seconds, timeout_seconds)
        )
        if budget <= 0:
            raise InferenceRequestError("INFERENCE_TURN_TIMEOUT", transport_attempts=0)
        deadline = time.monotonic() + budget
        for attempt in range(self.max_transport_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InferenceRequestError("INFERENCE_TURN_TIMEOUT", transport_attempts=attempt)
            try:
                status, body = self.transport(
                    url=url,
                    headers=headers,
                    payload=payload,
                    timeout_seconds=remaining,
                )
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt + 1 < self.max_transport_attempts:
                    continue
                raise InferenceRequestError(
                    "INFERENCE_UNAVAILABLE", transport_attempts=attempt + 1
                ) from exc
            last_status = status
            if (
                status not in {429, 500, 502, 503, 504}
                or attempt + 1 == self.max_transport_attempts
            ):
                return status, body, attempt + 1
        return last_status, b"", self.max_transport_attempts


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


def _urlopen_transport(
    *,
    url: str,
    headers: dict[str, str],
    payload: bytes,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)


def _status_code(status: int) -> str:
    if status == 402:
        return "INFERENCE_CREDITS_REQUIRED"
    if status in {401, 403}:
        return "INFERENCE_AUTHORIZATION_REQUIRED"
    if status == 404:
        return "INFERENCE_MODEL_UNAVAILABLE"
    if status == 429:
        return "INFERENCE_RATE_LIMITED"
    if status in {500, 502, 503, 504}:
        return "INFERENCE_UNAVAILABLE"
    return "INFERENCE_REQUEST_FAILED"


def completion_to_dict(completion: InferenceCompletion) -> dict[str, Any]:
    """Return a credential-free projection suitable for tests and trace metadata."""

    return {
        "provider_call_id": completion.provider_call_id,
        "model": completion.model,
        "content": completion.content,
        "tool_calls": [
            {
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            }
            for item in completion.tool_calls
        ],
        "usage": {
            "input_tokens": completion.usage.input_tokens,
            "output_tokens": completion.usage.output_tokens,
            "total_tokens": completion.usage.total_tokens,
        },
        "finish_reason": completion.finish_reason,
        "transport_attempts": completion.transport_attempts,
    }
