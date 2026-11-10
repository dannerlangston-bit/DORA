"""The transport injection point: everything that would cross the network goes through `Transport`.

Nothing in this package opens a connection unless a caller builds an `AnthropicTransport`, which
`AnthropicTransport.from_env` refuses to do unless the caller passes `allow_network=True` and
`ANTHROPIC_API_KEY` is set; the key is passed to the client explicitly, so no other credential source
(a CLI profile, a federation token) is ever picked up. Tests use a fake transport.

SDK usage follows the claude-api skill (Python SDK, read 2026-11-10): `client.messages.create`,
`client.messages.batches.create / retrieve / results`, `client.models.retrieve`; the SDK's default
retries (2, on 408, 409, 429, 5xx and connection errors) run inside `create_message` (3.15: "429 and
5xx responses use the SDK's default retries"). SDK exceptions become `TransportError` kinds, most
specific first: timeout, rate_limit (429), client (other 4xx), server (5xx), connection.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol, Sequence

TRANSIENT_KINDS = frozenset({"timeout", "rate_limit", "server", "connection"})
BATCH_RESULT_KINDS = ("succeeded", "errored", "canceled", "expired")


class NetworkNotAllowed(RuntimeError):
    """A live call was requested without `allow_network=True` and an `ANTHROPIC_API_KEY`."""


class TransportError(Exception):
    """A request failed after the SDK's own retries."""

    def __init__(self, kind: str, message: str = "", status: int | None = None):
        if kind not in TRANSIENT_KINDS | {"client"}:
            raise ValueError(f"transport error kind {kind!r}")
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind, self.status = kind, status

    @property
    def transient(self) -> bool:
        return self.kind in TRANSIENT_KINDS


@dataclass(frozen=True)
class Usage:
    """The usage block of one response, or a sum of them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                     self.cache_creation_input_tokens + other.cache_creation_input_tokens,
                     self.cache_read_input_tokens + other.cache_read_input_tokens)


@dataclass(frozen=True)
class ModelResponse:
    """What the job reads from one Messages response."""

    message_id: str
    model: str
    stop_reason: str | None
    text: str
    usage: Usage
    request_id: str | None = None
    refusal_category: str | None = None


@dataclass(frozen=True)
class BatchEntry:
    """One Message Batches result line, keyed by `custom_id`."""

    custom_id: str
    kind: str
    response: ModelResponse | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in BATCH_RESULT_KINDS:
            raise ValueError(f"batch result kind {self.kind!r}")
        if (self.kind == "succeeded") != (self.response is not None):
            raise ValueError("a succeeded entry carries its response, and only it")


class Transport(Protocol):
    """The five calls the Reviewer job makes."""

    def create_message(self, params: Mapping[str, Any]) -> ModelResponse: ...

    def retrieve_model(self, model_id: str) -> bool: ...

    def create_batch(self, requests: Sequence[tuple[str, Mapping[str, Any]]]) -> str: ...

    def batch_ended(self, batch_id: str) -> bool: ...

    def batch_results(self, batch_id: str) -> Iterator[BatchEntry]: ...


def _response(msg: Any) -> ModelResponse:
    u = msg.usage
    details = getattr(msg, "stop_details", None)
    return ModelResponse(
        message_id=msg.id, model=msg.model, stop_reason=msg.stop_reason,
        text="".join(b.text for b in msg.content if getattr(b, "type", None) == "text"),
        usage=Usage(u.input_tokens or 0, u.output_tokens or 0, u.cache_creation_input_tokens or 0,
                    u.cache_read_input_tokens or 0),
        request_id=getattr(msg, "_request_id", None),
        refusal_category=getattr(details, "category", None) if details is not None else None)


class AnthropicTransport:
    """`Transport` over the official `anthropic` SDK. Construct it only through `from_env`."""

    def __init__(self, client: Any):
        import anthropic                 # imported here so tests and the offline path never need it
        self._sdk = anthropic
        self._client = client

    @classmethod
    def from_env(cls, *, allow_network: bool, env: Mapping[str, str] | None = None,
                 base_url: str | None = None) -> "AnthropicTransport":
        """A live transport, only with `allow_network=True` and `ANTHROPIC_API_KEY` set."""
        env = os.environ if env is None else env
        if not allow_network:
            raise NetworkNotAllowed("live Reviewer calls need allow_network=True")
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise NetworkNotAllowed("live Reviewer calls need ANTHROPIC_API_KEY")
        import anthropic
        kwargs: dict[str, Any] = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        return cls(anthropic.Anthropic(**kwargs))

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        sdk = self._sdk
        try:
            return fn(*args, **kwargs)
        except sdk.APITimeoutError as e:
            raise TransportError("timeout", str(e)) from e
        except sdk.RateLimitError as e:
            raise TransportError("rate_limit", e.message, 429) from e
        except sdk.APIStatusError as e:
            raise TransportError("server" if e.status_code >= 500 else "client", e.message, e.status_code) from e
        except sdk.APIConnectionError as e:
            raise TransportError("connection", str(e)) from e

    def create_message(self, params: Mapping[str, Any]) -> ModelResponse:
        return _response(self._call(self._client.messages.create, **params))

    def retrieve_model(self, model_id: str) -> bool:
        """`GET /v1/models/{id}`: True when served, False on 404."""
        try:
            self._call(self._client.models.retrieve, model_id)
        except TransportError as e:
            if e.status == 404:
                return False
            raise
        return True

    def create_batch(self, requests: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
        batch = self._call(self._client.messages.batches.create,
                           requests=[{"custom_id": cid, "params": dict(p)} for cid, p in requests])
        return batch.id

    def batch_ended(self, batch_id: str) -> bool:
        return self._call(self._client.messages.batches.retrieve, batch_id).processing_status == "ended"

    def batch_results(self, batch_id: str) -> Iterator[BatchEntry]:
        lines = self._call(lambda: list(self._client.messages.batches.results(batch_id)))   # read errors map too
        for r in lines:
            kind = r.result.type
            if kind == "succeeded":
                yield BatchEntry(r.custom_id, kind, _response(r.result.message))
            elif kind == "errored":
                yield BatchEntry(r.custom_id, kind, error_type=r.result.error.error.type)
            else:
                yield BatchEntry(r.custom_id, kind)
