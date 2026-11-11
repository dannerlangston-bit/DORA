"""Inline segments shared by the PR comment and the static HTML report (9.1).

Report prose is built once as a sequence of typed segments and rendered twice: as GitHub
markdown for the PR comment and as HTML for the run report. The split is what makes 9.1's
rule hold by construction: screen text and model text only ever travel as `Text` or `Code`,
which each renderer escapes; the only `Link` and `Image` segments are built by `Urls` from
report UI paths the Reporter composes itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Text:
    """Plain text; escaped by the renderer (never interpreted as markup)."""

    text: str


@dataclass(frozen=True)
class Code:
    """A code span (ids, outcome names); backticks inside are neutralized by the renderer."""

    text: str


@dataclass(frozen=True)
class Link:
    """A link to a report UI URL composed by `Urls` (9.1: the only links allowed)."""

    label: str
    url: str


@dataclass(frozen=True)
class Image:
    """An artifact image by content hash, shown at 1:1 (9.1)."""

    alt: str
    sha: str
    url: str


Inline = Text | Code | Link | Image
Line = tuple[Inline, ...]

_HEX = re.compile(r"^[0-9a-f]{4,64}$")


def is_artifact_hash(sha: str | None) -> bool:
    """True for a lowercase hex content hash (full sha256, or the shortened form of 9.3)."""
    return bool(sha) and bool(_HEX.match(sha))


@dataclass(frozen=True)
class Urls:
    """Report UI URL scheme of 9.3 (`/runs/<run>`, `/a/<sha256>`, `/cp/<finding>`, `/act/...`), plus the manifest
    template page `/docs/manifest-template` (the path is this module's choice; 9.3 shows none)."""

    base: str
    run_id: str

    def _q(self, s: str) -> str:
        return quote(s, safe="-._~")

    @property
    def report(self) -> str:
        return f"{self.base}/runs/{self._q(self.run_id)}"

    @property
    def matrix(self) -> str:
        return f"{self.report}/coverage"

    @property
    def manifest_template(self) -> str:
        """The change manifest template page (4.3, 2.10.2) of the report UI."""
        return f"{self.base}/docs/manifest-template"

    def anchor(self, name: str) -> str:
        return f"{self.report}#{self._q(name)}"

    def artifact(self, sha: str) -> str:
        if not is_artifact_hash(sha):
            raise ValueError(f"not an artifact hash: {sha!r}")
        return f"{self.base}/a/{sha}"

    def checkpoint(self, ref: str) -> str:
        return f"{self.base}/cp/{self._q(ref)}"

    def finding(self, finding_id: str) -> str:
        return f"{self.base}/f/{self._q(finding_id)}"

    def action(self, action: str, ref: str) -> str:
        return f"{self.base}/act/{self._q(action)}?f={self._q(ref)}"

    def image(self, alt: str, sha: str | None) -> Image | None:
        return Image(alt, sha, self.artifact(sha)) if is_artifact_hash(sha) else None


def T(text: str) -> Text:
    return Text(text)


def C(text: str) -> Code:
    return Code(text)


def plain(line: Line) -> str:
    """The line as plain text (for measuring and tests); images print their alt text."""
    out = []
    for seg in line:
        if isinstance(seg, (Text, Code)):
            out.append(seg.text)
        elif isinstance(seg, Link):
            out.append(seg.label)
        else:
            out.append(seg.alt)
    return "".join(out)


def join_and(parts: list[Line], sep: str = ", ", last: str = " and ") -> Line:
    """Join lines as an English list: "a", "a and b", "a, b and c"."""
    out: list[Inline] = []
    for i, part in enumerate(parts):
        if i:
            out.append(Text(last if i == len(parts) - 1 else sep))
        out.extend(part)
    return tuple(out)


def words_and(words: list[str]) -> str:
    """Plain-string variant of `join_and`."""
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                 "ten", "eleven", "twelve")


def number_word(n: int) -> str:
    """Small counts spelled out ("all four widths", 9.3); larger ones as digits."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"
