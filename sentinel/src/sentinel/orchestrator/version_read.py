"""The build-id read of 7.3: main-window title first, Help > About OCR only when the title does not match.

`resolve_build_id` turns the two raw reads into the ids `rig.LaunchObservation` carries (`title_build_id`,
`about_build_id`) by mapping the version string the regex captures onto the build records (4.2 `version_string`,
config `builds/*.yaml`). The decisions over the result (`version_mismatch`, `version_changed_mid_run`,
`fingerprint_unreadable:build_id`) are `gates.check_launched` and `gates.check_build_unchanged`.

Rules (7.3, 4.2):

- The regexes are config values confirmed in Phase 0 (`phase0/branches.yaml` `version_read: {title_regex,
  about_regex}`, 4.16). Each must have a named group `version`; without one the whole match is the version string.
- The first match stands; a read is never retried into a different answer. A title that does not match falls to the
  About read exactly once; the About text is read lazily (a callable) so the OCR runs only then.
- A version string that exactly one build carries resolves to that build id.

Choices (reported):

1. Until Phase 0 records them, the title regex is the community-reported `Main@thinkorswim [build <version>]` and
   the About regex `build <version>` (7.3 "Community sources report"); `regexes` overrides either.
2. A version string that several builds carry (a branch build whose title does not tell it from main) is a
   non-match at that read (the title falls to About; About ambiguous too is unreadable), since the read cannot name
   the build. 4.2 leaves open whether the title distinguishes a branch build; that is a Phase 0 question.
3. A version string that no build carries is still a read: it is reported as `unlisted:<version>`, which differs
   from every requested build id, so the gate says `version_mismatch` (the updater landed a build nobody listed)
   rather than `fingerprint_unreadable`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_TITLE_REGEX = r"Main@thinkorswim \[build (?P<version>[^\]]+)\]"
DEFAULT_ABOUT_REGEX = r"build (?P<version>[0-9][0-9A-Za-z.\-]*)"
UNLISTED = "unlisted:"


@dataclass(frozen=True)
class BuildRead:
    """The outcome of the 7.3 version read: the ids for `LaunchObservation` and what was read where."""

    title_build_id: str | None          # LaunchObservation.title_build_id: None when the title did not resolve
    about_build_id: str | None          # LaunchObservation.about_build_id: read only when the title did not resolve
    source: str | None                  # title | about | None (unreadable)
    version_string: str | None
    detail: str

    @property
    def build_id(self) -> str | None:
        """The resolved id (or `unlisted:<version>`), None when both reads missed."""
        return self.title_build_id if self.title_build_id is not None else self.about_build_id


def _version(text: str | None, pattern: str) -> str | None:
    """The first match's `version` group (or the whole match), None when the pattern does not match."""
    if not text:
        return None
    m = re.search(pattern, text)
    if m is None:
        return None
    return (m.groupdict().get("version") or m.group(0)).strip() or None


def _builds(builds: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """build id -> version string, from `SentinelConfig.builds` (id -> document) or a list of build documents."""
    docs = builds.values() if isinstance(builds, Mapping) else builds
    out = {}
    for d in docs:
        b = d.get("build", d)
        out[str(b["build_id"])] = str(b["version_string"])
    return out


def _resolve(version: str, versions: Mapping[str, str]) -> tuple[str | None, str]:
    """(id, why) for one read version string: the single build carrying it, `unlisted:<v>`, or None (ambiguous)."""
    ids = sorted(b for b, v in versions.items() if v == version)
    if len(ids) == 1:
        return ids[0], f"version {version} is build {ids[0]}"
    if not ids:
        return f"{UNLISTED}{version}", f"version {version} is on no build record"
    return None, f"version {version} is carried by {', '.join(ids)}: the read cannot tell them apart"


def resolve_build_id(title: str | None, about_text: str | None | Callable[[], str | None],
                     builds: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
                     regexes: Mapping[str, str | None] | None = None) -> BuildRead:
    """The 7.3 version read over the main-window title (`GetWindowText`) and, only when the title does not resolve,
    the Help > About OCR text (`about_text`, a string or a callable that performs the OCR once). `regexes` is the
    `version_read` block of `phase0/branches.yaml` (`title_regex`, `about_regex`)."""
    rx = dict(regexes or {})
    title_rx = rx.get("title_regex") or DEFAULT_TITLE_REGEX
    about_rx = rx.get("about_regex") or DEFAULT_ABOUT_REGEX
    versions = _builds(builds)
    notes = []
    v = _version(title, title_rx)
    if v is not None:
        bid, why = _resolve(v, versions)
        if bid is not None:
            return BuildRead(bid, None, "title", v, f"title: {why}")
        notes.append(f"title: {why}")
    else:
        notes.append("title: no match")
    text = about_text() if callable(about_text) else about_text
    av = _version(text, about_rx)
    if av is not None:
        bid, why = _resolve(av, versions)
        notes.append(f"about: {why}")
        if bid is not None:
            return BuildRead(None, bid, "about", av, "; ".join(notes))
    else:
        notes.append("about: no match")
    return BuildRead(None, None, None, av or v, "; ".join(notes))
