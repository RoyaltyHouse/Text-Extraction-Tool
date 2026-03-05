import json


# Load field descriptions from JSON file
with open("field_descriptions.json", "r", encoding="utf-8") as f:
    field_descriptions_details = json.load(f)

# Fields extracted once and shared across the entire agreement
UNIVERSAL_FIELDS = [
    "Document Name",
    "Execution Status",
    "Signatures",
    "Song Title",
    "Artist Name",
    "Single/Multisong Line",
    "Direct Counterparty",
    "Alternative Counterparties",
    "Effective Date",
    "Distributor",
    "Label",
    "Organization Counting Units",
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


def build_extraction_prompt(line_index):
    """Build a single-pass extraction prompt for GPT.

    Args:
        line_index: dict of {line_num: {page, text, words}} from Textract
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
    array_fields_list = ", ".join(f'"{f}"' for f in sorted(ARRAY_FIELDS)) or "none"

    producer_example_1 = _build_producer_example("<First Producer Name>")
    producer_example_2 = _build_producer_example("<Second Producer Name>")
    song_example_1 = _build_song_example("<Song Title 1>")
    song_example_2 = _build_song_example("<Song Title 2>")

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
Found field: {{"value": "...", "lines": [L1, L2]}}
Missing field: {{"value": "not found", "lines": []}}
Array fields ({array_fields_list}): [{{"value": "...", "lines": [L1]}}, ...]
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

SIGNATURE ANALYSIS ORDER: Populate "Signatures" FIRST by scanning the signature block, then
derive "Execution Status" (FX/PX/NX) from the signed/unsigned counts in that array.

PHASE 4 — PRODUCER-SPECIFIC FIELDS (for EACH producer)
{producer_field_text}

- Extract from the section pertaining to THAT producer only
- If a term applies to all producers, DUPLICATE it into every entry
- Missing: {{"value": "not found", "lines": []}}

PHASE 5 — SONG-SPECIFIC FIELDS (for EACH song)
Fields: {song_field_names}
(Same definitions as Phase 4)

- Extract from the section for THAT song only (per-track schedule, subsection, table row)
- For "Producer Royalty Points": use the royalty subsection, NOT the Bumps subsection. Include per-producer breakdown if stated alongside the aggregate rate.
- For "Type of Royalty": use the type of royalty subsection, NOT the royalty points or bumps subsection.
- "is_rate_explicit": true if explicitly stated for this song; false if from a blanket clause
- "advance_scope": set to "agreement" if ONE advance covers all songs (no per-track breakdown); set to "song" if each track has its own distinct advance amount stated explicitly. Always duplicate the advance value into every song entry regardless.
- Blanket values: DUPLICATE into every song entry
- Missing: {{"value": "not found", "lines": []}}

OUTPUT FORMAT
-------------
{{
  "Document Name": {{"value": "...", "lines": [1]}},
  "Execution Status": {{"value": "FX|PX|NX", "lines": [1]}},
  "Signatures": [
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
  "producers": [
{producer_example_1},
{producer_example_2}
  ],
  "songs": [
{song_example_1},
{song_example_2}
  ]
}}

One producer entry per producer identified. One song entry per song identified. No placeholders.

CONTRACT TEXT
-------------
\"\"\"
{contract_text}
\"\"\"
""".strip()
