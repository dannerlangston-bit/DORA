"""The real Reviewer adapter, part 4: the deprecation alarm input, the live entry point's network
guard, the SDK transport's mapping (3.15, 10.7), its pinned headers (3.12) and its egress gate (6.10). The SDK
client here is a stand-in object or sends into an `httpx2.MockTransport`; no request leaves the process."""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from test_reviewer import MODEL, FakeTransport, answer, make_item, resp

from sentinel.egress import EgressAllowlist, EgressGate
from sentinel.reviewer import (AnthropicTransport, NetworkNotAllowed, TransportError, alarms_due, live, model_check,
                               parse_notices, review_live)
from sentinel.reviewer.transport import UnsafeEnvironment

NOTICES = {"schema_version": 1, "notices": [
    {"model_id": MODEL, "announced": "2027-01-01", "retires": "2027-03-15", "source": "vendor notice"},
    {"model_id": "claude-opus-4-8", "announced": "2027-01-01", "retires": "2027-02-01"}]}


# -- deprecation alarm input (10.7) ---------------------------------------------------------------

def test_alarms_fire_at_announcement_t45_t30_t14_and_retirement_for_pinned_ids_only():
    notices = parse_notices(NOTICES)
    every = alarms_due(notices, [MODEL], since=None, today=date(2027, 3, 15))
    assert [(a.kind, a.due.isoformat()) for a in every] == [
        ("announced", "2027-01-01"), ("t_minus_45", "2027-01-29"), ("t_minus_30", "2027-02-13"),
        ("t_minus_14", "2027-03-01"), ("retired", "2027-03-15")]
    assert {a.model_id for a in every} == {MODEL}                               # claude-opus-4-8 is not pinned
    day = alarms_due(notices, [MODEL], since=date(2027, 2, 12), today=date(2027, 2, 13))
    assert [a.kind for a in day] == ["t_minus_30"]
    assert alarms_due(notices, [MODEL], since=date(2027, 2, 13), today=date(2027, 2, 14)) == []
    short = alarms_due(notices, ["claude-opus-4-8"], since=None, today=date(2027, 2, 1))
    assert [a.kind for a in short] == ["announced", "t_minus_30", "t_minus_14", "retired"]   # T-45 precedes it


def test_bad_deprecation_files_are_refused():
    with pytest.raises(ValueError, match="schema_version"):
        parse_notices({"notices": []})
    with pytest.raises(ValueError, match="ISO date"):
        parse_notices({"schema_version": 1, "notices": [{"model_id": MODEL, "announced": "soon", "retires": "x"}]})
    with pytest.raises(ValueError, match="retires before"):
        parse_notices({"schema_version": 1, "notices": [{"model_id": MODEL, "announced": "2027-02-01",
                                                         "retires": "2027-01-01"}]})


def test_a_pinned_id_missing_from_the_models_api_is_an_alarm_and_a_harness_error_finding():
    alarms, findings = model_check(FakeTransport(served=(MODEL,)), [MODEL, "claude-opus-4-8"], run_id="run-9",
                                   today=date(2026, 11, 10))
    assert [(a.model_id, a.kind) for a in alarms] == [("claude-opus-4-8", "missing")]
    assert [(f.kind, f.detail, f.run_id) for f in findings] == [("harness_error", "pinned_model_missing", "run-9")]
    assert model_check(FakeTransport(), [MODEL], run_id="run-9", today=date(2026, 11, 10)) == ([], [])


# -- no network unless asked (3.12, brief) --------------------------------------------------------

def test_review_live_refuses_without_allow_network_or_an_api_key():
    with pytest.raises(NetworkNotAllowed, match="allow_network"):
        review_live([make_item()], env={"ANTHROPIC_API_KEY": "sk-test"})
    with pytest.raises(NetworkNotAllowed, match="ANTHROPIC_API_KEY"):
        review_live([make_item()], allow_network=True, env={})
    with pytest.raises(NetworkNotAllowed):
        AnthropicTransport.from_env(allow_network=False, env={"ANTHROPIC_API_KEY": "sk-test"})


def test_the_live_transport_uses_only_the_given_key_and_builds_no_request():
    t = AnthropicTransport.from_env(allow_network=True, env={"ANTHROPIC_API_KEY": "sk-test"},
                                    base_url="https://reviewer.example.internal")
    assert t._client.api_key == "sk-test" and str(t._client.base_url).startswith("https://reviewer.example.internal")


def test_importing_the_package_does_not_load_the_sdk():
    code = "import sys, sentinel.reviewer; print('anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# -- SDK transport mapping (stand-in client, no I/O) ----------------------------------------------

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _client(create=None, retrieve=None, batch=None) -> SimpleNamespace:
    def raiser(exc):
        def f(*a, **k):
            raise exc
        return f
    return SimpleNamespace(
        messages=SimpleNamespace(create=create, batches=batch),
        models=SimpleNamespace(retrieve=retrieve if callable(retrieve) else raiser(retrieve)))


@pytest.mark.parametrize("exc, kind, status", [
    (anthropic.APITimeoutError(request=REQ), "timeout", None),
    (anthropic.RateLimitError("slow", response=httpx2.Response(429, request=REQ), body=None), "rate_limit", 429),
    (anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQ), body=None), "client", 400),
    (anthropic.InternalServerError("down", response=httpx2.Response(503, request=REQ), body=None), "server", 503),
    (anthropic.APIConnectionError(request=REQ), "connection", None),
])
def test_sdk_errors_map_to_transport_error_kinds(exc, kind, status):
    def create(**params):
        raise exc
    with pytest.raises(TransportError) as e:
        AnthropicTransport(_client(create=create)).create_message({"model": MODEL})
    assert (e.value.kind, e.value.status, e.value.transient) == (kind, status, kind != "client")


def test_sdk_messages_and_batch_results_are_read_into_plain_records():
    msg = SimpleNamespace(
        id="msg_1", model=MODEL, stop_reason="refusal", _request_id="req_9",
        stop_details=SimpleNamespace(category="cyber"),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"a":1}')],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=None,
                              cache_read_input_tokens=3))
    sent = {}
    t = AnthropicTransport(_client(create=lambda **p: sent.update(p) or msg))
    r = t.create_message({"model": MODEL, "max_tokens": 5})
    assert (r.text, r.refusal_category, r.request_id, r.usage.cache_read_input_tokens) == ('{"a":1}', "cyber",
                                                                                           "req_9", 3)
    assert sent == {"model": MODEL, "max_tokens": 5}
    results = [SimpleNamespace(custom_id="rv-1", result=SimpleNamespace(type="succeeded", message=msg)),
               SimpleNamespace(custom_id="rv-2", result=SimpleNamespace(
                   type="errored", error=SimpleNamespace(error=SimpleNamespace(type="api_error")))),
               SimpleNamespace(custom_id="rv-3", result=SimpleNamespace(type="expired"))]
    batches = SimpleNamespace(create=lambda requests: SimpleNamespace(id="msgbatch_x", n=len(requests)),
                              retrieve=lambda bid: SimpleNamespace(processing_status="ended"),
                              results=lambda bid: iter(results))
    t = AnthropicTransport(_client(batch=batches))
    assert t.create_batch([("rv-1", {"model": MODEL})]) == "msgbatch_x" and t.batch_ended("msgbatch_x")
    got = list(t.batch_results("msgbatch_x"))
    assert [(e.custom_id, e.kind, e.error_type) for e in got] == [("rv-1", "succeeded", None),
                                                                  ("rv-2", "errored", "api_error"),
                                                                  ("rv-3", "expired", None)]


def test_models_api_404_means_not_served():
    nf = anthropic.NotFoundError("no", response=httpx2.Response(404, request=REQ), body=None)
    assert AnthropicTransport(_client(retrieve=nf)).retrieve_model("gone") is False
    assert AnthropicTransport(_client(retrieve=lambda mid: SimpleNamespace(id=mid))).retrieve_model(MODEL) is True
    down = anthropic.InternalServerError("down", response=httpx2.Response(500, request=REQ), body=None)
    with pytest.raises(TransportError):
        AnthropicTransport(_client(retrieve=down)).retrieve_model(MODEL)


def test_review_live_runs_the_synchronous_job_once_allowed(monkeypatch):
    fake = FakeTransport(respond=lambda p: resp(answer(confidence=0.95)))
    seen = {}

    def from_env(**kw):
        seen.update(kw)
        return fake
    monkeypatch.setattr(live.AnthropicTransport, "from_env", staticmethod(from_env))
    verdicts = review_live([make_item()], allow_network=True, env={"ANTHROPIC_API_KEY": "sk-test"})
    assert seen["allow_network"] is True and len(fake.calls) == 1
    assert [v.state for v in verdicts.values()] == ["answered"]


# -- pinned headers (3.12, 3.15) and the egress gate (6.10) -------------------------------------------------------

KEY = {"ANTHROPIC_API_KEY": "sk-test"}
MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
           "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn", "stop_sequence": None,
           "usage": {"input_tokens": 3, "output_tokens": 2}}
PARAMS = {"model": MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
ALLOW = EgressAllowlist.from_dict({"signed_by": "sec", "signed_at": "2026-11-01T00:00:00Z", "signature": "pgp:1",
                                   "hosts": [{"host": "api.anthropic.com", "purposes": ["review", "model_check"]}]})


class Sink:
    def __init__(self):
        self.rows, self.findings = [], []

    def log_egress(self, row):
        self.rows.append(row)
        return len(self.rows)

    def write_finding(self, finding, *, synthetic=False):
        self.findings.append(finding)


def mock(seen, status=200, body=None):
    def handler(request):
        seen.append(request)
        return httpx2.Response(status, headers={"request-id": f"req_{len(seen)}"}, json=body or MESSAGE)
    return httpx2.MockTransport(handler)


@pytest.mark.parametrize("where", ["process", "mapping"])
def test_custom_headers_in_the_environment_refuse_the_client(monkeypatch, caplog, where):
    leak = "X-Leak: secret-value\nanthropic-beta: foo"
    if where == "process":
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", leak)
    env = {**KEY, "ANTHROPIC_CUSTOM_HEADERS": leak} if where == "mapping" else KEY
    with caplog.at_level(logging.ERROR, logger="sentinel.reviewer.transport"):
        with pytest.raises(UnsafeEnvironment, match="ANTHROPIC_CUSTOM_HEADERS"):
            AnthropicTransport.from_env(allow_network=True, env=env)
    assert "ANTHROPIC_CUSTOM_HEADERS is set" in caplog.text and "secret-value" not in caplog.text
    assert issubclass(UnsafeEnvironment, NetworkNotAllowed)


def test_a_request_carries_only_the_sdk_headers(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    seen, sink = [], Sink()
    t = AnthropicTransport.from_env(allow_network=True, env=KEY, gate=EgressGate(ALLOW, sink),
                                    http_transport=mock(seen))
    assert t._client._custom_headers == {}
    t.create_message(PARAMS)
    headers = {k.lower() for k in seen[0].headers}
    assert "x-leak" not in headers and "anthropic-beta" not in headers


def test_every_gated_request_writes_one_row_with_purpose_model_and_request_id():
    seen, sink = [], Sink()
    gate = EgressGate(ALLOW, sink, run_id="run-9", shard_id="run-9/1")
    t = AnthropicTransport.from_env(allow_network=True, env=KEY, gate=gate, http_transport=mock(seen))
    with gate.context(checkpoint_id="census/x.y/ws.z", artifact_sha256="c" * 64, redaction_version="redaction-1-a"):
        r = t.create_message(PARAMS)
    assert r.request_id == "req_1"
    (row,) = sink.rows
    assert (row.destination, row.purpose, row.status, row.response_status, row.request_id, row.model_id) == \
        ("api.anthropic.com", "review", "sent", 200, "req_1", MODEL)
    assert row.bytes == len(seen[0].content) and row.run_id == "run-9" and row.artifact_sha256 == "c" * 64
    model = {"id": MODEL, "type": "model", "display_name": "m", "created_at": "2026-01-01T00:00:00Z"}
    t2 = AnthropicTransport.from_env(allow_network=True, env=KEY, gate=gate, http_transport=mock(seen, body=model))
    assert t2.retrieve_model(MODEL) is True
    with gate.context(purpose="preflight"):                                # preflight N2's own purpose wins
        with pytest.raises(TransportError, match="purpose preflight not allowlisted"):
            t2.retrieve_model(MODEL)
    assert [(x.purpose, x.status) for x in sink.rows] == [("review", "sent"), ("model_check", "sent"),
                                                          ("preflight", "refused")]


def test_a_host_off_the_allowlist_is_refused_before_connecting_and_not_retried():
    seen, sink = [], Sink()
    gate = EgressGate(ALLOW, sink, run_id="run-9")
    t = AnthropicTransport.from_env(allow_network=True, env=KEY, base_url="https://collector.invalid", gate=gate,
                                    http_transport=mock(seen))
    with pytest.raises(TransportError) as e:
        t.create_message(PARAMS)
    assert (e.value.kind, e.value.transient) == ("client", False) and "egress refused" in str(e.value)
    assert seen == [] and [(r.status, r.destination) for r in sink.rows] == [("refused", "collector.invalid")]
    assert [(f.kind, f.severity) for f in sink.findings] == [("harness_error", "S3")]


def test_each_attempt_without_a_response_is_a_failed_row(monkeypatch):
    monkeypatch.setattr("anthropic._base_client.time.sleep", lambda s: None)
    sink = Sink()

    def down(request):
        raise httpx2.ConnectError("reset", request=request)
    t = AnthropicTransport.from_env(allow_network=True, env=KEY, gate=EgressGate(ALLOW, sink),
                                    http_transport=httpx2.MockTransport(down))
    with pytest.raises(TransportError) as e:
        t.create_message(PARAMS)
    assert e.value.kind == "connection"
    assert [r.status for r in sink.rows] == ["failed"] * 3                  # the first try and the SDK's 2 retries
