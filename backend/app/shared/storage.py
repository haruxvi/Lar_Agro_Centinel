"""Storage of rasters and analysis artifacts, behind a backend abstraction.

Today rasters live on the local filesystem; their destination is object
storage. Every caller talks to ``StorageBackend``, so moving to S3 is a
configuration change rather than a refactor of every module that reads a
raster. The local backend is not fit for production on ephemeral
filesystems: see KL-005 in docs/KNOWN-LIMITATIONS.md.

Keys are hierarchical and predictable::

    rasters/{predio_id}/{analysis_id}/{artifact}

so everything of a predio shares a prefix (bulk deletion, S3 lifecycle
rules). Keys are only ever built by :func:`analysis_artifact_key` from
validated UUIDs and a fixed artifact name, never from user input: a file
name chosen by a user inside a key is path traversal.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol

from app.shared.config import Settings, get_settings
from app.shared.exceptions import DomainError

RASTERS_PREFIX: Final = "rasters"
MAX_KEY_LENGTH: Final = 512
STREAM_CHUNK_BYTES: Final = 64 * 1024

# One plain name per artifact: letters, digits and underscores, then a known
# extension. No dots elsewhere, so no "..", no hidden files, no double
# extensions.
_ARTIFACT_NAME: Final = re.compile(r"^[A-Za-z0-9_]{1,64}\.(tif|png|json)$")
# What any single key component may contain. Deliberately narrower than what a
# filesystem accepts: no separators, no drive letters, no control characters.
_KEY_COMPONENT: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

CONTENT_TYPES: Final = {
    ".tif": "image/tiff",
    ".png": "image/png",
    ".json": "application/json",
}

# Served by ABAC-checked endpoints of the analysis module (Bloque 8).
_API_PREFIX: Final = "/api/v1"
_SERVED_ARTIFACTS: Final = {"ndvi.tif": "raster", "preview.png": "preview"}


class StorageError(DomainError):
    """Base class for storage errors."""


class InvalidStorageKeyError(StorageError):
    """A key that could address something outside the storage root."""

    def __init__(self, reason: str) -> None:
        """Record why the key was refused, without echoing it back."""
        super().__init__(reason)
        self.reason = reason


class StorageKeyNotFoundError(StorageError):
    """Nothing is stored under the key.

    Raised instead of the backend's own error (``FileNotFoundError`` locally,
    a 404 on S3), so callers handle one exception whatever the backend.
    """


class StorageBackend(Protocol):
    """Where rasters and analysis artifacts are kept."""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store ``data`` under ``key`` and return the key."""
        ...

    async def get(self, key: str) -> bytes:
        """Return everything stored under ``key``."""
        ...

    def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the content under ``key`` in chunks, never all at once.

        Declared without ``async``: an async generator function is a plain
        callable returning an async iterator, and that is what it must match.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return whether something is stored under ``key``."""
        ...

    async def delete(self, key: str) -> None:
        """Remove what is stored under ``key``. Missing keys are not an error."""
        ...

    async def get_url(self, key: str, expires_s: int = 3600) -> str:
        """Return a URL through which a client can fetch ``key``."""
        ...


# --- keys ---------------------------------------------------------------------


def analysis_artifact_key(
    predio_id: uuid.UUID, analysis_id: uuid.UUID, artifact: str
) -> str:
    """Build the key of an analysis artifact, e.g. ``ndvi.tif`` or ``preview.png``.

    The ids must be ``uuid.UUID`` instances, not strings: a string that merely
    looks like a UUID is exactly how a crafted path gets in.
    """
    if not isinstance(predio_id, uuid.UUID) or not isinstance(analysis_id, uuid.UUID):
        raise InvalidStorageKeyError("ids must be UUID instances")
    if not _ARTIFACT_NAME.fullmatch(artifact):
        raise InvalidStorageKeyError("artifact name is not allowed")
    return f"{RASTERS_PREFIX}/{predio_id}/{analysis_id}/{artifact}"


def predio_prefix(predio_id: uuid.UUID) -> str:
    """Return the prefix shared by every artifact of a predio."""
    if not isinstance(predio_id, uuid.UUID):
        raise InvalidStorageKeyError("ids must be UUID instances")
    return f"{RASTERS_PREFIX}/{predio_id}/"


def validate_key(key: str) -> str:
    """Return ``key`` if it is a safe relative key, or raise.

    Applied by every backend on every call, as a second line behind
    :func:`analysis_artifact_key`: a key that did not come from there is
    still refused if it could climb out of the storage root.
    """
    if not key or len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageKeyError("key is empty or too long")
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise InvalidStorageKeyError("key must be a relative POSIX path")
    for component in key.split("/"):
        if component in {"", ".", ".."}:
            raise InvalidStorageKeyError("key has an empty or relative component")
        if not _KEY_COMPONENT.fullmatch(component):
            raise InvalidStorageKeyError("key has a component with forbidden characters")
    return key


def content_type_for(key: str) -> str:
    """Return the content type implied by the key's extension."""
    content_type = CONTENT_TYPES.get(Path(key).suffix.lower())
    if content_type is None:
        raise InvalidStorageKeyError("key has an unknown extension")
    return content_type


def api_path_for(key: str) -> str:
    """Return the ABAC-checked API path that serves ``key``.

    Only artifacts with an endpoint can be addressed; anything else (the
    metadata file, for one) has no public URL at all.
    """
    parts = validate_key(key).split("/")
    if len(parts) != 4 or parts[0] != RASTERS_PREFIX:
        raise StorageError("no endpoint serves this key")
    _, predio_id, analysis_id, artifact = parts
    endpoint = _SERVED_ARTIFACTS.get(artifact)
    if endpoint is None:
        raise StorageError("no endpoint serves this artifact")
    try:
        predio, analysis = uuid.UUID(predio_id), uuid.UUID(analysis_id)
    except ValueError as exc:
        raise InvalidStorageKeyError("key ids are not UUIDs") from exc
    return f"{_API_PREFIX}/predios/{predio}/analyses/{analysis}/{endpoint}"


# --- local filesystem -----------------------------------------------------------


class LocalStorageBackend:
    """Stores artifacts as files under a root directory.

    Every path is resolved (symlinks included) and checked to lie inside the
    root before it is touched. The root is created on the first write, never
    at construction, so building the backend has no side effects.
    """

    def __init__(self, root: str | Path) -> None:
        """Bind the backend to ``root``."""
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        """Return the resolved storage root."""
        return self._root

    def _path(self, key: str) -> Path:
        path = (self._root / validate_key(key)).resolve()
        if not path.is_relative_to(self._root):
            raise InvalidStorageKeyError("key resolves outside the storage root")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Write ``data`` atomically: readers never see a half-written file."""
        if content_type != content_type_for(key):
            raise StorageError("content type does not match the key's extension")
        path = self._path(key)
        await asyncio.to_thread(self._write_atomically, path, data)
        return key

    @staticmethod
    def _write_atomically(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    async def get(self, key: str) -> bytes:
        """Return the file's bytes."""
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise StorageKeyNotFoundError(key) from exc

    async def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the file in chunks of STREAM_CHUNK_BYTES.

        A missing key raises StorageKeyNotFoundError on the first iteration;
        callers about to start an HTTP response should check ``exists`` first.
        """
        path = self._path(key)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError as exc:
            raise StorageKeyNotFoundError(key) from exc
        try:
            while chunk := await asyncio.to_thread(handle.read, STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def exists(self, key: str) -> bool:
        """Return whether a file is stored under ``key``."""
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        """Remove the file; deleting a missing key is a no-op, as on S3."""
        path = self._path(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def get_url(self, key: str, expires_s: int = 3600) -> str:
        """Return the API path that serves ``key``, never a filesystem path.

        ``expires_s`` does not apply: the endpoint checks access on every
        request instead of trusting a signed URL.
        """
        if expires_s <= 0:
            raise StorageError("expires_s must be positive")
        return api_path_for(key)


# --- S3 (not implemented) -------------------------------------------------------

_S3_NOT_IMPLEMENTED: Final = (
    "The S3 storage backend is not implemented yet. Use STORAGE_BACKEND=local "
    "for now, and read KL-005 before deploying it anywhere with an ephemeral "
    "filesystem."
)


class S3StorageBackend:
    """Placeholder for object storage. Fails loudly; it is not half-built."""

    def __init__(self, bucket: str, region: str) -> None:
        """Refuse to exist until it is implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    async def get(self, key: str) -> bytes:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    def get_stream(self, key: str) -> AsyncIterator[bytes]:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    async def exists(self, key: str) -> bool:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    async def delete(self, key: str) -> None:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)

    async def get_url(self, key: str, expires_s: int = 3600) -> str:
        """Not implemented."""
        raise NotImplementedError(_S3_NOT_IMPLEMENTED)


# --- factory --------------------------------------------------------------------


def build_storage(settings: Settings) -> StorageBackend:
    """Build the configured backend. With "s3" this raises: fail closed."""
    if settings.storage_backend == "s3":
        # Settings already guarantee both are set when the backend is s3.
        return S3StorageBackend(
            bucket=settings.storage_s3_bucket or "",
            region=settings.storage_s3_region or "",
        )
    return LocalStorageBackend(settings.storage_local_path)


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """Return the process-wide storage backend."""
    return build_storage(get_settings())
