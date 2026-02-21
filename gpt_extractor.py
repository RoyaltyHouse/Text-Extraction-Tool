from collections import OrderedDict
from dotenv import load_dotenv
from prompt import build_extraction_prompt, ARRAY_FIELDS
import os
import json
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY2"))

# Characters that Textract and GPT represent differently.
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


# ── Line-based coordinate resolution ─────────────────────────────────────────

def _resolve_coords(value, line_nums, line_index):
    """Look up word-level bboxes for a value on the given line numbers.

    Collects all words from the candidate lines into a flat list, then finds
    the contiguous word span that best matches the value text. This handles:
    - Sub-line precision (value is part of a line → only those words highlighted)
    - Multi-line values (span crosses line boundaries → words from each line)
    - Extra lines GPT included (e.g. a header above the value → those words
      won't be part of the best match and get dropped)

    Returns a list of per-line merged bboxes, or None if lines are invalid.
    """
    if not value or not line_nums or not line_index:
        return None

    value_norm = _normalize(value).lower().strip()

    # Collect all words from the candidate lines, tagging each with its line num
    all_words = []  # [(word_dict, line_num), ...]
    for ln in line_nums:
        entry = line_index.get(ln)
        if not entry:
            continue
        for w in entry.get("words", []):
            if w.get("bbox"):
                all_words.append((w, ln))

    if not all_words:
        return None

    # Find the best matching contiguous word span across all candidate words
    matched = _find_word_span(value_norm, all_words)

    if not matched:
        return None

    # Group matched bboxes by line number, merge each group into one rect
    lines_bboxes = OrderedDict()
    for bbox, ln in matched:
        lines_bboxes.setdefault(ln, []).append(bbox)

    result = []
    for bboxes in lines_bboxes.values():
        merged = _merge_bboxes(bboxes)
        if merged:
            result.append(merged)

    return result if result else None


def _find_word_span(value_norm, tagged_words):
    """Find the best matching contiguous word span.

    tagged_words: list of (word_dict, line_num) tuples.

    Returns list of (bbox, line_num) for the matched words, or all words
    as fallback if no good match is found.
    """
    n = len(tagged_words)
    best_start, best_len, best_score = 0, n, 0

    for i in range(n):
        combined = ""
        for j in range(i, n):
            w = _normalize(tagged_words[j][0]["text"]).lower().strip()
            combined = f"{combined} {w}".strip() if combined else w

            if value_norm == combined:
                # Exact match — return immediately
                return [(tagged_words[i + k][0]["bbox"], tagged_words[i + k][1])
                        for k in range(j - i + 1)]

            if value_norm in combined:
                score = len(value_norm) / len(combined)
                if score > best_score:
                    best_start, best_len, best_score = i, j - i + 1, score

            # Stop expanding if we've overshot
            if len(combined) > len(value_norm) * 1.5:
                break

        if best_score == 1.0:
            break

    if best_score > 0.5:
        return [(tagged_words[best_start + k][0]["bbox"], tagged_words[best_start + k][1])
                for k in range(best_len)]

    # Fallback: return all words (GPT said these lines, trust it)
    return [(w[0]["bbox"], w[1]) for w in tagged_words]


def _apply_coords(fields_dict, line_index, skip_keys=None):
    """Resolve coordinates for all fields using their 'lines' arrays.

    Also derives page_number from the line index so the frontend continues
    to receive it without GPT having to return it.
    """
    skip_keys = skip_keys or set()

    for field_name, field_data in fields_dict.items():
        if field_name in skip_keys:
            continue

        if field_name in ARRAY_FIELDS and isinstance(field_data, list):
            for entry in field_data:
                if not isinstance(entry, dict):
                    continue
                _resolve_single_field(entry, line_index)
            continue

        if not isinstance(field_data, dict):
            continue
        _resolve_single_field(field_data, line_index)


def _resolve_single_field(field_data, line_index):
    """Resolve coords and derive page_number for one field entry."""
    value = field_data.get("value")
    line_nums = field_data.get("lines", [])

    if not value or value == "not found" or not isinstance(value, str) or not line_nums:
        field_data["coords"] = None
        field_data["page_number"] = _page_from_lines(line_nums, line_index)
        return

    field_data["coords"] = _resolve_coords(value, line_nums, line_index)
    field_data["page_number"] = _page_from_lines(line_nums, line_index)


def _page_from_lines(line_nums, line_index):
    """Derive page number from the first valid line number."""
    for ln in (line_nums or []):
        entry = line_index.get(ln)
        if entry:
            return entry["page"]
    return None


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


def extract_field_information(line_index):
    """Single-pass extraction with line-number-based coordinate resolution.

    Args:
        line_index: dict of {line_num: {page, text, words}} from Textract

    Returns a dict with universal fields at the top level, a "producers" array
    with per-producer field extractions, and a "songs" array with per-song field
    extractions — each entry enriched with bounding box coords where found.
    """
    prompt = build_extraction_prompt(line_index)
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

    if line_index:
        try:
            _apply_coords(extracted_fields, line_index, skip_keys={"producers", "songs"})

            for producer in extracted_fields.get("producers", []):
                _apply_coords(producer, line_index, skip_keys={"producer_name"})

            for song in extracted_fields.get("songs", []):
                _apply_coords(song, line_index, skip_keys={"song_title", "is_rate_explicit"})

        except Exception as e:
            print(f"[WARNING] Failed to add coordinates: {e}")

    return extracted_fields
