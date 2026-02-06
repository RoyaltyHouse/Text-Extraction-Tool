# extractor.py
from pypdf import PdfReader
from gpt_extractor import extract_field_information
import json , os, tempfile, time
import boto3
from flask import jsonify
import time


# S3 configuration
S3_BUCKET = os.getenv("BUCKET_NAME")  # Replace with your actual bucket name
s3 = boto3.client('s3')
textract = boto3.client('textract')


def extract_text_from_pdf(text, blocks=None):
    if text is None:
        return {"error": "No text provided for extraction"}
    print("[DEBUG] Extraction started for text")

    open_ai_Data = extract_field_information(text, blocks)

    if isinstance(open_ai_Data, dict):
        return open_ai_Data
    return json.loads(open_ai_Data)


def textract_text_image_by_image(file):
    extraction_result = textract_lines_by_page_from_file(file, bucket=S3_BUCKET)
    if isinstance(extraction_result, tuple) or not isinstance(extraction_result, dict):
        return extraction_result

    # Handle new structure
    extract = extraction_result.get("text", {})

    if isinstance(extract, dict):
        # If nested like {1: [...]} and only one page, unwrap it
        if len(extract) == 1:
            return next(iter(extract.values()))
        else:
            return [line for lines in extract.values() for line in lines]
    return []

    
def extract_text_from_word(path):
    print("Extracting text from Word document:", path)
    return path



def textract_lines_by_page_from_file(file, bucket=S3_BUCKET):

    # Save the file to a temporary local path
    local_path = os.path.join(tempfile.gettempdir(), file.filename)
    file.save(local_path)
    print(f"[DEBUG] Saved file locally at: {local_path}")

    # Upload to S3
    key = file.filename
    s3.upload_file(local_path, bucket, key)
    print(f"[DEBUG] Upload successful: s3://{bucket}/{key}")

    # Start async text detection
    job = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}})
    job_id = job["JobId"]
    print(f"[DEBUG] Started Textract job with JobId: {job_id}")

    # Poll until done or timeout
    start_time = time.time()
    while True:
        if time.time() - start_time > 115:
            return jsonify({"error": "Text extraction failed due to poor image quality, formatting, or an unreadable document."}), 200

        resp = textract.get_document_text_detection(JobId=job_id, MaxResults=1000)
        status = resp["JobStatus"]
        print(f"[DEBUG] Job status: {status}")
    
        if status in ("SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"):
            break
        time.sleep(2)


    if status != "SUCCEEDED":
        raise RuntimeError(f"Textract job ended with status: {status}")

    # Collect all pages using NextToken
    blocks = resp["Blocks"]
    next_token = resp.get("NextToken")
    while next_token:
        resp = textract.get_document_text_detection(
            JobId=job_id, MaxResults=1000, NextToken=next_token
        )
        blocks.extend(resp["Blocks"])
        next_token = resp.get("NextToken")

    page_text_dict = {}
    page_blocks_dict = {}

    for b in blocks:
        if b.get("BlockType") == "LINE" and "Text" in b:
            page_num = b.get("Page", 1)
            text = b["Text"]

            page_text_dict.setdefault(page_num, []).append(text)

            bbox = b.get("Geometry", {}).get("BoundingBox", {})
            if bbox:
                page_blocks_dict.setdefault(page_num, []).append({
                    "text": text,
                    "bbox": bbox
                })

    if not any(page_text_dict.values()):
        return jsonify({"error": "No extractable text found in the document."}), 400
    else:
        sample_preview = []
        for lines in page_text_dict.values():
            sample_preview.extend(lines)
            if len(sample_preview) >= 5:
                break
        print("[DEBUG] Sample extracted lines:", sample_preview[:5])
        return {
            "text": page_text_dict,
            "blocks": page_blocks_dict
        }
    



