from dotenv import load_dotenv
from prompt import build_final_document_prompt
import os
import json
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY2"))

def find_evidence_location(evidence_text, page_blocks):
    """Find the bounding box for the given evidence text in the page blocks."""
    if not evidence_text or not page_blocks:
        return None

    evidence_lower = evidence_text.lower().strip()
    best_match = None
    best_score = 0

    for block in page_blocks:
        block_text = block.get("text", "").lower().strip()

        # Skip empty blocks
        if not block_text:
            continue

        # Check for exact substring match
        if evidence_lower in block_text:
            score = len(evidence_lower) / len(block_text)
            if score > best_score:
                best_score = score
                best_match = block
        # Check for reverse match (block is substring of evidence)
        elif block_text in evidence_lower and len(block_text) > 10:
            score = len(block_text) / len(evidence_lower)
            if score > best_score:
                best_score = score
                best_match = block

    if best_match and best_match.get("bbox"):
        return best_match["bbox"]

    return None

def extract_field_information(page_text, page_blocks=None):
    prompt = build_final_document_prompt(page_text)
    print(f"[DEBUG] Sending prompt to OpenAI: {prompt[:100]}...")

    response = client.chat.completions.create(
        model="gpt-4-1106-preview",
        messages=[
            {"role": "system", "content": "You are an intelligent document extraction assistant."},
            {"role": "user", "content": prompt.strip()}
        ],
        temperature=0.2
    )

    content = response.choices[0].message.content
    print(f"[DEBUG] Received response from OpenAI: {content.strip()}")

    if content.strip().startswith('```'):
        lines = content.strip().splitlines()
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].startswith('```'):
            lines = lines[:-1]
        content = '\n'.join(lines)

    try:
        extracted_fields = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse GPT response as JSON: {str(e)}")
        print(f"[ERROR] Raw content: {content}")
        return {"error": f"Failed to parse extraction results: {str(e)}"}

    if page_blocks:
        try:
            for field_name, field_data in extracted_fields.items():
                if isinstance(field_data, dict) and field_data.get("value") and field_data.get("value") != "not found":
                    page_num = field_data.get("page_number")
                    value = field_data.get("value")

                    if not isinstance(value, str):
                        continue
                    if page_num is None or not isinstance(page_num, int):
                        continue

                    blocks_on_page = page_blocks.get(page_num, [])
                    bbox = find_evidence_location(value, blocks_on_page)
                    field_data["coords"] = bbox
        except Exception as e:
            print(f"[WARNING] Failed to add coordinates: {str(e)}")

    return extracted_fields
