import json
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_descriptions.json"), encoding="utf-8") as f:
    FIELD_DESCRIPTIONS = json.load(f)

ARRAY_FIELDS = {name for name, data in FIELD_DESCRIPTIONS.items() if data.get("is_array")}

UNIVERSAL_FIELDS = [
    "signatures",
    "Song Title",
    "Artist Name",
    "Single/Multisong Line",
    "Direct Counterparty",
    "Alternative Counterparties",
    "Effective Date",
    "Contract Term",
    "Distributor",
    "Label",
    "Organization Counting Units",
]

PRODUCER_FIELDS = [
    "Client Party",
    "Lawyer Information",
    "Producer Royalty Points",
    "Type of Royalty",
    "Bumps",
    "Third Party Money",
    "Producer Advance Legal Recoupment",
    "Recoupment Classification",
    "Legal Advance",
    "Classification of Recoupment Language",
]

# Financial terms that can vary per track.
SONG_FIELDS = [
    "Producer Royalty Points",
    "Type of Royalty",
    "Bumps",
    "Third Party Money",
    "Producer Advance Legal Recoupment",
    "Recoupment Classification",
    "Classification of Recoupment Language",
]

_LEAF = '{"value": "...", "lines": [1]}'
# Output-format examples for fields that aren't a plain {value, lines} leaf.
_SHAPES = {
    "signatures": '[\n    {"value": "<Party Name>", "signed": true, "lines": [1]},\n'
                  '    {"value": "<Party Name>", "signed": false, "lines": [2]}\n  ]',
    "Lawyer Information": '{"value": {"client_lawyer": "...", "counterparty_lawyer": "..."}, "lines": [1]}',
    "Advance Mapping": """{
    "structure": "per_producer_per_song|per_song|per_producer|per_master|aggregate|equal_split|none",
    "aggregate_total": "$10,000",
    "split_note": "to be split equally among producers",
    "entries": [
      {"producer": "<Producer Name or 'all'>", "song": "<Song Title or 'all'>", "value": "$5,000", "lines": [42]}
    ],
    "lines": [40, 41, 42]
  }""",
}


def _field_list(names):
    return "\n".join(f"{i}. **{name}**\n   {FIELD_DESCRIPTIONS[name]['description']}" for i, name in enumerate(names, 1))


def _rows(indent, fields, head=(), tail=()):
    shapes = (f'"{f}": {_SHAPES.get(f) or (f"[{_LEAF}]" if f in ARRAY_FIELDS else _LEAF)}' for f in fields)
    return ",\n".join(indent + row for row in (*head, *shapes, *tail))


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


def build_extraction_prompt(line_index, signature_blocks):
    array_fields = ", ".join(f'"{f}"' for f in sorted(ARRAY_FIELDS))
    song_fields = ", ".join(f'"{f}"' for f in SONG_FIELDS)
    universal_rows = _rows("  ", UNIVERSAL_FIELDS + ["Advance Mapping"])
    producer_rows = _rows("      ", PRODUCER_FIELDS, head=['"producer_name": "<Producer Name>"'])
    song_rows = _rows("      ", SONG_FIELDS, head=['"song_title": "<Song Title>"'],
                      tail=['"is_rate_explicit": true', '"advance_scope": "agreement|song"'])
    return f"""Extract structured data from a music royalty contract.

CRITICAL RULES — READ BEFORE EXTRACTING
========================================
LINE REFERENCES: Each contract line is prefixed [LN]. For every field, "lines" must list
ONLY the line(s) where the returned value text physically appears. The value you return
MUST be findable on those lines. NEVER reference a header/label line when the value is on
the next line.
  WRONG: value "$60,000", lines → "3.6 Legal Advance. Upon full execution, Label shall pay..."
  RIGHT: value "$60,000", lines → "advance of Sixty Thousand Dollars ($60,000.00) recoupable..."

VALUE PRECISION: Return ONLY the specific data point — not the surrounding sentence, section
header, label prefix, or any additional context. Do NOT rephrase, summarize, or append info
from other fields.
  WRONG: "The Artist for whom the Master Recordings were produced is DEVON HARRIS, p/k/a Quay Global"
  RIGHT: "Quay Global"
  WRONG: "Multi-Song, 3 Tracks"  (appended track count)
  RIGHT: "Multi-Song arrangement"  (verbatim from document)
  WRONG: "2.0% of NAR"  (appended royalty type from a different field)
  RIGHT: "2.0%"

VALUES MUST BE VERBATIM from the document text. NEVER invent, infer, combine, or guess —
unless a field's description lists fixed values to choose from or defines a calculation.
Found field: {{"value": "...", "lines": [42, 43]}}
Missing field: {{"value": "not found", "lines": []}}
Array fields ({array_fields}): [{{"value": "...", "lines": [42]}}, ...]
"lines" must contain plain integers only — NOT strings, NOT the [L] prefix from the contract text.
NEVER omit a field. Return valid JSON ONLY — no Markdown, no explanation.

PHASE 1 — IDENTIFY PRODUCERS
Scan the opening paragraph and signature block for every producer party.
- Names/entities followed by ("Producer"), ("you"), or the letter's addressee
- Multiple C/O entries tied to different names
- "each of you", "collectively", or listed co-producers
Rules:
- Use EXACT names from the document
- Always identify at least one; use "Producer" only if no name exists
- NEVER include the Artist, Label, or Distributor as a producer

PHASE 2 — IDENTIFY SONGS
Scan the entire document for every song/master recording.
- Song titles in opening paragraphs ("master recording entitled '...'")
- Schedules, appendices, tables ("Schedule A", "List of Masters")
Rules:
- Use EXACT titles from the document
- Always identify at least one

PHASE 3 — UNIVERSAL FIELDS (once for the whole agreement)
{_field_list(UNIVERSAL_FIELDS)}

PHASE 4 — ADVANCE MAPPING (FILL BEFORE PRODUCER/SONG ADVANCES)
{FIELD_DESCRIPTIONS['Advance Mapping']['description']}

PHASE 5 — PRODUCER-SPECIFIC FIELDS (for EACH producer)
{_field_list(PRODUCER_FIELDS)}

- Extract from the section pertaining to THAT producer only
- If a term applies to all producers, DUPLICATE it into every entry
- Missing fields: {{"value": "not found", "lines": []}}

PHASE 6 — SONG-SPECIFIC FIELDS (for EACH song)
Fields: {song_fields}
(Same definitions as Phase 5)

- Extract from the section for THAT song only (per-track schedule, subsection, table row)
- For "Producer Royalty Points": use the royalty subsection, NOT the Bumps subsection. Include per-producer breakdown if stated alongside the aggregate rate.
- For "Type of Royalty": use the type of royalty subsection, NOT the royalty points or bumps subsection.
- "is_rate_explicit": true if explicitly stated for this song; false if from a blanket clause
- "advance_scope": set to "agreement" if ONE advance covers all songs (no per-track breakdown); set to "song" if each track has its own distinct advance amount stated explicitly. Must be consistent with `Advance Mapping.structure` (per_song / per_master / per_producer_per_song → "song"; aggregate / equal_split / per_producer → "agreement"; none → "agreement").
- Other blanket values: DUPLICATE into every song entry
- Missing fields: {{"value": "not found", "lines": []}}

OUTPUT FORMAT
-------------
{{
{universal_rows},
  "producers": [
    {{
{producer_rows}
    }}
  ],
  "songs": [
    {{
{song_rows}
    }}
  ]
}}

The example above shows ONE producer and ONE song entry — emit one entry per producer
identified and one per song identified, following the same schema. No placeholders.

CONTRACT TEXT
-------------
\"\"\"
{_contract_text(line_index)}
\"\"\"
{_signature_blocks(signature_blocks)}
""".strip()
