"""Where original files live: local disk (`file://...`) or S3-compatible storage (`s3://bucket/prefix`, MinIO with
S3_ENDPOINT_URL). Content-addressed by SHA-256, so a file stored in ten project folders is kept once."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from rag.config.settings import Settings


def blob_key(sha256: str) -> str:
    return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None: ...


class LocalObjectStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("object key escapes the store root")
        return path

    def _put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            Path(tmp).replace(path)  # atomic on the same filesystem
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3ObjectStore:
    def __init__(self, bucket: str, prefix: str, settings: Settings) -> None:
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.secret("s3_access_key"),
            aws_secret_access_key=settings.secret("s3_secret_key"),
            region_name=settings.s3_region,
        )

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._client.put_object, Bucket=self.bucket, Key=self._key(key), Body=data)

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self._client.get_object, Bucket=self.bucket, Key=self._key(key))
        return await asyncio.to_thread(response["Body"].read)

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=self._key(key))
        except ClientError:
            return False
        return True

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=self._key(key))


def make_object_store(settings: Settings) -> ObjectStore:
    url = settings.object_store_url
    parsed = urlparse(url)
    if parsed.scheme == "file":
        root = url[len("file://"):]
        return LocalObjectStore(root)
    if parsed.scheme == "s3":
        return S3ObjectStore(parsed.netloc, parsed.path, settings)
    raise ValueError(f"OBJECT_STORE_URL must start with file:// or s3:// (got {url!r})")
