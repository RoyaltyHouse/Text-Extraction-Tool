from collections import OrderedDict
from dotenv import load_dotenv
from prompt import build_extraction_prompt, ARRAY_FIELDS
import os
import json
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY2"), timeout=180.0)

# Characters that Textract and GPT represent differently.
_CHAR_NORMALIZATIONS = str.maketrans({
    "\u2019": "'",   # right single quotation mark
    "\u2018": "'",   # left single quotation mark
    "\u2032": "'",   # prime
    "\u201c": '"',   # left double quotation mark
    "\u201d": '"',   # right double quotation mark
    "\u2014": "--",  # em dash
    "\u2013": "--",  # en dash
    "\u2012": "--",  # figure dash
    "\u00b7": ".",   # middle dot
    "\u2022": "-",   # bullet
    "\u00a0": " ",   # non-breaking space
})

_NEIGHBOR_RADIUS = 2  # lines above/below GPT's lines to search on miss


def _normalize(text: str) -> str:
    """Normalise Unicode punctuation so GPT text and Textract tokens compare equal."""
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
        "Height": bottom - top,
    }


# ── Word collection helpers ──────────────────────────────────────────────────

def _words_for_lines(line_nums, line_index):
    """Collect all (word_dict, line_num) tuples for a set of line numbers."""
    words = []
    for ln in line_nums:
        entry = line_index.get(ln)
        if not entry:
            continue
        for w in entry.get("words", []):
            if w.get("bbox"):
                words.append((w, ln))
    return words


def _expand_line_nums(line_nums, line_index, radius=_NEIGHBOR_RADIUS):
    """Return line_nums expanded by ±radius, clamped to valid keys."""
    expanded = set(line_nums)
    for ln in line_nums:
        for offset in range(-radius, radius + 1):
            candidate = ln + offset
            if candidate in line_index:
                expanded.add(candidate)
    return sorted(expanded)


# ── Word-span matching ───────────────────────────────────────────────────────

def _find_word_span(value_norm, tagged_words):
    """Find the contiguous word span that best matches *value_norm*.

    Returns list of (bbox, line_num) for matched words, or None if nothing
    scores above the acceptance threshold.
    """
    n = len(tagged_words)
    if n == 0:
        return None

    best_start, best_len, best_score = 0, 0, 0.0

    for i in range(n):
        combined = ""
        for j in range(i, n):
            w = _normalize(tagged_words[j][0]["text"]).lower().strip()
            combined = f"{combined} {w}".strip() if combined else w

            if value_norm == combined:
                return [
                    (tagged_words[i + k][0]["bbox"], tagged_words[i + k][1])
                    for k in range(j - i + 1)
                ]

            if value_norm in combined:
                score = len(value_norm) / len(combined)
                if score > best_score:
                    best_start, best_len, best_score = i, j - i + 1, score

            if len(combined) > len(value_norm) * 1.5:
                break

        if best_score == 1.0:
            break

    if best_score > 0.5:
        return [
            (tagged_words[best_start + k][0]["bbox"], tagged_words[best_start + k][1])
            for k in range(best_len)
        ]

    return None


# ── Coordinate resolution ────────────────────────────────────────────────────

def _resolve_coords(value, line_nums, line_index):
    """Resolve word-level bboxes for *value* near the given *line_nums*.

    Strategy (in order):
      1. Search only the lines GPT referenced.
      2. If no match, expand search to ±NEIGHBOR_RADIUS lines.
      3. If still no match, fall back to highlighting the original lines fully.

    Returns (coords, matched_line_nums) where coords is a list of per-line
    merged bboxes and matched_line_nums is the set of lines actually used.
    Returns (None, line_nums) when resolution fails entirely.
    """
    if not value or not line_nums or not line_index:
        return None, line_nums

    value_norm = _normalize(value).lower().strip()

    # --- Attempt 1: exact lines GPT referenced ---
    primary_words = _words_for_lines(line_nums, line_index)
    matched = _find_word_span(value_norm, primary_words) if primary_words else None

    # --- Attempt 2: expand to neighbor lines ---
    if matched is None:
        expanded = _expand_line_nums(line_nums, line_index)
        if set(expanded) != set(line_nums):
            neighbor_words = _words_for_lines(expanded, line_index)
            matched = _find_word_span(value_norm, neighbor_words) if neighbor_words else None

    # --- Fallback: highlight all words on GPT's original lines ---
    if matched is None and primary_words:
        matched = [(w[0]["bbox"], w[1]) for w in primary_words]

    if not matched:
        return None, line_nums

    # Group by line number → merge each line's bboxes into one rect
    grouped = OrderedDict()
    for bbox, ln in matched:
        grouped.setdefault(ln, []).append(bbox)

    actual_lines = sorted(grouped.keys())
    coords = [_merge_bboxes(bbs) for bbs in grouped.values() if _merge_bboxes(bbs)]
    return (coords or None), actual_lines


def _resolve_single_field(field_data, line_index):
    """Resolve coords, correct line references, and derive page_number."""
    value = field_data.get("value")
    line_nums = field_data.get("lines", [])

    if not value or value == "not found" or not isinstance(value, str) or not line_nums:
        field_data["coords"] = None
        field_data["page_number"] = _page_from_lines(line_nums, line_index)
        return

    coords, actual_lines = _resolve_coords(value, line_nums, line_index)
    field_data["coords"] = coords
    field_data["lines"] = actual_lines
    field_data["page_number"] = _page_from_lines(actual_lines, line_index)


def _apply_coords(fields_dict, line_index, skip_keys=None):
    """Resolve coordinates for every field in *fields_dict*."""
    skip_keys = skip_keys or set()

    for field_name, field_data in fields_dict.items():
        if field_name in skip_keys:
            continue

        if field_name in ARRAY_FIELDS and isinstance(field_data, list):
            for entry in field_data:
                if isinstance(entry, dict):
                    _resolve_single_field(entry, line_index)
            continue

        if isinstance(field_data, dict):
            _resolve_single_field(field_data, line_index)


def _page_from_lines(line_nums, line_index):
    """Derive page number from the first valid line number."""
    for ln in (line_nums or []):
        entry = line_index.get(ln)
        if entry:
            return entry["page"]
    return None


def _derive_execution_status(signatures):
    """Compute FX/PX/NX from the signatures array — no GPT involvement.

    GPT populates signatures[].signed but its derived Execution Status verdict
    is unreliable. Overwrite it with the mechanical count.
    """
    if not signatures or not isinstance(signatures, list):
        return None
    signed = sum(1 for s in signatures if s.get("signed") is True)
    total = len(signatures)
    if total == 0 or signed == 0:
        return "NX"
    if signed == total:
        return "FX"
    return "PX"


# ── GPT interaction ──────────────────────────────────────────────────────────

def _strip_markdown_fences(content):
    """Remove ```json / ``` fences GPT occasionally wraps responses in."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines)
    return content


def extract_field_information(line_index, annotations=None):
    """Single-pass extraction with line-number-based coordinate resolution."""
    anns = annotations or []
    prompt = build_extraction_prompt(line_index, anns)

    sig_anns = [a for a in anns if a.get("type") == "signature"]
    form_anns = [a for a in anns if a.get("type") == "form_field"]
    print(f"[DEBUG] {len(sig_anns)} SIGNATURE annotations:")
    for a in sig_anns:
        print(
            f"  - page={a['page']} near=L{a.get('near_line')} "
            f"x={a.get('left')} conf={a.get('confidence')}%"
        )
    short_forms = [a for a in form_anns if len(a.get("value") or "") < 60]
    print(
        f"[DEBUG] {len(short_forms)} short FORM FIELDs "
        f"(out of {len(form_anns)} total):"
    )
    for a in short_forms:
        val = a.get("value") or "[BLANK]"
        print(
            f'  - page={a["page"]} near=L{a.get("near_line")} '
            f'x={a.get("left")} "{a["key"]}" => "{val}"'
        )

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=16384,
    )

    choice = response.choices[0]
    if choice.finish_reason == "length":
        print(f"[ERROR] GPT response truncated (hit max_tokens). Input may be too large.")
        return {"error": "Extraction failed: response truncated due to document length"}

    content = _strip_markdown_fences(choice.message.content or "")

    try:
        extracted_fields = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse GPT response as JSON: {e}")
        print(f"[ERROR] Raw content (first 500 chars): {content[:500]}")
        return {"error": f"Failed to parse extraction results: {e}"}

    # Override GPT's Execution Status verdict with a mechanical count from
    # the signatures array — GPT populates signed/unsigned per party but
    # its derived FX/PX/NX is unreliable.
    sigs = extracted_fields.get("signatures", [])
    computed_status = _derive_execution_status(sigs)
    if computed_status is not None:
        es = extracted_fields.get("Execution Status")
        if isinstance(es, dict):
            es["value"] = computed_status
        else:
            extracted_fields["Execution Status"] = {"value": computed_status, "lines": []}

    if line_index:
        try:
            _apply_coords(extracted_fields, line_index, skip_keys={"producers", "songs"})

            for producer in extracted_fields.get("producers", []):
                _apply_coords(producer, line_index, skip_keys={"producer_name"})

            for song in extracted_fields.get("songs", []):
                _apply_coords(song, line_index, skip_keys={"song_title", "is_rate_explicit", "advance_scope"})
        except Exception as e:
            print(f"[WARNING] Coordinate resolution failed: {e}")

    return extracted_fields
