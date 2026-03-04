import json
import boto3
import os
from botocore.exceptions import ClientError
from datetime import datetime, timezone

S3_BUCKET = os.getenv("BUCKET_NAME")
_s3 = boto3.client("s3")


def _key(job_id: str) -> str:
    return f"jobs/{job_id}.json"


def _read(job_id: str) -> dict:
    try:
        resp = _s3.get_object(Bucket=S3_BUCKET, Key=_key(job_id))
        return json.loads(resp["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            raise KeyError(job_id)
        raise


def _write(job_id: str, payload: dict) -> None:
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=_key(job_id),
        Body=json.dumps(payload),
        ContentType="application/json",
    )


def create_job(job_id: str, s3_key: str) -> None:
    """Write a new job record with status='pending'."""
    _write(job_id, {
        "job_id": job_id,
        "status": "pending",
        "s3_key": s3_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def get_job(job_id: str) -> dict:
    """Read the current job record from S3. Raises KeyError if not found."""
    return _read(job_id)


def set_processing(job_id: str) -> None:
    j = _read(job_id)
    j["status"] = "processing"
    _write(job_id, j)


def set_done(job_id: str, result: dict) -> None:
    j = _read(job_id)
    j["status"] = "done"
    j["result"] = result
    _write(job_id, j)


def set_failed(job_id: str, error: str) -> None:
    j = _read(job_id)
    j["status"] = "failed"
    j["error"] = error
    _write(job_id, j)
