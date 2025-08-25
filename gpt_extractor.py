from dotenv import load_dotenv
from prompt import build_final_document_prompt
import os
import json
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extract_field_information(page_text):
    prompt=build_final_document_prompt(page_text)
    print(f"[DEBUG] Sending prompt to OpenAI: {prompt[:100]}...")  # Limit to first 100 characters for debug
    # Call OpenAI API to extract field information
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
    # Remove code block markers if present
    

    if content.strip().startswith('```'):
        lines = content.strip().splitlines()
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].startswith('```'):
            lines = lines[:-1]
        content = '\n'.join(lines)
    
    return json.loads(content)

