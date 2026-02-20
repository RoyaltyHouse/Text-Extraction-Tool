import json



# Load field descriptions from JSON file
with open("field_descriptions.json", "r", encoding="utf-8") as f:
    field_descriptions_details = json.load(f)



def build_final_document_prompt(pages_text: dict) -> str:
    field_list_text = "\n".join(
        [f"{i+1}. **{name}**\n   {desc}" for i, (name, desc) in enumerate(field_descriptions_details.items())]
    )

    # Prepare contract text by adding page number headers
    contract_text = "\n".join(
        [f"\n--- Page {page_num} ---\n{text}" for page_num, text in pages_text.items()]
    )

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

### Fields to Extract:
{field_list_text}

---

### Contract Text:
\"\"\"
{contract_text}
\"\"\"

---

### Output Format (after the last page):
{{
  "Document Name": {{
    "value": "...",
    "page_number": 2
  }},
  "Execution Status": {{
    "value": "...",
    "page_number": 4
  }},
  ...
  "International Sales Policy": {{
    "value": "not found",
    "page_number": null
  }}
}}
Always respond with valid JSON only.Do not include Markdown formatting or explanation.
"""