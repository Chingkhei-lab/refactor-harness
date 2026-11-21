"""Provider adapters, retries and error classification for the model bake-off.

One class covers every OpenAI-compatible endpoint: numes.ai and Gemini's
OpenAI-compatible surface today, OpenRouter later (it is an entry in `PROVIDERS`
with a different base URL and one extra header). A second provider protocol is
only needed if function calling stops being representable as
`tools=[{"type": "function", ...}]`.

Two things this module deliberately does *not* do:

* It never logs or records an `Authorization` header. Cassettes are meant to be
  re-analysable and shareable, and a key in one is a leaked key.
* It does not silently repair a malformed tool call. A call whose `arguments` do
  not parse as JSON is returned with `arguments=None` and an `argument_error`, so
  the caller can count it as an invalid tool call instead of inventing one.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

# Statuses worth retrying: the request may succeed unchanged.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _retryable(status: int) -> bool:
    """Every 5xx, not only the ones OpenAI documents.

    The provider's edge answers 520/521/522/524 for an upstream hiccup, and a first
    version of this module listed only {500, 502, 503, 504}. The effect was that
    the *most common* failure was classified correctly as `server` and never
    retried: 11 of 31 task failures in the first stage-1 run were a single 520 on
    the first turn. A retry policy that covers only the documented statuses is a
    policy for a provider that only has documented failures.
    """
    return status in RETRY_STATUSES or 500 <= status < 600


class ErrorType:
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    EMPTY = "empty_response"
    BAD_TOOL_CALL = "bad_tool_call"
    NONE = ""


@dataclass(frozen=True)
class ProviderConfig:
    """One OpenAI-compatible provider. Adding OpenRouter is one more entry."""

    name: str
    base_url: str
    key_env: str
    extra_headers: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, ProviderConfig] = {
    "numes": ProviderConfig(
        name="numes",
        base_url="https://api.numes.ai/api/v1",
        key_env="NUMES_API_KEY",
    ),
    "gemini": ProviderConfig(
        name="gemini",
        # Gemini's documented OpenAI-compatible surface. Function calling is
        # `tools` / `tool_calls` exactly as OpenAI specifies, so the same
        # normalisation applies.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
    ),
}


@dataclass(frozen=True)
class ModelSpec:
    """A model on a provider, plus what is known about invoking it."""

    provider: str
    model_id: str
    label: str
    supports_temperature: bool = True
    max_tokens: int = 1024

    @property
    def slug(self) -> str:
        return f"{self.provider}--{self.model_id.replace('/', '-').replace(':', '-')}"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str
    arguments: dict[str, Any] | None
    argument_error: str = ""
    # Provider-specific fields that must be echoed back with the assistant turn.
    # Gemini 3.x puts a `thought_signature` under `extra_content.google` and
    # rejects the next turn with HTTP 400 if it is dropped: the tool call cannot be
    # replayed without it.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.arguments is not None and not self.argument_error


@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    attempts: int = 0
    error_type: str = ErrorType.NONE
    error_detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    rate_limit_headers: dict[str, str] = field(default_factory=dict)

    @property
    def finish_reason(self) -> str:
        choices = self.raw.get("choices") or []
        if not choices:
            return ""
        return str(choices[0].get("finish_reason") or "")

    @property
    def total_tokens(self) -> int:
        try:
            return int(self.usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return 0


def load_keys(path: Path = REPO_ROOT / ".harness" / "bakeoff" / "keys.env") -> None:
    """Load `KEY=value` lines into the environment without touching existing ones.

    The file lives under `.harness/`, which is git-ignored, because a key in a
    tracked file is a key in a repository.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def _classify_status(status: int, body: str) -> str:
    if status == 429:
        return ErrorType.RATE_LIMIT
    if status in (401, 403):
        return ErrorType.AUTH
    if status >= 500:
        return ErrorType.SERVER
    del body
    return ErrorType.INVALID_REQUEST


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            return max(0.0, float(str(raw).rstrip("s")))
        except ValueError:
            continue
    return None


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower().startswith(("x-ratelimit", "ratelimit", "retry-after"))
    }


class OpenAICompatible:
    """A minimal, strict client: one request method, retries, no repair."""

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        *,
        max_attempts: int = 5,
        timeout_s: float = 240.0,
        backoff_base: float = 2.0,
        sleep: Any = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError(f"{config.name}: no key in {config.key_env}")
        self.config = config
        self._api_key = api_key
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.backoff_base = backoff_base
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self.config.extra_headers)
        return headers

    def chat(
        self,
        model: ModelSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
        record: Any = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": messages,
            "max_tokens": max_tokens or model.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if temperature is not None and model.supports_temperature:
            payload["temperature"] = temperature

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        result = ChatResult(ok=False)
        started = time.perf_counter()
        last: ChatResult | None = None
        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            try:
                response = httpx.post(
                    url, headers=self._headers(), json=payload, timeout=self.timeout_s
                )
            except httpx.TimeoutException as exc:
                last = ChatResult(
                    ok=False,
                    error_type=ErrorType.TIMEOUT,
                    error_detail=f"{type(exc).__name__}: {exc}",
                    attempts=attempt,
                )
            except httpx.HTTPError as exc:
                last = ChatResult(
                    ok=False,
                    error_type=ErrorType.NETWORK,
                    error_detail=f"{type(exc).__name__}: {exc}",
                    attempts=attempt,
                )
            else:
                headers = _rate_limit_headers(response.headers)
                if _retryable(response.status_code):
                    wait = _retry_after_seconds(response.headers)
                    if wait is None:
                        wait = self.backoff_base * (2 ** (attempt - 1)) + random.random() * 0.5
                    last = ChatResult(
                        ok=False,
                        error_type=_classify_status(response.status_code, response.text),
                        error_detail=f"HTTP {response.status_code}: {response.text[:300]}",
                        attempts=attempt,
                        rate_limit_headers=headers,
                    )
                    if attempt < self.max_attempts:
                        if record is not None:
                            record(
                                {
                                    "attempt": attempt,
                                    "error_type": last.error_type,
                                    "status": response.status_code,
                                    "sleep_s": wait,
                                }
                            )
                        self._sleep(wait)
                        continue
                elif response.status_code >= 400:
                    last = ChatResult(
                        ok=False,
                        error_type=_classify_status(response.status_code, response.text),
                        error_detail=f"HTTP {response.status_code}: {response.text[:300]}",
                        attempts=attempt,
                        rate_limit_headers=headers,
                    )
                else:
                    parsed = self._parse(response, attempt)
                    parsed.latency_s = time.perf_counter() - started
                    parsed.rate_limit_headers = headers
                    if record is not None:
                        record(
                            {
                                "attempt": attempt,
                                "status": response.status_code,
                                "latency_s": parsed.latency_s,
                                "usage": parsed.usage,
                                "finish_reason": parsed.finish_reason,
                            }
                        )
                    return parsed
            if attempt < self.max_attempts and last is not None and last.error_type in (
                ErrorType.TIMEOUT,
                ErrorType.NETWORK,
            ):
                wait = self.backoff_base * (2 ** (attempt - 1)) + random.random() * 0.5
                if record is not None:
                    record({"attempt": attempt, "error_type": last.error_type, "sleep_s": wait})
                self._sleep(wait)
                continue
            if last is not None and last.error_type not in (ErrorType.TIMEOUT, ErrorType.NETWORK):
                break
        assert last is not None
        last.latency_s = time.perf_counter() - started
        return last

    def _parse(self, response: httpx.Response, attempt: int) -> ChatResult:
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            return ChatResult(
                ok=False,
                error_type=ErrorType.INVALID_REQUEST,
                error_detail=f"unparseable body: {exc}",
                attempts=attempt,
            )
        choices = body.get("choices") or []
        if not choices:
            return ChatResult(
                ok=False,
                error_type=ErrorType.EMPTY,
                error_detail=str(body)[:300],
                raw=body,
                attempts=attempt,
            )
        message = choices[0].get("message") or {}
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            raw_args = function.get("arguments")
            extras = {
                key: value
                for key, value in raw_call.items()
                if key not in ("id", "type", "function")
            }
            if isinstance(raw_args, dict):
                calls.append(
                    ToolCall(
                        id=str(raw_call.get("id") or f"call_{index}"),
                        name=str(function.get("name") or ""),
                        arguments_raw=json.dumps(raw_args),
                        arguments=raw_args,
                        extra=extras,
                    )
                )
                continue
            text = str(raw_args if raw_args is not None else "")
            try:
                parsed_args = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                calls.append(
                    ToolCall(
                        id=str(raw_call.get("id") or f"call_{index}"),
                        name=str(function.get("name") or ""),
                        arguments_raw=text,
                        arguments=None,
                        argument_error=f"{type(exc).__name__}: {exc}",
                        extra=extras,
                    )
                )
                continue
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments_raw=text,
                    arguments=parsed_args if isinstance(parsed_args, dict) else None,
                    argument_error=(
                        "" if isinstance(parsed_args, dict) else "arguments are not an object"
                    ),
                    extra=extras,
                )
            )
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        return ChatResult(
            ok=True,
            content=str(content or ""),
            reasoning=str(message.get("reasoning") or message.get("reasoning_content") or ""),
            tool_calls=calls,
            usage=dict(body.get("usage") or {}),
            raw=body,
            attempts=attempt,
            request_id=str(response.headers.get("x-request-id") or body.get("id") or ""),
        )

    def time_to_first_token(
        self, model: ModelSpec, messages: list[dict[str, Any]], *, max_tokens: int = 16
    ) -> dict[str, Any]:
        """Measure TTFT once, with streaming, and abandon the stream after chunk one.

        The bake-off itself is not streamed: reassembling fragmented tool-call
        deltas by hand would add a failure mode to the thing being measured. This
        exists because "latency" in a model listing is time to *first* token, and a
        full-response time cannot be compared with it.
        """
        payload = {
            "model": model.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        started = time.perf_counter()
        ttft: float | None = None
        try:
            with httpx.stream(
                "POST", url, headers=self._headers(), json=payload, timeout=self.timeout_s
            ) as response:
                if response.status_code >= 400:
                    return {
                        "ok": False,
                        "ttft_s": None,
                        "status": response.status_code,
                        "detail": response.read()[:200].decode("utf-8", "replace"),
                    }
                for line in response.iter_lines():
                    if line.startswith("data:") and line.strip() != "data: [DONE]":
                        ttft = time.perf_counter() - started
                        break
        except httpx.HTTPError as exc:
            return {"ok": False, "ttft_s": None, "detail": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": ttft is not None,
            "ttft_s": ttft,
            "total_s": time.perf_counter() - started,
        }


def assistant_message(result: ChatResult) -> dict[str, Any]:
    """Rebuild the assistant turn, including any field the provider needs echoed.

    Both the smoke test and the bake-off replay tool results, and both must replay
    them *exactly* — a dropped `extra_content` turns a working provider into a
    permanent HTTP 400 (`missing a thought_signature in functionCall parts`).
    """
    message: dict[str, Any] = {"role": "assistant", "content": result.content or None}
    calls: list[dict[str, Any]] = []
    for call in result.tool_calls:
        entry: dict[str, Any] = {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments_raw},
        }
        entry.update(call.extra)
        calls.append(entry)
    message["tool_calls"] = calls
    return message


class Provider(Protocol):
    """What a bake-off needs from a provider, so a second family is one class."""

    config: ProviderConfig

    def chat(
        self,
        model: ModelSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = ...,
        tool_choice: str | None = ...,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        record: Any = ...,
    ) -> ChatResult: ...


def build(provider: str) -> OpenAICompatible:
    load_keys()
    config = PROVIDERS[provider]
    return OpenAICompatible(config, os.environ.get(config.key_env, ""))


class Cassette:
    """Append-only JSONL record of every request and response.

    Re-analysis reads these and never calls the network, so a second look at the
    same run costs nothing and cannot change its numbers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def redact(value: Any) -> Any:
    """Strip anything that looks like a credential before it reaches a cassette."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in ("authorization", "x-goog-api-key", "api_key", "key"):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def iter_json_lines(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        yield from Cassette(path).read()
