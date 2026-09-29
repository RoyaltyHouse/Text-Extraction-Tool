import json
import os
from datetime import datetime, timezone
import boto3

S3_BUCKET = os.getenv("BUCKET_NAME")
_s3 = boto3.client("s3")

def _write(job_id, job):
    _s3.put_object(Bucket=S3_BUCKET, Key=f"jobs/{job_id}.json", Body=json.dumps(job), ContentType="application/json")

def create_job(job_id, s3_key):
    _write(job_id, {"job_id": job_id, "status": "pending", "s3_key": s3_key,
                    "created_at": datetime.now(timezone.utc).isoformat()})

def get_job(job_id):
    try:
        return json.loads(_s3.get_object(Bucket=S3_BUCKET, Key=f"jobs/{job_id}.json")["Body"].read())
    except _s3.exceptions.NoSuchKey:
        raise KeyError(job_id)

def update_job(job_id, **fields):
    _write(job_id, {**get_job(job_id), **fields})