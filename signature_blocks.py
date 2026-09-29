import re

# Exact matches only, so e-sign audit-trail keys ("Document created by", "Signer") don't form phantom blocks.
_SIGNED_KEYS = {"by", "name", "print name", "printed name", "signature", "signed", "sign", "signatory", "authorized signatory"}
_META_KEYS = {"title", "date", "its"}
_NORM_KEY_RE = re.compile(r"[^a-z ]")
# SoundExchange/LOD payment forms and DocuSign audit pages aren't party signatures.
_EXCLUDED_RE = re.compile(r"\b(soundexchange|letter of direction|lod|final audit report)\b", re.IGNORECASE)
_Y_GAP, _X_GAP = 0.05, 0.20

def _classify_key(key):
    norm = _NORM_KEY_RE.sub("", (key or "").lower()).strip()
    return "signed_signal" if norm in _SIGNED_KEYS else "meta" if norm in _META_KEYS else None

def _chain_cluster(items, axis, gap):
    ordered = sorted(items, key=lambda c: c[axis])
    groups = [[ordered[0]]]
    for item in ordered[1:]:
        if item[axis] - groups[-1][-1][axis] <= gap:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups

def _build_block(cluster, excluded):
    fields = [{"key": c["key"], "value": c["value"], "is_signed_signal": c["kind"] == "signed_signal"}
              for c in cluster if c["kind"] != "signature"]
    # Only a Textract SIGNATURE counts: typed names under blank signature lines come back as filled values.
    signed = any(c["kind"] == "signature" for c in cluster)
    # Meta-only clusters (a stray "Date:" in a header) aren't signature blocks.
    if not signed and not any(f["is_signed_signal"] for f in fields):
        return None
    near_lines = [c["near_line"] for c in cluster if c["near_line"] is not None]
    return {
        "page": cluster[0]["page"],
        "x": round(min(c["left"] for c in cluster), 3),
        "line_range": (min(near_lines), max(near_lines)) if near_lines else (None, None),
        "fields": fields,
        "signed": signed,
        "excluded": excluded,
    }


def cluster_signature_blocks(annotations, line_index):
    by_page = {}
    for ann in annotations:
        kind = "signature" if ann["type"] == "signature" else _classify_key(ann["key"])
        if kind:
            by_page.setdefault(ann["page"], []).append({**ann, "kind": kind})

    blocks = []
    for page in sorted(by_page):
        # Whole page, since the LOD/audit header usually sits far above its signature block.
        excluded = bool(_EXCLUDED_RE.search(" ".join(e["text"] for e in line_index.values() if e["page"] == page)))
        for band in _chain_cluster(by_page[page], "top", _Y_GAP):
            for column in _chain_cluster(band, "left", _X_GAP):
                block = _build_block(column, excluded)
                if block:
                    blocks.append(block)
    return blocks
