"""rigfiles: the JRE release file, the jar listing, vmoptions, the process allowlist and the 7.12 hardening reads;
and L-XREF-28 over the checked-in template (7.1, 7.3, 7.12, 12.7 B3 and H10)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from sentinel.lint.checks import rigfiles as lint_rigfiles
from sentinel.lint.repo import Repo
from sentinel.orchestrator.ports import ProcessInfo
from sentinel.rigfiles import (HardeningReads, ProcessAllowlist, check_hardening, check_processes, jar_listing_hash,
                               java_major, load_process_allowlist, parse_jre_release, parse_vmoptions, pinned_java2d,
                               validate_vmoptions, vmoptions_sha256)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
RELEASE = ('IMPLEMENTOR="Azul Systems, Inc."\nJAVA_VERSION="11.0.12"\nJAVA_VERSION_DATE="2021-07-20"\n'
           'OS_ARCH="x86_64"\nOS_NAME="Windows"\n')
VM_OPTIONS = """# rendered
-Xms512m
-Xmx4g
-Dsun.java2d.d3d=false
-Dsun.java2d.noddraw=true
-XX:ErrorFile=C:\\sentinel\\crash\\hs_err_pid%p.log
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=C:\\sentinel\\crash
-XX:+CreateCoredumpOnCrash
"""


# -- JRE release and jar listing ---------------------------------------------------------------------------------
def test_parse_jre_release_reads_the_three_values_and_hashes_the_whole_file():
    r = parse_jre_release(RELEASE)
    assert (r.java_version, r.implementor, r.os_arch, r.major) == ("11.0.12", "Azul Systems, Inc.", "x86_64", 11)
    assert r.sha256.startswith("sha256:") and len(r.sha256) == 71
    assert r.fingerprint() == {"sha256": r.sha256, "java_version": "11.0.12"}
    assert parse_jre_release(RELEASE + "\n").sha256 != r.sha256          # the whole file is hashed


def test_parse_jre_release_without_java_version_is_unreadable():
    with pytest.raises(ValueError, match="JAVA_VERSION"):
        parse_jre_release('IMPLEMENTOR="x"\n')


@pytest.mark.parametrize("version,major", [("1.8.0_292", 8), ("11.0.12", 11), ("17", 17), ("21.0.1+12", 21)])
def test_java_major(version, major):
    assert java_major(version) == major


def test_jar_listing_hash_does_not_depend_on_order_and_sees_sizes():
    entries = [("lib/a.jar", 10), ("lib\\b.jar", 20), ("thinkorswim.jar", 3)]
    assert jar_listing_hash(entries) == jar_listing_hash(reversed(entries))
    assert jar_listing_hash(entries) != jar_listing_hash([("lib/a.jar", 11), ("lib/b.jar", 20), ("thinkorswim.jar", 3)])
    with pytest.raises(ValueError):
        jar_listing_hash([("a.jar", 1), ("a.jar", 1)])


# -- vmoptions -------------------------------------------------------------------------------------------------------
def test_a_complete_vm_options_file_is_valid_and_pins_software():
    assert validate_vmoptions(VM_OPTIONS, "vm", 11) == ()
    assert pinned_java2d(VM_OPTIONS) == "software"
    assert vmoptions_sha256(VM_OPTIONS) != vmoptions_sha256(VM_OPTIONS.replace("4g", "3g"))


def test_missing_create_coredump_is_named():
    text = VM_OPTIONS.replace("-XX:+CreateCoredumpOnCrash\n", "")
    problems = validate_vmoptions(text, "vm", 11)
    assert any("-XX:+CreateCoredumpOnCrash is missing" in p for p in problems)


def test_d3d_true_on_a_vm_rig_is_refused_and_physical_needs_it():
    d3d = VM_OPTIONS.replace("-Dsun.java2d.d3d=false\n-Dsun.java2d.noddraw=true\n", "-Dsun.java2d.d3d=true\n")
    assert pinned_java2d(d3d) == "d3d"
    assert any("sun.java2d.d3d=false is required on a vm rig" in p for p in validate_vmoptions(d3d, "vm", 11))
    assert validate_vmoptions(d3d, "physical", 11) == ()
    assert any("physical" in p for p in validate_vmoptions(VM_OPTIONS, "physical", 11))


def test_flags_are_checked_against_the_jdk_major():
    problems = validate_vmoptions(VM_OPTIONS, "vm", 8)
    assert any("CreateCoredumpOnCrash is not known to JDK 8" in p for p in problems)
    unknown = VM_OPTIONS + "-XX:+UseShinyNewThing\n"
    assert any("UseShinyNewThing is not a flag checked" in p for p in validate_vmoptions(unknown, "vm"))


def test_disabled_heap_dump_repeats_and_garbage_lines_are_problems():
    text = VM_OPTIONS.replace("-XX:+HeapDumpOnOutOfMemoryError", "-XX:-HeapDumpOnOutOfMemoryError") + "-Xmx2g\nhello\n"
    problems = validate_vmoptions(text, "vm", 11)
    assert any("must be -XX:+HeapDumpOnOutOfMemoryError" in p for p in problems)
    assert any("-Xmx is given twice" in p for p in problems)
    assert any("'hello' is not a JVM option" in p for p in problems)
    opts, bad = parse_vmoptions("-XX:HeapDumpPath=C:\\Program Files\\crash\n")
    assert bad == () and opts[0].value == "C:\\Program Files\\crash"


def test_opengl_flag_changes_the_pinned_pipeline():
    assert pinned_java2d("-Dsun.java2d.opengl=true\n") == "opengl"
    assert pinned_java2d("-Xmx1g\n") == "d3d"


# w4-rig:1: Java2D reads its boolean properties with WindowsFlags.getBooleanProp: exactly '', t, T, true and True
# are true, f, F, false and False false, and any other spelling keeps the default
@pytest.mark.parametrize("line", ["-Dsun.java2d.opengl", "-Dsun.java2d.opengl=t", "-Dsun.java2d.opengl=T",
                                  "-Dsun.java2d.opengl=True"])
def test_opengl_in_any_spelling_the_jvm_reads_as_true_is_refused_on_a_vm_rig(line):
    text = VM_OPTIONS + line + "\n"
    assert pinned_java2d(text) == "opengl"
    assert any("pin the opengl Java2D pipeline" in p for p in validate_vmoptions(text, "vm", 11))


def test_java2d_pins_in_a_spelling_the_jvm_ignores_are_refused_and_pin_d3d():
    upper = VM_OPTIONS.replace("d3d=false", "d3d=FALSE").replace("noddraw=true", "noddraw=TRUE")
    assert pinned_java2d(upper) == "d3d"                                     # the JVM keeps Direct3D on
    problems = validate_vmoptions(upper, "vm", 11)
    assert any("-Dsun.java2d.d3d=false is required on a vm rig" in p and "'FALSE'" in p for p in problems)
    assert any("-Dsun.java2d.noddraw=true is required on a vm rig" in p for p in problems)
    assert any("pin the d3d Java2D pipeline" in p for p in problems)
    ignored = VM_OPTIONS + "-Dsun.java2d.opengl=TRUE\n"
    assert pinned_java2d(ignored) == "software"
    assert any("sun.java2d.opengl='TRUE' is not a value the JVM reads" in p for p in validate_vmoptions(ignored, "vm"))


def test_an_explicit_d3d_true_wins_over_noddraw_as_in_the_jvm():
    both = "-Dsun.java2d.d3d=true\n-Dsun.java2d.noddraw=true\n"
    assert pinned_java2d(both) == "d3d" and pinned_java2d("-Dsun.java2d.noddraw=t\n") == "software"


def test_a_value_flag_given_as_plus_or_minus_and_a_boolean_flag_given_a_value_are_refused():
    text = VM_OPTIONS.replace("-XX:ErrorFile=C:\\sentinel\\crash\\hs_err_pid%p.log", "-XX:+ErrorFile") \
        .replace("-XX:HeapDumpPath=C:\\sentinel\\crash", "-XX:-HeapDumpPath")
    problems = validate_vmoptions(text, "vm", 11)
    assert any("-XX:+ErrorFile must be -XX:ErrorFile=<value>" in p for p in problems)
    assert any("-XX:-HeapDumpPath must be -XX:HeapDumpPath=<value>" in p for p in problems)
    other = VM_OPTIONS + "-XX:+MaxMetaspaceSize\n-XX:UseG1GC=true\n"
    problems = validate_vmoptions(other, "vm", 11)
    assert any("-XX:+MaxMetaspaceSize must be -XX:MaxMetaspaceSize=<value>" in p for p in problems)
    assert any("-XX:UseG1GC=true must be -XX:+UseG1GC or -XX:-UseG1GC" in p for p in problems)
    assert validate_vmoptions(VM_OPTIONS + "-XX:MaxMetaspaceSize=512m\n-XX:-UseG1GC\n", "vm", 11) == ()


# -- process allowlist -----------------------------------------------------------------------------------------------
ALLOW = ProcessAllowlist("base-os.g1", ("orch.exe", "hook.exe", "watchdog.exe", "helper.exe"), ("thinkorswim.exe",),
                         ("System", "svchost.exe", "explorer.exe"))


def test_allowed_processes_pass_case_insensitively_and_baseline_repeats():
    procs = ["System", "svchost.exe", "SVCHOST.EXE", "explorer.exe", "Orch.exe", "hook.exe", "watchdog.exe",
             ProcessInfo(pid=9, ppid=1, name="thinkorswim.exe")]
    assert check_processes(procs, ALLOW) is None


def test_onedrive_is_rig_spec_mismatch_naming_it():
    r = check_processes(["System", "OneDrive.exe", "orch.exe"], ALLOW)
    assert r is not None and not r.passed
    assert (r.unreached.outcome, r.unreached.reason, r.unreached.gate) == ("env_error", "rig_spec_mismatch",
                                                                           "provisioned")
    assert r.run.status == "complete_with_env_errors" and "OneDrive.exe" in r.detail


def test_a_fourth_harness_process_fails():
    r = check_processes(["orch.exe", "hook.exe", "watchdog.exe", "watchdog.exe", "helper.exe"], ALLOW)
    assert r is not None and "watchdog.exe x2" in r.detail and r.evidence["duplicate_harness"] == ["watchdog.exe"]


def test_the_example_allowlist_loads_and_holds_four_harness_images():
    a = load_process_allowlist(EXAMPLE / "rig" / "process_allowlist.yaml")
    assert len(a.harness) == 4 and "thinkorswim.exe" in a.app_tree and a.sha256.startswith("sha256:")
    assert check_processes([*a.harness, *a.app_tree, "svchost.exe"], a) is None


# -- hardening -------------------------------------------------------------------------------------------------------
GOOD = HardeningReads(audio_muted=True, mouse_enhance_precision=False, mouse_speed=10, taskbar_animations=False,
                      wallpaper_solid=True, taskbar_autohide=False, foreign_windows=0)


def test_hardening_passes_on_the_pins_and_fails_when_audio_is_on():
    assert check_hardening(GOOD) is None
    r = check_hardening(HardeningReads(**{**GOOD.__dict__, "audio_muted": False}))
    assert r is not None and r.unreached.reason == "rig_spec_mismatch" and "audio_muted is False" in r.detail


def test_hardening_unreadable_and_mouse_reads_are_named():
    r = check_hardening(HardeningReads(**{**GOOD.__dict__, "mouse_speed": None, "mouse_enhance_precision": True}))
    assert r is not None and "mouse_speed unreadable" in r.detail and "mouse_enhance_precision is True" in r.detail


# -- L-XREF-28 ---------------------------------------------------------------------------------------------------------
def _lint(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    lint_rigfiles.check(Repo(root), lambda rule, file, msg, path="", severity=None: out.append((rule, msg)),
                        1, date(2026, 11, 10))
    return out


def test_the_checked_in_template_is_lint_clean_and_renders_validly():
    assert _lint(EXAMPLE) == []


def test_lint_reports_a_template_without_the_crash_flag(tmp_path):
    (tmp_path / "rig").mkdir()
    text = (EXAMPLE / lint_rigfiles.TEMPLATE).read_text(encoding="utf-8")
    (tmp_path / lint_rigfiles.TEMPLATE).write_text(text.replace("-XX:+CreateCoredumpOnCrash\n", ""), encoding="utf-8")
    assert any("CreateCoredumpOnCrash" in m for r, m in _lint(tmp_path) if r == "L-XREF-28")


def test_lint_reports_a_template_that_cannot_be_rendered(tmp_path):
    (tmp_path / "rig").mkdir()
    text = (EXAMPLE / lint_rigfiles.TEMPLATE).read_text(encoding="utf-8")
    (tmp_path / lint_rigfiles.TEMPLATE).write_text("# costs $5\n" + text, encoding="utf-8")
    assert any("cannot be rendered: line 1" in m for _r, m in _lint(tmp_path))


def test_lint_reports_a_missing_template(tmp_path):
    shutil.copytree(EXAMPLE / "gateway", tmp_path / "gateway")
    assert [r for r, _ in _lint(tmp_path)] == ["L-XREF-28"]
