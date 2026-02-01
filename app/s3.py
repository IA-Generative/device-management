from __future__ import annotations

import os

import boto3
from botocore.client import Config

from .settings import settings

def s3_client():
    # Compatible AWS S3 et S3 compatibles (MinIO, etc.)
    endpoint_url = settings.s3_endpoint_url or None
    region = settings.aws_region or os.getenv("AWS_REGION") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        ),
    )
