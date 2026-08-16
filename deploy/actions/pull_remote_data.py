#!/usr/bin/env python3
"""Download recent TrendRadar SQLite files from an S3-compatible bucket."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> int:
    region = os.environ.get("S3_REGION", "").strip()
    kwargs = {
        "endpoint_url": _required("S3_ENDPOINT_URL"),
        "aws_access_key_id": _required("S3_ACCESS_KEY_ID"),
        "aws_secret_access_key": _required("S3_SECRET_ACCESS_KEY"),
        "config": BotoConfig(
            s3={"addressing_style": "virtual"},
            signature_version="s3v4",
        ),
    }
    if region:
        kwargs["region_name"] = region

    s3 = boto3.client("s3", **kwargs)
    bucket = _required("S3_BUCKET_NAME")
    days = max(1, int(os.environ.get("PULL_DAYS", "7")))
    now = datetime.now(ZoneInfo(os.environ.get("TZ", "Asia/Shanghai")))

    for offset in range(days):
        date_str = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        for db_type in ("news", "rss"):
            key = f"{db_type}/{date_str}.db"
            destination = Path("output") / db_type / f"{date_str}.db"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3.download_file(bucket, key, str(destination))
                print(f"downloaded {key} -> {destination}")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"404", "NoSuchKey", "Not Found"}:
                    print(f"skip missing {key}")
                    continue
                raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
