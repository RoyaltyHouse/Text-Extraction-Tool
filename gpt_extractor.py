from dotenv import load_dotenv
from prompt import build_detection_prompt, build_final_document_prompt, ARRAY_FIELDS
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


def _merge_bboxes(bboxes):
    """Merge multiple bounding boxes into one encompassing rectangle."""
    if not bboxes:
        return None
    if len(bboxes) == 1:
        return bboxes[0]

    left = min(b["Left"] for b in bboxes)
    top = min(b["Top"] for b in bboxes)
    right = max(b["Left"] + b["Width"] for b in bboxes)
    bottom = max(b["Top"] + b["Height"] for b in bboxes)

    return {
        "Left": left,
        "Top": top,
        "Width": right - left,
        "Height": bottom - top
    }


def _build_label_bbox_map(field_names, page_blocks):
    """Single scan over page_blocks to locate every field label's bounding box.

    Works with WORD blocks by checking if any significant word from the field
    name appears in the document.
    """
    field_lowers = {name: name.lower().strip() for name in field_names}
    best_scores = {}
    label_bboxes = {}

    for block in page_blocks:
        block_text = block.get("text", "").lower().strip()
        if not block_text or len(block_text) < 2:
            continue

        for name, field_lower in field_lowers.items():
            # Check for full field name in word
            if field_lower in block_text:
                score = len(field_lower) / len(block_text)
                if score > best_scores.get(name, 0):
                    best_scores[name] = score
                    label_bboxes[name] = block.get("bbox")
            # Check if word is significant part of field name (e.g., "Artist" in "Artist Name")
            elif block_text in field_lower and len(block_text) >= 4:
                score = len(block_text) / len(field_lower) * 0.8
                if score > best_scores.get(name, 0):
                    best_scores[name] = score
                    label_bboxes[name] = block.get("bbox")

    return label_bboxes


LONG_VALUE_THRESHOLD = 8  # words — above this, use anchor matching


def _find_anchor(anchor_text, page_blocks):
    """Find all starting indices where a short word sequence appears in page_blocks.

    Returns list of start indices sorted by match quality.
    """
    anchor_lower = anchor_text.lower().strip()
    anchor_word_count = len(anchor_lower.split())
    num_blocks = len(page_blocks)
    hits = []

    for i in range(num_blocks - anchor_word_count + 1):
        combined = ""
        for j in range(anchor_word_count):
            word = page_blocks[i + j].get("text", "")
            combined = f"{combined} {word}" if combined else word
        combined_lower = combined.lower().strip()

        if anchor_lower == combined_lower:
            hits.append((i, 1.0))
        elif anchor_lower in combined_lower:
            hits.append((i, len(anchor_lower) / len(combined_lower)))

    # Best scores first
    hits.sort(key=lambda h: -h[1])
    return hits


def find_evidence_location(evidence_text, page_blocks, label_bbox=None):
    """Find the bounding box for the given evidence text in WORD blocks.

    Expects page_blocks to be pre-sorted in reading order.
    - Short values (<=8 words): sequence matching with label proximity.
    - Long values (>8 words): anchor on first/last few words and span between.
    """
    if not evidence_text or not page_blocks:
        return None

    evidence_lower = evidence_text.lower().strip()
    evidence_words = evidence_lower.split()
    evidence_len = len(evidence_lower)
    num_blocks = len(page_blocks)

    # ── Long values: anchor approach ──
    if len(evidence_words) > LONG_VALUE_THRESHOLD:
        anchor_size = min(5, len(evidence_words) // 2)
        start_text = " ".join(evidence_words[:anchor_size])
        end_text = " ".join(evidence_words[-anchor_size:])

        start_hits = _find_anchor(start_text, page_blocks)
        end_hits = _find_anchor(end_text, page_blocks)

        if start_hits and end_hits:
            # Try each start/end combo to find a valid span
            for start_idx, _ in start_hits:
                for end_idx, _ in end_hits:
                    end_pos = end_idx + anchor_size
                    if end_idx >= start_idx and end_pos - start_idx <= len(evidence_words) * 2:
                        bboxes = [page_blocks[j]["bbox"]
                                  for j in range(start_idx, min(end_pos, num_blocks))
                                  if page_blocks[j].get("bbox")]
                        if bboxes:
                            return _merge_bboxes(bboxes)

        # Fallback: start anchor found, estimate span from evidence word count
        if start_hits:
            start_idx = start_hits[0][0]
            span = min(len(evidence_words), num_blocks - start_idx)
            bboxes = [page_blocks[start_idx + j]["bbox"]
                      for j in range(span)
                      if page_blocks[start_idx + j].get("bbox")]
            if bboxes:
                return _merge_bboxes(bboxes)

        # Last resort: fall through to short-value matching below

    # ── Short values: sequence matching ──
    max_words = len(evidence_words) + 3
    matches = []  # (start_index, length, score)

    for i in range(num_blocks):
        combined = ""

        for length in range(1, min(max_words + 1, num_blocks - i + 1)):
            word = page_blocks[i + length - 1].get("text", "")
            combined = f"{combined} {word}" if combined else word
            combined_lower = combined.lower().strip()

            score = 0
            if evidence_lower == combined_lower:
                score = 1.0
            elif evidence_lower in combined_lower:
                score = evidence_len / len(combined_lower)
            elif combined_lower in evidence_lower and len(combined_lower) > 3:
                score = len(combined_lower) / evidence_len * 0.7

            if score > 0.3:
                matches.append((i, length, score))

            if score == 1.0:
                break
            if len(combined_lower) > evidence_len * 1.5 and score == 0:
                break

    if not matches:
        return None

    best_score = max(m[2] for m in matches)
    top_matches = [m for m in matches if m[2] >= best_score * 0.95]
    best_length = min(m[1] for m in top_matches)
    top_matches = [m for m in top_matches if m[1] <= best_length + 2]

    def _extract_bbox(start, length):
        bboxes = [page_blocks[start + j]["bbox"]
                   for j in range(length) if page_blocks[start + j].get("bbox")]
        return _merge_bboxes(bboxes)

    if len(top_matches) == 1 or not label_bbox:
        best = min(top_matches, key=lambda m: (-m[2], m[1]))
        return _extract_bbox(best[0], best[1])

    closest = min(top_matches, key=lambda m: _bbox_distance(
        _extract_bbox(m[0], m[1]) or {}, label_bbox
    ))
    return _extract_bbox(closest[0], closest[1])

# ── Coordinate application ────────────────────────────────────────────────────

def _apply_coords_to_fields(fields_dict, page_blocks, skip_keys=None):
    """Apply bounding box coordinates to a flat dict of {field_name: field_data}.

    Handles two value shapes:
    - Scalar: {"value": str, "page_number": int}  — standard fields
    - Array:  [{"value": str, "page_number": int}, ...]  — ARRAY_FIELDS
              Each entry gets its own coords so every occurrence is highlightable.

    Skips non-string values (e.g. Lawyer Information's nested dict) and any
    keys listed in skip_keys (e.g. "producers", "producer_name").

    Groups work by page so the label-map scan runs once per page.
    """
    skip_keys = skip_keys or set()

    # Collect all (field_name, entry_dict) pairs grouped by page number.
    # Array fields contribute one pair per entry; scalar fields contribute one.
    fields_by_page = {}
    for field_name, field_data in fields_dict.items():
        if field_name in skip_keys:
            continue

        if field_name in ARRAY_FIELDS and isinstance(field_data, list):
            # Array-valued field — each entry is matched independently
            for entry in field_data:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("value")
                if not value or value == "not found" or not isinstance(value, str):
                    continue
                page_num = entry.get("page_number")
                if page_num is not None and isinstance(page_num, int):
                    fields_by_page.setdefault(page_num, []).append((field_name, entry))
            continue

        # Scalar field — standard {value, page_number} dict
        if not isinstance(field_data, dict):
            continue
        value = field_data.get("value")
        if not value or value == "not found":
            continue
        if not isinstance(value, str):
            # Non-string values (e.g. Lawyer Information's nested dict) cannot
            # be located in word blocks — skip coords entirely for those fields.
            continue
        page_num = field_data.get("page_number")
        if page_num is not None and isinstance(page_num, int):
            fields_by_page.setdefault(page_num, []).append((field_name, field_data))

    for page_num, fields in fields_by_page.items():
        blocks_on_page = page_blocks.get(page_num, [])
        # Sort into reading order: row (bucketed by 1% of page height) then left
        sorted_blocks = sorted(blocks_on_page, key=lambda b: (
            round(b.get("bbox", {}).get("Top", 0) * 100),
            b.get("bbox", {}).get("Left", 0)
        ))
        # Single scan to find all label positions for this page
        label_map = _build_label_bbox_map([name for name, _ in fields], sorted_blocks)

        for field_name, field_data in fields:
            bbox = find_evidence_location(
                field_data["value"], sorted_blocks, label_bbox=label_map.get(field_name)
            )
            field_data["coords"] = bbox


# ── GPT calls ─────────────────────────────────────────────────────────────────

def _strip_markdown_fences(content):
    """Remove ```json / ``` fences that GPT occasionally wraps responses in."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines)
    return content


def detect_producers(page_text):
    """Pass 1: classify how many producers are party to this agreement.

    Returns a list of producer name strings. Always returns at least one entry.
    Falls back to ["Producer"] if detection fails or GPT returns unexpected output.
    """
    prompt = build_detection_prompt(page_text)
    print("[DEBUG] Running producer detection pass...")

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
                # Sort once per page for reading order — bucket by row first
                # so words on the same line stay consecutive despite tiny
                # Top-value differences from Textract
                sorted_blocks = sorted(blocks_on_page, key=lambda b: (
                    round(b.get("bbox", {}).get("Top", 0) * 100),
                    b.get("bbox", {}).get("Left", 0)
                ))
                # Single scan to find all label positions for this page
                label_map = _build_label_bbox_map([name for name, _ in fields], sorted_blocks)

                for field_name, field_data in fields:
                    value = field_data.get("value")
                    if not isinstance(value, str):
                        continue
                    bbox = find_evidence_location(value, sorted_blocks, label_bbox=label_map.get(field_name))
                    field_data["coords"] = bbox
        except Exception as e:
            print(f"[WARNING] Failed to add coordinates: {str(e)}")

    return extracted_fields
