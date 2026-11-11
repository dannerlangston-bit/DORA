"""The gated branch's single-use send token (6.7; 6.2 Label rules, 6.4, 5.4.3 stage 4, 5.4.4): sendgate.SendGate."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from sentinel.orchestrator.gateway import (Action, DenyRect, Flag, InputEvent, Rect, SafetyContext, refusal,
                                           slow_half)
from sentinel.orchestrator.ports import SessionSafety
from sentinel.orchestrator.sendgate import PER_DAY_CAP, PER_RUN_CAP, SendGate, SendPolicy
from sentinel.orchestrator.simrig import CONFIRM_POINT, SimulatedRig

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
NOW = 1000.0
EMPTY_SHA, GOLDEN_SHA = "sha-empty", "sha-golden"
SEND = DenyRect(Rect(900, 600, 80, 28), "Send")
ON = SendPolicy(enabled=True)


def gate(policy: SendPolicy = ON, **kw) -> SendGate:
    return SendGate(policy, {"empty": EMPTY_SHA, "golden": GOLDEN_SHA}, **kw)


def send(**kw) -> Action:
    base = dict(point=(920, 610), resolved_name="Send", under_point_text="Send", flow_class="paper_send_ok",
                account_role="empty", order_stage_step=True)
    return Action("click", **{**base, **kw})


def ctx(g: SendGate | None, **kw) -> SafetyContext:
    base = dict(flag=Flag.SET, order_dialog_present=True, deny_rects=(SEND,), dialog_foreground=True,
                dialog_read=True, session=SessionSafety("a3_a9", NOW - 0.2, EMPTY_SHA), now=NOW, send_gate=g)
    return SafetyContext(**{**base, **kw})


def test_with_every_gate_held_one_send_click_passes_and_makes_a_working_row_and_a_second_is_no_token():
    rig = SimulatedRig()
    rig.set_hook_state(armed=True, deny_all=False)
    rig.attested = True
    rig.ticket_visible = True
    r = rig.send_input(Action("click", point=CONFIRM_POINT, resolved_name="Confirm and Send",
                              under_point_text="Confirm and Send", order_stage_step=True))
    assert r.refused is None and rig.dialog_open
    g = gate()
    now = rig.clock.monotonic()
    a = send()
    s = slow_half(a, ctx(g, flag=rig.gw.effective_flag(now), deny_rects=tuple(rig.gw.deny_rects),
                         session=SessionSafety("a3_a9", now - 0.2, EMPTY_SHA), now=now))
    assert not s.refused and s.send_ok and s.dialog_ok
    assert g.issued == 1 and g.pending is a
    rig.send_input(a, events=[InputEvent("move", 920, 610)])
    token = rig.gw.issue_click(920, 610, now, dialog_ok=s.dialog_ok, send_ok=s.send_ok)
    res = rig.send_input(a, events=[InputEvent("button_down", 920, 610, button="left"),
                                    InputEvent("button_up", 920, 610, button="left")])
    assert all(d.allowed for d in res.decisions) and rig.sent_orders == 1 and len(rig.book.working) == 1
    again = rig.send_input(a, events=[InputEvent("button_down", 920, 610, button="left")])
    assert [d.reason for d in again.decisions] == ["no_token"] and rig.sent_orders == 1
    row = rig.book.working[0]
    declared = g.declare(run_id="nightly-1", shard_id="shard-1", flow_id="flow.paper.send", step_id="s09",
                         row_key=row.key, sent_at="2026-11-09T19:00:00Z", token_id=token.id, symbol="CEI", qty=1.0)
    assert declared.row_key == row.key and g.declared_rows == {row.key} and g.pending is None


@pytest.mark.parametrize("name,action,context", [
    ("agent_profile", lambda: send(profile="agent"), lambda g: ctx(g)),
    ("explorer_profile", lambda: send(profile="explorer"), lambda g: ctx(g)),
    ("stale_frame", lambda: send(), lambda g: ctx(g, session=SessionSafety("a3_a9", NOW - 0.6, EMPTY_SHA))),
    ("no_assertion", lambda: send(), lambda g: ctx(g, session=SessionSafety("a3_a9", None, EMPTY_SHA))),
    ("wrong_account", lambda: send(), lambda g: ctx(g, session=SessionSafety("a3_a9", NOW - 0.2, GOLDEN_SHA))),
    ("undeclared_account", lambda: send(account_role=None), lambda g: ctx(g)),
    ("attestation_a3_a8", lambda: send(), lambda g: ctx(g, session=SessionSafety("a3_a8", NOW - 0.2, EMPTY_SHA))),
    ("session_kicked", lambda: send(), lambda g: ctx(g, session=SessionSafety("a3_a9", NOW - 0.2, EMPTY_SHA, True))),
    ("no_session", lambda: send(), lambda g: ctx(g, session=None, attestation_passed=True)),
    ("wrong_class", lambda: send(flow_class="standard"), lambda g: ctx(g)),
    ("dialog_not_foreground", lambda: send(), lambda g: ctx(g, dialog_foreground=False)),
    ("dialog_numbers_not_read", lambda: send(), lambda g: ctx(g, dialog_read=False)),
    ("no_dialog", lambda: send(), lambda g: ctx(g, order_dialog_present=False)),
    ("policy_disabled", lambda: send(), lambda g: ctx(SendGate(SendPolicy(), {"empty": EMPTY_SHA}))),
    ("no_send_gate", lambda: send(), lambda g: ctx(None)),
    ("one_label_only", lambda: send(under_point_text=None), lambda g: ctx(g)),
])
def test_each_failing_gate_is_a_never_tokenize_attempted_send(name, action, context):
    g = gate()
    s = slow_half(action(), context(g))
    assert (s.reason, s.attempted_send, s.send_ok) == ("never_tokenize", True, False), name
    assert refusal(s).attempted_send and g.issued == 0 and g.pending is None


def test_the_per_run_cap_refuses_the_sixth_send():
    g = gate()
    for i in range(PER_RUN_CAP):
        assert slow_half(send(), ctx(g)).send_ok, i
        g.declare(run_id="r", shard_id="shard-1", flow_id="f", step_id="s", row_key=f"row-{i}",
                  sent_at="2026-11-09T19:00:00Z")
    sixth = slow_half(send(), ctx(g))
    assert sixth.reason == "never_tokenize" and "per-run send cap" in sixth.detail
    assert len(g.declared_rows) == PER_RUN_CAP


def test_the_per_day_cap_counts_earlier_runs():
    g = gate(sends_today=PER_DAY_CAP - 1)
    assert slow_half(send(), ctx(g)).send_ok
    g.void()                                                     # a voided token keeps its count (choice 2)
    assert g.pending is None and g.issued == 1
    assert "per-day send cap" in slow_half(send(), ctx(g)).detail


def test_one_pending_send_at_a_time():
    g = gate()
    assert slow_half(send(), ctx(g)).send_ok
    s = slow_half(send(), ctx(g))
    assert s.reason == "never_tokenize" and "pending" in s.detail and g.issued == 1


def test_a_cleared_send_still_meets_the_flag_rules_and_is_not_committed_when_refused():
    g = gate()
    s = slow_half(send(mods=frozenset({"shift"})), ctx(g))
    assert s.reason == "shift_click_dialog" and s.attempted_send
    overlap = DenyRect(Rect(910, 600, 40, 28), "Auto Send")
    s = slow_half(send(), ctx(g, deny_rects=(SEND, overlap)))
    assert s.reason == "deny_rect" and "Auto Send" in s.detail
    assert g.issued == 0 and g.pending is None
    assert slow_half(send(), ctx(g, hook_alive=False)).reason == "hook_host_dead"


def test_declare_needs_a_pending_send_and_commit_needs_the_gates():
    g = gate()
    with pytest.raises(ValueError):
        g.declare(run_id="r", shard_id="shard-1", flow_id="f", step_id="s", row_key="row", sent_at="2026-11-09T19:00:00Z")
    with pytest.raises(ValueError):
        g.commit(send(profile="agent"), ctx(g))
    with pytest.raises(ValueError):
        SendGate(ON, {"margin": GOLDEN_SHA})


def test_send_policy_from_the_policy_file():
    example = yaml.safe_load((EXAMPLE / "policy" / "send_policy.yaml").read_text())
    assert SendPolicy.from_doc(example) == SendPolicy()                        # OPEN: the default branch
    signed = {"schema_version": 1, "decision": "approved", "enabled": True, "risk_owner": "risk-owner",
              "signed_at": "2026-11-01T12:00:00Z", "signature": "sig"}
    assert SendPolicy.from_doc(signed).enabled
    assert not SendPolicy.from_doc({**signed, "decision": "OPEN"}).enabled
    assert not SendPolicy.from_doc({**signed, "signature": None}).enabled
    assert not SendPolicy.from_doc({**signed, "enabled": "true"}).enabled
    assert not SendPolicy.from_doc(None).enabled
    assert replace(SendPolicy.from_doc(signed), per_run_cap=1).per_day_cap == PER_DAY_CAP
