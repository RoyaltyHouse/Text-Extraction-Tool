import json
import os


# Load field descriptions from the JSON file sitting alongside this module,
# regardless of the caller's working directory.
_FIELD_DESCRIPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "field_descriptions.json"
)
with open(_FIELD_DESCRIPTIONS_PATH, "r", encoding="utf-8") as f:
    field_descriptions_details = json.load(f)

# Fields extracted once and shared across the entire agreement
UNIVERSAL_FIELDS = [
    "Execution Status",
    "signatures",
    "Song Title",
    "Artist Name",
    "Single/Multisong Line",
    "Direct Counterparty",
    "Alternative Counterparties",
    "Effective Date",
    "Distributor",
    "Label",
    "Organization Counting Units",
    "Advance Mapping",
]

# Derived from field_descriptions.json — any field with "is_array": true
# returns a list of {value, lines} entries instead of a single object.
# To make a field array-typed, just add "is_array": true to its entry in the JSON.
ARRAY_FIELDS = {
    name for name, data in field_descriptions_details.items()
    if isinstance(data, dict) and data.get("is_array", False)
}

# Fields extracted separately for each producer party to the agreement
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

# Fields extracted separately for each song covered by the agreement.
# These are the financial/royalty terms from PRODUCER_FIELDS that may vary
# per track. is_rate_explicit is a synthetic boolean flag (not in
# field_descriptions) that indicates whether each rate was explicitly stated
# for this specific song or inferred from a blanket clause.
SONG_FIELDS = [
    "Producer Royalty Points",
    "Type of Royalty",
    "Bumps",
    "Third Party Money",
    "Producer Advance Legal Recoupment",
    "Recoupment Classification",
    "Classification of Recoupment Language",
]


def _get_description(name):
    """Extract the description string from a field entry (object or plain string)."""
    data = field_descriptions_details.get(name)
    if data is None:
        return "Extract this field from the document."
    if isinstance(data, dict):
        return data.get("description", "Extract this field from the document.")
    return data  # plain string — legacy / manually-added field


def _format_field_list(field_names):
    """Build a numbered field description block from a list of field names."""
    lines = []
    for i, name in enumerate(field_names):
        lines.append(f"{i + 1}. **{name}**\n   {_get_description(name)}")
    return "\n".join(lines)


def _format_contract_text(line_index):
    """Render line_index into numbered lines with page separators."""
    parts = []
    current_page = None
    for line_num in sorted(line_index.keys()):
        entry = line_index[line_num]
        if entry["page"] != current_page:
            current_page = entry["page"]
            parts.append(f"\n--- Page {current_page} ---")
        parts.append(f"[L{line_num}] {entry['text']}")
    return "\n".join(parts)


def _format_line_range(line_range):
    lo, hi = line_range
    if lo is None:
        return ""
    return f"L{lo}" if lo == hi else f"L{lo}-L{hi}"


def _format_signature_blocks(blocks):
    """Render the clustered signature blocks as a deterministic enumeration.

    Each block represents one party. The LLM must produce exactly one entry
    in `signatures` per block listed, in order, using the contract text near
    the listed lines to determine the party role/name.

    Excluded blocks (SoundExchange/LOD/audit-report context) are filtered out
    before rendering — they don't reach the prompt at all, so the LLM never
    sees them and can't accidentally include them.
    """
    active = [b for b in blocks if not b["excluded"]]
    if not active:
        return "\nDETECTED SIGNATURE BLOCKS: none found.\n"

    signed_count = sum(1 for b in active if b["signed"])
    parts = [
        "",
        f"DETECTED SIGNATURE BLOCKS ({len(active)} total — {signed_count} signed, "
        f"{len(active) - signed_count} unsigned). Emit one entry in `signatures` "
        f"per block below, in the order shown.",
        "",
    ]
    for i, b in enumerate(active, 1):
        loc = _format_line_range(b["line_range"])
        loc_part = f"  {loc}" if loc else ""
        verdict = "SIGNED" if b["signed"] else "UNSIGNED"
        parts.append(f"[{i}] page {b['page']}  x={b['x']}{loc_part}  | verdict: {verdict}")
        for f in b["fields"]:
            anchor = "*" if f["is_signed_signal"] else " "
            value_repr = f'"{f["value"]}"' if f["filled"] else "[BLANK]"
            parts.append(f"    {anchor} {f['key']:<10} {value_repr}")
        parts.append("")
    return "\n".join(parts)


def _build_producer_example(placeholder_name, indent="    "):
    """Build one entry in the producers array for the output format example."""
    producer_example_fields = [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
    lines = [f"{indent}{{", f'{indent}  "producer_name": "{placeholder_name}",']
    for field in producer_example_fields:
        if field in ARRAY_FIELDS:
            lines.append(f'{indent}  "{field}": [{{"value": "...", "lines": [1]}}],')
        else:
            lines.append(f'{indent}  "{field}": {{"value": "...", "lines": [1]}},')
    lines[-1] = lines[-1].rstrip(",")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _build_song_example(placeholder_title, indent="    "):
    """Build one entry in the songs array for the output format example."""
    song_example_fields = [f for f in SONG_FIELDS if f in field_descriptions_details]
    lines = [f"{indent}{{", f'{indent}  "song_title": "{placeholder_title}",']
    for field in song_example_fields:
        if field in ARRAY_FIELDS:
            lines.append(f'{indent}  "{field}": [{{"value": "...", "lines": [1]}}],')
        else:
            lines.append(f'{indent}  "{field}": {{"value": "...", "lines": [1]}},')
    # Synthetic flags — always last, no trailing comma on final entry
    lines.append(f'{indent}  "is_rate_explicit": true,')
    lines.append(f'{indent}  "advance_scope": "agreement|song"')
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def build_extraction_prompt(line_index, signature_blocks=None):
    """Build a single-pass extraction prompt for GPT.

    Args:
        line_index: dict of {line_num: {page, text, words}} from Textract
        signature_blocks: list of per-party blocks from cluster_signature_blocks().
            The LLM treats this as a deterministic enumeration — one signatures[]
            entry per active block, no enumeration of its own.
    """
    universal_field_text = _format_field_list(
        [f for f in UNIVERSAL_FIELDS if f in field_descriptions_details]
    )
    producer_field_text = _format_field_list(
        [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
    )
    # Song fields are a subset of producer fields — list names only to avoid
    # repeating the full descriptions and inflating the prompt token count.
    song_field_names = ", ".join(
        f'"{f}"' for f in SONG_FIELDS if f in field_descriptions_details
    )
    contract_text = _format_contract_text(line_index)
    signature_blocks_block = _format_signature_blocks(signature_blocks or [])
    array_fields_list = ", ".join(f'"{f}"' for f in sorted(ARRAY_FIELDS)) or "none"

    producer_example = _build_producer_example("<Producer Name>")
    song_example = _build_song_example("<Song Title>")

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

VALUES MUST BE VERBATIM from the document text. NEVER invent, infer, combine, or guess.
Found field: {{"value": "...", "lines": [42, 43]}}
Missing field: {{"value": "not found", "lines": []}}
Array fields ({array_fields_list}): [{{"value": "...", "lines": [42]}}, ...]
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
{universal_field_text}

SIGNATURES: A deterministic enumeration of signature blocks is provided in the
DETECTED SIGNATURE BLOCKS section below. Do NOT enumerate blocks yourself.
For each block listed, emit exactly one entry in "signatures" in the order shown:
  - "value": the party's entity name (e.g. "Cheeze Beatz, LLC", "Maybach Music Group, LLC")
    or person's full name (e.g. "Darryl McCorkell"). Read the contract text near the
    block's listed lines AND the typed name on/near the signature line.
    DO NOT use "An Authorized Signatory" — that is the signer's capacity description,
    not their identity. Fall back to a generic role ("Producer", "Label", "Company") only
    when no specific name is visible. Never return "not found" — use the best label available.
  - "signed": copy the listed mechanical verdict exactly — true for SIGNED, false for UNSIGNED.
  - "lines": the block's listed line range as integers.
"Execution Status" is computed automatically — set its value to "TBD".

PHASE 4 — ADVANCE MAPPING (FILL BEFORE PRODUCER/SONG ADVANCES)
Build `Advance Mapping` as the SINGLE SOURCE OF TRUTH for every advance amount in
the document. The per-producer and per-song `Producer Advance Legal Recoupment`
scalars in Phases 5 and 6 are derived FROM this mapping — they must be consistent
with `Advance Mapping.entries`. Do not invent advance amounts in the producer or
song entries that do not appear in the mapping.

Recognize these five patterns (the document may use any of them):

  (1) IN-TEXT (paragraph form). Advances stated inside body paragraphs under
      sections like "Recording Costs", "Compensation", "Producer Advance",
      "Advance". The amount can be tied to a producer, a song, both, or all.
      WATCH: this is the highest-miss case. If a paragraph names BOTH a producer
      AND a song near a dollar figure, emit a `per_producer_per_song` entry —
      do NOT collapse to aggregate.

  (2) SCHEDULE / TABLE. Listed in "Schedule A", "Schedule 1", or a labeled
      table, one row per song. Use `per_song` (or `per_producer_per_song` if
      the table also breaks out by producer).

  (3) PER-MASTER. "$X per master recording" or "$X for each Master". Set
      `structure = "per_master"`, `aggregate_total = "$X"` (the per-master
      rate), and emit ONE entry per song with value = $X.

  (4) AGGREGATE-ONLY. One total advance with no per-song or per-producer
      breakdown. Set `structure = "aggregate"`, `aggregate_total = "$X"`, and
      emit ONE entry with `producer: "all", song: "all", value: "$X"`.

  (5) NONE. No advance language anywhere. Set `structure = "none"`,
      `aggregate_total = "not found"`, `entries = []`.

Equal-split case: if the document states the advance is "to be split equally
among producers", set `structure = "equal_split"`, `aggregate_total` to the
stated total, `split_note` to the literal phrase from the document, and emit
one entry per producer with value = aggregate / producer_count.

Multi-producer per-share case: if distinct amounts are stated per producer
(e.g. "$6,666.67 payable to Producer A and $3,333.34 payable to Producer B"),
use `per_producer` (or `per_producer_per_song` if also broken out by song).
Emit one entry per (producer, song) pair with that producer's stated share.

PHASE 5 — PRODUCER-SPECIFIC FIELDS (for EACH producer)
{producer_field_text}

- Extract from the section pertaining to THAT producer only
- If a term applies to all producers, DUPLICATE it into every entry
- `Producer Advance Legal Recoupment` is PROJECTED from `Advance Mapping`:
    • If the mapping has a single entry for this producer → use that value.
    • If the mapping has multiple per-song entries for this producer → use the
      SUM of that producer's entries.
    • If `equal_split` → use aggregate / producer_count for every producer.
    • If `aggregate` with no per-producer split → copy the aggregate total.
    • If `none` → "not found".
- Missing other fields: {{"value": "not found", "lines": []}}

PHASE 6 — SONG-SPECIFIC FIELDS (for EACH song)
Fields: {song_field_names}
(Same definitions as Phase 5)

- Extract from the section for THAT song only (per-track schedule, subsection, table row)
- For "Producer Royalty Points": use the royalty subsection, NOT the Bumps subsection. Include per-producer breakdown if stated alongside the aggregate rate.
- For "Type of Royalty": use the type of royalty subsection, NOT the royalty points or bumps subsection.
- "is_rate_explicit": true if explicitly stated for this song; false if from a blanket clause
- "advance_scope": set to "agreement" if ONE advance covers all songs (no per-track breakdown); set to "song" if each track has its own distinct advance amount stated explicitly. Must be consistent with `Advance Mapping.structure` (per_song / per_master / per_producer_per_song → "song"; aggregate / equal_split / per_producer → "agreement"; none → "agreement").
- `Producer Advance Legal Recoupment` is PROJECTED from `Advance Mapping`:
    • If the mapping has a single entry for this song → use that value.
    • If the mapping has multiple per-producer entries for this song → use the
      SUM across producers for that song.
    • If `aggregate` / `equal_split` / `per_producer` (no per-song breakdown) →
      copy the aggregate total into every song.
    • If `none` → "not found".
- Other blanket values: DUPLICATE into every song entry
- Missing other fields: {{"value": "not found", "lines": []}}

OUTPUT FORMAT
-------------
{{
  "Execution Status": {{"value": "FX|PX|NX", "lines": [1]}},
  "signatures": [
    {{"value": "Party Role or Name", "signed": true, "lines": [1]}},
    {{"value": "Party Role or Name", "signed": false, "lines": [2]}}
  ],
  "Song Title": {{"value": "...", "lines": [1]}},
  "Artist Name": {{"value": "...", "lines": [1]}},
  "Single/Multisong Line": {{"value": "...", "lines": [1]}},
  "Direct Counterparty": {{"value": "...", "lines": [1]}},
  "Alternative Counterparties": {{"value": "...", "lines": [1]}},
  "Effective Date": {{"value": "...", "lines": [1]}},
  "Distributor": {{"value": "...", "lines": [1]}},
  "Label": {{"value": "...", "lines": [1]}},
  "Organization Counting Units": {{"value": "...", "lines": [1]}},
  "Advance Mapping": {{
    "structure": "per_producer_per_song|per_song|per_producer|per_master|aggregate|equal_split|none",
    "aggregate_total": "$10,000",
    "split_note": "to be split equally among producers",
    "entries": [
      {{"producer": "<Producer Name or 'all'>", "song": "<Song Title or 'all'>", "value": "$5,000", "lines": [42]}}
    ],
    "lines": [40, 41, 42]
  }},
  "producers": [
{producer_example}
  ],
  "songs": [
{song_example}
  ]
}}

The example above shows ONE producer and ONE song entry — emit one entry per producer
identified and one per song identified, following the same schema. No placeholders.

CONTRACT TEXT
-------------
\"\"\"
{contract_text}
\"\"\"
{signature_blocks_block}
""".strip()
