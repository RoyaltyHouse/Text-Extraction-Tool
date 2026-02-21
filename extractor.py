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


def extract_text_from_pdf(line_index):
    if not line_index:
        return {"error": "No text provided for extraction"}
    print("[DEBUG] Extraction started")

    result = extract_field_information(line_index)

    if isinstance(result, dict):
        return result
    return json.loads(result)


def textract_text_image_by_image(file):
    extraction_result = textract_lines_by_page_from_file(file, bucket=S3_BUCKET)
    if isinstance(extraction_result, tuple) or not isinstance(extraction_result, dict):
        return extraction_result

    line_index = extraction_result.get("line_index", {})
    if not line_index:
        return []
    return [entry["text"] for entry in line_index.values()]


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

    # Build ID -> block lookup for Relationship resolution
    block_by_id = {b["Id"]: b for b in blocks if "Id" in b}

    # Collect LINE blocks grouped by page, preserving Textract reading order
    lines_by_page = {}
    for b in blocks:
        if b.get("BlockType") == "LINE" and "Text" in b:
            page_num = b.get("Page", 1)
            lines_by_page.setdefault(page_num, []).append(b)

    # Build line_index with global numbering and child WORD bboxes
    line_index = {}
    global_line_num = 1
    for page_num in sorted(lines_by_page.keys()):
        for line_block in lines_by_page[page_num]:
            # Resolve child WORD blocks via Relationships
            child_ids = []
            for rel in line_block.get("Relationships", []):
                if rel.get("Type") == "CHILD":
                    child_ids.extend(rel.get("Ids", []))

            words = []
            for cid in child_ids:
                wb = block_by_id.get(cid)
                if wb and wb.get("BlockType") == "WORD" and "Text" in wb:
                    bbox = wb.get("Geometry", {}).get("BoundingBox", {})
                    if bbox:
                        words.append({"text": wb["Text"], "bbox": bbox})

            line_index[global_line_num] = {
                "page": page_num,
                "text": line_block["Text"],
                "words": words,
            }
            global_line_num += 1

    if not line_index:
        return jsonify({"error": "No extractable text found in the document."}), 400

    sample = [line_index[i]["text"] for i in sorted(line_index)[:5]]
    print("[DEBUG] Sample extracted lines:", sample)
    return {"line_index": line_index}
