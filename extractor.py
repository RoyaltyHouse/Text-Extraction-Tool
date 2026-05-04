# extractor.py
from pypdf import PdfReader
from gpt_extractor import extract_field_information
import json , os, tempfile, time
import boto3
import time


# S3 configuration
S3_BUCKET = os.getenv("BUCKET_NAME")  # Replace with your actual bucket name
s3 = boto3.client('s3')
textract = boto3.client('textract')


def extract_text_from_pdf(line_index, annotations=None):
    if not line_index:
        return {"error": "No text provided for extraction"}
    print("[DEBUG] Extraction started")

    result = extract_field_information(line_index, annotations or [])

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



def _block_bbox(block):
    return (block or {}).get("Geometry", {}).get("BoundingBox")


def _resolve_kv_text(block, block_by_id):
    """Concatenate the text of a KEY or VALUE block by walking CHILD relationships."""
    if not block:
        return ""
    parts = []
    for rel in block.get("Relationships", []):
        if rel.get("Type") != "CHILD":
            continue
        for cid in rel.get("Ids", []):
            child = block_by_id.get(cid)
            if not child:
                continue
            if child.get("BlockType") == "WORD" and "Text" in child:
                parts.append(child["Text"])
            elif child.get("BlockType") == "SELECTION_ELEMENT":
                parts.append(f"[{child.get('SelectionStatus', 'UNSELECTED')}]")
    return " ".join(parts).strip()


def _nearest_global_line(page, bbox, lines_by_page_with_global):
    """Return the global line number whose LINE block sits closest vertically to bbox."""
    candidates = lines_by_page_with_global.get(page, [])
    if not candidates or not bbox:
        return None
    cy = bbox["Top"] + bbox["Height"] / 2

    def vertical_distance(item):
        line_block, _ = item
        lb_bbox = _block_bbox(line_block) or {}
        line_cy = lb_bbox.get("Top", 0) + lb_bbox.get("Height", 0) / 2
        return abs(line_cy - cy)

    _, nearest_global = min(candidates, key=vertical_distance)
    return nearest_global


def textract_lines_by_page_from_file(file, bucket=S3_BUCKET):

    # Save the file to a temporary local path
    local_path = os.path.join(tempfile.gettempdir(), file.filename)
    file.save(local_path)
    print(f"[DEBUG] Saved file locally at: {local_path}")

    # Upload to S3
    key = file.filename
    s3.upload_file(local_path, bucket, key)
    print(f"[DEBUG] Upload successful: s3://{bucket}/{key}")

    # Start async document analysis with FORMS + SIGNATURES so we get
    # KEY_VALUE_SET pairs (e.g. "Name:" → "John Smith") and SIGNATURE blocks
    # (wet/digital signatures Textract detects independent of OCR text).
    job = textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
        FeatureTypes=["FORMS", "SIGNATURES"],
    )
    job_id = job["JobId"]
    print(f"[DEBUG] Started Textract job with JobId: {job_id}")

    # Poll until done or timeout
    start_time = time.time()
    while True:
        if time.time() - start_time > 115:
            return {"error": "Text extraction failed due to poor image quality, formatting, or an unreadable document."}, 200

        resp = textract.get_document_analysis(JobId=job_id, MaxResults=1000)
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
        resp = textract.get_document_analysis(
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

    # Build line_index with global numbering and child WORD bboxes.
    # Also keep a (line_block, global_num) list per page for nearest-line lookup.
    line_index = {}
    lines_by_page_with_global = {}
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
            lines_by_page_with_global.setdefault(page_num, []).append(
                (line_block, global_line_num)
            )
            global_line_num += 1

    if not line_index:
        return {"error": "No extractable text found in the document."}, 400

    # Parse FORMS + SIGNATURES into structural annotations so the prompt can
    # use them as ground truth for signed/unsigned determination.
    annotations = []

    for b in blocks:
        if b.get("BlockType") != "SIGNATURE":
            continue
        page = b.get("Page", 1)
        bbox = _block_bbox(b)
        if not bbox:
            continue
        annotations.append({
            "type": "signature",
            "page": page,
            "confidence": round(b.get("Confidence", 0), 1),
            "near_line": _nearest_global_line(page, bbox, lines_by_page_with_global),
            "left": round(bbox["Left"], 3),
        })

    value_block_by_id = {
        b["Id"]: b
        for b in blocks
        if b.get("BlockType") == "KEY_VALUE_SET" and "VALUE" in b.get("EntityTypes", [])
    }

    for kb in blocks:
        if kb.get("BlockType") != "KEY_VALUE_SET" or "KEY" not in kb.get("EntityTypes", []):
            continue
        key_text = _resolve_kv_text(kb, block_by_id)
        if not key_text:
            continue

        value_block = None
        for rel in kb.get("Relationships", []):
            if rel.get("Type") == "VALUE":
                ids = rel.get("Ids", [])
                if ids:
                    value_block = value_block_by_id.get(ids[0])
                    break
        value_text = _resolve_kv_text(value_block, block_by_id) if value_block else ""

        page = kb.get("Page", 1)
        kb_bbox = _block_bbox(kb)
        annotations.append({
            "type": "form_field",
            "page": page,
            "key": key_text,
            "value": value_text,
            "near_line": _nearest_global_line(page, kb_bbox, lines_by_page_with_global),
            "left": round(kb_bbox["Left"], 3) if kb_bbox else None,
        })

    annotations.sort(key=lambda a: (a["page"], a.get("near_line") or 0))

    sample = [line_index[i]["text"] for i in sorted(line_index)[:5]]
    print("[DEBUG] Sample extracted lines:", sample)
    print(f"[DEBUG] Detected {sum(1 for a in annotations if a['type'] == 'signature')} signatures, "
          f"{sum(1 for a in annotations if a['type'] == 'form_field')} form fields")
    return {"line_index": line_index, "annotations": annotations}
