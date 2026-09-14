"""Pure Meshy retopology and immutable-backup pipeline rules.

This module intentionally depends only on the Python standard library.  It is
safe to import from Blender, a Substance integration, a command-line tool, or a
unit test without importing ``bpy`` or launching an external application.

Snapshot publication is deliberately create-once.  A later call verifies the
source identity, manifest, and copied file and performs no write when they all
match.  A mismatch is an error; this module never repairs or overwrites an
existing snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import json
import numbers
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Mapping, Union


PathLike = Union[str, os.PathLike]

CONTRACT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
MANIFEST_KIND = "meshy_immutable_snapshot"
SET_MANIFEST_KIND = "meshy_immutable_snapshot_set"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SnapshotError(RuntimeError):
    """Base class for immutable snapshot failures."""


class SnapshotConflictError(SnapshotError):
    """The current source does not match a previously published snapshot."""


class SnapshotIntegrityError(SnapshotError):
    """A snapshot manifest or one of its backup files is invalid."""


def _path_argument(value: PathLike, name: str) -> Path:
    if isinstance(value, bytes):
        raise TypeError(f"{name} must be a text path")
    try:
        return Path(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a path-like value") from exc


def _existing_regular_file(value: PathLike, name: str) -> Path:
    path = _path_argument(value, name)
    try:
        if not path.is_file():
            raise FileNotFoundError(f"{name} is not a regular file: {path}")
    except OSError as exc:
        raise FileNotFoundError(f"unable to inspect {name}: {path}") from exc
    return path


def _validated_chunk_size(chunk_size: Any) -> int:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, numbers.Integral):
        raise ValueError("chunk_size must be a positive integer")
    value = int(chunk_size)
    if value <= 0:
        raise ValueError("chunk_size must be a positive integer")
    return value


def sha256_file(file_path: PathLike, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the lowercase SHA-256 digest of a regular file."""
    path = _existing_regular_file(file_path, "file_path")
    block_size = _validated_chunk_size(chunk_size)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_manifest(file_path: PathLike) -> Dict[str, Any]:
    """Return a stable ``size``/``sha256`` identity for a regular file.

    Metadata is sampled before and after hashing so a file that changes while
    it is being inspected cannot silently become a published source identity.
    """
    path = _existing_regular_file(file_path, "file_path")
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    before_marker = (before.st_size, before.st_mtime_ns, before.st_ino)
    after_marker = (after.st_size, after.st_mtime_ns, after.st_ino)
    if before_marker != after_marker:
        raise SnapshotConflictError(f"file changed while hashing: {path}")
    return {"size": int(after.st_size), "sha256": digest}


def _logical_relative_filename(value: PathLike) -> str:
    if isinstance(value, bytes):
        raise ValueError("logical_filename must be a relative text path")
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ValueError("logical_filename must be a relative text path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("logical_filename must be a non-empty relative text path")
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError("logical_filename must not be absolute")
    if PureWindowsPath(raw).drive:
        raise ValueError("logical_filename must not contain a drive or UNC root")
    parts = raw.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("logical_filename must not contain traversal or empty segments")
    normalized = "/".join(parts)
    if parts[0].casefold() == MANIFEST_FILENAME.casefold():
        raise ValueError("logical_filename collides with the snapshot manifest")
    return normalized


def _identity_from_manifest(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotIntegrityError(f"{label} must be a JSON object")
    size = value.get("size")
    digest = value.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SnapshotIntegrityError(f"{label}.size must be a non-negative integer")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise SnapshotIntegrityError(f"{label}.sha256 must be a lowercase SHA-256")
    return {"size": size, "sha256": digest}


def validate_snapshot_manifest(
    manifest: Any,
    *,
    expected_logical_filename: PathLike | None = None,
) -> Dict[str, Any]:
    """Validate and return a defensive copy of a v1 snapshot manifest."""
    if not isinstance(manifest, Mapping):
        raise SnapshotIntegrityError("snapshot manifest must be a JSON object")
    if manifest.get("kind") != MANIFEST_KIND:
        raise SnapshotIntegrityError("snapshot manifest kind mismatch")
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != CONTRACT_VERSION:
        raise SnapshotIntegrityError("snapshot manifest schema version mismatch")
    try:
        logical = _logical_relative_filename(manifest.get("logical_filename"))
    except (TypeError, ValueError) as exc:
        raise SnapshotIntegrityError("snapshot manifest has an unsafe logical filename") from exc
    if expected_logical_filename is not None:
        expected = _logical_relative_filename(expected_logical_filename)
        if logical != expected:
            raise SnapshotIntegrityError(
                f"snapshot logical filename mismatch: {logical!r} != {expected!r}"
            )
    source_identity = _identity_from_manifest(manifest.get("source"), "source")
    backup = manifest.get("backup")
    if not isinstance(backup, Mapping):
        raise SnapshotIntegrityError("backup must be a JSON object")
    try:
        backup_path = _logical_relative_filename(backup.get("path"))
    except (TypeError, ValueError) as exc:
        raise SnapshotIntegrityError("backup.path is unsafe") from exc
    if backup_path != logical:
        raise SnapshotIntegrityError("backup.path does not match logical_filename")
    backup_identity = _identity_from_manifest(backup, "backup")
    return {
        "kind": MANIFEST_KIND,
        "schema_version": CONTRACT_VERSION,
        "logical_filename": logical,
        "source": source_identity,
        "backup": {"path": backup_path, **backup_identity},
    }


def load_snapshot_manifest(snapshot_dir: PathLike) -> Dict[str, Any]:
    """Read and validate ``manifest.json`` from a snapshot directory."""
    directory = _path_argument(snapshot_dir, "snapshot_dir")
    manifest_path = directory / MANIFEST_FILENAME
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"unable to read snapshot manifest: {manifest_path}"
        ) from exc
    return validate_snapshot_manifest(payload)


def _normalized_snapshot_sources(
    sources: Mapping[PathLike, PathLike],
) -> Dict[str, Path]:
    if not isinstance(sources, Mapping) or not sources:
        raise ValueError("sources must be a non-empty mapping of logical paths to files")
    normalized: Dict[str, Path] = {}
    path_keys: Dict[str, str] = {}
    for logical_value, source_value in sources.items():
        logical = _logical_relative_filename(logical_value)
        collision_key = logical.casefold()
        if collision_key in path_keys:
            raise ValueError(
                "sources contains duplicate normalized/casefold logical paths: "
                f"{path_keys[collision_key]!r} and {logical!r}"
            )
        path_keys[collision_key] = logical
        normalized[logical] = _existing_regular_file(
            source_value, f"source for {logical!r}"
        )
    ordered_paths = sorted(normalized, key=lambda item: item.casefold())
    for index, logical in enumerate(ordered_paths):
        prefix = logical.casefold() + "/"
        if any(other.casefold().startswith(prefix) for other in ordered_paths[index + 1 :]):
            raise ValueError(
                f"sources contains a file/directory path collision at {logical!r}"
            )
    return dict(sorted(normalized.items(), key=lambda item: item[0].casefold()))


def validate_snapshot_set_manifest(manifest: Any) -> Dict[str, Any]:
    """Validate and canonicalize a multi-file immutable snapshot manifest."""
    if not isinstance(manifest, Mapping):
        raise SnapshotIntegrityError("snapshot-set manifest must be a JSON object")
    if manifest.get("kind") != SET_MANIFEST_KIND:
        raise SnapshotIntegrityError("snapshot-set manifest kind mismatch")
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != CONTRACT_VERSION:
        raise SnapshotIntegrityError("snapshot-set manifest schema version mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SnapshotIntegrityError("snapshot-set manifest files must be a non-empty array")

    normalized_entries = []
    path_keys: Dict[str, str] = {}
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise SnapshotIntegrityError(
                f"snapshot-set files[{index}] must be a JSON object"
            )
        try:
            logical = _logical_relative_filename(entry.get("path"))
        except (TypeError, ValueError) as exc:
            raise SnapshotIntegrityError(
                f"snapshot-set files[{index}].path is unsafe"
            ) from exc
        collision_key = logical.casefold()
        if collision_key in path_keys:
            raise SnapshotIntegrityError(
                "snapshot-set manifest contains duplicate normalized/casefold paths: "
                f"{path_keys[collision_key]!r} and {logical!r}"
            )
        path_keys[collision_key] = logical
        source_identity = _identity_from_manifest(
            entry.get("source"), f"files[{index}].source"
        )
        backup_identity = _identity_from_manifest(
            entry.get("backup"), f"files[{index}].backup"
        )
        normalized_entries.append(
            {
                "path": logical,
                "source": source_identity,
                "backup": backup_identity,
            }
        )
    normalized_entries.sort(key=lambda entry: entry["path"].casefold())
    return {
        "kind": SET_MANIFEST_KIND,
        "schema_version": CONTRACT_VERSION,
        "files": normalized_entries,
    }


def load_snapshot_set_manifest(snapshot_dir: PathLike) -> Dict[str, Any]:
    """Read and validate a multi-file manifest from a snapshot directory."""
    directory = _path_argument(snapshot_dir, "snapshot_dir")
    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SnapshotIntegrityError(
            f"snapshot-set manifest is missing or not a regular file: {manifest_path}"
        )
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"unable to read snapshot-set manifest: {manifest_path}"
        ) from exc
    return validate_snapshot_set_manifest(payload)


def _destination_path(value: PathLike) -> Path:
    requested = _path_argument(value, "destination_dir")
    if requested.name in {"", ".", ".."}:
        raise ValueError("destination_dir must name a child directory")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(
            f"destination parent does not exist: {requested.parent}"
        ) from exc
    if not parent.is_dir():
        raise NotADirectoryError(f"destination parent is not a directory: {parent}")
    return parent / requested.name


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _assert_snapshot_directory(destination: Path) -> None:
    if destination.is_symlink():
        raise SnapshotIntegrityError(
            f"snapshot destination must not be a symbolic link: {destination}"
        )
    if not destination.is_dir():
        raise SnapshotIntegrityError(
            f"snapshot destination is not a directory: {destination}"
        )


def _backup_path_inside_snapshot(destination: Path, logical: str) -> Path:
    root = destination.resolve(strict=True)
    cursor = destination
    for part in logical.split("/"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise SnapshotIntegrityError(
                f"snapshot backup path contains a symbolic link: {cursor}"
            )
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SnapshotIntegrityError(
            f"snapshot backup escapes or is missing: {logical}"
        ) from exc
    if not resolved.is_file():
        raise SnapshotIntegrityError(f"snapshot backup is not a file: {logical}")
    return resolved


def _snapshot_set_files(destination: Path) -> set[str]:
    """Return all regular snapshot files except the root manifest.

    Symlinks and special filesystem entries are never valid immutable snapshot
    members, even when they resolve to a regular file inside the directory.
    """
    root = destination.resolve(strict=True)
    discovered: set[str] = set()
    pending = [(root, "")]
    while pending:
        directory, relative_parent = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"unable to enumerate snapshot-set directory: {directory}"
            ) from exc
        for entry in entries:
            relative = (
                f"{relative_parent}/{entry.name}"
                if relative_parent
                else entry.name
            )
            if entry.is_symlink():
                raise SnapshotIntegrityError(
                    f"snapshot-set contains a symbolic link: {relative}"
                )
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), relative))
                elif entry.is_file(follow_symlinks=False):
                    if relative == MANIFEST_FILENAME:
                        continue
                    discovered.add(relative.replace("\\", "/"))
                else:
                    raise SnapshotIntegrityError(
                        f"snapshot-set contains a special filesystem entry: {relative}"
                    )
            except OSError as exc:
                raise SnapshotIntegrityError(
                    f"unable to inspect snapshot-set entry: {relative}"
                ) from exc
    return discovered


def verify_immutable_snapshot(
    source_path: PathLike,
    destination_dir: PathLike,
    logical_filename: PathLike,
) -> Dict[str, Any]:
    """Verify an existing snapshot without writing any byte."""
    source = _existing_regular_file(source_path, "source_path")
    destination = _destination_path(destination_dir)
    logical = _logical_relative_filename(logical_filename)
    if not _lexists(destination):
        raise SnapshotIntegrityError(f"snapshot destination does not exist: {destination}")
    _assert_snapshot_directory(destination)
    manifest = load_snapshot_manifest(destination)
    manifest = validate_snapshot_manifest(
        manifest, expected_logical_filename=logical
    )

    try:
        current_source = file_manifest(source)
    except (FileNotFoundError, OSError, SnapshotConflictError) as exc:
        raise SnapshotConflictError(
            f"unable to verify current snapshot source: {source}"
        ) from exc
    if current_source != manifest["source"]:
        raise SnapshotConflictError(
            "current source hash/size differs from the immutable snapshot manifest"
        )

    backup_path = _backup_path_inside_snapshot(destination, logical)
    try:
        current_backup = file_manifest(backup_path)
    except (FileNotFoundError, OSError, SnapshotConflictError) as exc:
        raise SnapshotIntegrityError(
            f"unable to verify snapshot backup: {logical}"
        ) from exc
    expected_backup = {
        "size": manifest["backup"]["size"],
        "sha256": manifest["backup"]["sha256"],
    }
    if current_backup != expected_backup:
        raise SnapshotIntegrityError(
            "snapshot backup hash/size differs from its manifest"
        )
    return copy.deepcopy(manifest)


def verify_immutable_snapshot_set(
    sources: Mapping[PathLike, PathLike],
    destination_dir: PathLike,
) -> Dict[str, Any]:
    """Verify an exact multi-file snapshot and all current sources, read-only."""
    normalized_sources = _normalized_snapshot_sources(sources)
    destination = _destination_path(destination_dir)
    if not _lexists(destination):
        raise SnapshotIntegrityError(f"snapshot destination does not exist: {destination}")
    _assert_snapshot_directory(destination)
    manifest = verify_immutable_snapshot_set_archive(destination)
    manifest_entries = {entry["path"]: entry for entry in manifest["files"]}

    expected_paths = set(normalized_sources)
    manifest_paths = set(manifest_entries)
    if expected_paths != manifest_paths:
        missing_sources = sorted(manifest_paths - expected_paths, key=str.casefold)
        added_sources = sorted(expected_paths - manifest_paths, key=str.casefold)
        raise SnapshotConflictError(
            "current source mapping differs from the immutable snapshot-set manifest; "
            f"missing={missing_sources!r}, added={added_sources!r}"
        )

    for logical, source in normalized_sources.items():
        entry = manifest_entries[logical]
        try:
            current_source = file_manifest(source)
        except (FileNotFoundError, OSError, SnapshotConflictError) as exc:
            raise SnapshotConflictError(
                f"unable to verify current snapshot-set source: {logical}"
            ) from exc
        if current_source != entry["source"]:
            raise SnapshotConflictError(
                f"current source hash/size differs for snapshot-set member: {logical}"
            )

    return copy.deepcopy(manifest)


def verify_immutable_snapshot_set_archive(
    destination_dir: PathLike,
) -> Dict[str, Any]:
    """Verify an immutable snapshot-set without consulting mutable source paths.

    Use this after the pipeline deliberately replaces canonical textures.  It
    proves that the create-once archive is complete and untampered without
    mistaking the new canonical files for a new original source snapshot.
    """
    destination = _destination_path(destination_dir)
    if not _lexists(destination):
        raise SnapshotIntegrityError(f"snapshot destination does not exist: {destination}")
    _assert_snapshot_directory(destination)
    manifest = load_snapshot_set_manifest(destination)
    manifest_entries = {entry["path"]: entry for entry in manifest["files"]}
    manifest_paths = set(manifest_entries)
    actual_backup_paths = _snapshot_set_files(destination)
    if actual_backup_paths != manifest_paths:
        missing_backups = sorted(manifest_paths - actual_backup_paths, key=str.casefold)
        unlisted_backups = sorted(actual_backup_paths - manifest_paths, key=str.casefold)
        raise SnapshotIntegrityError(
            "snapshot-set file membership differs from its manifest; "
            f"missing={missing_backups!r}, unlisted={unlisted_backups!r}"
        )
    for logical, entry in manifest_entries.items():
        backup_path = _backup_path_inside_snapshot(destination, logical)
        try:
            current_backup = file_manifest(backup_path)
        except (FileNotFoundError, OSError, SnapshotConflictError) as exc:
            raise SnapshotIntegrityError(
                f"unable to verify snapshot-set backup: {logical}"
            ) from exc
        if current_backup != entry["backup"]:
            raise SnapshotIntegrityError(
                f"snapshot-set backup hash/size differs for member: {logical}"
            )
    return copy.deepcopy(manifest)


def _write_manifest_last(directory: Path, manifest: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_path = directory / MANIFEST_FILENAME
    with manifest_path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_cleanup_temp(temp_dir: Path, parent: Path, prefix: str) -> None:
    if not _lexists(temp_dir):
        return
    resolved_parent = parent.resolve(strict=True)
    resolved_temp = temp_dir.resolve(strict=True)
    if resolved_temp.parent != resolved_parent or not resolved_temp.name.startswith(prefix):
        raise SnapshotError(
            f"refusing to clean temporary path outside destination parent: {temp_dir}"
        )
    if resolved_temp.is_symlink() or not resolved_temp.is_dir():
        raise SnapshotError(f"refusing to clean unexpected temporary path: {temp_dir}")
    shutil.rmtree(resolved_temp)


def publish_immutable_snapshot(
    source_path: PathLike,
    destination_dir: PathLike,
    logical_filename: PathLike,
) -> Dict[str, Any]:
    """Publish or verify one create-once snapshot.

    For a new destination, the source is copied into a sibling temporary
    directory, both identities are verified, the manifest is written last, and
    the complete directory is atomically renamed into place.  For an existing
    destination, this delegates to :func:`verify_immutable_snapshot` and makes
    no writes.
    """
    source = _existing_regular_file(source_path, "source_path")
    destination = _destination_path(destination_dir)
    logical = _logical_relative_filename(logical_filename)
    if _lexists(destination):
        return verify_immutable_snapshot(source, destination, logical)

    source_identity = file_manifest(source)
    parent = destination.parent
    prefix = f".{destination.name}.tmp-"
    temp_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=os.fspath(parent)))
    try:
        backup_path = temp_dir.joinpath(*logical.split("/"))
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_path)

        backup_identity = file_manifest(backup_path)
        source_after_copy = file_manifest(source)
        if source_after_copy != source_identity:
            raise SnapshotConflictError("source changed while snapshot was being copied")
        if backup_identity != source_identity:
            raise SnapshotIntegrityError(
                "copied backup hash/size does not match the source"
            )

        manifest = {
            "kind": MANIFEST_KIND,
            "schema_version": CONTRACT_VERSION,
            "logical_filename": logical,
            "source": source_identity,
            "backup": {"path": logical, **backup_identity},
        }
        _write_manifest_last(temp_dir, manifest)
        try:
            os.rename(temp_dir, destination)
        except OSError:
            if _lexists(destination):
                return verify_immutable_snapshot(source, destination, logical)
            raise
        return copy.deepcopy(manifest)
    finally:
        _safe_cleanup_temp(temp_dir, parent, prefix)


def publish_immutable_snapshot_set(
    sources: Mapping[PathLike, PathLike],
    destination_dir: PathLike,
) -> Dict[str, Any]:
    """Publish or verify one exact, create-once set of related files.

    ``sources`` maps each logical relative path in the stage directory to its
    current source file.  Every file is copied before any hash verification;
    all sources and backups are then rehashed, the manifest is written last,
    and the complete sibling temporary directory is atomically renamed.
    """
    normalized_sources = _normalized_snapshot_sources(sources)
    destination = _destination_path(destination_dir)
    if _lexists(destination):
        return verify_immutable_snapshot_set(normalized_sources, destination)

    source_identities = {
        logical: file_manifest(source)
        for logical, source in normalized_sources.items()
    }
    parent = destination.parent
    prefix = f".{destination.name}.tmp-"
    temp_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=os.fspath(parent)))
    try:
        for logical, source in normalized_sources.items():
            backup_path = temp_dir.joinpath(*logical.split("/"))
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup_path)

        entries = []
        for logical, source in normalized_sources.items():
            source_after_copy = file_manifest(source)
            if source_after_copy != source_identities[logical]:
                raise SnapshotConflictError(
                    f"source changed while snapshot-set was being copied: {logical}"
                )
            backup_path = temp_dir.joinpath(*logical.split("/"))
            backup_identity = file_manifest(backup_path)
            if backup_identity != source_identities[logical]:
                raise SnapshotIntegrityError(
                    f"copied snapshot-set backup differs from source: {logical}"
                )
            entries.append(
                {
                    "path": logical,
                    "source": source_identities[logical],
                    "backup": backup_identity,
                }
            )

        manifest = {
            "kind": SET_MANIFEST_KIND,
            "schema_version": CONTRACT_VERSION,
            "files": entries,
        }
        _write_manifest_last(temp_dir, manifest)
        try:
            os.rename(temp_dir, destination)
        except OSError:
            if _lexists(destination):
                return verify_immutable_snapshot_set(
                    normalized_sources, destination
                )
            raise
        return copy.deepcopy(manifest)
    finally:
        _safe_cleanup_temp(temp_dir, parent, prefix)


__all__ = [
    "CONTRACT_VERSION",
    "MANIFEST_FILENAME",
    "SET_MANIFEST_KIND",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotIntegrityError",
    "file_manifest",
    "load_snapshot_manifest",
    "load_snapshot_set_manifest",
    "publish_immutable_snapshot",
    "publish_immutable_snapshot_set",
    "sha256_file",
    "validate_snapshot_manifest",
    "validate_snapshot_set_manifest",
    "verify_immutable_snapshot",
    "verify_immutable_snapshot_set",
    "verify_immutable_snapshot_set_archive",
]
