"""One-shot job: create the object-store buckets if they do not exist."""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

from pipeline.common.settings import get_settings
from pipeline.common.storage import BUCKETS


def main() -> int:
    s = get_settings()
    ak, sk = s.secret("s3_access_key"), s.secret("s3_secret_key")
    if ak is None or sk is None:
        raise SystemExit("object store credentials are not configured")
    s3 = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id=ak.get_secret_value(),
        aws_secret_access_key=sk.get_secret_value(),
    )
    for attempt in range(10):  # the S3 gateway can lag the store's own healthcheck
        try:
            existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
            break
        except EndpointConnectionError:
            if attempt == 9:
                raise
            time.sleep(2)
    for b in BUCKETS:
        if b not in existing:
            try:
                s3.create_bucket(Bucket=b)
                print(f"created bucket {b}")
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                raise SystemExit(f"could not create bucket {b}: {code}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
