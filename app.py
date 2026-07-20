import awsgi
from flask import Flask, request, jsonify
import json
import boto3
import requests
import urllib.parse
import re
import traceback
import uuid
from extractor import extract_text_from_pdf, textract_lines_by_page_from_file, textract_text_image_by_image
from flask_cors import CORS
import os
import job_store

app = Flask(__name__)
CORS(app)

@app.errorhandler(Exception)
def handle_exception(e):
    """Catch-all: return the real error in JSON so we can see it in the response body."""
    tb = traceback.format_exc()
    return jsonify({
        "error": str(e),
        "type": type(e).__name__,
        "traceback": tb
    }), 500

# S3 / Lambda configuration
S3_BUCKET = os.getenv("BUCKET_NAME")
LAMBDA_FUNCTION_ARN = os.getenv(
    "LAMBDA_FUNCTION_ARN",
    "arn:aws:lambda:us-east-2:713944518341:function:extract-tool-api",
)

s3 = boto3.client('s3')
lambda_client = boto3.client('lambda')

JSON_FILE = "field_descriptions.json"

ALLOWED_EXTS = {"pdf"}


@app.route('/extract', methods=['POST'])
def uploads():
    artist_id = request.args.get("artist_id")
    original_document_id = request.args.get("original_document_id")
    print(f"Received artist_id: {artist_id} and original_document_id: {original_document_id}")
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "No file provided"}), 400

    results = []
    # Initialize variables for combined text and file list
    combined_text, file_list, page_counter = {}, [], 1
    for file in files:
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            print(f"[DEBUG] Received PDF file: {file.filename}")
            extraction_result = textract_lines_by_page_from_file(file, bucket=S3_BUCKET)
            if isinstance(extraction_result, tuple):
                return jsonify(extraction_result[0]), extraction_result[1]
            if not isinstance(extraction_result, dict):
                return jsonify({"error": "Unexpected extraction result"}), 500

            # Check if it's an error response
            if "error" in extraction_result:
                return jsonify(extraction_result), 400

            line_index = extraction_result.get("line_index", {})
            annotations = extraction_result.get("annotations", [])
            preview = extract_text_from_pdf(line_index, annotations)
            results.append({
                'file': file.filename,
                "artist_id": artist_id,
                "original_document_id": original_document_id,
                'preview': preview
            })
        elif filename.endswith(('.doc', '.docx')):
            print(f"[DEBUG] Received file: {file.filename}")
            results.append({
                "file": file.filename,
                "error": "Currently, only PDF files are supported. Word document support is coming soon."
            })
        elif filename.endswith(('.jpeg', '.jpg', '.png')):
            print(f"[DEBUG] Received image files: {file.filename}")
            extract_text = textract_text_image_by_image(file)
            combined_text[str(page_counter)] = extract_text
            page_counter += 1
            file_list.append(file.filename)
        else:
            results.append({
                "file": file.filename,
                "error": "File type not supported. Only PDF, JPEG, JGE, PNG is allowed."
            })
    if combined_text:
        # Build a minimal line_index from image text (no word bboxes)
        img_line_index = {}
        ln = 1
        for page_str, lines in combined_text.items():
            page_num = int(page_str)
            if isinstance(lines, list):
                for line_text in lines:
                    img_line_index[ln] = {"page": page_num, "text": line_text, "words": []}
                    ln += 1
            else:
                img_line_index[ln] = {"page": page_num, "text": str(lines), "words": []}
                ln += 1
        preview = extract_text_from_pdf(img_line_index)
        results.append({
            'file': ", ".join(file_list),
            "artist_id": artist_id,
            "original_document_id": original_document_id,
            'preview': preview
        })
    return jsonify(results)


def _normalize_to_direct_download(url: str) -> str:
    """
    If the URL is a Google Drive share/view URL, convert it to a direct download link.
    Otherwise, return the original URL.
    Supported forms:
      - https://drive.google.com/file/d/<FILE_ID>/view?...  -> uc?export=download&id=<FILE_ID>
      - https://drive.google.com/open?id=<FILE_ID>          -> uc?export=download&id=<FILE_ID>
      - https://drive.google.com/uc?id=<FILE_ID>&export=download -> unchanged
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        if "drive.google.com" not in host:
            return url
        # Already a uc direct link with id
        if parsed.path.startswith("/uc"):
            qs = urllib.parse.parse_qs(parsed.query)
            if "id" in qs:
                return f"https://drive.google.com/uc?export=download&id={qs['id'][0]}"
            return url
        # /file/d/<id>/view or /file/d/<id>/
        m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
        if m:
            file_id = m.group(1)
            return f"https://drive.google.com/uc?export=download&id={file_id}"
        # /open?id=<id>
        qs = urllib.parse.parse_qs(parsed.query)
        if "id" in qs:
            return f"https://drive.google.com/uc?export=download&id={qs['id'][0]}"
        return url
    except Exception:
        return url


@app.route('/presigned-upload-url', methods=['POST'])
def presigned_upload_url():
    data = request.get_json(force=True)
    filename = data.get('filename')
    content_type = data.get('content_type', 'application/pdf')

    if not filename:
        return jsonify({"error": "Missing 'filename' in request body"}), 400

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTS)}"}), 400

    s3_key = f"uploads/{uuid.uuid4()}/{filename}"

    upload_url = s3.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': S3_BUCKET,
            'Key': s3_key,
            'ContentType': content_type
        },
        ExpiresIn=300
    )

    return jsonify({"upload_url": upload_url, "s3_key": s3_key}), 200


def _run_extraction(job_id, s3_key, url=None, artist_id=None, original_document_id=None):
    """
    Core extraction pipeline. Runs outside of a Flask request context — called
    from the background Lambda invocation dispatched by extract_from_url.
    Writes final state (done/failed) to S3 via job_store.
    """
    try:
        job_store.set_processing(job_id)

        # Resolve the file URL
        if s3_key:
            file_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': s3_key},
                ExpiresIn=900,  # 15 min — enough for the full pipeline
            )
        elif url:
            file_url = _normalize_to_direct_download(url)
        else:
            raise ValueError("Either s3_key or url must be provided")

        # Download the file
        resp = requests.get(file_url, stream=True, timeout=30)
        resp.raise_for_status()
        content = resp.content

        # Determine filename
        cd = resp.headers.get('Content-Disposition', '')
        filename = None
        if 'filename=' in cd:
            filename = cd.split('filename=')[-1].strip('"; ')
        if not filename:
            parsed = urllib.parse.urlparse(file_url)
            path_name = os.path.basename(parsed.path)
            filename = path_name or "document.pdf"
        if '.' not in filename:
            filename = f"{filename}.pdf"

        ext = filename.rsplit('.', 1)[-1].lower()
        if ext not in ALLOWED_EXTS:
            raise ValueError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTS)}")

        class _DownloadedFileAdapter:
            def __init__(self, name, data_bytes):
                self.filename = name
                self._data = data_bytes

            def save(self, dst_path):
                with open(dst_path, "wb") as f:
                    f.write(self._data)

        downloaded = _DownloadedFileAdapter(filename, content)

        # Run Textract (up to 115 s) — note: this function returns Flask response
        # tuples on its error paths, so we guard for that explicitly.
        extraction_result = textract_lines_by_page_from_file(downloaded, bucket=S3_BUCKET)

        if isinstance(extraction_result, tuple):
            # (dict, status_code) tuple returned from extractor error path
            body = extraction_result[0]
            error_msg = body.get("error", "Textract returned an error response") if isinstance(body, dict) else str(body)
            raise RuntimeError(error_msg)

        if "error" in extraction_result:
            raise RuntimeError(extraction_result["error"])

        line_index = extraction_result.get("line_index", {})
        annotations = extraction_result.get("annotations", [])

        # Run GPT extraction
        preview = extract_text_from_pdf(line_index, annotations)

        job_store.set_done(job_id, {
            "file": filename,
            "artist_id": artist_id,
            "original_document_id": original_document_id,
            "preview": preview,
        })

    except Exception:
        job_store.set_failed(job_id, traceback.format_exc())


@app.route('/extract_from_url', methods=['POST'])
def extract_from_url():
    artist_id = request.args.get("artist_id")
    original_document_id = request.args.get("original_document_id")
    data = request.get_json(force=True)
    s3_key = data.get('s3_key')
    file_url = data.get('url')

    if not s3_key and not file_url:
        return jsonify({"error": "Missing 'url' or 's3_key' in request body"}), 400

    job_id = str(uuid.uuid4())
    job_store.create_job(job_id, s3_key or file_url)

    lambda_client.invoke(
        FunctionName=LAMBDA_FUNCTION_ARN,
        InvocationType="Event",   # async fire-and-forget — returns immediately
        Payload=json.dumps({
            "job_id": job_id,
            "s3_key": s3_key,
            "url": file_url,
            "artist_id": artist_id,
            "original_document_id": original_document_id,
        }).encode(),
    )

    return jsonify({"job_id": job_id, "status": "pending"}), 202


@app.route('/result/<job_id>', methods=['GET'])
def get_result(job_id):
    try:
        return jsonify(job_store.get_job(job_id)), 200
    except KeyError:
        return jsonify({"error": "Job not found"}), 404


@app.route("/get_fields", methods=["GET"])
def get_fields():
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r") as f:
            return jsonify(json.load(f))
    return jsonify({})

@app.route("/add_field", methods=["POST"])
def update_field():
    data = request.get_json()
    field = data.get("field")
    value = data.get("value")
    print(data)
    if not field or value is None:
        return jsonify({"error": "Missing 'field' or 'value'"}), 400

    # Load existing data
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r") as f:
            json_data = json.load(f)
    else:
        json_data = {}

    # Update field
    json_data[field] = value

    # Save back to file
    with open(JSON_FILE, "w") as f:
        json.dump(json_data, f, indent=4)

    return jsonify({"message": "Field updated successfully"}), 200

@app.route("/delete_field/<field_key>", methods=["DELETE"])
def delete_field(field_key):
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r") as f:
            json_data = json.load(f)
    else:
        return jsonify({"error": "File not found"}), 404

    if field_key not in json_data:
        return jsonify({"error": "Field not found"}), 404

    del json_data[field_key]

    with open(JSON_FILE, "w") as f:
        json.dump(json_data, f, indent=4)

    return jsonify({"message": f"Field '{field_key}' deleted"}), 200

@app.route('/max', methods=['GET'])
def max_route():
    return jsonify({"message": "Api gateway is working"}), 200


if __name__ == '__main__':
    app.run(debug=True)


def lambda_handler(event, context):
    if 'httpMethod' in event:
        # Standard API Gateway HTTP request
        return awsgi.response(app, event, context)
    elif 'job_id' in event:
        # Background async invocation dispatched by extract_from_url.
        # Runs outside API Gateway so no 29 s timeout constraint.
        _run_extraction(
            job_id=event['job_id'],
            s3_key=event.get('s3_key'),
            url=event.get('url'),
            artist_id=event.get('artist_id'),
            original_document_id=event.get('original_document_id'),
        )
        return {'statusCode': 200, 'body': 'Extraction complete'}
    elif 'Records' in event:
        # S3 or other AWS service event
        return {
            'statusCode': 200,
            'body': 'Event processed successfully'
        }
    else:
        # Simple test event
        return {
            'statusCode': 200,
            'body': 'Lambda function is working! Use API Gateway to access the endpoints.',
            'headers': {
                'Content-Type': 'application/json'
            }
        }
