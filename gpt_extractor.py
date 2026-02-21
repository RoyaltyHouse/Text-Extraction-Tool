from dotenv import load_dotenv
from prompt import build_extraction_prompt, ARRAY_FIELDS
import os
import re
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
    """Merge multiple bounding boxes into one encompassing rectangle.
    Kept for label-proximity distance calculations only."""
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


# Fraction of page height used to decide whether two words are on the same line.
_LINE_BUCKET_SIZE = 0.008


def _group_bboxes_by_line(bboxes):
    """Group bounding boxes into per-line merged rects.

    Words whose vertical centre falls within _LINE_BUCKET_SIZE of each other
    are considered to be on the same line. Each group is merged into one bbox
    that spans only the words on that line, avoiding the wide empty rectangle
    that a single merged bbox produces for multi-line text.

    Returns a list of merged per-line bboxes, in top-to-bottom order.
    """
    if not bboxes:
        return []

    # Sort by vertical centre
    def _vcenter(b):
        return b["Top"] + b["Height"] / 2

    sorted_boxes = sorted(bboxes, key=_vcenter)

    lines = []  # list of lists of bboxes
    for bbox in sorted_boxes:
        vc = _vcenter(bbox)
        # Try to add to an existing line bucket
        placed = False
        for line in lines:
            line_vc = _vcenter(line[0])
            if abs(vc - line_vc) <= _LINE_BUCKET_SIZE:
                line.append(bbox)
                placed = True
                break
        if not placed:
            lines.append([bbox])

    return [_merge_bboxes(line) for line in lines]


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

# Characters that Textract and GPT represent differently.
# Keys are unicode variants; values are the canonical ASCII replacement.
_CHAR_NORMALIZATIONS = str.maketrans({
    "\u2019": "'",   # right single quotation mark  →  straight apostrophe
    "\u2018": "'",   # left single quotation mark   →  straight apostrophe
    "\u2032": "'",   # prime                        →  straight apostrophe
    "\u201c": '"',   # left double quotation mark   →  straight double quote
    "\u201d": '"',   # right double quotation mark  →  straight double quote
    "\u2014": "--",  # em dash                      →  double hyphen
    "\u2013": "--",  # en dash                      →  double hyphen
    "\u2012": "--",  # figure dash                  →  double hyphen
    "\u00b7": ".",   # middle dot                   →  period
    "\u2022": "-",   # bullet                       →  hyphen
    "\u00a0": " ",   # non-breaking space           →  regular space
})


def _normalize(text: str) -> str:
    """Normalise Unicode punctuation variants so GPT text and Textract tokens
    compare equal regardless of which encoding the source PDF used."""
    return text.translate(_CHAR_NORMALIZATIONS)


# Matches any character that is not a word character or whitespace.
# Pre-compiled for performance since it's called once per block per field.
_PUNCT_RE = re.compile(r'[^\w\s]')


def _clean_for_compare(text: str) -> str:
    """Normalise Unicode and strip all punctuation for robust token comparison.

    GPT output often attaches punctuation to adjacent words (e.g. "NAR;" or
    "3.0%") while Textract emits them as separate tokens ("NAR", ";") or with
    different tokenisation ("3", ".", "0", "%").  Removing all punctuation
    before comparison collapses both representations to the same word sequence
    so anchor matching and span calculations work regardless of how the source
    PDF was tokenised.
    """
    normalized = _normalize(text)
    cleaned = _PUNCT_RE.sub(' ', normalized)
    return ' '.join(cleaned.lower().split())


def _find_anchor(anchor_text, page_blocks):
    """Find all starting block indices where a word sequence appears in page_blocks.

    All comparison is done after _clean_for_compare so that punctuation
    differences between GPT output (e.g. "NAR;" or "3.0%") and Textract tokens
    (["NAR", ";"] or ["3", "0"]) don't prevent matches.

    Punctuation-only blocks (e.g. a lone ";" or ".") contribute zero clean words
    and are automatically skipped when accumulating the comparison window.

    Returns a list of (start_index, score) tuples sorted best-first.
    """
    anchor_clean = _clean_for_compare(anchor_text)
    anchor_words = anchor_clean.split()
    anchor_word_count = len(anchor_words)
    if not anchor_word_count:
        return []

    num_blocks = len(page_blocks)
    hits = []

    for i in range(num_blocks):
        words_collected = []
        j = i
        # Accumulate clean words from consecutive blocks until we have enough.
        # A single block may contribute 0 (pure-punct) or 2+ (hyphenated) words.
        while len(words_collected) < anchor_word_count and j < num_blocks:
            raw = page_blocks[j].get("text", "")
            clean = _clean_for_compare(raw)
            if clean:
                words_collected.extend(clean.split())
            j += 1

        if len(words_collected) < anchor_word_count:
            break  # not enough content left for any remaining position

        combined = " ".join(words_collected[:anchor_word_count])

        if anchor_clean == combined:
            hits.append((i, 1.0))
        elif anchor_clean in combined:
            hits.append((i, len(anchor_clean) / len(combined)))
        elif combined in anchor_clean and len(combined) > 3:
            hits.append((i, len(combined) / len(anchor_clean) * 0.8))

    hits.sort(key=lambda h: -h[1])
    return hits


def _blocks_for_clean_words(page_blocks, start_idx, num_clean_words):
    """Count how many blocks from start_idx are needed to accumulate num_clean_words
    clean words (after _clean_for_compare).

    Punctuation-only blocks contribute 0 clean words but still advance the
    block index.  A hyphenated token like "J-Kwest" contributes 2 clean words
    from a single block.

    Returns (block_count, words_accumulated):
      - block_count: number of blocks consumed  (end_pos = start_idx + block_count)
      - words_accumulated: actual clean words collected (may exceed num_clean_words
        if the last block contributed multiple words)
    """
    words_so_far = 0
    j = start_idx
    num_blocks = len(page_blocks)
    while words_so_far < num_clean_words and j < num_blocks:
        raw = page_blocks[j].get("text", "")
        clean = _clean_for_compare(raw)
        words_so_far += len(clean.split()) if clean else 0
        j += 1
    return j - start_idx, words_so_far


def find_evidence_location(evidence_text, page_blocks, label_bbox=None):
    """Find per-line bounding boxes for the given evidence text in WORD blocks.

    Expects page_blocks to be pre-sorted in reading order.
    - Short values (<=8 clean words): sequence matching with label proximity.
    - Long values (>8 clean words): anchor on first/last N clean words and span.

    All text comparison uses _clean_for_compare which strips ALL punctuation so
    that GPT tokens like "NAR;" match Textract's separate ["NAR", ";"] blocks,
    and tokens like "3.0%" match ["3", "0"] without a period or percent sign.

    Span calculations use _blocks_for_clean_words so that blocks containing
    multi-word tokens (e.g. "J-Kwest" → 2 clean words) or zero-word tokens
    (e.g. ";") are counted correctly rather than assuming 1 word = 1 block.

    Returns a list of per-line merged bboxes so the viewer can render one
    highlight rectangle per text line.  Returns None when not located.
    """
    if not evidence_text or not page_blocks:
        return None

    evidence_clean = _clean_for_compare(evidence_text)
    evidence_clean_words = evidence_clean.split()
    num_clean_words = len(evidence_clean_words)
    if not num_clean_words:
        return None

    evidence_len = len(evidence_clean)
    num_blocks = len(page_blocks)

    def _collect_lines(start, length):
        bboxes = [page_blocks[start + j]["bbox"]
                  for j in range(length) if page_blocks[start + j].get("bbox")]
        return _group_bboxes_by_line(bboxes) or None

    # ── Long values: anchor approach ──
    if num_clean_words > LONG_VALUE_THRESHOLD:
        anchor_size = min(5, num_clean_words // 2)
        start_text = " ".join(evidence_clean_words[:anchor_size])
        end_text   = " ".join(evidence_clean_words[-anchor_size:])

        start_hits = _find_anchor(start_text, page_blocks)
        end_hits   = _find_anchor(end_text,   page_blocks)

        if start_hits and end_hits:
            for start_idx, _ in start_hits:
                for end_idx, _ in end_hits:
                    # Count blocks for the end-anchor span so multi-word tokens
                    # (e.g. "J-Kwest") and punct-only tokens (";") are handled.
                    end_block_count, _ = _blocks_for_clean_words(page_blocks, end_idx, anchor_size)
                    end_pos = end_idx + end_block_count
                    span = end_pos - start_idx
                    if end_idx >= start_idx and span <= num_clean_words * 3:
                        result = _collect_lines(start_idx, min(end_pos, num_blocks) - start_idx)
                        if result:
                            return result

        if start_hits:
            start_idx = start_hits[0][0]
            # Count blocks forward until we've covered all evidence clean words.
            span_block_count, _ = _blocks_for_clean_words(page_blocks, start_idx, num_clean_words)
            span = min(span_block_count, num_blocks - start_idx)
            result = _collect_lines(start_idx, span)
            if result:
                return result

        # Last resort: fall through to short-value matching below

    # ── Short values: sequence matching ──
    max_words = num_clean_words + 3
    matches = []  # (start_index, length, score)

    for i in range(num_blocks):
        combined = ""

        for length in range(1, min(max_words + 1, num_blocks - i + 1)):
            raw_word = page_blocks[i + length - 1].get("text", "")
            clean_word = _clean_for_compare(raw_word)
            if clean_word:
                combined = f"{combined} {clean_word}" if combined else clean_word
            combined_lower = combined.strip()

            score = 0
            if evidence_clean == combined_lower:
                score = 1.0
            elif evidence_clean in combined_lower:
                score = evidence_len / len(combined_lower) if combined_lower else 0
            elif combined_lower in evidence_clean and len(combined_lower) > 3:
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

    def _merged_bbox_for_proximity(start, length):
        """Single merged bbox used only for label-proximity distance calc."""
        bboxes = [page_blocks[start + j]["bbox"]
                  for j in range(length) if page_blocks[start + j].get("bbox")]
        return _merge_bboxes(bboxes)

    if len(top_matches) == 1 or not label_bbox:
        best = min(top_matches, key=lambda m: (-m[2], m[1]))
        return _collect_lines(best[0], best[1])

    closest = min(top_matches, key=lambda m: _bbox_distance(
        _merged_bbox_for_proximity(m[0], m[1]) or {}, label_bbox
    ))
    return _collect_lines(closest[0], closest[1])

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
            line_bboxes = find_evidence_location(
                field_data["value"], sorted_blocks, label_bbox=label_map.get(field_name)
            )
            # Store as a list of per-line bboxes (or None when not found).
            # A single-line result is still a list of length 1.
            field_data["coords"] = line_bboxes


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


def extract_field_information(page_text, page_blocks=None):
    """Single-pass extraction: identify producers + songs and extract all fields.

    Args:
        page_text:   dict of {page_num: [lines]} from Textract
        page_blocks: dict of {page_num: [{text, bbox}]} WORD blocks for coords

    Returns a dict with universal fields at the top level, a "producers" array
    with per-producer field extractions, and a "songs" array with per-song field
    extractions — each entry enriched with bounding box coords where found.
    """
    prompt = build_extraction_prompt(page_text)
    print("[DEBUG] Sending extraction prompt to OpenAI (single-pass)...")

    response = client.chat.completions.create(
        model="gpt-4-1106-preview",
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    content = _strip_markdown_fences(response.choices[0].message.content)
    print(f"[DEBUG] Received extraction response from OpenAI: {content[:300]}...")

    try:
        extracted_fields = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse GPT response as JSON: {e}")
        print(f"[ERROR] Raw content: {content}")
        return {"error": f"Failed to parse extraction results: {e}"}

    if page_blocks:
        try:
            # Apply coords to universal top-level fields (skip nested arrays)
            _apply_coords_to_fields(extracted_fields, page_blocks, skip_keys={"producers", "songs"})

            # Apply coords to each producer's individual fields
            for producer in extracted_fields.get("producers", []):
                _apply_coords_to_fields(producer, page_blocks, skip_keys={"producer_name"})

            # Apply coords to each song's individual fields
            for song in extracted_fields.get("songs", []):
                _apply_coords_to_fields(song, page_blocks, skip_keys={"song_title", "is_rate_explicit"})

        except Exception as e:
            print(f"[WARNING] Failed to add coordinates: {e}")

    return extracted_fields
