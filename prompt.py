def _contract_text(line_index):
    parts, page = [], None
    for n in sorted(line_index):
        if line_index[n]["page"] != page:
            page = line_index[n]["page"]
            parts.append(f"\n--- Page {page} ---")
        parts.append(f"[L{n}] {line_index[n]['text']}")
    return "\n".join(parts)

def _signature_blocks(blocks):
    active = [b for b in blocks if not b["excluded"]]
    if not active:
        return "\nDETECTED SIGNATURE BLOCKS: none found.\n"
    signed = sum(b["signed"] for b in active)
    parts = ["", f"DETECTED SIGNATURE BLOCKS ({len(active)} total — {signed} signed, {len(active) - signed} unsigned). "
                 "Emit one entry in `signatures` per block below, in the order shown.", ""]
    for i, b in enumerate(active, 1):
        lo, hi = b["line_range"]
        loc = "" if lo is None else f"  L{lo}" if lo == hi else f"  L{lo}-L{hi}"
        parts.append(f"[{i}] page {b['page']}  x={b['x']}{loc}  | verdict: {'SIGNED' if b['signed'] else 'UNSIGNED'}")
        for f in b["fields"]:
            value = f'"{f["value"]}"' if f["value"] else "[BLANK]"
            parts.append(f"    {'*' if f['is_signed_signal'] else ' '} {f['key']:<10} {value}")
        parts.append("")
    return "\n".join(parts)

def build_extraction_prompt(line_index, signature_blocks, instructions):
    return f"""{instructions.strip()}

CONTRACT TEXT
-------------
\"\"\"
{_contract_text(line_index)}
\"\"\"
{_signature_blocks(signature_blocks)}
""".strip()
