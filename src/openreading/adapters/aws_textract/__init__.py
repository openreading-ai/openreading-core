"""AWS Textract adapter (optional extra `textract`; boto3, SigV4, BYO-IAM)."""

from __future__ import annotations

from openreading.adapters.aws_textract.adapter import (
    AWSTextractAdapter,
    S3Uploader,
    TextractClient,
)

__all__ = ["AWSTextractAdapter", "TextractClient", "S3Uploader"]
