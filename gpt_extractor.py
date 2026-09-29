import json
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

from prompt import build_extraction_prompt
from signature_blocks import cluster_signature_blocks

load_dotenv()

# No retries: a retried timeout can outlast the Lambda, which then dies before marking the job failed.
client = OpenAI(api_key=os.getenv("OPENROUTER_API_KEY"), base_url="https://openrouter.ai/api/v1", max_retries=0)
DEFAULT_MODEL = os.getenv("OPENROUTER_DEFAULT_MODEL", "openai/gpt-5.4-mini")

# Curly quotes, primes, dashes, middle dot, bullet and nbsp render differently in Textract and model output.
_CHAR_NORMALIZATIONS = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u2032": "'", "\u201c": '"', "\u201d": '"',
    "\u2014": "--", "\u2013": "--", "\u2012": "--", "\u00b7": ".", "\u2022": "-", "\u00a0": " ",
})
_NEIGHBOR_RADIUS = 2


def _norm(text):
    return text.translate(_CHAR_NORMALIZATIONS).lower().strip()


def _merge_bboxes(bboxes):
    if len(bboxes) == 1:
        return bboxes[0]
    left, top = min(b["Left"] for b in bboxes), min(b["Top"] for b in bboxes)
    return {"Left": left, "Top": top,
            "Width": max(b["Left"] + b["Width"] for b in bboxes) - left,
            "Height": max(b["Top"] + b["Height"] for b in bboxes) - top}


def _words(line_nums, line_index):
    return [(w, ln) for ln in line_nums for w in line_index.get(ln, {}).get("words", [])]


def _find_word_span(value, words):
    # Score is the share of the span's text the value covers; it must beat half.
    texts = [_norm(w["text"]) for w, _ in words]
    best, best_score = None, 0.5
    for i in range(len(words)):
        combined = ""
        for j in range(i, len(words)):
            combined = f"{combined} {texts[j]}".strip()
            if value == combined:
                return words[i:j + 1]
            if value in combined and len(value) / len(combined) > best_score:
                best, best_score = words[i:j + 1], len(value) / len(combined)
            if len(combined) > len(value) * 1.5:
                break
    return best


def _line_nums(lines):
    # Models sometimes return "42" or "L42" despite the prompt; anything else unreadable is dropped.
    return [int(m.group(1)) for ln in (lines if isinstance(lines, list) else [lines])
            if (m := re.fullmatch(r"\[?L?(\d+)\]?", str(ln).strip()))]


def _resolve_field(field, line_index):
    value, lines = field.get("value"), _line_nums(field.get("lines"))
    coords = None
    if isinstance(value, str) and value and value != "not found" and lines:
        value, words = _norm(value), _words(lines, line_index)
        match = _find_word_span(value, words)
        if match is None:
            nearby = sorted(set(lines) | {ln + d for ln in lines for d in range(-_NEIGHBOR_RADIUS, _NEIGHBOR_RADIUS + 1)
                                          if ln + d in line_index})
            if set(nearby) != set(lines):
                match = _find_word_span(value, _words(nearby, line_index))
        match = match or words  # fall back to highlighting the referenced lines
        if match:
            by_line = {}
            for w, ln in match:
                by_line.setdefault(ln, []).append(w["bbox"])
            coords, lines = [_merge_bboxes(b) for b in by_line.values()], sorted(by_line)
    field.update(lines=lines, coords=coords, page_number=next((line_index[ln]["page"] for ln in lines if ln in line_index), None))


def _apply_coords(node, line_index):
    if isinstance(node, dict) and "value" in node:
        _resolve_field(node, line_index)
    elif isinstance(node, (dict, list)):
        for child in node.values() if isinstance(node, dict) else node:
            _apply_coords(child, line_index)


def _reconcile_signatures(blocks, gpt_signatures):
    # Blocks decide the count and signed verdict; the model only labels each party, in block order.
    labels = gpt_signatures if isinstance(gpt_signatures, list) else []
    out = []
    for i, block in enumerate(b for b in blocks if not b["excluded"]):
        label = labels[i].get("value") if i < len(labels) and isinstance(labels[i], dict) else None
        lo, hi = block["line_range"]
        out.append({"value": label.strip() if isinstance(label, str) and label.strip() else f"Party {i + 1}",
                    "signed": block["signed"], "lines": list(range(lo, hi + 1)) if lo is not None else []})
    return out


def _derive_execution_status(signatures):
    signed = sum(s["signed"] for s in signatures)
    return "NX" if not signed else "FX" if signed == len(signatures) else "PX"


def extract_field_information(line_index, annotations=(), deadline=None, instructions=None):
    blocks = cluster_signature_blocks(annotations, line_index)
    for i, b in enumerate(blocks, 1):
        tag = "EXCLUDED" if b["excluded"] else "SIGNED" if b["signed"] else "UNSIGNED"
        fields = ", ".join(f'{f["key"]}={f["value"]!r}' for f in b["fields"]) or "(no form fields)"
        print(f"[BLOCK {i}] page={b['page']} x={b['x']} lines={b['line_range']} -> {tag} | {fields}")

    response = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": build_extraction_prompt(line_index, blocks, instructions)},
        ],
        max_completion_tokens=16384,
        # OpenRouter's own reasoning control; the OpenAI-style top-level reasoning_effort isn't reliably honoured.
        extra_body={"reasoning": {"effort": "low"}},
        # 10 s spare to record the result before the deadline.
        timeout=180.0 if deadline is None else min(180.0, deadline - time.time() - 10),
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        print("[ERROR] GPT response truncated (hit max_tokens). Input may be too large.")
        return {"error": "Extraction failed: response truncated due to document length"}

    content = (choice.message.content or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()[1:]
        content = "\n".join(lines[:-1] if lines and lines[-1].startswith("```") else lines)
    try:
        fields = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse GPT response as JSON: {e}. Raw content (first 500 chars): {content[:500]}")
        return {"error": f"Failed to parse extraction results: {e}"}

    signatures = fields["signatures"] = _reconcile_signatures(blocks, fields.get("signatures"))
    fields["Execution Status"] = {"value": _derive_execution_status(signatures),
                                  "lines": next((s["lines"][:1] for s in signatures if s["lines"]), [])}
    try:
        _apply_coords(fields, line_index)
    except Exception as e:
        print(f"[WARNING] Coordinate resolution failed: {e}")
    return fields
