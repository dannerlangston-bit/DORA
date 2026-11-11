"""The transport injection point: everything that would cross the network goes through `Transport`.

Nothing in this package opens a connection unless a caller builds an `AnthropicTransport`, which
`AnthropicTransport.from_env` refuses to do unless the caller passes `allow_network=True` and
`ANTHROPIC_API_KEY` is set; the key is passed to the client explicitly, so no other credential source
(a CLI profile, a federation token) is ever picked up. The endpoint is passed explicitly too: the
reviewer config's `endpoint_base_url` (the security-approved endpoint, 3.12) or else the documented
production endpoint `DEFAULT_BASE_URL`, so the SDK never takes `ANTHROPIC_BASE_URL` (or a profile's base
URL) from the process environment; the SDK's OpenTelemetry spans are switched off explicitly, so no
environment variable can turn on request-content tracing. Request headers are pinned as well (3.12, 3.15: the
request is byte-stable and the version tuple records its settings): the SDK merges `ANTHROPIC_CUSTOM_HEADERS` from
the process environment into every request, so `from_env` refuses to build a client while that variable is set
(`UnsafeEnvironment`, logged by name, never by value), passes an explicit empty `default_headers`, and checks the
built client carries no custom header. Tests use a fake transport.

Egress (6.10): given an `egress.EgressGate`, the client sends through an HTTP client whose every request, retries
included, is checked against the signed allowlist before connecting and logged to `egress_log` (`sent` with the
HTTP status and request id, `failed` without a response, `refused` with the reason and a `harness_error`
finding). Redirects are not followed, so no request reaches a host the gate did not see. A refusal is neither
wrapped nor retried by the SDK and becomes `TransportError("client", "egress refused: ...")`. The purpose of each
call is `review` (messages), `batch_review` (batches) or `model_check` (models), unless the caller's
`gate.context(purpose=...)` says otherwise (preflight N2, for example); the model id goes on the row too.

SDK usage follows the claude-api skill (Python SDK, read 2026-11-10): `client.messages.create`,
`client.messages.batches.create / retrieve / results`, `client.models.retrieve`; the SDK's default
retries (2, on 408, 409, 429, 5xx and connection errors) run inside `create_message` (3.15: "429 and
5xx responses use the SDK's default retries"). SDK exceptions become `TransportError` kinds, most
specific first: timeout, rate_limit (429), client (other 4xx), server (5xx), connection.
"""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from ..egress import EgressGate

TRANSIENT_KINDS = frozenset({"timeout", "rate_limit", "server", "connection"})
DEFAULT_BASE_URL = "https://api.anthropic.com"     # the documented production endpoint (Claude API docs)
BATCH_RESULT_KINDS = ("succeeded", "errored", "canceled", "expired")
# Process environment variables the SDK would turn into request headers (anthropic/_client.py).
HEADER_ENV = ("ANTHROPIC_CUSTOM_HEADERS",)
log = logging.getLogger(__name__)


class NetworkNotAllowed(RuntimeError):
    """A live call was requested without `allow_network=True` and an `ANTHROPIC_API_KEY`."""


class UnsafeEnvironment(NetworkNotAllowed):
    """The process environment would change what the Reviewer sends (3.12, 3.15): no client is built."""


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


@lru_cache(maxsize=None)
def _gated_types(sdk: Any) -> tuple[type, type]:
    """(GatedHttpClient, GateError) for this SDK: they subclass SDK types, so they are built on first use."""
    import httpx2

    class GateError(sdk.AnthropicError):
        """An egress refusal or a gate failure carried through the SDK, which neither wraps nor retries its own
        error type (anthropic/_base_client.py)."""

        def __init__(self, original: BaseException):
            super().__init__(str(original))
            self.original = original

    class GatedHttpClient(sdk.DefaultHttpxClient):
        """The SDK's default HTTP client with every request sent through an `EgressGate` (6.10)."""

        def __init__(self, gate: "EgressGate", **kwargs: Any):
            super().__init__(follow_redirects=False, **kwargs)
            self._gate = gate

        def send(self, request: Any, **kwargs: Any) -> Any:
            kwargs["follow_redirects"] = False
            url = request.url
            try:
                nbytes = len(request.content)
            except httpx2.RequestNotRead:
                nbytes = int(request.headers.get("content-length") or 0)
            parent, raised = super().send, []

            def once() -> Any:
                try:
                    return parent(request, **kwargs)
                except Exception as e:                 # the SDK maps and retries what the network raised
                    raised.append(e)
                    raise
            try:
                return self._gate.request(url.host, once, port=url.port or (443 if url.scheme == "https" else 80),
                                          nbytes=nbytes, status_of=lambda r: r.status_code,
                                          request_id_of=lambda r: r.headers.get("request-id"))
            except Exception as e:
                if raised and e is raised[0]:
                    raise
                raise GateError(e) from None

    return GatedHttpClient, GateError


class AnthropicTransport:
    """`Transport` over the official `anthropic` SDK. Construct it only through `from_env`."""

    def __init__(self, client: Any, gate: "EgressGate | None" = None):
        import anthropic                 # imported here so tests and the offline path never need it
        self._sdk = anthropic
        self._client = client
        self._gate = gate
        self._gate_errors: tuple[type, ...] = (_gated_types(anthropic)[1],) if gate is not None else ()

    @classmethod
    def from_env(cls, *, allow_network: bool, env: Mapping[str, str] | None = None, base_url: str | None = None,
                 gate: "EgressGate | None" = None, http_transport: Any = None) -> "AnthropicTransport":
        """A live transport, only with `allow_network=True` and `ANTHROPIC_API_KEY` set. `base_url` is the
        reviewer config's `endpoint_base_url`; without one the client is pinned to `DEFAULT_BASE_URL`, never
        to an endpoint named by the process environment. Refuses (`UnsafeEnvironment`) while a variable of
        `HEADER_ENV` is set in `env` or in the process environment, which the SDK reads directly. With `gate`,
        every request goes through it (6.10); `http_transport` is the `httpx2` transport under the gated client
        (default: the SDK's network transport with its proxy settings)."""
        env = os.environ if env is None else env
        if not allow_network:
            raise NetworkNotAllowed("live Reviewer calls need allow_network=True")
        key = env.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise NetworkNotAllowed("live Reviewer calls need ANTHROPIC_API_KEY")
        for name in HEADER_ENV:
            if name in env or name in os.environ:
                log.error("%s is set: the Reviewer client is not built, since the SDK would add its headers to "
                          "every request (3.12, 6.10)", name)
                raise UnsafeEnvironment(f"{name} is set in the environment; unset it to run the Reviewer (3.12)")
        import inspect

        import anthropic
        kwargs: dict[str, Any] = {"api_key": key, "base_url": base_url or DEFAULT_BASE_URL, "default_headers": {}}
        if "open_telemetry" in inspect.signature(anthropic.Anthropic.__init__).parameters:
            kwargs["open_telemetry"] = False             # no span, so no env switch can export request content
        if gate is not None:
            gated, _ = _gated_types(anthropic)
            kwargs["http_client"] = gated(gate, **({"transport": http_transport} if http_transport else {}))
        client = anthropic.Anthropic(**kwargs)
        if getattr(client, "_custom_headers", None):
            raise UnsafeEnvironment(f"the client carries headers it was not given: {sorted(client._custom_headers)}")
        return cls(client, gate)

    def _egress(self, purpose: str, model_id: str | None = None) -> Any:
        """The row fields this call knows (6.10); the caller's own gate context wins."""
        if self._gate is None:
            return contextlib.nullcontext()
        return self._gate.default_context(purpose=purpose, model_id=model_id)

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        sdk = self._sdk
        try:
            return fn(*args, **kwargs)
        except self._gate_errors as e:
            from ..egress import EgressRefused
            if isinstance(e.original, EgressRefused):
                raise TransportError("client", f"egress refused: {e.original.reason}") from None
            raise e.original from None
        except sdk.APITimeoutError as e:
            raise TransportError("timeout", str(e)) from e
        except sdk.RateLimitError as e:
            raise TransportError("rate_limit", e.message, 429) from e
        except sdk.APIStatusError as e:
            raise TransportError("server" if e.status_code >= 500 else "client", e.message, e.status_code) from e
        except sdk.APIConnectionError as e:
            raise TransportError("connection", str(e)) from e

    def create_message(self, params: Mapping[str, Any]) -> ModelResponse:
        with self._egress("review", params.get("model")):
            return _response(self._call(self._client.messages.create, **params))

    def retrieve_model(self, model_id: str) -> bool:
        """`GET /v1/models/{id}`: True when served, False on 404."""
        try:
            with self._egress("model_check", model_id):
                self._call(self._client.models.retrieve, model_id)
        except TransportError as e:
            if e.status == 404:
                return False
            raise
        return True

    def create_batch(self, requests: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
        models = {p.get("model") for _, p in requests}
        with self._egress("batch_review", models.pop() if len(models) == 1 else None):
            batch = self._call(self._client.messages.batches.create,
                               requests=[{"custom_id": cid, "params": dict(p)} for cid, p in requests])
        return batch.id

    def batch_ended(self, batch_id: str) -> bool:
        with self._egress("batch_review"):
            return self._call(self._client.messages.batches.retrieve, batch_id).processing_status == "ended"

    def batch_results(self, batch_id: str) -> Iterator[BatchEntry]:
        with self._egress("batch_review"):
            lines = self._call(lambda: list(self._client.messages.batches.results(batch_id)))   # read errors map
        for r in lines:
            kind = r.result.type
            if kind == "succeeded":
                yield BatchEntry(r.custom_id, kind, _response(r.result.message))
            elif kind == "errored":
                yield BatchEntry(r.custom_id, kind, error_type=r.result.error.error.type)
            else:
                yield BatchEntry(r.custom_id, kind)
