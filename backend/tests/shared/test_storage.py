"""Raster storage: the local backend, key safety, and the S3 placeholder."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.shared.config import Settings
from app.shared.storage import (
    STREAM_CHUNK_BYTES,
    InvalidStorageKeyError,
    LocalStorageBackend,
    S3StorageBackend,
    StorageError,
    StorageKeyNotFoundError,
    analysis_artifact_key,
    api_path_for,
    build_storage,
    predio_prefix,
    validate_key,
)

PREDIO = uuid.UUID("11111111-1111-4111-8111-111111111111")
ANALYSIS = uuid.UUID("22222222-2222-4222-8222-222222222222")
TIF = "image/tiff"


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "rasters-root")


def _key(artifact: str = "ndvi.tif") -> str:
    return analysis_artifact_key(PREDIO, ANALYSIS, artifact)


# --- keys ---------------------------------------------------------------------


def test_keys_follow_the_hierarchical_convention() -> None:
    assert _key() == f"rasters/{PREDIO}/{ANALYSIS}/ndvi.tif"
    assert _key("preview.png").endswith("/preview.png")
    assert _key("metadata.json").endswith("/metadata.json")
    assert _key().startswith(predio_prefix(PREDIO))


def test_keys_only_accept_uuid_instances() -> None:
    # A string that looks like a UUID is how a crafted path would get in.
    with pytest.raises(InvalidStorageKeyError):
        analysis_artifact_key(str(PREDIO), ANALYSIS, "ndvi.tif")  # type: ignore[arg-type]
    with pytest.raises(InvalidStorageKeyError):
        analysis_artifact_key(PREDIO, "../../etc", "ndvi.tif")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "artifact",
    ["../ndvi.tif", "ndvi.tif.exe", "ndvi.exe", ".hidden.tif", "a/b.tif", "", "ndvi"],
)
def test_artifact_names_are_an_allowlist(artifact: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        analysis_artifact_key(PREDIO, ANALYSIS, artifact)


@pytest.mark.parametrize(
    "key",
    [
        "../secret.tif",
        "rasters/../../etc/passwd",
        f"rasters/{PREDIO}/../../../x.tif",
        f"rasters/{PREDIO}/{ANALYSIS}/..",
        "rasters/./ndvi.tif",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "rasters\\..\\..\\x.tif",
        "rasters//ndvi.tif",
        "rasters/ndvi.tif/",
        "rasters/nd\x00vi.tif",
        "",
        "a" * 600,
    ],
)
def test_unsafe_keys_are_rejected(key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        validate_key(key)


# --- local backend --------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_and_get_preserve_the_exact_bytes(storage: LocalStorageBackend) -> None:
    data = bytes(range(256)) * 4096 + b"\x00\xff"  # ~1 MiB, with zero bytes
    assert await storage.put(_key(), data, TIF) == _key()
    assert await storage.get(_key()) == data


@pytest.mark.asyncio
async def test_exists_before_and_after(storage: LocalStorageBackend) -> None:
    assert await storage.exists(_key()) is False
    await storage.put(_key(), b"tiff", TIF)
    assert await storage.exists(_key()) is True


@pytest.mark.asyncio
async def test_delete_removes_and_is_idempotent(storage: LocalStorageBackend) -> None:
    await storage.put(_key(), b"tiff", TIF)
    await storage.delete(_key())
    assert await storage.exists(_key()) is False
    await storage.delete(_key())  # like S3: deleting nothing is not an error


@pytest.mark.asyncio
async def test_a_missing_key_raises_the_storage_error_not_the_os_one(
    storage: LocalStorageBackend,
) -> None:
    with pytest.raises(StorageKeyNotFoundError) as caught:
        await storage.get(_key())
    assert not isinstance(caught.value, FileNotFoundError)


@pytest.mark.asyncio
async def test_get_stream_yields_chunks_that_add_up(storage: LocalStorageBackend) -> None:
    data = os.urandom(STREAM_CHUNK_BYTES * 3 + 123)
    await storage.put(_key(), data, TIF)

    chunks = [chunk async for chunk in storage.get_stream(_key())]
    assert len(chunks) == 4
    assert all(len(chunk) <= STREAM_CHUNK_BYTES for chunk in chunks)
    assert b"".join(chunks) == data


@pytest.mark.asyncio
async def test_streaming_a_missing_key_raises_the_storage_error(
    storage: LocalStorageBackend,
) -> None:
    with pytest.raises(StorageKeyNotFoundError):
        async for _ in storage.get_stream(_key()):
            pass


@pytest.mark.asyncio
async def test_the_content_type_must_match_the_extension(
    storage: LocalStorageBackend,
) -> None:
    with pytest.raises(StorageError, match="content type"):
        await storage.put(_key(), b"<html>", "text/html")


@pytest.mark.asyncio
async def test_writes_leave_no_temporary_files(storage: LocalStorageBackend) -> None:
    await storage.put(_key(), b"first", TIF)
    await storage.put(_key(), b"second", TIF)  # overwrite, atomically
    folder = storage.root / "rasters" / str(PREDIO) / str(ANALYSIS)
    assert sorted(p.name for p in folder.iterdir()) == ["ndvi.tif"]
    assert await storage.get(_key()) == b"second"


@pytest.mark.asyncio
async def test_the_root_is_not_created_until_the_first_write(tmp_path: Path) -> None:
    root = tmp_path / "not-yet"
    backend = LocalStorageBackend(root)
    assert not root.exists()
    assert await backend.exists(_key()) is False
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["../outside.tif", "rasters/../../outside.tif", "/etc/passwd"]
)
async def test_every_operation_rejects_unsafe_keys(
    storage: LocalStorageBackend, key: str
) -> None:
    with pytest.raises(InvalidStorageKeyError):
        await storage.put(key, b"x", TIF)
    with pytest.raises(InvalidStorageKeyError):
        await storage.get(key)
    with pytest.raises(InvalidStorageKeyError):
        await storage.exists(key)
    with pytest.raises(InvalidStorageKeyError):
        await storage.delete(key)


@pytest.mark.asyncio
async def test_a_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tif").write_bytes(b"secret")
    (root / "rasters").mkdir(parents=True)
    try:
        (root / "rasters" / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this platform does not allow creating symlinks here")

    backend = LocalStorageBackend(root)
    # The key itself is well-formed; only resolving it reveals the escape.
    with pytest.raises(InvalidStorageKeyError, match="outside the storage root"):
        await backend.get("rasters/escape/secret.tif")


# --- URLs -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_urls_are_api_paths_never_filesystem_paths(
    storage: LocalStorageBackend,
) -> None:
    raster = await storage.get_url(_key("ndvi.tif"))
    preview = await storage.get_url(_key("preview.png"))

    assert raster == f"/api/v1/predios/{PREDIO}/analyses/{ANALYSIS}/raster"
    assert preview == f"/api/v1/predios/{PREDIO}/analyses/{ANALYSIS}/preview"
    assert str(storage.root) not in raster


def test_artifacts_without_an_endpoint_have_no_url() -> None:
    with pytest.raises(StorageError, match="no endpoint"):
        api_path_for(_key("metadata.json"))


@pytest.mark.asyncio
async def test_a_non_positive_expiry_is_rejected(storage: LocalStorageBackend) -> None:
    with pytest.raises(StorageError, match="expires_s"):
        await storage.get_url(_key(), expires_s=0)


# --- factory and the S3 placeholder ----------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "secret_key": "a-real-secret-key-with-at-least-32-chars",
        "totp_secret_encryption_key": "a-different-real-key-of-32-chars-min",
        "database_url": "postgresql+psycopg://lar_app:pw@localhost:5432/db",
        "database_migration_url": "postgresql+psycopg://lar_owner:pw@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
    }
    return Settings(_env_file=None, **{**base, **overrides})


def test_the_factory_builds_the_local_backend(tmp_path: Path) -> None:
    backend = build_storage(_settings(storage_local_path=str(tmp_path / "r")))
    assert isinstance(backend, LocalStorageBackend)
    assert backend.root == (tmp_path / "r").resolve()


def test_the_s3_backend_fails_loudly_instead_of_half_working() -> None:
    settings = _settings(
        storage_backend="s3", storage_s3_bucket="lar", storage_s3_region="sa-east-1"
    )
    with pytest.raises(NotImplementedError, match="not implemented yet.*KL-005"):
        build_storage(settings)
    with pytest.raises(NotImplementedError):
        S3StorageBackend(bucket="lar", region="sa-east-1")


def test_the_app_refuses_to_start_with_s3_storage() -> None:
    from app.main import create_app

    settings = _settings(
        storage_backend="s3", storage_s3_bucket="lar", storage_s3_region="sa-east-1"
    )
    with pytest.raises(NotImplementedError):
        create_app(settings)
