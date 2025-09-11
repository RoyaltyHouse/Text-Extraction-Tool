import awsgi
from flask import Flask, request, jsonify
import json
import boto3,time
import requests
import urllib.parse
import re
import tempfile
from extractor import extract_text_from_pdf,textract_text_image_by_image,textract_lines_by_page_from_file
from database import save_data_to_database, get_data_from_database, delete_data_from_database
from flask_cors import CORS
import os
import time
from botocore.exceptions import NoCredentialsError

app = Flask(__name__)
CORS(app)

# S3 configuration
S3_BUCKET = os.getenv("BUCKET_NAME")  # Replace with your actual bucket name
s3 = boto3.client('s3')
textract = boto3.client('textract')

JSON_FILE = "field_descriptions.json"

ALLOWED_EXTS = {"pdf"}
ALLOWED_MIME = {"application/pdf"}


@app.route('/extract', methods=['POST'])
def uploads():
    artist_id = request.args.get("artist_id")
    original_document_id = request.args.get("original_document_id")
    print(f"Received artist_id: {artist_id} and document_id: {original_document_id}")
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
            page_text_dict = textract_lines_by_page_from_file(file, bucket=S3_BUCKET)
            if isinstance(page_text_dict, tuple) or not isinstance(page_text_dict, dict):
                return page_text_dict  # This handles both (jsonify_dict, 200) and just a Response object

            preview = extract_text_from_pdf(page_text_dict)
            databaseResponse = save_data_to_database(preview, file.filename,artist_id,original_document_id)
            results.append({
                'file': file.filename,
                'DatabaseResponse': databaseResponse.get("message") + " in database",
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
        preview = extract_text_from_pdf(combined_text)
        databaseResponse = save_data_to_database(preview, file.filename)
        results.append({
            'file': ", ".join(file_list),
            'DatabaseResponse': databaseResponse.get("message") + " in database",
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

# Extract text from file URL endpoint
@app.route('/extract_from_url', methods=['POST'])
def extract_from_url():
    data = request.get_json(force=True)
    file_url = data.get('url')
    # Normalize Google Drive share links to direct-download
    file_url = _normalize_to_direct_download(file_url)
    if not file_url:
        return jsonify({"error": "Missing 'url' in request body"}), 400
    try:
        # Download the file (supports pre‑signed S3, public URLs, and normalized Google Drive links)
        resp = requests.get(file_url, stream=True, timeout=30)
        resp.raise_for_status()
        content = resp.content
        # Determine a filename
        # 1) Try Content-Disposition
        cd = resp.headers.get('Content-Disposition', '')
        filename = None
        if 'filename=' in cd:
            filename = cd.split('filename=')[-1].strip('"; ')
        # 2) Fallback to URL path
        if not filename:
            parsed = urllib.parse.urlparse(file_url)
            path_name = os.path.basename(parsed.path)
            filename = path_name or "document.pdf"
        # Normalize extension (default to pdf)
        if '.' in filename:
            ext = filename.rsplit('.', 1)[-1].lower()
        else:
            ext = 'pdf'
            filename = f"{filename}.pdf"
        # Enforce allowed extensions
        if ext not in ALLOWED_EXTS:
            return jsonify({"error": f"Unsupported file type '.{ext}'. Allowed: {', '.join(ALLOWED_EXTS)}"}), 400
        # Create a minimal FileStorage-like adapter so we can reuse the existing helper
        class _DownloadedFileAdapter:
            def __init__(self, name, data_bytes):
                self.filename = name
                self._data = data_bytes
            def save(self, dst_path):
                with open(dst_path, "wb") as f:
                    f.write(self._data)
        downloaded = _DownloadedFileAdapter(filename, content)
        # Reuse the same Textract flow
        page_text_dict = textract_lines_by_page_from_file(downloaded, bucket=S3_BUCKET)
        # Format preview with existing utility
        preview = extract_text_from_pdf(page_text_dict)
        databaseResponse = save_data_to_database(preview,filename )
        return jsonify({
            "url": file_url,
            "file": filename,
            "DatabaseResponse": databaseResponse.get("message")+" in database",
            "preview": preview
        }), 200
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to download file: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": f"Extraction failed: {str(e)}"}), 500

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
    if not field or not value:
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
    # Handle different event types
    if 'httpMethod' in event:
        # API Gateway event
        return awsgi.response(app, event, context)
    elif 'Records' in event:
        # S3 or other AWS service event
        return {
            'statusCode': 200,
            'body': 'Event processed successfully'
        }
    else:
        # Simple test event or other event type
        return {
            'statusCode': 200,
            'body': 'Lambda function is working! Use API Gateway to access the endpoints.',
            'headers': {
                'Content-Type': 'application/json'
            }
        }
    

json_str = {
    "Alternative Counterparties": {
        "page_number": 1,
        "value": "RCA Records, Sony Music Entertainment"
    },
    "Artist Name": {
        "page_number": 1,
        "value": "A$AP Mob"
    },
    "Classification of Recoupment Language": {
        "page_number": 4,
        "value": "No royalty shall be payable to you hereunder until Company has recouped all Recording Costs incurred in connection with the Album at the \"net artist\" rate (i.e., Our Basic Rate less the Producer Basic Rate and the royalty rate payable to all other producers, engineers, mixers, and other royalty participants) (excluding the Advance and any \"in-pocket\" Artist advances. After recoupment of such Recording Costs as aforesaid, royalties shall be payable to you hereunder for all records sold for which royalties are payable, retroactively from the first such record sold, subject to recoupment from such royalties of the Advance."
    },
    "Client Party": {
        "page_number": 1,
        "value": "19/20 Music, LLC"
    },
    "Direct Counterparty": {
        "page_number": 1,
        "value": "ASAP WORLDWIDE, LLC"
    },
    "Distributor": {
        "page_number": 1,
        "value": "RCA Records"
    },
    "Document Name": {
        "page_number": 1,
        "value": "A$AP Mob - Cozy Tapes: Vol. 2 FX"
    },
    "Effective Date": {
        "page_number": 1,
        "value": "August 1, 2017"
    },
    "Execution Status": {
        "page_number": 9,
        "value": "FX"
    },
    "Label": {
        "page_number": 1,
        "value": "Sony Music Entertainment"
    },
    "Lawyer Information": {
        "page_number": 1,
        "value": {
            "client_lawyer": "C/O B. Lawrence Watkins & Associates, P.C., 325 Edgewood Avenue, SE, Suite 200, Atlanta, Georgia 30312",
            "counterparty_lawyer": "C/O Davis Shapiro Lewit Grabel Leven Granderson & Blake, LLP, 150 S. Rodeo Drive, Suite 200, Beverly Hills, CA 90212"
        }
    },
    "Legal Advance": {
        "page_number": 3,
        "value": "$1,000"
    },
    "Organization Counting Units": {
        "page_number": 4,
        "value": "USNRC"
    },
    "Producer Advance Legal Recoupment": {
        "page_number": 3,
        "value": "$6,000"
    },
    "Producer Royalty Points": {
        "page_number": 4,
        "value": "3% of the Royalty Base Price"
    },
    "Recoupment Classification": {
        "page_number": 3,
        "value": "non-returnable but recoupable"
    },
    "Single/Multisong Line": {
        "page_number": 1,
        "value": "one (1) master recording"
    },
    "Song Title": {
        "page_number": 1,
        "value": "Bahamas"
    },
    "Third Party Money": {
        "page_number": 13,
        "value": "15%"
    },
    "Type of Royalty": {
        "page_number": 4,
        "value": "NAR"
    }
}