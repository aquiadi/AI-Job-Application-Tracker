"""Where bytes live: Cloud Storage in cloud, a directory on disk locally.

Three classes of object, kept in separate buckets because their lifecycles differ:
uploads (a resume PDF, kept until the user deletes it), raw payloads (what an ATS
returned, kept so a schema bump can re-extract without re-fetching), and artifacts
(rendered documents, regenerable).

The local implementation is not a mock. It writes real files, enforces the same key
rules, and is what `DELETE /me` deletes from in development — so the deletion path is
exercised by the same test either way.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: Keys are `<user_id>/<kind>/<name>`. Anchored, no dots, so nothing can traverse out
#: of the prefix it was given — which is what keeps one user's objects under one
#: prefix that `DELETE /me` can remove wholesale.
_KEY = re.compile(r"^[0-9a-f-]{36}/[a-z_]+/[A-Za-z0-9][A-Za-z0-9._-]{0,190}$")


class StorageError(Exception):
    """An object could not be written, read or removed."""


def object_key(user_id: uuid.UUID, kind: str, name: str) -> str:
    """Build a key, rejecting anything that would escape the user's prefix."""
    key = f"{user_id}/{kind}/{name}"
    if ".." in key or not _KEY.match(key):
        raise StorageError(f"unsafe object key: {key!r}")
    return key


class ObjectStore(Protocol):
    """Read, write and delete blobs, addressed by key."""

    async def put(self, key: str, data: bytes, *, content_type: str) -> str:
        """Store `data`, returning the URI to record on the row."""
        ...

    async def get(self, uri: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> int:
        """Remove everything under a prefix. Returns how many objects went."""
        ...


@dataclass(slots=True)
class LocalObjectStore:
    """Files under a directory, addressed with `file://` URIs."""

    root: Path

    async def put(self, key: str, data: bytes, *, content_type: str) -> str:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        # Threaded for the same reason the Cloud Storage implementation is: these are
        # blocking calls, and an event loop that stalls on a local disk write stalls
        # just as completely as one waiting on a network.
        await asyncio.to_thread(write)
        return f"file://{path}"

    async def get(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        # Re-check containment: a URI read back from the database is data, and a row
        # written by an older, buggier version of this code should not be able to read
        # an arbitrary file off disk.
        if not path.is_relative_to(self.root.resolve()):
            raise StorageError(f"{uri} is outside the object store")
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise StorageError(f"could not read {uri}") from exc

    async def delete_prefix(self, prefix: str) -> int:
        target = self._path(prefix)

        def remove() -> int:
            if not target.exists():
                return 0
            removed = sum(1 for path in target.rglob("*") if path.is_file())
            shutil.rmtree(target)
            return removed

        return await asyncio.to_thread(remove)

    def _path(self, key: str) -> Path:
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise StorageError(f"unsafe object key: {key!r}")
        return resolved


@dataclass(slots=True)
class GcsObjectStore:
    """Google Cloud Storage, one bucket per class of object."""

    bucket_name: str
    _client: Any | None = None

    def _bucket(self) -> Any:
        if self._client is None:
            # Lazily, so a local process never constructs a storage client and never
            # looks for credentials it does not have.
            import google.cloud.storage as gcs

            self._client = gcs.Client()
        return self._client.bucket(self.bucket_name)

    async def put(self, key: str, data: bytes, *, content_type: str) -> str:
        def upload() -> None:
            self._bucket().blob(key).upload_from_string(data, content_type=content_type)

        # The storage client is synchronous; a thread keeps the event loop free.
        await asyncio.to_thread(upload)
        return f"gs://{self.bucket_name}/{key}"

    async def get(self, uri: str) -> bytes:
        prefix = f"gs://{self.bucket_name}/"
        if not uri.startswith(prefix):
            raise StorageError(f"{uri} is not in {self.bucket_name}")

        def download() -> bytes:
            data: bytes = self._bucket().blob(uri.removeprefix(prefix)).download_as_bytes()
            return data

        return await asyncio.to_thread(download)

    async def delete_prefix(self, prefix: str) -> int:
        def remove() -> int:
            bucket = self._bucket()
            blobs = list(bucket.list_blobs(prefix=prefix))
            for blob in blobs:
                blob.delete()
            return len(blobs)

        return await asyncio.to_thread(remove)
