from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pathlib import Path
import pdfplumber
import io
import re
import os
from typing import List, Optional
from supabase import create_client, Client
from dotenv import load_dotenv

# Ensure .env is always loaded from the Backend directory
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialize Supabase client with administrative service role key
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="GradeFlow API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------
# Fix for Swagger UI file picker bug in newer FastAPI versions (0.129.1+)
# Forces OpenAPI schema to output format="binary" so Swagger renders 
# the interactive "Choose Files" button instead of a plain text box.
# ---------------------------------------------------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description="GradeFlow Academic Processing API",
        routes=app.routes,
    )

    # 1. Fix schema components
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    for model_def in schemas.values():
        properties = model_def.get("properties", {})
        for prop in properties.values():
            if prop.get("contentMediaType") == "application/octet-stream":
                prop.pop("contentMediaType", None)
                prop["format"] = "binary"
                prop["type"] = "string"
            if prop.get("type") == "array" and "items" in prop:
                if prop["items"].get("contentMediaType") == "application/octet-stream":
                    prop["items"].pop("contentMediaType", None)
                    prop["items"]["format"] = "binary"
                    prop["items"]["type"] = "string"

    # 2. Fix inline path request bodies
    for path_item in openapi_schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict):
                request_body = operation.get("requestBody", {})
                content = request_body.get("content", {})
                for media_type_obj in content.values():
                    media_schema = media_type_obj.get("schema", {})
                    for prop in media_schema.get("properties", {}).values():
                        if prop.get("contentMediaType") == "application/octet-stream":
                            prop.pop("contentMediaType", None)
                            prop["format"] = "binary"
                            prop["type"] = "string"
                        if prop.get("type") == "array" and "items" in prop:
                            if prop["items"].get("contentMediaType") == "application/octet-stream":
                                prop["items"].pop("contentMediaType", None)
                                prop["items"]["format"] = "binary"
                                prop["items"]["type"] = "string"

    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi


def ensure_storage_bucket(bucket_name: str = "transcripts"):
    """
    Guarantees the storage bucket exists in Supabase.
    """
    try:
        supabase.storage.get_bucket(bucket_name)
    except Exception:
        try:
            supabase.storage.create_bucket(bucket_name, options={"public": True})
            print(f"[Supabase Storage] Created bucket '{bucket_name}'.")
        except Exception as e:
            # Bucket may already exist or have custom permissions
            pass


@app.get("/wakeup")
def wake_up():
    return {"status": "Server is awake and ready."}

@app.get("/")
def read_root():
    return {"message": "Welcome to the GradeFlow API! Go to /docs for the API documentation."}

@app.get("/api/student/{registration_number}")
def get_student_records(registration_number: str):
    """
    Checks Supabase database for existing academic records and upload history
    for this student using B-Tree index on registration_number for O(log N) retrieval.
    """
    clean_reg = registration_number.strip()
    try:
        # 1. Layer C: Query academic grades via indexed registration_number
        grades_response = supabase.table("student_grades") \
            .select("*") \
            .eq("registration_number", clean_reg) \
            .execute()
        records = grades_response.data if grades_response and hasattr(grades_response, 'data') else []

        # 2. Layer B: Query upload history via indexed registration_number
        upload_history = []
        try:
            files_response = supabase.table("uploaded_files") \
                .select("*") \
                .eq("registration_number", clean_reg) \
                .order("uploaded_at", desc=True) \
                .execute()
            if files_response and hasattr(files_response, 'data'):
                upload_history = files_response.data
        except Exception as file_err:
            print(f"[Notice] Failed to fetch upload history: {file_err}")

        # 3. Calculate instant performance metrics on the fly
        grade_points_map = {
            "S": 10.0, "A": 9.0, "B": 8.0, "C": 7.0, "D": 6.0, "E": 5.0, "F": 0.0
        }
        total_credits = 0.0
        weighted_points = 0.0
        passed_count = 0
        backlog_count = 0

        for r in records:
            c = float(r.get("credits") or 0.0)
            g = str(r.get("grade") or "").strip().upper()
            pt = grade_points_map.get(g, 0.0)
            total_credits += c
            weighted_points += (c * pt)
            if g == "F" or pt == 0.0:
                backlog_count += 1
            else:
                passed_count += 1

        sgpa = round(weighted_points / total_credits, 2) if total_credits > 0 else 0.0

        return {
            "found": len(records) > 0,
            "registration_number": clean_reg,
            "total_records": len(records),
            "metrics": {
                "total_credits": total_credits,
                "weighted_points": round(weighted_points, 1),
                "sgpa": sgpa,
                "passed_count": passed_count,
                "backlog_count": backlog_count
            },
            "data": records,
            "upload_history": upload_history
        }
    except Exception as e:
        print(f"Error checking student records: {e}")
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "data": [],
            "upload_history": [],
            "error": str(e)
        }

@app.post("/api/upload")
async def parse_transcript(
    files: List[UploadFile] = File(..., description="Select transcript files (.txt or .pdf)"),
    registration_number: Optional[str] = Form(None, description="Optional student registration number override")
):
    """
    Industry-Standard 3-Layer Document Pipeline:
    - Layer A: Raw file upload to Supabase Storage bucket ('transcripts/{registration_number}/{filename}')
    - Layer B: File metadata tracking in PostgreSQL table ('uploaded_files')
    - Layer C: Structured grade parsing and idempotent upsert into ('student_grades')
    """
    parsed_courses = []
    uploaded_files_summary = []
    
    # Pre-check storage bucket
    ensure_storage_bucket("transcripts")

    for file in files:
        filename = file.filename
        lower_name = filename.lower()
        if not (lower_name.endswith('.pdf') or lower_name.endswith('.txt')):
            continue 

        file_bytes = await file.read()
        extracted_text = ""
        
        # Parse content
        if lower_name.endswith('.pdf'):
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            extracted_text += text + "\n"
            except Exception as e:
                print(f"Skipping corrupted PDF {filename}: {e}")
                continue
        elif lower_name.endswith('.txt'):
            try:
                extracted_text = file_bytes.decode('utf-8', errors='ignore')
            except Exception:
                continue

        # Extract course metadata
        meta_pattern = r"Subject Code\s*:\s*([A-Z0-9]+).*?\nSubject Title\s*:\s*(.*?)\s*\nSubject Credit\s*:\s*([\d\.]+)"
        meta_match = re.search(meta_pattern, extracted_text)
        course_code = meta_match.group(1) if meta_match else "UNKNOWN"
        course_name = meta_match.group(2) if meta_match else "UNKNOWN"
        course_credits = float(meta_match.group(3)) if meta_match else 0.0

        # Extract student rows
        row_pattern = r"(\d{9})\s+(\d{1,2}\.\d{2})\s+([\d\.]+|AB|DT|MP)\s+(\d{1,3}\.\d{2})\s+([A-Z]{1,2})"
        file_student_reg = None

        for match in re.finditer(row_pattern, extracted_text):
            reg = match.group(1)
            if not file_student_reg:
                file_student_reg = reg
            course = {
                "registration_number": reg,
                "course_code": course_code,
                "course_name": course_name,
                "credits": course_credits,
                "total_marks": float(match.group(4)),
                "grade": match.group(5)
            }
            parsed_courses.append(course)

        # Determine effective registration number for Layer A & B storage
        target_reg = registration_number.strip() if registration_number else (file_student_reg or "general")
        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
        storage_path = f"{target_reg}/{safe_filename}"
        mime_type = file.content_type or ("application/pdf" if lower_name.endswith('.pdf') else "text/plain")

        # Layer A: Store original raw document in Supabase Storage
        file_url = f"{SUPABASE_URL}/storage/v1/object/public/transcripts/{storage_path}"
        try:
            supabase.storage.from_("transcripts").upload(
                path=storage_path,
                file=file_bytes,
                file_options={"content-type": mime_type, "upsert": "true"}
            )
            try:
                url_resp = supabase.storage.from_("transcripts").get_public_url(storage_path)
                if isinstance(url_resp, str):
                    file_url = url_resp
                elif isinstance(url_resp, dict) and "publicUrl" in url_resp:
                    file_url = url_resp["publicUrl"]
            except Exception:
                pass
        except Exception as storage_err:
            print(f"[Supabase Storage Notice]: {storage_err}")

        # Layer B: Record upload metadata in uploaded_files table
        file_record = {
            "registration_number": target_reg,
            "file_name": filename,
            "file_url": file_url,
            "file_size": len(file_bytes),
            "mime_type": mime_type
        }
        try:
            supabase.table("uploaded_files").insert(file_record).execute()
        except Exception as meta_err:
            print(f"[Uploaded Files Notice]: {meta_err}")

        uploaded_files_summary.append(file_record)
    
    # Layer C: Idempotent Upsert into student_grades table (prevents duplicate course spam)
    if parsed_courses:
        try:
            supabase.table("student_grades").upsert(
                parsed_courses,
                on_conflict="registration_number,course_code"
            ).execute()
        except Exception as upsert_err:
            print(f"[Upsert Notice, attempting standard insert]: {upsert_err}")
            try:
                supabase.table("student_grades").insert(parsed_courses).execute()
            except Exception as insert_err:
                print(f"[Database Insertion Error]: {insert_err}")

    return {
        "total_records_found": len(parsed_courses),
        "data": parsed_courses,
        "uploaded_files": uploaded_files_summary
    }
