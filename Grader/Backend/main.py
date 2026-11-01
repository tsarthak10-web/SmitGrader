from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pathlib import Path
import pdfplumber
import io
import re
import os
from typing import List, Optional, Dict, Any
from supabase import create_client, Client
from dotenv import load_dotenv

# Ensure .env is always loaded from the Backend directory
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Safe initialization of Supabase client to prevent boot crashes on Render
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[Supabase] Successfully connected to database.")
    except Exception as e:
        print(f"[Supabase Warning] Failed to initialize client: {e}")
else:
    print("[Supabase Notice] SUPABASE_URL or SUPABASE_KEY environment variable is missing on host environment.")

app = FastAPI(title="GradeFlow API", version="1.0.0")

# Allow all origins, including any Netlify domain and local environments
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",
        "https://gradersmit.netlify.app",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"https://.*\.netlify\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------
# Fix for Swagger UI file picker bug in newer FastAPI versions (0.129.1+)
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
    """Guarantees the storage bucket exists in Supabase."""
    if not supabase:
        return
    try:
        supabase.storage.get_bucket(bucket_name)
    except Exception:
        try:
            supabase.storage.create_bucket(bucket_name, options={"public": True})
            print(f"[Supabase Storage] Created bucket '{bucket_name}'.")
        except Exception:
            pass


def parse_page_text_for_metadata(page_text: str):
    """
    Extracts Subject Code, Subject Title, and Credits from page header text.
    Handles flexible spacing, casing, and separators.
    """
    meta_pattern = r"Subject Code\s*[:\-]?\s*([A-Z0-9]+).*?(?:Subject Title\s*[:\-]?\s*(.*?))?(?:Subject Credit\s*[:\-]?\s*([\d\.]+)|Credit\s*[:\-]?\s*([\d\.]+))"
    match = re.search(meta_pattern, page_text, re.IGNORECASE | re.DOTALL)
    if not match:
        # Fallback simpler regex
        code_m = re.search(r"Subject Code\s*[:\-]?\s*([A-Z0-9]+)", page_text, re.IGNORECASE)
        title_m = re.search(r"Subject Title\s*[:\-]?\s*([^\n\r]+)", page_text, re.IGNORECASE)
        cred_m = re.search(r"(?:Subject Credit|Credit)\s*[:\-]?\s*([\d\.]+)", page_text, re.IGNORECASE)
        c_code = code_m.group(1).strip() if code_m else None
        c_name = title_m.group(1).strip() if title_m else None
        c_cred = float(cred_m.group(1)) if cred_m else None
        return c_code, c_name, c_cred

    c_code = match.group(1).strip() if match.group(1) else None
    c_name = None
    if match.group(2):
        raw_name = match.group(2).strip()
        c_name = re.split(r'Subject Credit|Credit|\n', raw_name)[0].strip() or None
    c_cred = None
    cred_val = match.group(3) or match.group(4)
    if cred_val:
        try:
            c_cred = float(cred_val)
        except ValueError:
            pass
    return c_code, c_name, c_cred


def parse_document_bytes(file_bytes: bytes, filename: str, default_reg: Optional[str] = None) -> List[dict]:
    """
    Comprehensive document parser:
    1. Per-page / per-section Subject Code tracking (supports multi-subject/multi-page files)
    2. Class grade sheet student row parsing (RegNo, Internal, External, Total, Grade)
    3. Individual student transcript parsing fallback
    """
    lower_name = filename.lower()
    pages_text = []

    if lower_name.endswith('.pdf'):
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text and text.strip():
                        pages_text.append(text)
        except Exception as e:
            print(f"[Parser Warning] Error reading PDF {filename}: {e}")
            return []
    elif lower_name.endswith('.txt') or lower_name.endswith('.csv'):
        try:
            raw_text = file_bytes.decode('utf-8', errors='ignore')
            # Split sections if multiple subjects exist in text
            sections = re.split(r'(?=Subject Code\s*[:\-])', raw_text, flags=re.IGNORECASE)
            for s in sections:
                if s.strip():
                    pages_text.append(s)
        except Exception as e:
            print(f"[Parser Warning] Error reading text {filename}: {e}")
            return []

    parsed_courses = []
    current_code = "UNKNOWN"
    current_name = "UNKNOWN"
    current_credits = 4.0

    # Flexible row pattern for class grade sheet rows:
    # Match: RegNo (8-11 digits), Int marks/status, Ext marks/status, Total marks, Grade (S, A, B, C, D, E, F, etc.)
    row_pattern = r"(\d{8,11})\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+)\s+([A-Z\+]{1,3})"

    for p_idx, text in enumerate(pages_text):
        c_code, c_name, c_cred = parse_page_text_for_metadata(text)
        if c_code:
            current_code = c_code
        if c_name:
            current_name = c_name
        if c_cred is not None:
            current_credits = c_cred

        # 1. Check for class grade sheet rows
        page_matches = list(re.finditer(row_pattern, text))
        if page_matches:
            for m in page_matches:
                reg = m.group(1).strip()
                try:
                    tot_m = float(m.group(4))
                except ValueError:
                    tot_m = 0.0
                grade_val = m.group(5).strip().upper()

                parsed_courses.append({
                    "registration_number": reg,
                    "course_code": current_code,
                    "course_name": current_name,
                    "credits": current_credits,
                    "total_marks": tot_m,
                    "grade": grade_val
                })
        else:
            # 2. Check for individual student transcript format
            # e.g.: CS302 Computer Architecture 4.0 A
            indiv_reg_match = re.search(r"(?:Registration|Reg|Roll)\s*(?:No|Number)?\s*[:\-]?\s*(\d{8,11})", text, re.IGNORECASE)
            effective_reg = indiv_reg_match.group(1).strip() if indiv_reg_match else (default_reg or "general")

            indiv_pattern = r"([A-Z]{2,4}\d{2,4}[A-Z0-9]*)\s+([A-Za-z0-9\s&,\-\(\)]+?)\s+([\d\.]+)\s+([SABCDEF][\+]*)"
            for m in re.finditer(indiv_pattern, text):
                code = m.group(1).strip()
                name = m.group(2).strip()
                try:
                    cr = float(m.group(3))
                except ValueError:
                    cr = 4.0
                gr = m.group(4).strip().upper()

                parsed_courses.append({
                    "registration_number": effective_reg,
                    "course_code": code,
                    "course_name": name,
                    "credits": cr,
                    "total_marks": 0.0,
                    "grade": gr
                })

    return parsed_courses


def save_courses_to_db(courses: List[dict]):
    """
    Deduplicates courses by (registration_number, course_code) and
    safely upserts/inserts them into the Supabase 'student_grades' table.
    """
    if not courses or not supabase:
        return

    # In-memory deduplication: keep cleanest/latest record
    deduped: Dict[tuple, dict] = {}
    for c in courses:
        r = str(c.get("registration_number", "")).strip()
        code = str(c.get("course_code", "")).strip()
        if r and code:
            deduped[(r, code)] = c
    clean_list = list(deduped.values())

    # Chunked upsert/insert
    chunk_size = 50
    for i in range(0, len(clean_list), chunk_size):
        chunk = clean_list[i:i + chunk_size]
        try:
            supabase.table("student_grades").upsert(
                chunk,
                on_conflict="registration_number,course_code"
            ).execute()
        except Exception as upsert_err:
            # If unique constraint or on_conflict isn't defined, insert row-by-row gracefully
            for row in chunk:
                try:
                    supabase.table("student_grades").upsert(
                        row,
                        on_conflict="registration_number,course_code"
                    ).execute()
                except Exception:
                    try:
                        supabase.table("student_grades").insert(row).execute()
                    except Exception:
                        pass


@app.get("/wakeup")
def wake_up():
    return {
        "status": "Server is awake and ready.",
        "supabase_connected": supabase is not None
    }


@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "GradeFlow Academic Processing API",
        "docs": "/docs",
        "supabase_connected": supabase is not None
    }


@app.get("/api/student/{registration_number}")
def get_student_records(registration_number: str):
    """
    1. Queries Supabase 'student_grades' for this student.
    2. If not found, references previously uploaded class files to find and recover grades.
    3. Calculates real-time GPA and performance metrics.
    """
    clean_reg = registration_number.strip()
    if not supabase:
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "data": [],
            "upload_history": [],
            "notice": "Database connection pending environment variable configuration on Render."
        }
    try:
        # 1. Query student_grades table via indexed registration_number
        grades_response = supabase.table("student_grades") \
            .select("*") \
            .eq("registration_number", clean_reg) \
            .execute()
        records = grades_response.data if grades_response and hasattr(grades_response, 'data') else []

        # 2. If student is not yet in student_grades, reference already uploaded documents
        if len(records) == 0:
            try:
                files_resp = supabase.table("uploaded_files") \
                    .select("*") \
                    .order("uploaded_at", desc=True) \
                    .limit(20) \
                    .execute()
                uploaded_docs = files_resp.data if files_resp and hasattr(files_resp, 'data') else []

                newly_recovered = []
                for doc in uploaded_docs:
                    file_name = doc.get("file_name", "")
                    uploader_reg = doc.get("registration_number", "general")
                    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', file_name)

                    candidate_paths = [
                        f"{uploader_reg}/{safe_name}",
                        f"shared/{safe_name}",
                        safe_name
                    ]

                    file_bytes = None
                    for p in candidate_paths:
                        try:
                            file_bytes = supabase.storage.from_("transcripts").download(p)
                            if file_bytes:
                                break
                        except Exception:
                            continue

                    if file_bytes:
                        doc_courses = parse_document_bytes(file_bytes, file_name, clean_reg)
                        # Save entire class from this doc so all classmates get updated too
                        save_courses_to_db(doc_courses)
                        # Filter for target student
                        for dc in doc_courses:
                            if str(dc.get("registration_number", "")).strip() == clean_reg:
                                newly_recovered.append(dc)

                if newly_recovered:
                    # Deduplicate
                    d_map = {}
                    for nr in newly_recovered:
                        d_map[(nr["registration_number"], nr["course_code"])] = nr
                    records = list(d_map.values())
            except Exception as ref_err:
                print(f"[Reference Upload Notice]: {ref_err}")

        # 3. Query upload history
        upload_history = []
        try:
            hist_resp = supabase.table("uploaded_files") \
                .select("*") \
                .order("uploaded_at", desc=True) \
                .limit(10) \
                .execute()
            if hist_resp and hasattr(hist_resp, 'data'):
                upload_history = hist_resp.data
        except Exception:
            pass

        # 4. Calculate instant performance metrics on the fly
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
    Class-Wide Document Pipeline:
    - Parses all subjects and all student records from uploaded files
    - Saves the raw file to Supabase Storage ('transcripts/shared/{filename}')
    - Records file metadata in 'uploaded_files'
    - Upserts ALL extracted student grades into 'student_grades' so any student in the class can look up their grade immediately!
    """
    all_parsed_courses = []
    uploaded_files_summary = []

    # Ensure storage bucket exists
    ensure_storage_bucket("transcripts")

    for file in files:
        filename = file.filename
        lower_name = filename.lower()
        if not (lower_name.endswith('.pdf') or lower_name.endswith('.txt') or lower_name.endswith('.csv')):
            continue

        file_bytes = await file.read()
        file_courses = parse_document_bytes(file_bytes, filename, registration_number)
        all_parsed_courses.extend(file_courses)

        # Storage paths
        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
        target_reg = registration_number.strip() if registration_number else "shared"
        storage_path = f"shared/{safe_filename}"
        mime_type = file.content_type or ("application/pdf" if lower_name.endswith('.pdf') else "text/plain")

        # Layer A: Store original file in Supabase Storage
        file_url = f"{SUPABASE_URL}/storage/v1/object/public/transcripts/{storage_path}" if SUPABASE_URL else ""
        if supabase:
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
        if supabase:
            try:
                supabase.table("uploaded_files").insert(file_record).execute()
            except Exception as meta_err:
                print(f"[Uploaded Files Notice]: {meta_err}")

        uploaded_files_summary.append(file_record)

    # Layer C: Idempotently upsert ALL parsed student records to student_grades
    # This allows every person in the class to access their grades without re-uploading
    if all_parsed_courses and supabase:
        save_courses_to_db(all_parsed_courses)

    return {
        "total_records_found": len(all_parsed_courses),
        "data": all_parsed_courses,
        "uploaded_files": uploaded_files_summary
    }
