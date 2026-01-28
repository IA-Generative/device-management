from __future__ import annotations

import boto3
from botocore.client import Config


def s3_client():
    # Compatible AWS S3 et S3 compatibles (MinIO, etc.)
    return boto3.client("s3", config=Config(signature_version="s3v4"))
