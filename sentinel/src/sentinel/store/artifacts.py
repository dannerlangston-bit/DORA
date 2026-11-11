"""Content-addressed artifact store on disk (8.4 Artifact layout on disk).

A set (a shard directory on the rig, `runs/<run_id>`, `baselines/<lineage>/<build_id>` or `eval/<version>` on
the control host) holds `objects/<aa>/<sha256>.<ext>`, with `aa` the first two hex digits of the hash, and one
`manifest.json` that maps human names to hashes.

Write path (8.4): hash the bytes, write them to a temporary name under `objects`, flush, read back and verify the
hash, then rename into place. A failed write or a failed read-back is retried once; a second failure raises
`ArtifactPersistError`, which the caller turns into `harness_error (reason: artifact_persist_failed)` (8.9).

Redaction (8.4, 6.10, 8.9): an image (or video) is refused unless it arrives with a redaction header saying the
redaction stage passed and naming the redaction version; the refusal is `RedactionRefused`, which the caller
turns into `harness_error (reason: redaction_version_missing)`. Images are recognised by media type, by kind, by
their leading bytes and by Pillow being able to read them, so a PNG, WebP or TIFF declared as text is still caught.

Plaintext secrets (6.9, 8.4): a set opened with a `scanner` (`store.secret_scan.SecretScanner`) scans every
non-image put before it is written; a hit raises `SecretLeaked` naming the secret references, never the value, and
nothing is written, so a leak mid-run stops uploads at once. `quarantine(run_id, quarantine_root)` moves the set's
files to `<quarantine_root>/<run_id>/` (the control host's `quarantine/<run_id>/`, 8.4), leaves a
`QUARANTINED.json` marker so later puts and reads raise `ArtifactQuarantined` (the report UI says "quarantined"),
and, given the store, sets `quarantined_at` on the set's `artifact_sets` rows.

Choices where 8.4 is silent:
- The "redaction header" is the `Redaction` value passed with the bytes, the in-process form of the
  redaction-version header of the 8.2 `PUT /v1/artifacts/{sha256}` request.
- Text artifacts (logs, exports, ledgers, reviewer requests) are accepted without a redaction header: 8.4 sends
  them through the plaintext scan instead (6.9). Their redaction version is recorded when given.
- Every set has its own directory (`ArtifactStore.at(root, set_path)` opens `<root>/<set_path>/`), so a set's
  root holds that set alone, and `quarantine/` lies outside every set: `quarantine` refuses a quarantine root
  inside the set it moves.
- Quarantine moves every file of the set (objects, manifest, ledgers under `shards/`), since a leaked secret can
  sit in a ledger as well as in an object. A file already in the quarantine directory with the same bytes is
  dropped; one with other bytes keeps both, the moved copy under the next free `<name>.<n>`.
- `manifest.json` is written by `write_manifest`, not on every put, so a shard of 2,900 captures does not
  rewrite a growing file 2,900 times; the ledger of 8.2 carries the artifact rows in between. An existing
  manifest is loaded when the set is opened, so a resumed writer appends to it.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..records import now

if TYPE_CHECKING:
    from .store import Store

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Kinds of 8.3 `artifacts.kind` that are pictures of the screen (8.4 kinds and sources).
VISUAL_KINDS = frozenset({"capture", "crop", "heatmap", "diff_mask", "agent_frame", "video", "attestation", "gate",
                          "mutant"})
EXTENSIONS = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/bmp": "bmp", "video/mp4": "mp4",
    "application/json": "json", "application/x-ndjson": "jsonl", "text/csv": "csv", "text/plain": "txt",
}
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")
MANIFEST = "manifest.json"
QUARANTINE_MARKER = "QUARANTINED.json"


class ArtifactError(Exception):
    """An artifact could not be stored or read as 8.4 requires."""


class RedactionRefused(ArtifactError):
    """An image without a passing redaction header (8.4, 8.9: redaction_version_missing)."""


class ArtifactHashMismatch(ArtifactError):
    """The bytes do not hash to the sha256 the sender named (8.2, 8.9 upload hash mismatch)."""


class ArtifactPersistError(ArtifactError):
    """A write or read-back failed twice (8.4, 8.9: artifact_persist_failed).

    `enospc` is true when the disk was full, so the Orchestrator also asks the Watchdog for its immediate
    free-space check (8.4, 10.2).
    """

    def __init__(self, message: str, *, enospc: bool = False):
        super().__init__(message)
        self.enospc = enospc


class ArtifactCorrupt(ArtifactError):
    """A stored object no longer hashes to its name (8.6 artifact audit: corrupt)."""


class ArtifactMissing(ArtifactError):
    """No object with this hash is in the set (8.6 artifact audit: missing)."""


class ArtifactQuarantined(ArtifactError):
    """The set was moved to quarantine by the plaintext scan (8.4, 8.9); security clears it (8.6)."""


class SecretLeaked(ArtifactError):
    """A text artifact holds the plaintext of a secret (6.9): nothing was written. `refs` names the secrets."""

    def __init__(self, message: str, refs: tuple[str, ...]):
        super().__init__(message)
        self.refs = refs


class TextScanner(Protocol):
    """What the put hook asks of a secret scanner (`store.secret_scan.SecretScanner`)."""

    def refs_in(self, data: bytes) -> tuple[str, ...]:
        """The references of the secrets whose plaintext `data` holds, in any scanned encoding."""


@dataclass(frozen=True)
class Redaction:
    """The redaction header that travels with an image (6.10): which redaction version ran and that it passed."""
    version: str
    passed: bool = True


@dataclass(frozen=True)
class StoredArtifact:
    """An object verified on disk; the fields are the 8.4 manifest entry plus where it lives."""
    sha256: str
    name: str
    kind: str
    media_type: str
    bytes: int
    set_path: str
    path: str
    redaction_version: str | None = None
    checkpoint_id: str | None = None
    side: str | None = None
    captured_at: str | None = None
    width: int | None = None
    height: int | None = None

    def manifest_entry(self) -> dict:
        """The 8.4 manifest entry for this object."""
        keys = ("name", "sha256", "kind", "bytes", "media_type", "checkpoint_id", "side", "redaction_version",
                "captured_at")
        return {k: getattr(self, k) for k in keys}


@dataclass(frozen=True)
class ManifestHeader:
    """The 8.4 manifest header: the run or build id, the shard, the config and harness commits, created_at."""
    config_commit: str
    harness_commit: str
    run_id: str | None = None
    build_id: str | None = None
    shard_id: str | None = None
    created_at: str = field(default_factory=now)

    def __post_init__(self):
        if (self.run_id is None) == (self.build_id is None):
            raise ArtifactError("a manifest header names exactly one of run_id and build_id (8.4)")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_back(path: Path) -> bytes:
    """Read a just-written file back for verification (a seam the tests use to simulate a bad read-back)."""
    return path.read_bytes()


def has_image_magic(head: bytes) -> bool:
    """True when the leading bytes are those of an image or video file (PNG, JPEG, GIF, BMP, TIFF, WebP, MP4)."""
    return (head.startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[4:8] == b"ftyp")


def is_visual(data: bytes, kind: str, media_type: str) -> bool:
    """True for anything that may show the screen: image or video media type, a visual kind, or image bytes."""
    return (media_type.startswith(("image/", "video/")) or kind in VISUAL_KINDS or has_image_magic(data[:16])
            or _dimensions(data) != (None, None))      # any other format Pillow reads is a picture too


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # not an image Pillow can read; dimensions stay unknown
        return None, None


class ArtifactStore:
    """One artifact set on disk (8.4): `<root>/objects/<aa>/<sha256>.<ext>` and `<root>/manifest.json`.

    `set_path` is the set's name in the 8.3 `artifacts.set_path` column (`runs/<run_id>`,
    `baselines/<lineage>/<build_id>`, `eval/<version>`, or the shard id on the rig). `scanner` turns on the
    plaintext check of every non-image put (6.9).
    """

    @classmethod
    def at(cls, root: str | os.PathLike, set_path: str, *, scanner: TextScanner | None = None) -> "ArtifactStore":
        """The set `set_path` in its own directory under an artifact root, `<root>/<set_path>/` (8.4: the control
        host's `runs/<run_id>/`), so one set's quarantine never moves or blocks another set's files."""
        return cls(Path(root) / set_path, set_path, scanner=scanner)

    def __init__(self, root: str | os.PathLike, set_path: str, *, scanner: TextScanner | None = None):
        self.root = Path(root)
        self.set_path = set_path
        self.scanner = scanner
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict] = {}
        mf = self.root / MANIFEST
        if mf.exists():
            for e in json.loads(mf.read_text(encoding="utf-8")).get("entries", []):
                self._entries[e["name"]] = e

    @property
    def quarantined(self) -> dict | None:
        """The quarantine marker (run_id, set_path, quarantined_at, moved_to), or None for a live set (8.4)."""
        marker = self.root / QUARANTINE_MARKER
        return json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None

    def _refuse_if_quarantined(self, what: str) -> None:
        q = self.quarantined
        if q is not None:
            raise ArtifactQuarantined(f"{what}: set {self.set_path} was quarantined at {q['quarantined_at']} "
                                      "(8.4); security clears it")

    # -- paths -----------------------------------------------------------------------------------
    def _dir(self, sha: str) -> Path:
        return self.objects / sha[:2]

    def path_of(self, sha: str) -> Path | None:
        """The object file for `sha`, or None when the set does not hold it."""
        if not SHA256_RE.match(sha):
            raise ArtifactError(f"not a sha256: {sha!r}")
        d = self._dir(sha)
        if not d.is_dir():
            return None
        for p in sorted(d.glob(f"{sha}*")):
            if p.name == sha or p.name.startswith(sha + "."):
                return p
        return None

    # -- write -----------------------------------------------------------------------------------
    def put(self, data: bytes, *, name: str, kind: str, media_type: str, redaction: Redaction | None = None,
            expected_sha256: str | None = None, checkpoint_id: str | None = None, side: str | None = None,
            captured_at: str | None = None) -> StoredArtifact:
        """Store `data` under its sha256 and add it to the manifest (8.4 write path).

        Raises RedactionRefused, SecretLeaked, ArtifactQuarantined, ArtifactHashMismatch or ArtifactPersistError;
        nothing is written on a refusal.
        """
        self._refuse_if_quarantined(name)
        visual = is_visual(data, kind, media_type)
        if visual and (redaction is None or not redaction.passed or not redaction.version):
            raise RedactionRefused(f"{name}: an image is stored only with a passing redaction header (8.4, 6.10)")
        if not visual and self.scanner is not None:
            refs = self.scanner.refs_in(data)
            if refs:
                raise SecretLeaked(f"{name}: holds the plaintext of {', '.join(refs)}; not stored (6.9)", refs)
        sha = sha256_hex(data)
        if expected_sha256 is not None and expected_sha256 != sha:
            raise ArtifactHashMismatch(f"{name}: bytes hash to {sha}, sender named {expected_sha256} (8.2)")
        old = self._entries.get(name)
        if old is not None and old["sha256"] != sha:
            raise ArtifactError(f"{name} already names {old['sha256']} in this set (8.4)")
        path = self.path_of(sha)
        if path is not None and sha256_hex(path.read_bytes()) != sha:
            path.unlink()                              # a corrupt copy is replaced, never trusted
            path = None
        if path is None:
            path = self._write_verified(data, sha, EXTENSIONS.get(media_type, "bin"))
        width, height = _dimensions(data) if visual else (None, None)
        art = StoredArtifact(sha256=sha, name=name, kind=kind, media_type=media_type, bytes=len(data),
                             set_path=self.set_path, path=str(path),
                             redaction_version=redaction.version if redaction else None,
                             checkpoint_id=checkpoint_id, side=side, captured_at=captured_at,
                             width=width, height=height)
        self._entries[name] = art.manifest_entry()
        return art

    def _write_verified(self, data: bytes, sha: str, ext: str) -> Path:
        final = self._dir(sha) / f"{sha}.{ext}"
        tmp = self._dir(sha) / f".{sha}.{os.getpid()}.tmp"
        last: Exception | None = None
        enospc = False
        for _attempt in range(2):                      # 8.4: retried once
            try:
                final.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                if sha256_hex(_read_back(tmp)) != sha:
                    raise ArtifactCorrupt(f"read-back of {sha} did not verify")
                os.replace(tmp, final)
                return final
            except (OSError, ArtifactCorrupt) as e:
                last = e
                enospc = enospc or getattr(e, "errno", None) == errno.ENOSPC
                try:
                    tmp.unlink()
                except OSError:
                    pass
        raise ArtifactPersistError(f"could not persist {sha}: {last}", enospc=enospc)

    # -- read ------------------------------------------------------------------------------------
    def get(self, sha: str) -> bytes:
        """The bytes of `sha`, verified against the hash (ArtifactMissing, ArtifactCorrupt, ArtifactQuarantined)."""
        self._refuse_if_quarantined(sha)
        path = self.path_of(sha)
        if path is None:
            raise ArtifactMissing(sha)
        data = path.read_bytes()
        if sha256_hex(data) != sha:
            raise ArtifactCorrupt(f"{path} no longer hashes to its name")
        return data

    def verify(self, sha: str) -> bool:
        """True when the object exists and still hashes to its name (8.6 artifact audit)."""
        try:
            self.get(sha)
        except ArtifactError:
            return False
        return True

    def unlink(self, sha: str) -> bool:
        """Remove the object file (the 8.6 sweep calls this only after the store set deleted_at)."""
        path = self.path_of(sha)
        if path is None:
            return False
        path.unlink()
        return True

    # -- quarantine (8.4, 8.6, 8.9) -----------------------------------------------------------------
    def quarantine(self, run_id: str, quarantine_root: str | os.PathLike, *, store: "Store | None" = None,
                   at: str | None = None) -> Path:
        """Move every file of this set to `<quarantine_root>/<run_id>/` after a plaintext-scan hit (8.4, 6.9).

        The marker is written first, so puts and reads are refused from that moment even if the move stops half
        way. With `store`, every `artifact_sets` row of this set not yet quarantined gets `quarantined_at` (8.4,
        SD-82). Returns the quarantine directory; quarantining a quarantined set moves whatever is left. Raises
        ArtifactError, moving nothing, when `quarantine_root` lies inside this set's root (8.4: a sibling of the sets).
        """
        if Path(quarantine_root).resolve().is_relative_to(self.root.resolve()):
            raise ArtifactError(f"quarantine root {quarantine_root} lies inside set {self.set_path}'s directory "
                                f"{self.root}; quarantine/ is outside every set (8.4)")
        ts = datetime.fromisoformat(at or now())
        stamp = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(
            timespec="seconds")
        dest = Path(quarantine_root) / run_id
        dest.mkdir(parents=True, exist_ok=True)
        marker = self.quarantined or {"run_id": run_id, "set_path": self.set_path, "quarantined_at": stamp,
                                      "moved_to": str(dest)}
        tmp = self.root / f".{QUARANTINE_MARKER}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.root / QUARANTINE_MARKER)
        for src in sorted(p for p in self.root.rglob("*") if p.is_file() and p.name != QUARANTINE_MARKER):
            target = dest / src.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() == src.read_bytes():
                src.unlink()
                continue
            n = 1
            while target.exists():
                target = target.with_name(f"{src.name}.{n}")
                n += 1
            shutil.move(str(src), str(target))
        for d in sorted((p for p in self.root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        self._entries = {}
        if store is not None:
            rows = store.query("SELECT sha256 FROM artifact_sets WHERE set_path = ? AND quarantined_at IS NULL",
                               (self.set_path,))
            if rows:
                with store.batch("quarantine") as b:
                    for r in rows:
                        b.update("artifact_sets", {"sha256": r[0], "set_path": self.set_path},
                                 {"quarantined_at": marker["quarantined_at"]})
        return dest

    # -- manifest --------------------------------------------------------------------------------
    @property
    def entries(self) -> list[dict]:
        return [self._entries[k] for k in sorted(self._entries)]

    def write_manifest(self, header: ManifestHeader) -> Path:
        """Write `manifest.json` atomically: the header and every entry, sorted by name (8.4)."""
        body = {"header": {k: v for k, v in asdict(header).items() if v is not None}, "entries": self.entries}
        out = self.root / MANIFEST
        tmp = self.root / f".{MANIFEST}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
        return out

    @staticmethod
    def read_manifest(root: str | os.PathLike) -> dict:
        return json.loads((Path(root) / MANIFEST).read_text(encoding="utf-8"))
