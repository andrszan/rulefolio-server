from typing import BinaryIO, cast

import boto3
from botocore.client import BaseClient, Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.files.policy import S3_TIMEOUT_SECONDS


class StorageUnavailable(Exception):
    def __init__(self, deleted: int = 0) -> None:
        self.deleted = deleted


def _client() -> BaseClient:
    if not all(
        (
            settings.s3_endpoint,
            settings.s3_access_key_id,
            settings.s3_secret_access_key.get_secret_value(),
            settings.s3_bucket_name,
        )
    ):
        raise StorageUnavailable
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        config=Config(
            connect_timeout=S3_TIMEOUT_SECONDS,
            read_timeout=S3_TIMEOUT_SECONDS,
            retries={"max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path" if settings.s3_use_path_style else "auto"},
        ),
    )


def put_object(key: str, source: BinaryIO, content_type: str) -> None:
    try:
        _client().put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=source,
            ContentType=content_type,
            IfNoneMatch="*",
        )
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable from error


def open_object(key: str) -> BinaryIO | None:
    try:
        response = _client().get_object(Bucket=settings.s3_bucket_name, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchObject"}:
            return None
        raise StorageUnavailable from error
    except BotoCoreError as error:
        raise StorageUnavailable from error
    return cast(BinaryIO, response["Body"])


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable from error


def list_object_keys() -> list[str]:
    try:
        paginator = _client().get_paginator("list_objects_v2")
        return [
            item["Key"]
            for page in paginator.paginate(Bucket=settings.s3_bucket_name)
            for item in page.get("Contents", [])
        ]
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable from error


def delete_objects(keys: list[str]) -> int:
    deleted = 0
    try:
        client = _client()
        for start in range(0, len(keys), 1_000):
            response = client.delete_objects(
                Bucket=settings.s3_bucket_name,
                Delete={
                    "Objects": [{"Key": key} for key in keys[start : start + 1_000]]
                },
            )
            deleted += len(response.get("Deleted", []))
            if response.get("Errors"):
                raise StorageUnavailable(deleted)
    except (BotoCoreError, ClientError) as error:
        raise StorageUnavailable(deleted) from error
    return deleted
