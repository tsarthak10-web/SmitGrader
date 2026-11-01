from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Query
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


ROMAN_TO_YEAR = {
    "I": 1, "II": 2, "III": 3, "IV": 4,
    "1": 1, "2": 2, "3": 3, "4": 4,
    "1ST": 1, "2ND": 2, "3RD": 3, "4TH": 4,
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4
}

def normalize_year(val: Optional[str]) -> str:
    """
    Normalizes any year or semester string to standard format:
    '1st Year', '2nd Year', '3rd Year', '4th Year' or 'all'.
    """
    if not val:
        return "1st Year"

    s = str(val).strip()
    if s.lower() in ("all", "all years", "all-years", "all semesters", "*", "cumulative"):
        return "all"

    # Match Year 1-4 / 1st Year / 2nd Year / etc.
    m_yr = re.search(r"(?:year|yr)?[_\-\s]*([1-4])(?:st|nd|rd|th)?(?:\s*year)?", s, re.IGNORECASE)
    if m_yr and m_yr.group(1):
        num = int(m_yr.group(1))
        suff = "st" if num == 1 else ("nd" if num == 2 else ("rd" if num == 3 else "th"))
        return f"{num}{suff} Year"

    # Match Roman numeral I - IV for Year
    m_rom = re.search(r"\b(IV|III|II|I)\b(?:\s*year)?", s, re.IGNORECASE)
    if m_rom:
        rom = m_rom.group(1).upper()
        if rom in ROMAN_TO_YEAR:
            num = ROMAN_TO_YEAR[rom]
            suff = "st" if num == 1 else ("nd" if num == 2 else ("rd" if num == 3 else "th"))
            return f"{num}{suff} Year"

    # If input is semester (e.g. Semester 3 or 4 -> 2nd Year)
    m_sem = re.search(r"(?:semester|sem|term)[_\-\s]*([1-8])", s, re.IGNORECASE)
    if m_sem:
        sem_num = int(m_sem.group(1))
        yr_num = max(1, min(4, (sem_num + 1) // 2))
        suff = "st" if yr_num == 1 else ("nd" if yr_num == 2 else ("rd" if yr_num == 3 else "th"))
        return f"{yr_num}{suff} Year"

    # Match ordinal words like First Year / Second Year / Third Year / Fourth Year
    s_upper = s.upper()
    if "FIRST" in s_upper or "1ST" in s_upper:
        return "1st Year"
    elif "SECOND" in s_upper or "2ND" in s_upper:
        return "2nd Year"
    elif "THIRD" in s_upper or "3RD" in s_upper:
        return "3rd Year"
    elif "FOURTH" in s_upper or "4TH" in s_upper:
        return "4th Year"

    return "1st Year"

def normalize_semester(val: Optional[str]) -> str:
    """Alias for backwards compatibility."""
    return normalize_year(val)

def year_sort_key(yr_str: str) -> int:
    """Sort helper so 1st Year comes before 2nd Year, etc."""
    if not yr_str:
        return 999
    m = re.search(r"(\d+)", str(yr_str))
    return int(m.group(1)) if m else 999

def semester_sort_key(sem_str: str) -> int:
    """Alias for backwards compatibility."""
    return year_sort_key(sem_str)

def infer_year_from_course_code(course_code: Optional[str]) -> str:
    """
    Infers academic year based on course level digits:
    e.g. CS101, EC102 -> 1st Year
         MA206B1, CS202 -> 2nd Year
         CS302, IT305 -> 3rd Year
         CS410, CS499 -> 4th Year
    """
    if not course_code:
        return "1st Year"
    m = re.search(r"[A-Z]{2,4}(\d)", course_code.strip().upper())
    if m:
        digit = int(m.group(1))
        if digit == 1:
            return "1st Year"
        elif digit == 2:
            return "2nd Year"
        elif digit == 3:
            return "3rd Year"
        elif digit == 4:
            return "4th Year"
        elif 5 <= digit <= 8:
            yr = (digit + 1) // 2
            suff = "st" if yr == 1 else ("nd" if yr == 2 else ("rd" if yr == 3 else "th"))
            return f"{yr}{suff} Year"
    return "1st Year"

def infer_semester_from_course_code(course_code: Optional[str]) -> str:
    """Alias for backwards compatibility."""
    return infer_year_from_course_code(course_code)

def extract_year_from_text(text: str) -> Optional[str]:
    """
    Extracts academic year from document headers, footers, titles.
    """
    if not text:
        return None

    # 1. Regex for: Year : 2 / Yr - 1 / Year II / 2nd Year / Second Year
    p1 = r"(?:Academic\s+)?(?:Year|Yr)\s*[:\-]?\s*([0-9IVX]+(?:st|nd|rd|th)?)"
    m1 = re.search(p1, text, re.IGNORECASE)
    if m1:
        return normalize_year(m1.group(1))

    p2 = r"([0-9IVX]+(?:st|nd|rd|th)?)\s+(?:Year|Yr)"
    m2 = re.search(p2, text, re.IGNORECASE)
    if m2:
        return normalize_year(m2.group(1))

    p3 = r"\b(FIRST|SECOND|THIRD|FOURTH)\s+YEAR\b"
    m3 = re.search(p3, text, re.IGNORECASE)
    if m3:
        return normalize_year(m3.group(1))

    # 2. Semester headers mapped to Year
    p_sem = r"(?:Semester|Sem|Term)\s*[:\-]?\s*([0-9IVX]+(?:st|nd|rd|th)?)"
    m_sem = re.search(p_sem, text, re.IGNORECASE)
    if m_sem:
        return normalize_year(m_sem.group(1))

    p_sem2 = r"([0-9IVX]+(?:st|nd|rd|th)?)\s+(?:Semester|Sem|Term)"
    m_sem2 = re.search(p_sem2, text, re.IGNORECASE)
    if m_sem2:
        return normalize_year(m_sem2.group(1))

    return None

def extract_semester_from_text(text: str) -> Optional[str]:
    """Alias for backwards compatibility."""
    return extract_year_from_text(text)

def parse_page_text_for_metadata(page_text: str):
    """
    Extracts Subject Code, Subject Title, Credits, and Academic Year from page header text.
    Handles flexible spacing, casing, and separators.
    """
    meta_pattern = r"Subject Code\s*[:\-]?\s*([A-Z0-9]+).*?(?:Subject Title\s*[:\-]?\s*(.*?))?(?:Subject Credit\s*[:\-]?\s*([\d\.]+)|Credit\s*[:\-]?\s*([\d\.]+))"
    match = re.search(meta_pattern, page_text, re.IGNORECASE | re.DOTALL)
    c_year = extract_year_from_text(page_text)

    if not match:
        code_m = re.search(r"Subject Code\s*[:\-]?\s*([A-Z0-9]+)", page_text, re.IGNORECASE)
        title_m = re.search(r"Subject Title\s*[:\-]?\s*([^\n\r]+)", page_text, re.IGNORECASE)
        cred_m = re.search(r"(?:Subject Credit|Credit)\s*[:\-]?\s*([\d\.]+)", page_text, re.IGNORECASE)
        c_code = code_m.group(1).strip() if code_m else None
        c_name = title_m.group(1).strip() if title_m else None
        c_cred = float(cred_m.group(1)) if cred_m else None
        return c_code, c_name, c_cred, c_year

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
    return c_code, c_name, c_cred, c_year


def parse_document_bytes(
    file_bytes: bytes,
    filename: str,
    default_reg: Optional[str] = None,
    default_year: Optional[str] = None,
    default_sem: Optional[str] = None
) -> List[dict]:
    """
    Comprehensive document parser:
    1. Per-page / per-section Subject Code and Academic Year tracking
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
    file_year = extract_year_from_text(filename)
    chosen_default = default_year or default_sem
    current_year = file_year or (normalize_year(chosen_default) if chosen_default and chosen_default != 'all' else None)

    # Flexible row pattern for class grade sheet rows:
    # Match: RegNo (8-11 digits), Int marks/status, Ext marks/status, Total marks, Grade (S, A, B, C, D, E, F, etc.)
    row_pattern = r"(\d{8,11})\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+)\s+([A-Z\+]{1,3})"

    for p_idx, text in enumerate(pages_text):
        c_code, c_name, c_cred, c_yr = parse_page_text_for_metadata(text)
        if c_code:
            current_code = c_code
        if c_name:
            current_name = c_name
        if c_cred is not None:
            current_credits = c_cred
        if c_yr:
            current_year = c_yr

        effective_year = current_year or infer_year_from_course_code(current_code)

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
                    "grade": grade_val,
                    "academic_year": effective_year,
                    "year": effective_year,
                    "semester": effective_year
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
                    "grade": gr,
                    "academic_year": effective_year or infer_year_from_course_code(code),
                    "year": effective_year or infer_year_from_course_code(code),
                    "semester": effective_year or infer_year_from_course_code(code)
                })

    return parsed_courses


def save_courses_to_db(courses: List[dict]):
    """
    Deduplicates courses by (registration_number, course_code, academic_year) and
    safely upserts/inserts them into the Supabase 'student_grades' table.
    Gracefully handles schemas with or without an 'academic_year' or 'semester' column.
    """
    if not courses or not supabase:
        return

    # In-memory deduplication: keep cleanest/latest record per (reg, course, year)
    deduped: Dict[tuple, dict] = {}
    for c in courses:
        r = str(c.get("registration_number", "")).strip()
        code = str(c.get("course_code", "")).strip()
        yr = str(c.get("academic_year") or c.get("year") or c.get("semester") or "1st Year").strip()
        if r and code:
            deduped[(r, code, yr)] = c
    clean_list = list(deduped.values())

    # Chunked upsert/insert
    chunk_size = 50
    for i in range(0, len(clean_list), chunk_size):
        chunk = clean_list[i:i + chunk_size]
        try:
            # 1. Attempt upsert including 'academic_year' and 'semester'
            supabase.table("student_grades").upsert(
                chunk,
                on_conflict="registration_number,course_code"
            ).execute()
        except Exception as upsert_err:
            err_str = str(upsert_err).lower()
            # If schema lacks 'academic_year', try with 'semester' only
            if "academic_year" in err_str:
                sem_chunk = []
                for row in chunk:
                    r_copy = dict(row)
                    r_copy.pop("academic_year", None)
                    r_copy.pop("year", None)
                    sem_chunk.append(r_copy)
                try:
                    supabase.table("student_grades").upsert(
                        sem_chunk,
                        on_conflict="registration_number,course_code"
                    ).execute()
                    continue
                except Exception:
                    pass

            # If schema also lacks 'semester', strip all period columns
            base_chunk = []
            for row in chunk:
                base_chunk.append({
                    "registration_number": row.get("registration_number"),
                    "course_code": row.get("course_code"),
                    "course_name": row.get("course_name"),
                    "credits": row.get("credits"),
                    "total_marks": row.get("total_marks", 0.0),
                    "grade": row.get("grade")
                })
            try:
                supabase.table("student_grades").upsert(
                    base_chunk,
                    on_conflict="registration_number,course_code"
                ).execute()
                continue
            except Exception:
                pass

            # Final row-by-row fallback
            for row in chunk:
                try:
                    supabase.table("student_grades").upsert(
                        row,
                        on_conflict="registration_number,course_code"
                    ).execute()
                except Exception:
                    try:
                        base_row = {
                            "registration_number": row.get("registration_number"),
                            "course_code": row.get("course_code"),
                            "course_name": row.get("course_name"),
                            "credits": row.get("credits"),
                            "total_marks": row.get("total_marks", 0.0),
                            "grade": row.get("grade")
                        }
                        supabase.table("student_grades").upsert(base_row).execute()
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
def get_student_records(
    registration_number: str,
    year: Optional[str] = Query(None),
    semester: Optional[str] = Query(None)
):
    """
    1. Queries Supabase 'student_grades' for this student.
    2. If not found, references previously uploaded class files to find and recover grades.
    3. Categorizes courses by academic year and computes both per-year GPA and cumulative CGPA.
    4. Identifies whether requested year is present or missing.
    """
    clean_reg = registration_number.strip()
    target_period = year or semester
    requested_year_clean = normalize_year(target_period) if target_period else "all"

    if not supabase:
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "requested_year": requested_year_clean,
            "requested_semester": requested_year_clean,
            "is_requested_year_present": False,
            "is_requested_semester_present": False,
            "active_year": "1st Year",
            "active_semester": "1st Year",
            "available_years": [],
            "available_semesters": [],
            "years_breakdown": {},
            "semesters_breakdown": {},
            "cumulative_metrics": {
                "total_credits": 0.0,
                "weighted_points": 0.0,
                "cgpa": 0.0,
                "sgpa": 0.0,
                "passed_count": 0,
                "backlog_count": 0
            },
            "metrics": {
                "total_credits": 0.0,
                "weighted_points": 0.0,
                "gpa": 0.0,
                "sgpa": 0.0,
                "passed_count": 0,
                "backlog_count": 0
            },
            "data": [],
            "all_records": [],
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
                    # Deduplicate by course_code and year
                    d_map = {}
                    for nr in newly_recovered:
                        d_map[(nr.get("course_code"), nr.get("academic_year") or nr.get("semester"))] = nr
                    records = list(d_map.values())
            except Exception as ref_err:
                print(f"[Reference Upload Notice]: {ref_err}")

        # Ensure every record has a well-defined 'academic_year' and 'semester'
        for r in records:
            yr = r.get("academic_year") or r.get("year") or r.get("semester")
            if not yr or str(yr).strip() in ("Unknown", "None", ""):
                yr = infer_year_from_course_code(r.get("course_code"))
            else:
                yr = normalize_year(yr)
            r["academic_year"] = yr
            r["year"] = yr
            r["semester"] = yr

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

        # 4. Group courses by academic year & calculate per-year and cumulative metrics
        grade_points_map = {
            "S": 10.0, "A": 9.0, "B": 8.0, "C": 7.0, "D": 6.0, "E": 5.0, "F": 0.0
        }
        years_dict: Dict[str, List[dict]] = {}
        for r in records:
            yr = r.get("academic_year", "1st Year")
            if yr not in years_dict:
                years_dict[yr] = []
            years_dict[yr].append(r)

        # Sort available years logically (1st Year, 2nd Year, 3rd Year, 4th Year)
        available_years = sorted(list(years_dict.keys()), key=year_sort_key)

        year_summaries: Dict[str, dict] = {}
        cum_credits = 0.0
        cum_weighted_points = 0.0
        cum_passed = 0
        cum_backlog = 0

        for yr, yr_courses in years_dict.items():
            yr_cred = 0.0
            yr_pts = 0.0
            yr_passed = 0
            yr_backlog = 0
            for c_row in yr_courses:
                c = float(c_row.get("credits") or 0.0)
                g = str(c_row.get("grade") or "").strip().upper()
                pt = grade_points_map.get(g, 0.0)
                yr_cred += c
                yr_pts += (c * pt)
                if g == "F" or pt == 0.0:
                    yr_backlog += 1
                else:
                    yr_passed += 1

            yr_gpa = round(yr_pts / yr_cred, 2) if yr_cred > 0 else 0.0
            year_summaries[yr] = {
                "year": yr,
                "academic_year": yr,
                "semester": yr,
                "total_credits": yr_cred,
                "weighted_points": round(yr_pts, 1),
                "gpa": yr_gpa,
                "sgpa": yr_gpa,
                "passed_count": yr_passed,
                "backlog_count": yr_backlog,
                "courses_count": len(yr_courses),
                "courses": yr_courses
            }

            cum_credits += yr_cred
            cum_weighted_points += yr_pts
            cum_passed += yr_passed
            cum_backlog += yr_backlog

        cgpa = round(cum_weighted_points / cum_credits, 2) if cum_credits > 0 else 0.0
        cumulative_metrics = {
            "total_credits": cum_credits,
            "weighted_points": round(cum_weighted_points, 1),
            "cgpa": cgpa,
            "gpa": cgpa,
            "sgpa": cgpa,  # for backwards compatibility
            "passed_count": cum_passed,
            "backlog_count": cum_backlog
        }

        # 5. Evaluate requested year vs available years
        is_requested_year_present = True
        notice_message = None

        if not target_period or str(target_period).strip().lower() in ["all", "", "none"]:
            requested_year_clean = "all"
            active_year = available_years[-1] if available_years else "1st Year"
            active_data = records
            active_metrics = cumulative_metrics
            is_requested_year_present = len(records) > 0
        else:
            requested_year_clean = normalize_year(target_period)
            if requested_year_clean in year_summaries:
                is_requested_year_present = True
                active_year = requested_year_clean
                active_data = years_dict[requested_year_clean]
                active_metrics = year_summaries[requested_year_clean]
            else:
                is_requested_year_present = False
                active_year = available_years[-1] if available_years else requested_year_clean
                active_data = records
                active_metrics = cumulative_metrics
                notice_message = (
                    f"{requested_year_clean} results have not been uploaded yet. "
                    f"Available academic years: {', '.join(available_years) if available_years else 'None'}."
                )

        return {
            "found": len(records) > 0,
            "registration_number": clean_reg,
            "total_records": len(records),
            "requested_year": requested_year_clean,
            "requested_semester": requested_year_clean,
            "is_requested_year_present": is_requested_year_present,
            "is_requested_semester_present": is_requested_year_present,
            "active_year": active_year,
            "active_semester": active_year,
            "available_years": available_years,
            "available_semesters": available_years,
            "years_breakdown": year_summaries,
            "semesters_breakdown": year_summaries,
            "cumulative_metrics": cumulative_metrics,
            "metrics": active_metrics,
            "data": active_data,
            "all_records": records,
            "upload_history": upload_history,
            "notice": notice_message
        }
    except Exception as e:
        print(f"Error checking student records: {e}")
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "requested_year": normalize_year(target_period) if target_period else "all",
            "requested_semester": normalize_year(target_period) if target_period else "all",
            "is_requested_year_present": False,
            "is_requested_semester_present": False,
            "active_year": "1st Year",
            "active_semester": "1st Year",
            "available_years": [],
            "available_semesters": [],
            "years_breakdown": {},
            "semesters_breakdown": {},
            "cumulative_metrics": {
                "total_credits": 0.0,
                "weighted_points": 0.0,
                "cgpa": 0.0,
                "sgpa": 0.0,
                "passed_count": 0,
                "backlog_count": 0
            },
            "metrics": {
                "total_credits": 0.0,
                "weighted_points": 0.0,
                "gpa": 0.0,
                "sgpa": 0.0,
                "passed_count": 0,
                "backlog_count": 0
            },
            "data": [],
            "all_records": [],
            "upload_history": [],
            "error": str(e)
        }


@app.post("/api/upload")
async def parse_transcript(
    files: List[UploadFile] = File(..., description="Select transcript files (.txt or .pdf)"),
    registration_number: Optional[str] = Form(None, description="Optional student registration number override"),
    target_year: Optional[str] = Form(None, description="Optional target year for the upload"),
    target_semester: Optional[str] = Form(None, description="Optional target semester alias")
):
    """
    Class-Wide Document Pipeline:
    - Parses all subjects, academic years, and student records from uploaded files
    - Saves the raw file to Supabase Storage ('transcripts/shared/{filename}')
    - Records file metadata in 'uploaded_files'
    - Upserts ALL extracted student grades into 'student_grades' with multi-year support!
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
        file_courses = parse_document_bytes(file_bytes, filename, registration_number, target_year, target_semester)
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

    detected_years = sorted(list(set(c.get("academic_year") or c.get("semester") for c in all_parsed_courses if (c.get("academic_year") or c.get("semester")))), key=year_sort_key)

    return {
        "total_records_found": len(all_parsed_courses),
        "detected_years": detected_years,
        "detected_semesters": detected_years,
        "data": all_parsed_courses,
        "uploaded_files": uploaded_files_summary
    }
