"""L-XREF-28: `rig/thinkorswim.vmoptions.template` carries the heap, crash and Java2D flags (4.15, 7.1, 7.3 step 4).

The template is plain text with no schema (4.16); it is read here from the checkout, not through the 4.1 layout. The
check is `rigfiles.validate_vmoptions`, the validator the Rig Manager and preflight B3 and H10 apply to the written
file: `-Xms` and `-Xmx`, `-XX:ErrorFile`, `-XX:+HeapDumpOnOutOfMemoryError`, `-XX:HeapDumpPath`,
`-XX:+CreateCoredumpOnCrash`, and the VM rig's Java2D flags (`-Dsun.java2d.d3d=false`, `-Dsun.java2d.noddraw=true`),
each flag known to a JDK major; its `${name}` placeholders must parse (`rigmanager.placeholders`), since the Rig
Manager renders it. The checked-in template is the VM rig's, the Phase 1 fleet (7.1); `phase0/branches.yaml`
records no JDK major yet, so the flags are checked against the table without one (rigfiles choice 2).
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from ...rigfiles import validate_vmoptions
from ...rigmanager import placeholders
from ..repo import Repo

RULE_IDS = ("L-XREF-28",)
TEMPLATE = "rig/thinkorswim.vmoptions.template"


def check(repo: Repo, add: Callable[..., None], phase: int, today: date) -> None:
    """Report a missing template and every problem `validate_vmoptions` finds in it."""
    path = repo.path(TEMPLATE)
    if not path.is_file():
        add("L-XREF-28", TEMPLATE, "the vmoptions template is missing; the Rig Manager writes thinkorswim.vmoptions "
                                   "from it (7.3 step 4)")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        add("L-XREF-28", TEMPLATE, f"the vmoptions template cannot be read: {e}")
        return
    try:
        placeholders(text)
    except ValueError as e:
        add("L-XREF-28", TEMPLATE, f"the template cannot be rendered: {e}")
    for problem in validate_vmoptions(text, "vm"):
        add("L-XREF-28", TEMPLATE, problem)
