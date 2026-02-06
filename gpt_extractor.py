from dotenv import load_dotenv
from prompt import build_final_document_prompt
import os
import json
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY2"))

def _bbox_distance(bbox1, bbox2):
    """Euclidean distance between the centres of two bounding boxes."""
    cx1 = bbox1.get("Left", 0) + bbox1.get("Width", 0) / 2
    cy1 = bbox1.get("Top", 0) + bbox1.get("Height", 0) / 2
    cx2 = bbox2.get("Left", 0) + bbox2.get("Width", 0) / 2
    cy2 = bbox2.get("Top", 0) + bbox2.get("Height", 0) / 2
    return ((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) ** 0.5


def _build_label_bbox_map(field_names, page_blocks):
    """Single scan over page_blocks to locate every field label's bounding box."""
    field_lowers = {name: name.lower().strip() for name in field_names}
    best_scores = {}
    label_bboxes = {}

    for block in page_blocks:
        block_text = block.get("text", "").lower().strip()
        if not block_text:
            continue
        for name, field_lower in field_lowers.items():
            if field_lower in block_text:
                score = len(field_lower) / len(block_text)
                if score > best_scores.get(name, 0):
                    best_scores[name] = score
                    label_bboxes[name] = block.get("bbox")

    return label_bboxes


def find_evidence_location(evidence_text, page_blocks, label_bbox=None):
    """Find the bounding box for the given evidence text in the page blocks.

    When label_bbox is provided and the value appears more than once on the
    page, uses proximity to the label to pick the correct occurrence.
    """
    if not evidence_text or not page_blocks:
        return None

    evidence_lower = evidence_text.lower().strip()
    matches = []

    for block in page_blocks:
        block_text = block.get("text", "").lower().strip()
        if not block_text:
            continue

        score = 0
        # Check for exact substring match
        if evidence_lower in block_text:
            score = len(evidence_lower) / len(block_text)
        # Check for reverse match (block is substring of evidence)
        elif block_text in evidence_lower and len(block_text) > 10:
            score = len(block_text) / len(evidence_lower)

        if score > 0 and block.get("bbox"):
            matches.append((block, score))

    if not matches:
        return None

    if len(matches) == 1:
        return matches[0][0]["bbox"]

    # Multiple matches — try to disambiguate using the label position
    if not label_bbox:
        return max(matches, key=lambda m: m[1])[0]["bbox"]

    # Among competitively-scored matches, pick the one closest to the label
    best_score = max(m[1] for m in matches)
    top_matches = [(b, s) for b, s in matches if s >= best_score * 0.8]
    closest = min(top_matches, key=lambda m: _bbox_distance(m[0]["bbox"], label_bbox))
    return closest[0]["bbox"]

def extract_field_information(page_text, page_blocks=None):
    prompt = build_final_document_prompt(page_text)
    print(f"[DEBUG] Sending prompt to OpenAI: {prompt[:100]}...")

    response = client.chat.completions.create(
        model="gpt-4-1106-preview",
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": prompt.strip()}
        ],
        temperature=0.2
    )

    content = response.choices[0].message.content
    print(f"[DEBUG] Received response from OpenAI: {content.strip()}")

    if content.strip().startswith('```'):
        lines = content.strip().splitlines()
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].startswith('```'):
            lines = lines[:-1]
        content = '\n'.join(lines)

    try:
        extracted_fields = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse GPT response as JSON: {str(e)}")
        print(f"[ERROR] Raw content: {content}")
        return {"error": f"Failed to parse extraction results: {str(e)}"}

    if page_blocks:
        try:
            # Group fields by page so we build one label map per page
            fields_by_page = {}
            for field_name, field_data in extracted_fields.items():
                if isinstance(field_data, dict) and field_data.get("value") and field_data.get("value") != "not found":
                    page_num = field_data.get("page_number")
                    if page_num is not None and isinstance(page_num, int):
                        fields_by_page.setdefault(page_num, []).append((field_name, field_data))

            for page_num, fields in fields_by_page.items():
                blocks_on_page = page_blocks.get(page_num, [])
                # Single scan to find all label positions for this page
                label_map = _build_label_bbox_map([name for name, _ in fields], blocks_on_page)

                for field_name, field_data in fields:
                    value = field_data.get("value")
                    if not isinstance(value, str):
                        continue
                    bbox = find_evidence_location(value, blocks_on_page, label_bbox=label_map.get(field_name))
                    field_data["coords"] = bbox
        except Exception as e:
            print(f"[WARNING] Failed to add coordinates: {str(e)}")

    return extracted_fields
