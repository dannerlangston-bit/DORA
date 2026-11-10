"""The real Reviewer adapter, part 4: the deprecation alarm input, the live entry point's network
guard, and the SDK transport's mapping (3.15, 10.7). The SDK client here is a stand-in object; no
request leaves the process."""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from test_reviewer import MODEL, FakeTransport, answer, make_item, resp

from sentinel.reviewer import (AnthropicTransport, NetworkNotAllowed, TransportError, alarms_due, live, model_check,
                               parse_notices, review_live)

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
