import json
from typing import Any, Dict, Optional

def convert_extracted_to_db(
    extracted: Dict[str, Dict[str, Any]],
    file_name: str,
    artist_id,original_document_id
) -> Dict[str, Any]:
    """
    Convert extracted fields like:
      {"Artist Name": {"page_number": 1, "value": "A$AP Mob"}, ...}
    into your DB schema with *_page and *_value fields.
    """

    FIELD_MAP = {
        "Alternative Counterparties": "alternative_counterparties",
        "Artist Name": "artist_name",
        "Classification of Recoupment Language": "classification_of_recoupment_language",
        "Client Party": "client_party",
        "Direct Counterparty": "direct_counterparty",
        "Distributor": "distributor",
        "Document Name": "document_name",
        "Effective Date": "effective_date",
        "Execution Status": "execution_status",
        "Label": "label",
        "Lawyer Information": "lawyer_information",
        "Legal Advance": "legal_advance",
        "Organization Counting Units": "organization_counting_units",
        "Producer Advance Legal Recoupment": "producer_advance_legal_recoupment",
        "Producer Royalty Points": "producer_royalty_points",
        "Recoupment Classification": "recoupment_classification",
        "Single/Multisong Line": "single_multisong_line",
        "Song Title": "song_title",
        "Third Party Money": "third_party_money",
        "Type of Royalty": "type_of_royalty",
    }

    def normalize_value(val: Any) -> Optional[str]:
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val) if val is not None else None

    payload: Dict[str, Optional[Any]] = {
        "file": file_name,
        "artist_id":artist_id,
        "original_document_id":original_document_id
    }

    for base in FIELD_MAP.values():
        payload[f"{base}_page"] = None
        payload[f"{base}_value"] = None

    for label, base in FIELD_MAP.items():
        item = extracted.get(label)
        if not item:
            continue
        payload[f"{base}_page"] = item.get("page_number")
        payload[f"{base}_value"] = normalize_value(item.get("value"))

    return payload



def convert_db_to_extracted(db_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Convert DB payload like:
        {"artist_name_page": 1, "artist_name_value": "A$AP Mob", ...}
    back into extracted format like:
        {"Artist Name": {"page_number": 1, "value": "A$AP Mob"}, ...}
    """

    FIELD_MAP = {
        "alternative_counterparties": "Alternative Counterparties",
        "artist_name": "Artist Name",
        "classification_of_recoupment_language": "Classification of Recoupment Language",
        "client_party": "Client Party",
        "direct_counterparty": "Direct Counterparty",
        "distributor": "Distributor",
        "document_name": "Document Name",
        "effective_date": "Effective Date",
        "execution_status": "Execution Status",
        "label": "Label",
        "lawyer_information": "Lawyer Information",
        "legal_advance": "Legal Advance",
        "organization_counting_units": "Organization Counting Units",
        "producer_advance_legal_recoupment": "Producer Advance Legal Recoupment",
        "producer_royalty_points": "Producer Royalty Points",
        "recoupment_classification": "Recoupment Classification",
        "single_multisong_line": "Single/Multisong Line",
        "song_title": "Song Title",
        "third_party_money": "Third Party Money",
        "type_of_royalty": "Type of Royalty",
    }

    extracted: Dict[str, Dict[str, Any]] = {}

    for base, label in FIELD_MAP.items():
        page_key = f"{base}_page"
        value_key = f"{base}_value"

        if page_key not in db_payload and value_key not in db_payload:
            continue

        page_number = db_payload.get(page_key)
        value = db_payload.get(value_key)

        # Try to load JSON if the value is a serialized dict/list
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                value = parsed
            except (json.JSONDecodeError, TypeError):
                pass

        extracted[label] = {
            "page_number": page_number,
            "value": value
        }

    return extracted