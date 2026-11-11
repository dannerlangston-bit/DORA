"""Secret steps (5.4.10, 6.9, 7.7): leased at the last moment, typed with secret tokens while Capture is suspended,
recorded as `<redacted:...>` and a count, refused outside login flows, and the vault_unreachable path."""
from __future__ import annotations

import json

import pytest

from sentinel.executor.primitives import VK_PACKET, PlannedAction, issue_tokens
from sentinel.executor.secret import LEASE_RETRY_S, SecretResult, run_secret_step, vault_unreachable
from sentinel.orchestrator.gateway import VK_TAB, Flag, GatewayState, hook_decide
from sentinel.orchestrator.ports import VaultLease, VaultPort
from sentinel.store.secret_scan import SecretScanner
from sentinel.taxonomy import TaxonomyError

SECRET = "Tr0ub4dor&3!"
STEP = {"id": "s03", "type": "secret", "target": "login.password-field", "vault_ref": "login.password", "commit": "tab"}


class FakeVault:
    def __init__(self, log: list[str], fail: int = 0, fail_secret: int = 0):
        self.log, self.fail, self.fail_secret = log, fail, fail_secret
        self.released: list[VaultLease] = []

    def lease(self, alias: str) -> VaultLease:
        self.log.append(f"lease:{alias}")
        if self.fail:
            self.fail -= 1
            raise ConnectionError("vault down")
        return VaultLease(alias, f"lease-{len(self.log)}", "2026-11-11T10:00:00+00:00")

    def secret(self, lease: VaultLease) -> str:
        self.log.append("secret")
        if self.fail_secret:
            self.fail_secret -= 1
            raise TimeoutError("vault read timed out")
        return SECRET

    def release(self, lease: VaultLease) -> None:
        self.released.append(lease)


class FakeCapture:
    def __init__(self, log: list[str]):
        self.log, self.suspended = log, False

    def suspend_capture(self) -> None:
        self.log.append("suspend")
        self.suspended = True

    def resume_capture(self) -> None:
        self.log.append("resume")
        self.suspended = False


class Gateway:
    """The executor's emission over the real gateway state and hook: records what Capture was doing meanwhile."""

    def __init__(self, log: list[str], capture: FakeCapture, status: str = "passed", boom: bool = False):
        self.log, self.capture, self.status, self.boom = log, capture, status, boom
        self.state = GatewayState(armed=True)
        self.actions: list[PlannedAction] = []
        self.suspended_while_typing: list[bool] = []
        self.decisions = []

    def __call__(self, action: PlannedAction) -> str:
        self.log.append("emit")
        self.actions.append(action)
        if self.boom:
            raise RuntimeError("emitter crashed")
        t = 10.0
        for unit in action.units:
            issue_tokens(unit, self.state, t)
            for em in unit.events:
                self.state.stamp_watcher(Flag.CLEAR, t)
                self.suspended_while_typing.append(self.capture.suspended)
                self.decisions.append(hook_decide(self.state, em.event, t))
            t += 0.01
        return self.status


def run(**kw):
    log: list[str] = []
    vault = kw.pop("vault", None) or FakeVault(log, **kw.pop("vault_kw", {}))
    vault.log = log
    cap = FakeCapture(log)
    gw = Gateway(log, cap, **kw.pop("gw_kw", {}))
    sleeps: list[float] = []
    res = run_secret_step(kw.pop("step", STEP), vault, flow_class=kw.pop("flow_class", "login"), emit=gw,
                          capture=cap, now=5.0, sleep=sleeps.append, **kw)
    return res, log, vault, cap, gw, sleeps


def test_a_secret_is_leased_at_the_last_moment_and_typed_with_capture_suspended():
    res, log, vault, cap, gw, sleeps = run()
    assert log == ["lease:login.password", "secret", "suspend", "emit", "resume"]
    assert res.status == "passed" and res.chars == len(SECRET) and res.lease is not None
    assert gw.suspended_while_typing and all(gw.suspended_while_typing) and not cap.suspended
    assert all(d.allowed for d in gw.decisions) and sleeps == []
    assert vault.released == []                      # kept for the teardown scan, released there
    (action,) = gw.actions
    assert action.kind == "secret" and action.secret_ref == "login.password" and action.keys == (VK_TAB,)
    assert all(t.secret for t in gw.state.tokens.values())


def test_the_step_event_shows_only_the_placeholder_and_a_count():
    res, *_ = run()
    ev = res.step_event(run_id="r1", shard_id="sh1", side="candidate", seq=4, wall_at="2026-11-11T10:00:00+00:00",
                        mono_ms=1200, flow_id="flow.login.paper", step_id="s03")
    assert ev.target_text == "<redacted:login.password>" and ev.action == "secret"
    assert ev.keys == f"{len(SECRET)} chars + tab" and ev.step_outcome == "passed"


def test_the_secret_never_appears_in_a_record_or_an_emitted_event():
    res, log, vault, cap, gw, _ = run()
    scanner = SecretScanner({"login.password": SECRET})
    ev = res.step_event(run_id="r1", shard_id="sh1", side="candidate", seq=4, wall_at="2026-11-11T10:00:00+00:00",
                        mono_ms=1200, flow_id="flow.login.paper", step_id="s03")
    texts = {"step_event": json.dumps(ev.to_dict()), "result": repr(res), "log": json.dumps(log),
             "action": repr(gw.actions[0]),
             "events": repr([em.event for u in gw.actions[0].units for em in u.events]),
             "tokens": repr(list(gw.state.tokens.values()))}
    for where, text in texts.items():
        assert scanner.scan_text(text, where) == [], where
    # every typed key is a VK_PACKET pair: the clipboard (a Ctrl+V chord) is never used
    keys = {em.event.vk for u in gw.actions[0].units for em in u.events}
    assert keys == {VK_PACKET, VK_TAB}


def test_capture_resumes_even_when_the_emitter_fails():
    log: list[str] = []
    cap = FakeCapture(log)
    gw = Gateway(log, cap, boom=True)
    with pytest.raises(RuntimeError):
        run_secret_step(STEP, FakeVault(log), flow_class="login", emit=gw, capture=cap, now=1.0, sleep=lambda s: None)
    assert log[-2:] == ["emit", "resume"] and not cap.suspended


def test_a_secret_step_outside_a_login_flow_is_refused_before_any_lease():
    res, log, *_ = run(flow_class="standard")
    assert res.status == "failed" and "login" in res.detail and log == []
    assert res.lease is None and res.consequence is None


def test_a_failed_lease_is_retried_once_after_30_seconds():
    res, log, vault, cap, gw, sleeps = run(vault_kw={"fail": 1})
    assert sleeps == [LEASE_RETRY_S] and res.status == "passed"
    assert log[:3] == ["lease:login.password", "lease:login.password", "secret"]


def test_a_failed_secret_read_releases_its_lease_and_counts_as_the_lease_failing():
    res, log, vault, cap, gw, sleeps = run(vault_kw={"fail_secret": 1})
    assert sleeps == [LEASE_RETRY_S] and res.status == "passed" and len(vault.released) == 1


def test_two_failed_leases_are_vault_unreachable_and_nothing_is_typed():
    res, log, vault, cap, gw, sleeps = run(vault_kw={"fail": 2})
    assert res.status == "failed" and res.detail == "vault_unreachable" and gw.actions == []
    assert "suspend" not in log and sleeps == [LEASE_RETRY_S]
    c = res.consequence
    assert c.gate == "authenticated" and c.run.status == "complete_with_env_errors"
    assert (c.unreached.outcome, c.unreached.reason, c.unreached.gate) == ("env_error", "vault_unreachable",
                                                                           "authenticated")
    assert c.alert.priority == "P2" and "system_owner" in c.alert.paged and "on_call_channel" in c.alert.notified
    assert vault_unreachable(repeat=True).alert.priority == "P1"


def test_an_untypeable_secret_fails_the_step_without_quoting_it():
    class SpacedVault(FakeVault):
        def secret(self, lease):
            return "pass word"

    log: list[str] = []
    vault = SpacedVault(log)
    res, log, _, cap, gw, _ = run(vault=vault)
    assert res.status == "failed" and "pass word" not in res.detail and gw.actions == []
    assert "suspend" not in log and res.lease is not None


def test_the_emitter_outcome_is_the_step_outcome():
    res, *_ = run(gw_kw={"status": "blocked_by_gateway"})
    assert res.status == "blocked_by_gateway"
    with pytest.raises(TaxonomyError):
        SecretResult("typed", "login.password")


def test_a_vault_ref_that_cannot_be_redacted_is_refused():
    with pytest.raises(ValueError):
        run(step={**STEP, "vault_ref": "login password"})


def test_fake_vault_is_a_vault_port():
    assert isinstance(FakeVault([]), VaultPort)
