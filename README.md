# Contract Parser

A Flask-based API for extracting structured information from PDF documents (music royalty contracts) using OpenAI GPT and custom field logic.

## Features
- Upload PDF files and extract structured text fields
- Extract bounding box coordinates for each field (enables accurate PDF highlighting)
- Extract from direct file upload or from a file URL (Google Drive supported)
- Customizable field extraction using OpenAI GPT
- S3-based file storage with AWS Textract integration
- Supports both local development and AWS Lambda deployment

## Requirements
- Python 3.8+
- OpenAI API key
- AWS S3 bucket configured

## Installation

### For Local Development:

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd Text Extraction Tool
   ```

2. **Install local dependencies:**
   ```bash
   pip install -r requirements-local.txt
   ```

3. **Set up environment variables:**
   - Create a `.env` file in the project root with your OpenAI API key:
     ```env
     OPENAI_API_KEY2=your_openai_api_key_here
     ```

4. **Configure AWS S3:**
   - Ensure your AWS credentials are configured
   - Update the `S3_BUCKET` variable in `app.py` with your bucket name

5. **Run locally:**
   ```bash
   python app.py
   ```

### For AWS Lambda Deployment:

1. **Use the Lambda layer** (`lambda-layer-final-working.zip`) that contains all dependencies
2. **Upload the Lambda function code** (without local dependencies)
3. **Add environment variables** in Lambda console:
   - `OPENAI_API_KEY2`: Your OpenAI API key

## Running the App

### Local Development:
```bash
python app.py
```
The Flask server will start on `http://127.0.0.1:5000/` by default.

### Lambda Deployment:
- Deploy using the provided Lambda layer
- Configure API Gateway for HTTP endpoints

## API Endpoints

### 1. Upload a PDF file
- **POST** `/extract`
- Form-data: `file` (PDF)
- Example query params: `artist_id`, `original_document_id`
- Returns: Extracted fields with bounding box coordinates for PDF highlighting

**Response format:**
```json
{
  "Artist Name": {
    "value": "A$AP Mob",
    "page_number": 1,
    "coords": {
      "Left": 0.1234,
      "Top": 0.2567,
      "Width": 0.3456,
      "Height": 0.0234
    }
  }
}
```

### 2. Extract from a file URL
- **POST** `/extract_from_url`
- JSON body: `{ "url": "<file_url>" }`
- Query params: `artist_id`, `original_document_id`
- Returns: Same format as above with coordinates

### 3. Get field descriptions
- **GET** `/get_fields`
- Returns: JSON of all field descriptions

### 4. Add or update a field description
- **POST** `/add_field`
- JSON body: `{ "field": "Field Name", "value": "Description" }`

### 5. Delete a field description
- **DELETE** `/delete_field/<field_key>`

## Development vs Production

### Local Development:
- Uses `requirements-local.txt` with `awsgi` for local testing
- Runs Flask development server
- Full debugging capabilities

### Lambda Production:
- Uses Lambda layer with `aws-wsgi` (Lambda-compatible)
- No local dependencies needed
- Optimized for serverless execution

## Known Limitations
- **API Gateway timeout**: REST API has a hard 29-second timeout. Documents that take longer to process through Textract + OpenAI will return a 504 Gateway Timeout. For longer processing times, consider switching to Lambda function URLs (up to 15 min timeout).
- **File upload size**: Direct uploads via `/extract` are base64-encoded through API Gateway, so PDFs over ~4.5 MB will exceed Lambda's 6 MB payload limit. Use `/extract_from_url` for larger files.
- **Multi-line coordinate matching**: Bounding box coordinates match a single Textract LINE block. Fields spanning multiple lines return the best-matching single line's bbox. When a value appears multiple times on the same page, the system uses proximity to the field label to disambiguate.

## Notes
- Only PDF files are currently supported for extraction (images supported via Textract but without coordinate data).
- Requires a valid OpenAI API key (`OPENAI_API_KEY2` environment variable).
- Requires AWS Textract for OCR and bounding box extraction.
- Files are temporarily stored in AWS S3 bucket for Textract processing.
- Field descriptions are stored locally in `field_descriptions.json`.
- Coordinates are normalized (0.0-1.0 range) - multiply by page dimensions to get pixel coordinates.
   - `Left`: Distance from left edge (0.0 = left, 1.0 = right)
   - `Top`: Distance from top edge (0.0 = top, 1.0 = bottom)
   - `Width`: Width as percentage of page width
   - `Height`: Height as percentage of page height

## License
MIT 