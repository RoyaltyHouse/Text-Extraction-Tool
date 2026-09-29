import os
import time
import uuid

import boto3

S3_BUCKET = os.getenv("BUCKET_NAME")
s3 = boto3.client("s3")
textract = boto3.client("textract")


class TextractError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def upload(filename, data):
    key = f"uploads/{uuid.uuid4()}/{filename}"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data)
    return key


def _related(block, block_by_id, rel_type="CHILD"):
    return [block_by_id[i] for rel in block.get("Relationships", []) if rel["Type"] == rel_type
            for i in rel["Ids"] if i in block_by_id]


def _kv_text(block, block_by_id):
    return " ".join(
        c["Text"] if c["BlockType"] == "WORD" else f"[{c.get('SelectionStatus', 'UNSELECTED')}]"
        for c in _related(block, block_by_id) if c["BlockType"] in ("WORD", "SELECTION_ELEMENT")
    ).strip()


def textract_lines(key):
    job_id = textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": S3_BUCKET, "Name": key}}, FeatureTypes=["FORMS", "SIGNATURES"],
    )["JobId"]
    start = time.time()
    while (resp := textract.get_document_analysis(JobId=job_id, MaxResults=1000))["JobStatus"] == "IN_PROGRESS":
        if time.time() - start > 115:
            raise TextractError("Textract did not finish reading the document within 115 s.", 504)
        time.sleep(2)
    if resp["JobStatus"] != "SUCCEEDED":
        raise TextractError(f"Textract job ended with status: {resp['JobStatus']}", 500)

    blocks = resp["Blocks"]
    while resp.get("NextToken"):
        resp = textract.get_document_analysis(JobId=job_id, MaxResults=1000, NextToken=resp["NextToken"])
        blocks += resp["Blocks"]
    block_by_id = {b["Id"]: b for b in blocks}

    # Stable sort keeps Textract's reading order within each page.
    lines = sorted((b for b in blocks if b["BlockType"] == "LINE"), key=lambda b: b.get("Page", 1))
    if not lines:
        raise TextractError("No extractable text found in the document.", 400)
    line_index, line_centers = {}, {}
    for n, line in enumerate(lines, 1):
        page, bbox = line.get("Page", 1), line["Geometry"]["BoundingBox"]
        words = [{"text": w["Text"], "bbox": w["Geometry"]["BoundingBox"]}
                 for w in _related(line, block_by_id) if w["BlockType"] == "WORD"]
        line_index[n] = {"page": page, "text": line["Text"], "words": words}
        line_centers.setdefault(page, []).append((bbox["Top"] + bbox["Height"] / 2, n))

    def annotation(block, **fields):
        page, bbox = block.get("Page", 1), block["Geometry"]["BoundingBox"]
        center = bbox["Top"] + bbox["Height"] / 2
        near_line = min(line_centers.get(page, []), key=lambda c: abs(c[0] - center), default=(None, None))[1]
        return {"page": page, **fields, "near_line": near_line, "left": round(bbox["Left"], 3), "top": round(bbox["Top"], 3)}

    annotations = []
    for b in blocks:
        if "Geometry" not in b:
            continue
        if b["BlockType"] == "SIGNATURE":
            annotations.append(annotation(b, type="signature"))
        elif b["BlockType"] == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []) and (key := _kv_text(b, block_by_id)):
            value = _related(b, block_by_id, "VALUE")
            annotations.append(annotation(b, type="form_field", key=key, value=_kv_text(value[0], block_by_id) if value else ""))
    annotations.sort(key=lambda a: (a["page"], a["near_line"] or 0, a["left"]))
    return line_index, annotations
