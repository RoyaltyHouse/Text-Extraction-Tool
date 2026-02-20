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


def build_detection_prompt(pages_text):
    """Pass 1 prompt: identify all producers party to this agreement.

    Limits to the first 3 pages since producer names always appear in the
    opening paragraph, keeping the detection call fast and cheap.
    """
    first_pages = dict(list(pages_text.items())[:3])
    contract_text = _format_contract_text(first_pages)

    return f"""
You are analyzing the opening section of a music royalty contract to identify every producer who is a named party.

Look for:
- Names or entities followed by ("Producer"), ("you"), or defined as the addressee in the opening paragraph
- Multiple C/O entries tied to different producer or company names
- Language like "each of you", "collectively", or a list of named co-producers
- Entities signing as producers in the signature block (if visible)

Return ONLY a JSON object with a single key "producers" whose value is a list of producer name strings.
If only one producer is present, still return a list with one entry.
If you cannot determine the name, use "Producer" as a placeholder.

Example:
{{"producers": ["DJ Beats", "SuperProducer"]}}

Do not include any explanation. Return valid JSON only.

### Contract Text (first pages only):
\"\"\"
{contract_text}
\"\"\"
""".strip()


def build_final_document_prompt(pages_text, producers):
    """Pass 2 prompt: full field extraction with named producers.

    Universal fields are extracted once. Producer-specific fields are
    extracted per producer — values are duplicated if terms are shared.

    Args:
        pages_text: dict of {{page_num: [lines]}}
        producers:  list of producer name strings from the detection pass
    """
    universal_field_text = _format_field_list(
        [f for f in UNIVERSAL_FIELDS if f in field_descriptions_details]
    )
    producer_field_text = _format_field_list(
        [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
    )
    contract_text = _format_contract_text(pages_text)

    producer_count = len(producers)
    producer_list_str = ", ".join(f'"{p}"' for p in producers)
    array_fields_list = ", ".join(f'"{f}"' for f in sorted(ARRAY_FIELDS)) or "none"

    if producer_count > 1:
        producer_instruction = f"""This agreement involves {producer_count} producers: {producer_list_str}.

For EACH producer, extract their individual values for every Producer-Specific Field listed below.
- If a term applies identically to all producers (e.g. a shared royalty clause), duplicate the same value into each producer's entry — do not use a shared object.
- If a term is only found for one producer and not another, return "not found" for the producer where it is absent.
- Always include all Producer-Specific Fields for every producer, even if the value is "not found"."""
    else:
        producer_instruction = f"""This agreement involves one producer: {producer_list_str}.
Extract Producer-Specific Fields for that producer."""

    # Build a concrete output example using the actual producer names
    producer_example_entries = []
    for producer_name in producers:
        entry_lines = [f'    {{', f'      "producer_name": "{producer_name}",']
        producer_example_fields = [f for f in PRODUCER_FIELDS if f in field_descriptions_details]
        for field in producer_example_fields:
            if field in ARRAY_FIELDS:
                entry_lines.append(f'      "{field}": [{{"value": "...", "page_number": 1}}],')
            else:
                entry_lines.append(f'      "{field}": {{"value": "...", "page_number": 1}},')
        # Remove trailing comma from last field line
        entry_lines[-1] = entry_lines[-1].rstrip(",")
        entry_lines.append("    }")
        producer_example_entries.append("\n".join(entry_lines))
    producers_example = ",\n".join(producer_example_entries)

    return f"""
You are an intelligent document analysis agent tasked with extracting specific field values from a music royalty contract.

Below are the fields you must extract. Each field includes a detailed description of what it means and how to identify it.

Your task is:
- Read all pages of the contract (provided below).
- For each field:
    - Use the field description as instruction to identify the correct value.
    - If you are confident the value is found, return:
        {{
          "value": "actual value here",
          "page_number": X,
                    
        }}
    - If not found in the entire document, return:
        {{
          "value": "not found",
          "page_number": null
        }}


Return the results as a single JSON object (one key per field) only after the last page is processed.

---

### Universal Fields (extract once for the whole agreement):
{universal_field_text}

---

### Producer-Specific Fields (extract separately for each producer):
{producer_field_text}

---

### Producer Context:
{producer_instruction}

---

### Extraction Rules:
- For every field, if the value is found return:
    {{"value": "actual value here", "page_number": X}}
- If not found in the entire document, return:
    {{"value": "not found", "page_number": null}}
- For fields marked as array-type ({array_fields_list}): return an ARRAY of individual
  verbatim entries (one per occurrence), each as {{"value": "...", "page_number": X}}.
  Return [] if none found. Do NOT combine multiple values into a single string.
- Return valid JSON only. No Markdown, no explanation.

---

### Required Output Format:
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
{producers_example}
  ]
}}

---

### Contract Text:
\"\"\"
{contract_text}
\"\"\"

Always respond with valid JSON only. Do not include Markdown formatting or explanation.
""".strip()