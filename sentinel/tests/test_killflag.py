"""The kill switch's human channels: file flag, named pipe, `sentinel stop <rig>` and preflight H5 (6.6, 12.7 H5)."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.killflag import (FLAG_PATH, PIPE_NAME, PROBE_REASON, FileFlagChannel, MemoryKillChannel,
                                            acknowledge, check, h5_passed, h5_round_trip, main, pending, poll,
                                            rig_flag_path, tripwires)
from sentinel.orchestrator.killswitch import PROVEN_NONE, TripWires
from sentinel.orchestrator.machine import Orchestrator
from sentinel.orchestrator.ports import KillChannel, KillRequest
from sentinel.orchestrator.simrig import MemorySink, SimulatedRig, default_context

FLOW = "flow.trade.order-ticket.stock.limit"
REQ = KillRequest("rig-01", "cli", "asmith", "wrong build on the rig", "2026-11-10T14:00:00+00:00")


def flag(tmp_path: Path) -> FileFlagChannel:
    return FileFlagChannel(rig_flag_path(tmp_path, "rig-01"))


def test_the_fixed_path_and_the_simulated_rig_layout():
    assert FLAG_PATH == PureWindowsPath(r"C:\sentinel\run\kill.flag") and PIPE_NAME == r"\\.\pipe\sentinel-kill"
    assert rig_flag_path(Path("/rigs"), "rig-01") == Path("/rigs/rig-01/run/kill.flag")
    assert isinstance(FileFlagChannel(Path("x")), KillChannel) and isinstance(MemoryKillChannel(), KillChannel)


def test_the_file_flag_round_trips_a_request_and_acknowledge_keeps_a_newer_one(tmp_path):
    ch = flag(tmp_path)
    assert ch.poll() is None
    ch.post(REQ)
    assert ch.poll() == REQ and poll(ch) == REQ and not list(ch.path.parent.glob("*.tmp"))
    newer = KillRequest("rig-01", "cli", "bjones", "second stop", "2026-11-10T14:00:05+00:00")
    ch.post(newer)
    ch.acknowledge(REQ)                                   # handled the first; the second stays pending
    assert ch.poll() == newer
    ch.acknowledge(newer)
    assert ch.poll() is None and not ch.path.exists()


def test_a_flag_that_cannot_be_read_and_a_channel_that_raises_are_stop_requests(tmp_path):
    ch = flag(tmp_path)
    ch.path.parent.mkdir(parents=True)
    ch.path.write_text("not json", encoding="utf-8")
    assert poll(ch) is not None and "not a request" in poll(ch).reason

    class Broken(MemoryKillChannel):
        def poll(self):
            raise OSError("pipe broken")

    assert "pipe broken" in poll(Broken()).reason
    assert check(None, Broken(), gate="running").run_reason == "kill_switch"


def test_human_channels_feed_the_tripwires_and_the_decision_names_the_request(tmp_path):
    ch, pipe = flag(tmp_path), MemoryKillChannel()
    assert pending(ch, pipe) == {} and check(ch, pipe, gate="running") is None
    ch.post(REQ)
    assert tripwires(pending(ch, pipe)) == TripWires(file_flag=True)
    r = check(ch, pipe, gate="running", running_flow=FLOW)
    assert (r.run_status, r.run_reason) == ("aborted_safety", "kill_switch")
    assert (r.unreached.outcome, r.unreached.gate, r.unreached.reason) == ("env_error", "running", "kill_switch")
    assert r.has("quarantine_flow") and r.alert.is_safety and r.evidence["trigger"] == "file_flag"
    assert r.evidence["request"]["requested_by"] == "asmith" and "requested by asmith via cli" in r.detail
    ch.acknowledge(REQ)
    pipe.post(KillRequest("rig-01", "pipe", "drill-injector", "drill item 5", "2026-11-10T14:00:00+00:00"))
    assert check(ch, pipe, gate="running").evidence["trigger"] == "named_pipe"


@pytest.mark.parametrize("wires,trigger", [(TripWires(gateway_drops=10), "ten_drops"),
                                           (TripWires(new_working_or_filled=True), "new_order_row"),
                                           (TripWires(dialog_open_s=31.0), "dialog_open_30s")])
def test_automatic_tripwires_of_the_same_sample_carry_kill_switch_too(tmp_path, wires, trigger):
    r = check(flag(tmp_path), MemoryKillChannel(), gate="running", wires=wires)          # SD-59
    assert r.evidence["trigger"] == trigger and r.run_reason == r.unreached.reason == "kill_switch"
    assert "request" not in r.evidence


def test_the_h5_probe_is_never_a_stop_and_a_human_request_never_a_probe(tmp_path):
    ch = flag(tmp_path)
    ch.post(KillRequest("rig-01", "preflight", "sentinel", PROBE_REASON, "t"))
    assert poll(ch) is None and check(ch, gate="running") is None
    assert main(["rig-01", "--root", str(tmp_path), "--reason", PROBE_REASON, "--by", "asmith"]) == 2


def test_sentinel_stop_writes_the_flag_and_reports_usage_and_transport_errors(tmp_path, capsys):
    clock = ManualClock()
    assert main(["rig-01", "--root", str(tmp_path), "--by", "asmith", "--reason", "smoke"], clock=clock) == 0
    req = flag(tmp_path).poll()
    assert (req.rig_id, req.channel, req.requested_by, req.reason) == ("rig-01", "cli", "asmith", "smoke")
    assert req.at == clock.now().isoformat() and "kill switch requested on rig-01" in capsys.readouterr().out
    assert main(["rig-02"]) == 2 and "--root" in capsys.readouterr().err
    guest = MemoryKillChannel()
    assert main(["rig-03", "--by", "asmith"], transport=lambda rig: guest) == 0 and guest.poll().rig_id == "rig-03"

    class Down(MemoryKillChannel):
        def post(self, request):
            raise OSError("guest channel down")

    assert main(["rig-04"], transport=lambda rig: Down()) == 1 and "guest channel down" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([])


def test_sentinel_stop_mid_run_aborts_kill_switch_runs_the_sequence_and_proves_no_orders_sent(tmp_path):
    """The gap's end-to-end case: `sentinel stop rig-01` against a simulated rig mid-run."""
    clock = ManualClock()
    rig = SimulatedRig(clock=clock)
    sink = MemorySink(["c1", "c2", "c3"])
    ch, pipe = flag(tmp_path), MemoryKillChannel()

    def work(o: Orchestrator) -> None:
        o.sink.record("c1", "identical")
        assert check(ch, pipe, gate="running", running_flow=FLOW) is None
        assert main(["rig-01", "--root", str(tmp_path), "--by", "asmith"], clock=clock) == 0
        r = check(ch, pipe, gate="running", running_flow=FLOW)            # the next step boundary
        o.apply(r)

    report = Orchestrator(rig, default_context(), sink, clock=clock).run(work)
    assert (report.verdict.status, report.verdict.reason) == ("aborted_safety", "kill_switch")
    assert sink.outcomes["c1"][0] == "identical"
    assert {sink.outcomes[c][:3] for c in ("c2", "c3")} == {("env_error", "kill_switch", "running")}
    ks = report.kill_switch
    assert ks is not None and [s for s, _ in ks.steps] == ["deny_all", "kill_tree", "recovery_login", "proof",
                                                           "revert_snapshot"]
    assert report.proof.verdict == PROVEN_NONE and report.proof.line == "PROVEN: no orders sent" and rig.gw.deny_all
    asked = KillRequest(**ks.result.evidence["request"])
    assert asked.requested_by == "asmith" and acknowledge(ch, pipe) == [asked] and ch.poll() is None


def test_h5_round_trips_both_channels_and_leaves_nothing_behind(tmp_path):
    ch, pipe = flag(tmp_path), MemoryKillChannel()
    read = h5_round_trip(ch, pipe, rig_id="rig-01", clock=ManualClock())
    assert read.item == "H5" and read.values == {"file_flag": True, "named_pipe": True} and read.detail is None
    assert h5_passed(read) and ch.poll() is None and pipe.poll() is None


def test_h5_fails_when_the_flag_cannot_be_written_or_cleared(tmp_path):
    class ReadOnly(FileFlagChannel):
        def post(self, request):
            raise PermissionError("access denied by policy")

    read = h5_round_trip(ReadOnly(rig_flag_path(tmp_path, "rig-01")), MemoryKillChannel(), rig_id="rig-01",
                         clock=ManualClock())
    assert read.values == {"file_flag": False, "named_pipe": True} and not h5_passed(read)
    assert read.detail == "file_flag: PermissionError: access denied by policy"

    class Sticky(MemoryKillChannel):
        def acknowledge(self, request):
            pass                                            # the write works, the clear never does

    stuck = h5_round_trip(MemoryKillChannel(), Sticky(), rig_id="rig-01", clock=ManualClock())
    assert stuck.values["named_pipe"] is False and "did not clear" in stuck.detail


def test_h5_never_overwrites_a_pending_stop_and_clears_a_stale_probe(tmp_path):
    ch = flag(tmp_path)
    ch.post(REQ)
    read = h5_round_trip(ch, MemoryKillChannel(), rig_id="rig-01", clock=ManualClock())
    assert not h5_passed(read) and "a stop request is pending (asmith via cli" in read.detail and ch.poll() == REQ
    ch.acknowledge(REQ)
    ch.post(KillRequest("rig-01", "preflight", "sentinel", PROBE_REASON, "earlier"))
    assert h5_passed(h5_round_trip(ch, MemoryKillChannel(), rig_id="rig-01", clock=ManualClock()))
    assert ch.poll() is None


def test_acknowledge_marks_every_pending_request_handled(tmp_path):
    ch, pipe = flag(tmp_path), MemoryKillChannel()
    pipe_req = KillRequest("rig-01", "pipe", "watchdog", "stop", "t")
    ch.post(REQ)
    pipe.post(pipe_req)
    assert acknowledge(ch, pipe) == [REQ, pipe_req] and pending(ch, pipe) == {}
    assert acknowledge(ch, None) == []
