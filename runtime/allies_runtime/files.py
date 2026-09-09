"""Verified, profile-local staging for one immutable incoming file manifest."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .errors import IncomingFileError

MAX_FILES = 10
MAX_FILE_BYTES = 25_000_000
MAX_TOTAL_BYTES = 50_000_000
MAX_CHUNK_BYTES = 64 * 1024
MAX_FILE_SECONDS = 120.0
MAX_SET_SECONDS = 300.0
_RECEIPT_SCHEMA = "allies.incoming-files.v1"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10))}
)


@dataclass(frozen=True, slots=True)
class IncomingFile:
    file_id: str
    name: str
    media_type: str
    size: int
    sha256: str

    def manifest_value(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StagedFile:
    descriptor: IncomingFile
    path: str

    def hermes_value(self) -> dict[str, object]:
        return {**self.descriptor.manifest_value(), "path": self.path}


@dataclass(frozen=True, slots=True)
class StagedManifest:
    command_id: str
    manifest_sha256: str
    files: tuple[StagedFile, ...]

    def hermes_context(self) -> dict[str, object]:
        return {
            "schema_version": "v1",
            "kind": "allies_incoming_files",
            "files": [item.hermes_value() for item in self.files],
        }


FileFetcher = Callable[[IncomingFile], AsyncIterator[bytes] | Awaitable[AsyncIterator[bytes]]]


def parse_incoming_files(value: object) -> tuple[IncomingFile, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_FILES:
        raise IncomingFileError("incoming file manifest was invalid")
    files: list[IncomingFile] = []
    identifiers: set[str] = set()
    total = 0
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "file_id",
            "name",
            "media_type",
            "size",
            "sha256",
        }:
            raise IncomingFileError("incoming file manifest was invalid")
        try:
            file_id = str(UUID(str(item["file_id"])))
        except (TypeError, ValueError):
            raise IncomingFileError("incoming file manifest was invalid") from None
        name = item["name"]
        media_type = item["media_type"]
        size = item["size"]
        sha256 = item["sha256"]
        if (
            not _safe_name(name)
            or not _safe_media_type(media_type)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_FILE_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or file_id in identifiers
        ):
            raise IncomingFileError("incoming file manifest was invalid")
        identifiers.add(file_id)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise IncomingFileError("incoming file manifest was invalid")
        files.append(
            IncomingFile(
                file_id=file_id,
                name=name,
                media_type=media_type,
                size=size,
                sha256=sha256,
            )
        )
    return tuple(files)


def validate_hermes_file_context(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "kind",
        "files",
    }:
        raise IncomingFileError("incoming file context was invalid")
    if value.get("schema_version") != "v1" or value.get("kind") != "allies_incoming_files":
        raise IncomingFileError("incoming file context was invalid")
    rows = value.get("files")
    if not isinstance(rows, list):
        raise IncomingFileError("incoming file context was invalid")
    manifest = []
    paths = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "file_id", "name", "media_type", "size", "sha256", "path"
        }:
            raise IncomingFileError("incoming file context was invalid")
        manifest.append({key: row.get(key) for key in row if key != "path"})
        paths.append(row.get("path"))
    descriptors = parse_incoming_files(manifest)
    if len(paths) != len(descriptors) or not all(
        isinstance(path, str) and _safe_relative_path(path) for path in paths
    ):
        raise IncomingFileError("incoming file context was invalid")
    return {
        "schema_version": "v1",
        "kind": "allies_incoming_files",
        "files": [
            {**descriptor.manifest_value(), "path": path}
            for descriptor, path in zip(descriptors, paths, strict=True)
        ],
    }


async def stage_incoming_files(
    workspace: Path,
    command_id: str,
    value: object,
    fetch: FileFetcher,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> StagedManifest:
    """Stage the complete manifest before the model can see any path.

    A receipt identifies exact replay. If an old working copy has changed, a
    replay writes a new isolated directory. It never replaces the old copy.
    """

    descriptors = parse_incoming_files(value)
    command_id = _command_id(command_id)
    manifest_sha256 = _manifest_sha256(descriptors)
    workspace = _workspace(workspace)
    receipt_path = workspace.parent / ".allies-incoming-receipts" / f"{command_id}.json"
    previous = _read_receipt(receipt_path)
    if previous is not None:
        staged = _receipt_manifest(previous, command_id, manifest_sha256, descriptors)
        if staged is not None and _staged_files_match(workspace, staged):
            return StagedManifest(command_id, manifest_sha256, staged)

    started = clock()
    staging_root = workspace.parent / ".allies-incoming-staging"
    _directory(staging_root)
    temporary = staging_root / f"{command_id}.{os.urandom(8).hex()}"
    final_root = _next_target(workspace / "attachments", command_id, previous is not None)
    files: list[StagedFile] = []
    try:
        temporary.mkdir(mode=0o700)
        for descriptor in descriptors:
            _check_deadline(started, clock, MAX_SET_SECONDS)
            target_name = f"{descriptor.file_id}-{descriptor.name}"
            target = temporary / target_name
            await _download_file(descriptor, target, fetch, clock, started)
            files.append(
                StagedFile(
                    descriptor=descriptor,
                    path=(Path("attachments") / final_root.name / target_name).as_posix(),
                )
            )
        _directory(final_root.parent)
        os.replace(temporary, final_root)
        _sync_directory(final_root.parent)
        staged = tuple(files)
        _write_receipt(
            receipt_path,
            {
                "schema": _RECEIPT_SCHEMA,
                "command_id": command_id,
                "manifest_sha256": manifest_sha256,
                "files": [item.hermes_value() for item in staged],
            },
        )
        return StagedManifest(command_id, manifest_sha256, staged)
    except IncomingFileError:
        _remove_tree(temporary)
        raise
    except (OSError, UnicodeError):
        _remove_tree(temporary)
        raise IncomingFileError("incoming file staging failed") from None


async def _download_file(
    descriptor: IncomingFile,
    target: Path,
    fetch: FileFetcher,
    clock: Callable[[], float],
    set_started: float,
) -> None:
    started = clock()
    received = 0
    digest = hashlib.sha256()
    source = fetch(descriptor)
    if hasattr(source, "__await__"):
        source = await source
    if not hasattr(source, "__aiter__"):
        raise IncomingFileError("incoming file transport was invalid")
    try:
        with target.open("xb") as handle:
            async for chunk in source:
                _check_deadline(started, clock, MAX_FILE_SECONDS)
                _check_deadline(set_started, clock, MAX_SET_SECONDS)
                if not isinstance(chunk, bytes) or not chunk or len(chunk) > MAX_CHUNK_BYTES:
                    raise IncomingFileError("incoming file transport was invalid")
                received += len(chunk)
                if received > descriptor.size:
                    raise IncomingFileError("incoming file size did not match manifest")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except IncomingFileError:
        raise
    except (OSError, TypeError):
        raise IncomingFileError("incoming file transport failed") from None
    if received != descriptor.size or digest.hexdigest() != descriptor.sha256:
        raise IncomingFileError("incoming file digest did not match manifest")


def _receipt_manifest(
    value: object,
    command_id: str,
    manifest_sha256: str,
    descriptors: Sequence[IncomingFile],
) -> tuple[StagedFile, ...] | None:
    if not isinstance(value, Mapping) or value.get("schema") != _RECEIPT_SCHEMA:
        return None
    if value.get("command_id") != command_id or value.get("manifest_sha256") != manifest_sha256:
        raise IncomingFileError("incoming file replay conflicted with its receipt")
    rows = value.get("files")
    if not isinstance(rows, list) or len(rows) != len(descriptors):
        return None
    staged: list[StagedFile] = []
    for descriptor, row in zip(descriptors, rows, strict=True):
        if not isinstance(row, Mapping) or set(row) != {
            "file_id", "name", "media_type", "size", "sha256", "path"
        }:
            return None
        if any(row.get(key) != value for key, value in descriptor.manifest_value().items()):
            raise IncomingFileError("incoming file replay conflicted with its receipt")
        path = row.get("path")
        if not isinstance(path, str) or not _safe_relative_path(path):
            return None
        staged.append(StagedFile(descriptor=descriptor, path=path))
    return tuple(staged)


def _staged_files_match(workspace: Path, files: Sequence[StagedFile]) -> bool:
    for staged in files:
        path = workspace / staged.path
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != staged.descriptor.size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(MAX_CHUNK_BYTES), b""):
                    digest.update(chunk)
            if digest.hexdigest() != staged.descriptor.sha256:
                return False
        except OSError:
            return False
    return True


def _manifest_sha256(files: Sequence[IncomingFile]) -> str:
    payload = [item.manifest_value() for item in files]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_receipt(path: Path) -> object | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise IncomingFileError("incoming file receipt was invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise IncomingFileError("incoming file receipt was invalid") from None


def _write_receipt(path: Path, value: Mapping[str, object]) -> None:
    _directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise IncomingFileError("incoming file receipt could not be committed") from None


def _workspace(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir() or path.name != "workspace":
        raise IncomingFileError("profile workspace was unavailable")
    return path


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise IncomingFileError("incoming file path was unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise IncomingFileError("incoming file path was unsafe")


def _next_target(root: Path, command_id: str, replay: bool) -> Path:
    _directory(root)
    first = root / command_id
    if not replay and not first.exists():
        return first
    for ordinal in range(1, 1000):
        candidate = root / f"{command_id}-recovery-{ordinal}"
        if not candidate.exists():
            return candidate
    raise IncomingFileError("incoming file recovery namespace was exhausted")


def _safe_name(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255:
        return False
    normalized = unicodedata.normalize("NFC", value)
    base_name = value.split(".", 1)[0].upper()
    return (
        normalized == value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(character in '<>:"|?*' for character in value)
        and value.rstrip(". ") == value
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        and base_name not in _WINDOWS_RESERVED_NAMES
    )


def _safe_media_type(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8")) <= 127
        and "/" in value
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return (
        path.as_posix() == value
        and not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) == 3
        and path.parts[0] == "attachments"
    )


def _command_id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise IncomingFileError("incoming file command identity was invalid") from None


def _check_deadline(started: float, clock: Callable[[], float], limit: float) -> None:
    if clock() - started > limit:
        raise IncomingFileError("incoming file transfer timed out")


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "MAX_CHUNK_BYTES",
    "IncomingFile",
    "StagedFile",
    "StagedManifest",
    "parse_incoming_files",
    "stage_incoming_files",
    "validate_hermes_file_context",
]
