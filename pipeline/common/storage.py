"""Content-addressed object storage over the S3 API (SeaweedFS in compose, memory in tests)."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

_REF = re.compile(
    r"^(?P<bucket>[a-z0-9-]{3,63})/sha256/(?P<hex>[0-9a-f]{64})(?P<ext>\.[a-z0-9]{1,8})?$"
)
_EXT = re.compile(r"^[a-z0-9]{1,8}$")

BUCKETS = ("media", "consent", "evidence", "provenance")


def make_ref(bucket: str, data: bytes, ext: str) -> str:
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}")
    if not _EXT.match(ext):
        raise ValueError("bad extension")
    return f"{bucket}/sha256/{hashlib.sha256(data).hexdigest()}.{ext}"


def parse_ref(ref: str) -> tuple[str, str]:
    m = _REF.match(ref)
    if not m:
        raise ValueError("not an object reference")
    return m["bucket"], ref.split("/", 1)[1]


class ObjectStore(Protocol):
    def put(self, bucket: str, data: bytes, ext: str, content_type: str) -> str: ...
    def get(self, ref: str) -> bytes: ...


class MemoryObjectStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, bucket: str, data: bytes, ext: str, content_type: str) -> str:
        ref = make_ref(bucket, data, ext)
        self._data[ref] = data
        return ref

    def get(self, ref: str) -> bytes:
        parse_ref(ref)
        return self._data[ref]


class S3ObjectStore:
    """Writes are idempotent: the key is the content hash, so a re-run never duplicates data."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str) -> None:
        import boto3  # worker/api extra only

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    def put(self, bucket: str, data: bytes, ext: str, content_type: str) -> str:
        ref = make_ref(bucket, data, ext)
        _, key = parse_ref(ref)
        self._s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return ref

    def get(self, ref: str) -> bytes:
        bucket, key = parse_ref(ref)
        body: bytes = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return body
