import json


# Load field descriptions from JSON file
with open("field_descriptions.json", "r", encoding="utf-8") as f:
    field_descriptions_details = json.load(f)

# Fields extracted once and shared across the entire agreement
UNIVERSAL_FIELDS = [
    "Document Name",
    "Execution Status",
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
# returns a list of {value, page_number} entries instead of a single object.
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


def _format_contract_text(pages_text):
    """Render the page text dict into a readable contract string."""
    parts = []
    for page_num, text in pages_text.items():
        page_content = "\n".join(text) if isinstance(text, list) else text
        parts.append(f"\n--- Page {page_num} ---\n{page_content}")
    return "\n".join(parts)


def _build_producer_example(placeholder_name, indent="    "):
    """Build one entry in the producers array for the output format example."""
    producer_example_fields = [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
    lines = [f"{indent}{{", f'{indent}  "producer_name": "{placeholder_name}",']
    for field in producer_example_fields:
        if field in ARRAY_FIELDS:
            lines.append(f'{indent}  "{field}": [{{"value": "...", "page_number": 1}}],')
        else:
            lines.append(f'{indent}  "{field}": {{"value": "...", "page_number": 1}},')
    lines[-1] = lines[-1].rstrip(",")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _build_song_example(placeholder_title, indent="    "):
    """Build one entry in the songs array for the output format example."""
    song_example_fields = [f for f in SONG_FIELDS if f in field_descriptions_details]
    lines = [f"{indent}{{", f'{indent}  "song_title": "{placeholder_title}",']
    for field in song_example_fields:
        if field in ARRAY_FIELDS:
            lines.append(f'{indent}  "{field}": [{{"value": "...", "page_number": 1}}],')
        else:
            lines.append(f'{indent}  "{field}": {{"value": "...", "page_number": 1}},')
    # is_rate_explicit is always the last key — no trailing comma
    lines.append(f'{indent}  "is_rate_explicit": true')
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def build_extraction_prompt(pages_text):
    """Single-pass prompt: identify producers + songs and extract all fields.

    Phases:
      1   — Identify all producer parties
      1.5 — Identify all songs/masters covered by the agreement
      2   — Extract universal fields (once for the whole agreement)
      3   — Extract producer-specific fields for each producer
      4   — Extract song-specific fields for each song

    Args:
        pages_text: dict of {page_num: [lines]} from Textract
    """
    universal_field_text = _format_field_list(
        [f for f in UNIVERSAL_FIELDS if f in field_descriptions_details]
    )
    producer_field_text = _format_field_list(
        [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
    )
    song_field_text = _format_field_list(
        [f for f in SONG_FIELDS if f in field_descriptions_details]
    )
    contract_text = _format_contract_text(pages_text)
    array_fields_list = ", ".join(f'"{f}"' for f in sorted(ARRAY_FIELDS)) or "none"

    producer_example_1 = _build_producer_example("<First Producer Name>")
    producer_example_2 = _build_producer_example("<Second Producer Name>")
    song_example_1 = _build_song_example("<Song Title 1>")
    song_example_2 = _build_song_example("<Song Title 2>")

    return f"""You are an intelligent document analysis agent. Extract structured data from a music royalty contract in a single pass.

Process the document in the following order:

PHASE 1 — IDENTIFY PRODUCER PARTIES
-----------------------------------
Before extracting any fields, scan the opening paragraph and signature block to identify every producer who is a named party to this agreement.

Look for:
- Names or entities followed by ("Producer"), ("you"), or defined as the letter's addressee
- Multiple C/O entries tied to different producer or company names
- Language like "each of you", "collectively", or a list of named co-producers
- Entities signing as producers in the signature block

Producer identification rules:
- Use the EXACT name as written in the document — never paraphrase or abbreviate
- Always identify at least one producer; use "Producer" only if no name can be found
- NEVER include the Artist, Label, Counterparty/Publisher, or Distributor as a producer
- If multiple entities share one producer role (e.g. a joint venture), treat them as one combined entry
- The "producers" array in your output must contain EXACTLY one entry per producer you identify here

PHASE 1.5 — IDENTIFY ALL SONGS
--------------------------------
Scan the entire document to identify every song/master recording covered by this agreement.

Look for:
- Song titles in the opening paragraph (e.g., "master recording entitled 'REAL LOVE'")
- Schedules, appendices, or tables listing compositions (Schedule A, Schedule 1, "List of Masters", etc.)
- ISRC codes or track numbers associated with song names
- Language like "the following compositions:", "masters listed herein:", or "as set forth in Schedule"

Song identification rules:
- Use the EXACT title as written in the document — never paraphrase or abbreviate
- Always identify at least one song
- The "songs" array in your output must contain EXACTLY one entry per song identified

PHASE 2 — UNIVERSAL FIELDS (extract once for the whole agreement)
-----------------------------------------------------------------
{universal_field_text}

PHASE 3 — PRODUCER-SPECIFIC FIELDS (extract for EACH producer)
--------------------------------------------------------------
{producer_field_text}

Producer-specific rules:
- Create one entry in the "producers" array for EACH producer identified in Phase 1
- If a term applies identically to all producers (e.g. a shared royalty clause), DUPLICATE the value into every producer's entry — never use a shared object
- If a term exists for one producer but not another, return {{"value": "not found", "page_number": null}} for the absent producer
- Include ALL producer-specific fields for every producer, even when the value is "not found"

PHASE 4 — SONG-SPECIFIC FIELDS (extract for EACH song)
-------------------------------------------------------
{song_field_text}

Song-specific rules:
- Create one entry in the "songs" array for EACH song identified in Phase 1.5
- Set "is_rate_explicit": true if the field value is explicitly stated for this specific song (e.g. per-track schedule or table with individual rates)
- Set "is_rate_explicit": false if the value comes from a blanket clause applying to all songs — still populate all fields with those blanket values
- If a blanket value applies to all songs, DUPLICATE it into every song's entry — never use a shared object
- Include ALL song-specific fields for every song, even when the value is "not found"

EXTRACTION RULES (apply to every field)
---------------------------------------
1. Values must be VERBATIM from the document — never paraphrase, summarize, or interpret
2. Found field:   {{"value": "exact text from document", "page_number": X}}
3. Missing field: {{"value": "not found", "page_number": null}}
4. Array-type fields ({array_fields_list}):
   - Return a JSON array of individual verbatim entries: [{{"value": "...", "page_number": X}}, ...]
   - One entry per distinct occurrence in the document
   - Return [] if none found
   - NEVER combine multiple values into a single string
5. NEVER invent, infer, or guess values not explicitly present in the document
6. NEVER omit a field — every listed field must appear in the output
7. Return valid JSON ONLY — no Markdown fences, no explanation, no preamble

REQUIRED OUTPUT FORMAT
----------------------
{{
  "Document Name": {{"value": "...", "page_number": 1}},
  "Execution Status": {{"value": "...", "page_number": 1}},
  "Song Title": {{"value": "...", "page_number": 1}},
  "Artist Name": {{"value": "...", "page_number": 1}},
  "Single/Multisong Line": {{"value": "...", "page_number": 1}},
  "Direct Counterparty": {{"value": "...", "page_number": 1}},
  "Alternative Counterparties": {{"value": "...", "page_number": 1}},
  "Effective Date": {{"value": "...", "page_number": 1}},
  "Distributor": {{"value": "...", "page_number": 1}},
  "Label": {{"value": "...", "page_number": 1}},
  "Organization Counting Units": {{"value": "...", "page_number": 1}},
  "producers": [
{producer_example_1},
{producer_example_2}
  ],
  "songs": [
{song_example_1},
{song_example_2}
  ]
}}

The "producers" array must contain exactly one entry per producer identified in Phase 1.
The "songs" array must contain exactly one entry per song identified in Phase 1.5.
Single-producer agreements have exactly one producer entry. Single-song agreements have exactly one song entry — do not add placeholder entries.

CONTRACT TEXT
-------------
\"\"\"
{contract_text}
\"\"\"

Always respond with valid JSON only. Do not include Markdown formatting or explanation.
""".strip()
