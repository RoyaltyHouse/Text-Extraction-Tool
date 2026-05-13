"""Cluster Textract annotations into per-party signature blocks.

Textract's FORMS analysis returns each form field key/value pair as a separate
annotation. This module groups spatially adjacent signature-related fields into
one block per signing party — including blank/unsigned blocks that GPT tends to
silently skip when enumerating from raw OCR.

The clustering output drives both the prompt's per-block listing and the
mechanical FX/PX/NX verdict, which removes enumeration ambiguity from the LLM.
"""

import re


# Canonical form keys (normalized) that mark a cluster as a real signature
# block region. EXACT match only — "by" matches but "Document created by"
# does NOT, which keeps DocuSign / Adobe Sign audit-trail fields ("Signer",
# "Document created by", "Email viewed by", "Signature Date", ...) from
# creating phantom blocks via word-subset matches.
#
# Note: a filled value in one of these fields does NOT by itself imply the
# party signed — Textract often spatially pairs typed identifiers below blank
# signature lines as the form field's value (e.g. "By: ____ / Aaron Lockhart"
# becomes {key: "By:", value: "Aaron Lockhart"} even though the line is blank).
# The `signed` verdict requires a Textract SIGNATURE annotation in the block.
_SIGNED_KEYS = {
    "by",
    "name",
    "print name",
    "printed name",
    "signature",
    "signed",
    "sign",
    "signatory",
    "authorized signatory",
}

# Keys that anchor a signature block but whose filled value alone doesn't
# imply signing (a typed Date or Title without a name/signature isn't signed).
_META_KEYS = {"title", "date", "its"}

_NORM_KEY_RE = re.compile(r"[^a-z ]")
_EXCLUDED_RE = re.compile(r"\b(soundexchange|letter of direction|lod)\b", re.IGNORECASE)

# Textract coords are 0.0-1.0 normalized to page dimensions.
_Y_GAP = 0.05   # vertical gap between adjacent items in one block
_X_GAP = 0.20   # horizontal gap between adjacent items in one column
_CONTEXT_RADIUS = 10  # lines around the block to scan for exclusion patterns


def _norm_key(s):
    return _NORM_KEY_RE.sub("", (s or "").lower()).strip()


def _classify_key(key):
    """Return 'signed_signal', 'meta', or None for a form-field key."""
    norm = _norm_key(key)
    if not norm:
        return None
    if norm in _SIGNED_KEYS:
        return "signed_signal"
    if norm in _META_KEYS:
        return "meta"
    return None


def _chain_cluster(items, coord_fn, gap):
    """Chain-link 1D cluster: new group when consecutive sorted items exceed *gap*."""
    if not items:
        return []
    ordered = sorted(items, key=coord_fn)
    groups = [[ordered[0]]]
    for item in ordered[1:]:
        if coord_fn(item) - coord_fn(groups[-1][-1]) <= gap:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _build_candidates(annotations):
    """Project signature-related annotations into uniform candidate dicts."""
    candidates = []
    for ann in annotations:
        if ann.get("type") == "signature":
            kind = "signature"
        elif ann.get("type") == "form_field":
            kind = _classify_key(ann.get("key"))
            if not kind:
                continue
        else:
            continue
        if ann.get("top") is None or ann.get("left") is None:
            continue
        candidates.append({"kind": kind, "ann": ann,
                           "page": ann["page"], "top": ann["top"], "left": ann["left"]})
    return candidates


def _excluded_by_context(page, line_range, line_index):
    """True if SoundExchange/LOD wording appears in nearby contract text."""
    lo, hi = line_range
    if lo is None:
        return False
    text = " ".join(
        line_index[ln]["text"]
        for ln in range(max(1, lo - _CONTEXT_RADIUS), hi + _CONTEXT_RADIUS + 1)
        if ln in line_index and line_index[ln].get("page") == page
    )
    return bool(_EXCLUDED_RE.search(text))


def _build_block(cluster, line_index):
    """Materialize one cluster into a block dict, or None if not a real sig block."""
    fields = []
    has_signature_detection = False
    has_signed_anchor = False

    for it in cluster:
        if it["kind"] == "signature":
            has_signature_detection = True
            continue
        ann = it["ann"]
        value = (ann.get("value") or "").strip()
        is_signed_key = it["kind"] == "signed_signal"
        fields.append({
            "key": ann.get("key", ""),
            "value": value,
            "filled": bool(value),
            "is_signed_signal": is_signed_key,
        })
        if is_signed_key:
            has_signed_anchor = True

    # A cluster of only meta fields (e.g. a stray "Date:" in a header) isn't a
    # real signature block. Require either a signed-anchor field or a Textract
    # SIGNATURE detection.
    if not has_signature_detection and not has_signed_anchor:
        return None

    near_lines = [it["ann"].get("near_line")
                  for it in cluster if it["ann"].get("near_line") is not None]
    line_range = (min(near_lines), max(near_lines)) if near_lines else (None, None)

    return {
        "page": cluster[0]["page"],
        "x": round(min(it["left"] for it in cluster), 3),
        "line_range": line_range,
        "fields": fields,
        "has_signature_detection": has_signature_detection,
        "signed": has_signature_detection,
        "excluded": _excluded_by_context(cluster[0]["page"], line_range, line_index),
    }


def cluster_signature_blocks(annotations, line_index):
    """Group signature-related annotations into structured per-party blocks.

    Returns an ordered list of block dicts. Each block represents one signing
    party (signed or unsigned). Blocks tagged `excluded` (SoundExchange / LOD
    context) should not count toward FX/PX/NX.
    """
    candidates = _build_candidates(annotations)

    by_page = {}
    for c in candidates:
        by_page.setdefault(c["page"], []).append(c)

    blocks = []
    for page in sorted(by_page):
        for y_band in _chain_cluster(by_page[page], lambda c: c["top"], _Y_GAP):
            for column in _chain_cluster(y_band, lambda c: c["left"], _X_GAP):
                block = _build_block(column, line_index)
                if block is not None:
                    blocks.append(block)
    return blocks
