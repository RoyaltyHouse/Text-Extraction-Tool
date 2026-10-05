import json
import os
import re
import time
import traceback
import urllib.parse
import uuid

import awsgi
import boto3
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

import job_store
from extractor import S3_BUCKET, TextractError, s3, textract_lines, upload
from gpt_extractor import extract_field_information

app = Flask(__name__)
CORS(app)

LAMBDA_FUNCTION_ARN = os.getenv("LAMBDA_FUNCTION_ARN", "arn:aws:lambda:us-east-2:713944518341:function:extract-tool-api")
lambda_client = boto3.client("lambda")
ALLOWED_EXTS = {"pdf"}

@app.errorhandler(TextractError)
def handle_textract_error(e):
    return jsonify({"error": str(e)}), e.status

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({"error": str(e), "type": type(e).__name__}), 500

def _unsupported(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return None if ext in ALLOWED_EXTS else f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTS)}"

@app.route("/extract", methods=["POST"])
def uploads():
    ids = {"artist_id": request.args.get("artist_id"), "original_document_id": request.args.get("original_document_id")}
    instructions = request.form.get("instructions")
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "No file provided"}), 400
    if not instructions:
        return jsonify({"error": "Missing 'instructions' field"}), 400

    results, image_lines, image_names = [], {}, []
    for file in files:
        name = file.filename.lower()
        if name.endswith(".pdf"):
            preview = extract_field_information(*textract_lines(upload(file.filename, file.read())), instructions=instructions,
                                                exclude_signature_pages=request.form.getlist("exclude_signature_pages"))
            results.append({"file": file.filename, **ids, "preview": preview})
        elif name.endswith((".doc", ".docx")):
            results.append({"file": file.filename, "error": "Currently, only PDF files are supported. Word document support is coming soon."})
        elif name.endswith((".jpeg", ".jpg", ".png")):
            # Images parse together as one document, a page per image, without word coordinates.
            image_names.append(file.filename)
            line_index, _ = textract_lines(upload(file.filename, file.read()))
            for entry in line_index.values():
                image_lines[len(image_lines) + 1] = {"page": len(image_names), "text": entry["text"], "words": []}
        else:
            results.append({"file": file.filename, "error": "File type not supported. Only PDF, JPEG, JGE, PNG is allowed."})
    if image_lines:
        results.append({"file": ", ".join(image_names), **ids, "preview": extract_field_information(image_lines, instructions=instructions)})
    return jsonify(results)


def _normalize_to_direct_download(url):
    parsed = urllib.parse.urlparse(url)
    if "drive.google.com" not in parsed.netloc.lower():
        return url
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
    file_id = match.group(1) if match else urllib.parse.parse_qs(parsed.query).get("id", [None])[0]
    return f"https://drive.google.com/uc?export=download&id={file_id}" if file_id else url

@app.route("/presigned-upload-url", methods=["POST"])
def presigned_upload_url():
    data = request.get_json(force=True)
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "Missing 'filename' in request body"}), 400
    if error := _unsupported(filename):
        return jsonify({"error": error}), 400
    s3_key = f"uploads/{uuid.uuid4()}/{filename}"
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key, "ContentType": data.get("content_type", "application/pdf")},
        ExpiresIn=300,
    )
    return jsonify({"upload_url": upload_url, "s3_key": s3_key}), 200

def _run_extraction(job_id, s3_key=None, url=None, artist_id=None, original_document_id=None, instructions=None,
                    exclude_signature_pages=(), deadline=None):
    try:
        job_store.update_job(job_id, status="processing")
        if s3_key:
            filename = os.path.basename(s3_key)
        elif url:
            url = _normalize_to_direct_download(url)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            disposition = resp.headers.get("Content-Disposition", "")
            filename = (disposition.split("filename=")[-1].strip('"; ') if "filename=" in disposition else "") \
                or os.path.basename(urllib.parse.urlparse(url).path) or "document.pdf"
        else:
            raise ValueError("Either s3_key or url must be provided")
        if "." not in filename:
            filename += ".pdf"
        if error := _unsupported(filename):
            raise ValueError(error)

        preview = extract_field_information(*textract_lines(s3_key or upload(filename, resp.content)), deadline, instructions,
                                            exclude_signature_pages)
        job_store.update_job(job_id, status="done", result={
            "file": filename, "artist_id": artist_id, "original_document_id": original_document_id, "preview": preview,
        })
    except Exception:
        job_store.update_job(job_id, status="failed", error=traceback.format_exc())

@app.route("/extract_from_url", methods=["POST"])
def extract_from_url():
    data = request.get_json(force=True)
    s3_key, file_url = data.get("s3_key"), data.get("url")
    if not s3_key and not file_url:
        return jsonify({"error": "Missing 'url' or 's3_key' in request body"}), 400
    if not data.get("instructions"):
        return jsonify({"error": "Missing 'instructions' in request body"}), 400

    job_id = str(uuid.uuid4())
    job_store.create_job(job_id, s3_key or file_url)
    lambda_client.invoke(FunctionName=LAMBDA_FUNCTION_ARN, InvocationType="Event", Payload=json.dumps({
        "job_id": job_id,
        "s3_key": s3_key,
        "url": file_url,
        "artist_id": request.args.get("artist_id"),
        "original_document_id": request.args.get("original_document_id"),
        "instructions": data.get("instructions"),
        "exclude_signature_pages": data.get("exclude_signature_pages") or [],
    }).encode())
    return jsonify({"job_id": job_id, "status": "pending"}), 202

@app.route("/result/<job_id>", methods=["GET"])
def get_result(job_id):
    try:
        return jsonify(job_store.get_job(job_id)), 200
    except KeyError:
        return jsonify({"error": "Job not found"}), 404

@app.route("/max", methods=["GET"])
def max_route():
    return jsonify({"message": "Api gateway is working"}), 200

def lambda_handler(event, context):
    if "httpMethod" in event:
        return awsgi.response(app, event, context)
    if "job_id" in event:
        # Async self-invocation from /extract_from_url, clear of API Gateway's 29 s timeout.
        _run_extraction(**event, deadline=time.time() + context.get_remaining_time_in_millis() / 1000)
        return {"statusCode": 200, "body": "Extraction complete"}
    return {"statusCode": 200, "body": "Lambda function is working! Use API Gateway to access the endpoints."}

if __name__ == "__main__":
    app.run(debug=True)