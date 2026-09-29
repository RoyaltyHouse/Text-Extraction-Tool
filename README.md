# Contract Parser

Flask API on AWS Lambda that extracts structured fields from music royalty contracts. AWS Textract reads the PDF, an LLM (via OpenRouter) extracts the fields, and every value comes back with the page and bounding boxes needed to highlight it in the source document.

## How it works

1. **OCR**: Textract (FORMS + SIGNATURES) returns numbered lines with word boxes, plus detected signatures and form key/value pairs, each pinned to its nearest line (`extractor.py`).
2. **Signature blocks**: signature-related annotations are clustered by position into one block per signing party (`signature_blocks.py`). A block counts as signed only when Textract detected a signature in it. Blocks on SoundExchange / Letter of Direction pages or DocuSign audit pages are excluded.
3. **Extraction**: one prompt (`prompt.py`) combines the extraction rules, the field descriptions (`field_descriptions.json`), the numbered contract text and the signature blocks. The model returns agreement-wide fields, per-producer fields, per-song fields and an advance mapping, each with the line numbers it read the value from.
4. **Reconciliation**: the blocks decide how many parties there are and whether each signed. The model only names them. Execution Status (`FX` fully / `PX` partially / `NX` not executed) is computed from the blocks.
5. **Coordinates**: each value is matched to the words on its cited lines. If that fails, the search widens by two lines; if that also fails, the cited lines are highlighted whole. The matched words are merged into one box per line (`gpt_extractor.py`).

## API

### Async jobs (used by RoyaltyHouse)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/presigned-upload-url` | `{"filename": "x.pdf", "content_type": "application/pdf"}` | `{"upload_url", "s3_key"}`. PUT the PDF to `upload_url`. |
| POST | `/extract_from_url?artist_id=&original_document_id=` | `{"s3_key": "..."}` or `{"url": "..."}` | `202 {"job_id", "status": "pending"}` |
| GET | `/result/<job_id>` | | Job record: `status` is `pending`, `processing`, `done` or `failed`. `result.preview` holds the fields when done; `error` holds the traceback when failed. |

`url` can be a direct PDF link or a Google Drive share link. Jobs run in a background invocation of the same Lambda, outside API Gateway's 29 s limit.

### Synchronous

| Method | Path | Notes |
|---|---|---|
| POST | `/extract?artist_id=&original_document_id=` | Multipart, one or more `file` fields. Each PDF is parsed separately. JPEG/PNG images are parsed together as one document, a page per image, without coordinates. |
| GET | `/get_fields` | Field descriptions. |
| GET | `/max` | Health check. |

## Result format

```json
{
  "Effective Date": {"value": "July 16, 2026", "lines": [3], "page_number": 1,
                     "coords": [{"Left": 0.52, "Top": 0.11, "Width": 0.12, "Height": 0.01}]},
  "signatures": [{"value": "Kings From Queens, LLC", "signed": true, "lines": [120, 121], "page_number": 6, "coords": [...]}],
  "Execution Status": {"value": "FX", "lines": [120], "page_number": 6, "coords": [...]},
  "Advance Mapping": {"structure": "aggregate", "aggregate_total": "$10,000", "entries": [...], "lines": [...]},
  "producers": [{"producer_name": "...", "Client Party": {...}, "Producer Royalty Points": [{...}], ...}],
  "songs": [{"song_title": "...", "is_rate_explicit": true, "advance_scope": "agreement", ...}]
}
```

- `coords` holds one box per line the value spans, normalized 0–1 to the page (`Left`/`Top` from the top-left corner). It is `null` when the value can't be located.
- Missing values are `"not found"`. Fields marked `"is_array": true` in `field_descriptions.json` return a list of `{value, lines}` entries.

## Fields

- `field_descriptions.json` holds each field's extraction instructions.
- The lists in `prompt.py` set which fields are requested and at what level: universal, per producer or per song.
- Fields whose output isn't a plain `{value, lines}` have their output example in `_SHAPES`.

## Configuration

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key |
| `OPENROUTER_DEFAULT_MODEL` | Model slug, default `openai/gpt-5.4-mini` |
| `BUCKET_NAME` | S3 bucket for uploads, Textract input and job records (`jobs/<id>.json`) |
| `LAMBDA_FUNCTION_ARN` | Function that runs background jobs; the deploy workflow sets it |

## Deployment

Every push to `main` deploys through GitHub Actions (`.github/workflows/lambda_function.yaml`). The workflow zips the code, uploads it to the `extract-tool-api` Lambda and sets a 300 s timeout. Python dependencies come from a Lambda layer, not from `requirements.txt`.

## Local development

```bash
pip install -r requirements.txt
python app.py        # http://127.0.0.1:5000
```

Put `OPENROUTER_API_KEY` in `.env`. You also need AWS credentials that can reach the bucket, Textract and Lambda.

Locally, `/extract_from_url` still hands jobs to the deployed Lambda (`LAMBDA_FUNCTION_ARN`). Use `/extract` to run the whole pipeline on your machine.

## Tests

```bash
python tests/test_signature_blocks.py
```

## Limitations

- Background jobs accept PDFs only. Images are only supported through `/extract`, and they get no coordinates.
- Textract gets 115 s. The model call gets whatever time is left before the Lambda's 300 s timeout, up to 180 s, and is never retried. A slow or failed model call fails the job rather than leaving it stuck in `processing`.
- Unhandled errors return `500 {"error", "type"}`, with the traceback written to CloudWatch.
- `/extract` goes through API Gateway, which means a 29 s timeout and roughly a 4.5 MB file limit. Use the presigned-upload flow for real documents.

## License

MIT
