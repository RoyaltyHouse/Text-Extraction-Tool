from collections import OrderedDict
from dotenv import load_dotenv
from prompt import build_extraction_prompt, ARRAY_FIELDS
from signature_blocks import cluster_signature_blocks
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
    """Compute FX/PX/NX from a reconciled signatures list.

    Empty list means no signature blocks were detected — NX.
    Non-list (defensive) returns None so the caller doesn't override.
    """
    if not isinstance(signatures, list):
        return None
    signed = sum(1 for s in signatures if s.get("signed") is True)
    total = len(signatures)
    if total == 0 or signed == 0:
        return "NX"
    if signed == total:
        return "FX"
    return "PX"


def _reconcile_signatures(blocks, gpt_signatures):
    """Build the authoritative signatures[] from clusters + GPT role labels.

    The cluster output is the source of truth for both the count and the
    signed/unsigned verdict per party. GPT contributes only the human-readable
    role/name for each block via the labeling task. Excluded blocks
    (SoundExchange/LOD) are dropped — they don't count toward execution status.
    """
    active = [b for b in blocks if not b["excluded"]]
    if not isinstance(gpt_signatures, list):
        gpt_signatures = []

    out = []
    for i, block in enumerate(active):
        gpt_entry = gpt_signatures[i] if i < len(gpt_signatures) and isinstance(gpt_signatures[i], dict) else {}
        role = gpt_entry.get("value")
        if not isinstance(role, str) or not role.strip():
            role = f"Party {i + 1}"

        lo, hi = block["line_range"]
        lines = list(range(lo, hi + 1)) if lo is not None and hi is not None else []

        out.append({"value": role.strip(), "signed": block["signed"], "lines": lines})
    return out


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
    blocks = cluster_signature_blocks(annotations or [], line_index)
    active = [b for b in blocks if not b["excluded"]]
    print(f"[DEBUG] Clustered {len(blocks)} signature blocks "
          f"({len(active)} active, {sum(1 for b in active if b['signed'])} signed, "
          f"{len(blocks) - len(active)} excluded)")
    for i, b in enumerate(blocks, 1):
        tag = "EXCLUDED" if b["excluded"] else ("SIGNED" if b["signed"] else "UNSIGNED")
        fields_summary = ", ".join(f'{f["key"]}={f["value"]!r}' for f in b["fields"]) or "(no form fields)"
        print(f"[BLOCK {i}] page={b['page']} x={b['x']} lines={b['line_range']} "
              f"sig_detected={b['has_signature_detection']} -> {tag} | {fields_summary}")

    prompt = build_extraction_prompt(line_index, blocks)

    response = client.chat.completions.create(
        model="gpt-5.4-mini",
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": prompt},
        ],
        max_completion_tokens=16384,
        reasoning_effort="low",
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

    # Replace GPT's signatures[] with the cluster-derived list (GPT keeps the
    # role labels via order; we keep the deterministic count + signed verdict).
    # Execution Status is then derived from the reconciled list.
    extracted_fields["signatures"] = _reconcile_signatures(
        blocks, extracted_fields.get("signatures")
    )
    computed_status = _derive_execution_status(extracted_fields["signatures"])
    if computed_status is not None:
        es = extracted_fields.get("Execution Status")
        if isinstance(es, dict):
            es["value"] = computed_status
        else:
            extracted_fields["Execution Status"] = {"value": computed_status, "lines": []}

    if line_index:
        try:
            _apply_coords(
                extracted_fields,
                line_index,
                skip_keys={"producers", "songs", "Advance Mapping"},
            )

            for producer in extracted_fields.get("producers", []):
                _apply_coords(producer, line_index, skip_keys={"producer_name"})

            for song in extracted_fields.get("songs", []):
                _apply_coords(song, line_index, skip_keys={"song_title", "is_rate_explicit", "advance_scope"})

            # Advance Mapping is a structured object; resolve coords for each
            # nested entry independently. Each entry has {value, lines} so it
            # works directly with _resolve_single_field.
            mapping = extracted_fields.get("Advance Mapping")
            if isinstance(mapping, dict):
                for entry in mapping.get("entries", []) or []:
                    if isinstance(entry, dict):
                        _resolve_single_field(entry, line_index)
        except Exception as e:
            print(f"[WARNING] Coordinate resolution failed: {e}")

    return extracted_fields
