from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pathlib import Path
import pdfplumber
import io
import re
import os
from typing import List, Optional, Dict, Any, Tuple
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


# ---------------------------------------------------------------------
# Academic Structure: 4 Years, 2 Semesters per Year (8 Semesters Total)
# ---------------------------------------------------------------------
ROMAN_TO_NUM = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
    "1ST": 1, "2ND": 2, "3RD": 3, "4TH": 4, "5TH": 5, "6TH": 6, "7TH": 7, "8TH": 8,
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4,
    "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8
}

def semester_number_to_year(sem_num: int) -> str:
    """
    Computes Academic Year from Semester number:
    Sem 1, 2 -> '1st Year'
    Sem 3, 4 -> '2nd Year'
    Sem 5, 6 -> '3rd Year'
    Sem 7, 8 -> '4th Year'
    """
    try:
        n = int(sem_num)
    except (ValueError, TypeError):
        n = 1
    yr = max(1, min(4, (n + 1) // 2))
    suff = "st" if yr == 1 else ("nd" if yr == 2 else ("rd" if yr == 3 else "th"))
    return f"{yr}{suff} Year"

def normalize_semester(val: Optional[str]) -> str:
    """
    Normalizes any semester string to standard 'Semester 1' through 'Semester 8', or 'all'.
    """
    if not val:
        return "Semester 1"
    s = str(val).strip()
    if s.lower() in ("all", "all semesters", "all-semesters", "cumulative", "*", "overall", "all years"):
        return "all"

    # Match Semester / Sem / Term followed by number/roman
    m_sem = re.search(r"(?:semester|sem|term)[_\-\s]*([0-9IVX]+(?:st|nd|rd|th)?)", s, re.IGNORECASE)
    if m_sem:
        raw = m_sem.group(1).upper()
        if raw in ROMAN_TO_NUM:
            return f"Semester {ROMAN_TO_NUM[raw]}"
        m_dig = re.search(r"(\d+)", raw)
        if m_dig:
            num = max(1, min(8, int(m_dig.group(1))))
            return f"Semester {num}"

    # Match standalone Roman numerals I-VIII
    m_rom = re.search(r"\b(VIII|VII|VI|V|IV|III|II|I)\b", s, re.IGNORECASE)
    if m_rom:
        rom = m_rom.group(1).upper()
        if rom in ROMAN_TO_NUM:
            return f"Semester {ROMAN_TO_NUM[rom]}"

    # Match ordinal words
    m_ord = re.search(r"\b(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH)\b", s, re.IGNORECASE)
    if m_ord:
        w = m_ord.group(1).upper()
        if w in ROMAN_TO_NUM:
            return f"Semester {ROMAN_TO_NUM[w]}"

    # If pure number 1-8
    m_dig = re.search(r"(\d+)", s)
    if m_dig:
        n = int(m_dig.group(1))
        if 1 <= n <= 8:
            return f"Semester {n}"

    # If Year is given (e.g. "2nd Year"), map to the first semester of that year
    m_yr = re.search(r"(?:year|yr)[_\-\s]*([1-4])", s, re.IGNORECASE)
    if m_yr:
        yr = int(m_yr.group(1))
        return f"Semester {(yr * 2) - 1}"

    return "Semester 1"

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

    # Match Semester / Sem / Term 1-8 -> converts to corresponding year
    m_sem = re.search(r"(?:semester|sem|term)[_\-\s]*([0-9IVX]+(?:st|nd|rd|th)?)", s, re.IGNORECASE)
    if m_sem:
        raw = m_sem.group(1).upper()
        if raw in ROMAN_TO_NUM:
            return semester_number_to_year(ROMAN_TO_NUM[raw])
        m_dig = re.search(r"(\d+)", raw)
        if m_dig:
            return semester_number_to_year(int(m_dig.group(1)))

    # Match Year 1-4 / 1st Year / 2nd Year
    m_yr = re.search(r"(?:year|yr)?[_\-\s]*([1-4])(?:st|nd|rd|th)?(?:\s*year)?", s, re.IGNORECASE)
    if m_yr and m_yr.group(1):
        num = int(m_yr.group(1))
        suff = "st" if num == 1 else ("nd" if num == 2 else ("rd" if num == 3 else "th"))
        return f"{num}{suff} Year"

    # Match Roman numeral I - IV for Year
    m_rom = re.search(r"\b(IV|III|II|I)\b(?:\s*year)?", s, re.IGNORECASE)
    if m_rom:
        rom = m_rom.group(1).upper()
        if rom in ROMAN_TO_NUM:
            return semester_number_to_year(ROMAN_TO_NUM[rom] * 2 - 1)

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

def semester_sort_key(sem_str: str) -> int:
    """Sort helper so Semester 1 comes before Semester 2, etc."""
    if not sem_str:
        return 999
    m = re.search(r"(\d+)", str(sem_str))
    return int(m.group(1)) if m else 999

def year_sort_key(yr_str: str) -> int:
    """Sort helper so 1st Year comes before 2nd Year, etc."""
    if not yr_str:
        return 999
    m = re.search(r"(\d+)", str(yr_str))
    return int(m.group(1)) if m else 999

def infer_semester_and_year_from_course_code(course_code: Optional[str]) -> Tuple[str, str]:
    """
    Infers Semester (1-8) and Academic Year (1st-4th Year) from course code.
    e.g. CS101, MA101 -> Semester 1, 1st Year
         CS102, EC102 -> Semester 2, 1st Year
         MA206B1, CS201 -> Semester 3 / Semester 4, 2nd Year
         CS301, CS302 -> Semester 5 / Semester 6, 3rd Year
         CS401, CS402 -> Semester 7 / Semester 8, 4th Year
    """
    if not course_code:
        return ("Semester 1", "1st Year")

    code = course_code.strip().upper()
    # Check course level digit e.g. CS201, MA206B1, CS302
    m = re.search(r"[A-Z]{2,4}(\d)(\d{2})", code)
    if m:
        level_digit = int(m.group(1)) # 1, 2, 3, 4
        num_suffix = int(m.group(2))  # e.g. 01, 02, 06
        yr = max(1, min(4, level_digit))
        # Odd suffix -> Odd semester (1, 3, 5, 7); Even suffix -> Even semester (2, 4, 6, 8)
        is_even = (num_suffix % 2 == 0)
        sem_num = (yr * 2) if is_even else ((yr * 2) - 1)
        sem_str = f"Semester {sem_num}"
        yr_str = semester_number_to_year(sem_num)
        return (sem_str, yr_str)

    # Fallback to single digit after department code
    m_single = re.search(r"[A-Z]{2,4}(\d)", code)
    if m_single:
        digit = int(m_single.group(1))
        if 1 <= digit <= 4:
            sem_num = (digit * 2) - 1
            return (f"Semester {sem_num}", semester_number_to_year(sem_num))
        elif 5 <= digit <= 8:
            return (f"Semester {digit}", semester_number_to_year(digit))

    return ("Semester 1", "1st Year")

def infer_semester_from_course_code(course_code: Optional[str]) -> str:
    sem, _ = infer_semester_and_year_from_course_code(course_code)
    return sem

def infer_year_from_course_code(course_code: Optional[str]) -> str:
    _, yr = infer_semester_and_year_from_course_code(course_code)
    return yr

def extract_semester_and_year_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts Semester and Academic Year from document header, footer, or title text.
    Handles 'Semester : 3', 'Semester: III', 'B.TECH 3RD SEMESTER', etc.
    """
    if not text:
        return None, None

    # 1. Look for explicit Semester mentions:
    p_sem1 = r"(?:Semester|Sem|Term)\s*[:\-]?\s*([0-9IVX]+(?:st|nd|rd|th)?)"
    m_sem1 = re.search(p_sem1, text, re.IGNORECASE)
    if m_sem1:
        s_norm = normalize_semester(m_sem1.group(1))
        m_dig = re.search(r"(\d+)", s_norm)
        if m_dig:
            sem_num = int(m_dig.group(1))
            return s_norm, semester_number_to_year(sem_num)

    p_sem2 = r"([0-9IVX]+(?:st|nd|rd|th)?)\s+(?:Semester|Sem|Term)"
    m_sem2 = re.search(p_sem2, text, re.IGNORECASE)
    if m_sem2:
        s_norm = normalize_semester(m_sem2.group(1))
        m_dig = re.search(r"(\d+)", s_norm)
        if m_dig:
            sem_num = int(m_dig.group(1))
            return s_norm, semester_number_to_year(sem_num)

    # 2. Look for explicit Year mentions:
    p_yr1 = r"(?:Academic\s+)?(?:Year|Yr)\s*[:\-]?\s*([0-9IVX]+(?:st|nd|rd|th)?)"
    m_yr1 = re.search(p_yr1, text, re.IGNORECASE)
    if m_yr1:
        y_norm = normalize_year(m_yr1.group(1))
        m_ydig = re.search(r"(\d+)", y_norm)
        if m_ydig:
            yr_num = int(m_ydig.group(1))
            default_sem = f"Semester {(yr_num * 2) - 1}"
            return default_sem, y_norm

    p_yr2 = r"([0-9IVX]+(?:st|nd|rd|th)?)\s+(?:Year|Yr)"
    m_yr2 = re.search(p_yr2, text, re.IGNORECASE)
    if m_yr2:
        y_norm = normalize_year(m_yr2.group(1))
        m_ydig = re.search(r"(\d+)", y_norm)
        if m_ydig:
            yr_num = int(m_ydig.group(1))
            default_sem = f"Semester {(yr_num * 2) - 1}"
            return default_sem, y_norm

    return None, None

def extract_year_from_text(text: str) -> Optional[str]:
    _, yr = extract_semester_and_year_from_text(text)
    return yr

def extract_semester_from_text(text: str) -> Optional[str]:
    sem, _ = extract_semester_and_year_from_text(text)
    return sem


def parse_page_text_for_metadata(page_text: str):
    """
    Extracts Subject Code, Subject Title, Credits, and Semester / Academic Year from page header text.
    Handles flexible spacing, casing, and separators.
    """
    meta_pattern = r"Subject Code\s*[:\-]?\s*([A-Z0-9]+).*?(?:Subject Title\s*[:\-]?\s*(.*?))?(?:Subject Credit\s*[:\-]?\s*([\d\.]+)|Credit\s*[:\-]?\s*([\d\.]+))"
    match = re.search(meta_pattern, page_text, re.IGNORECASE | re.DOTALL)
    c_sem, c_year = extract_semester_and_year_from_text(page_text)

    if not match:
        code_m = re.search(r"Subject Code\s*[:\-]?\s*([A-Z0-9]+)", page_text, re.IGNORECASE)
        title_m = re.search(r"Subject Title\s*[:\-]?\s*([^\n\r]+)", page_text, re.IGNORECASE)
        cred_m = re.search(r"(?:Subject Credit|Credit)\s*[:\-]?\s*([\d\.]+)", page_text, re.IGNORECASE)
        c_code = code_m.group(1).strip() if code_m else None
        c_name = title_m.group(1).strip() if title_m else None
        c_cred = float(cred_m.group(1)) if cred_m else None
        return c_code, c_name, c_cred, c_sem, c_year

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
    return c_code, c_name, c_cred, c_sem, c_year


def parse_document_bytes(
    file_bytes: bytes,
    filename: str,
    default_reg: Optional[str] = None,
    default_year: Optional[str] = None,
    default_sem: Optional[str] = None
) -> List[dict]:
    """
    Comprehensive document parser with dual Semester and Academic Year tracking:
    1. Per-page / per-section Subject Code, Semester, and Academic Year tracking
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

    file_sem, file_yr = extract_semester_and_year_from_text(filename)
    chosen_sem = default_sem or default_year
    current_sem = file_sem or (normalize_semester(chosen_sem) if chosen_sem and chosen_sem != 'all' else None)
    current_year = file_yr or (normalize_year(default_year or chosen_sem) if (default_year or chosen_sem) and (default_year or chosen_sem) != 'all' else None)

    # Flexible row pattern for class grade sheet rows:
    # RegNo (8-11 digits), Int marks/status, Ext marks/status, Total marks, Grade (S, A, B, C, D, E, F)
    row_pattern = r"(\d{8,11})\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+)\s+([A-Z\+]{1,3})"

    for p_idx, text in enumerate(pages_text):
        c_code, c_name, c_cred, c_sem, c_yr = parse_page_text_for_metadata(text)
        if c_code:
            current_code = c_code
        if c_name:
            current_name = c_name
        if c_cred is not None:
            current_credits = c_cred
        if c_sem:
            current_sem = c_sem
        if c_yr:
            current_year = c_yr

        # Determine effective semester & year
        inferred_sem, inferred_yr = infer_semester_and_year_from_course_code(current_code)
        effective_sem = current_sem or inferred_sem
        effective_year = current_year or semester_number_to_year(semester_sort_key(effective_sem))

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
                    "semester": effective_sem,
                    "academic_year": effective_year,
                    "year": effective_year
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

                row_sem, row_yr = infer_semester_and_year_from_course_code(code)
                row_effective_sem = effective_sem or row_sem
                row_effective_yr = effective_year or semester_number_to_year(semester_sort_key(row_effective_sem))

                parsed_courses.append({
                    "registration_number": effective_reg,
                    "course_code": code,
                    "course_name": name,
                    "credits": cr,
                    "total_marks": 0.0,
                    "grade": gr,
                    "semester": row_effective_sem,
                    "academic_year": row_effective_yr,
                    "year": row_effective_yr
                })

    return parsed_courses


def save_courses_to_db(courses: List[dict]):
    """
    Deduplicates courses by (registration_number, course_code, semester) and
    safely upserts/inserts them into the Supabase 'student_grades' table.
    """
    if not courses or not supabase:
        return

    # In-memory deduplication: keep cleanest/latest record per (reg, course, semester)
    deduped: Dict[tuple, dict] = {}
    for c in courses:
        r = str(c.get("registration_number", "")).strip()
        code = str(c.get("course_code", "")).strip()
        sem = str(c.get("semester") or "Semester 1").strip()
        if r and code:
            deduped[(r, code, sem)] = c
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

            # Fallback without extra period columns
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
    semester: Optional[str] = Query(None),
    year: Optional[str] = Query(None)
):
    """
    1. Queries Supabase 'student_grades' for this student.
    2. If not found, references previously uploaded class files to recover grades.
    3. Categorizes courses by both Semester (Sem 1 - Sem 8) and Academic Year (1st - 4th Year).
    4. Computes per-semester SGPA, per-year GPA, and cumulative CGPA.
    5. Identifies whether requested semester/year is present or missing.
    """
    clean_reg = registration_number.strip()
    target_param = semester or year
    requested_filter = "all"
    if target_param and str(target_param).strip().lower() not in ["all", "", "none", "*", "cumulative"]:
        # Check if user specifically requested a semester or a year
        if "sem" in str(target_param).lower():
            requested_filter = normalize_semester(target_param)
        elif "year" in str(target_param).lower() or "yr" in str(target_param).lower():
            requested_filter = normalize_year(target_param)
        else:
            requested_filter = normalize_semester(target_param)

    if not supabase:
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "requested_filter": requested_filter,
            "requested_semester": requested_filter,
            "requested_year": normalize_year(requested_filter),
            "is_requested_semester_present": False,
            "is_requested_year_present": False,
            "active_semester": "Semester 1",
            "active_year": "1st Year",
            "available_semesters": [],
            "available_years": [],
            "semesters_breakdown": {},
            "years_breakdown": {},
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
                        save_courses_to_db(doc_courses)
                        for dc in doc_courses:
                            if str(dc.get("registration_number", "")).strip() == clean_reg:
                                newly_recovered.append(dc)

                if newly_recovered:
                    d_map = {}
                    for nr in newly_recovered:
                        key = (nr.get("course_code"), nr.get("semester") or nr.get("academic_year"))
                        d_map[key] = nr
                    records = list(d_map.values())
            except Exception as ref_err:
                print(f"[Reference Upload Notice]: {ref_err}")

        # Ensure every record has both 'semester' and 'academic_year' well-defined
        for r in records:
            sem_raw = r.get("semester")
            yr_raw = r.get("academic_year") or r.get("year")
            if not sem_raw or str(sem_raw).strip() in ("Unknown", "None", ""):
                inferred_sem, inferred_yr = infer_semester_and_year_from_course_code(r.get("course_code"))
                r["semester"] = inferred_sem
                r["academic_year"] = inferred_yr
                r["year"] = inferred_yr
            else:
                norm_sem = normalize_semester(sem_raw)
                r["semester"] = norm_sem
                r["academic_year"] = semester_number_to_year(semester_sort_key(norm_sem))
                r["year"] = r["academic_year"]

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

        # 4. Group courses by Semester & by Academic Year
        grade_points_map = {
            "S": 10.0, "A": 9.0, "B": 8.0, "C": 7.0, "D": 6.0, "E": 5.0, "F": 0.0
        }

        semesters_dict: Dict[str, List[dict]] = {}
        years_dict: Dict[str, List[dict]] = {}

        for r in records:
            sem = r.get("semester", "Semester 1")
            yr = r.get("academic_year", "1st Year")
            if sem not in semesters_dict:
                semesters_dict[sem] = []
            semesters_dict[sem].append(r)

            if yr not in years_dict:
                years_dict[yr] = []
            years_dict[yr].append(r)

        available_semesters = sorted(list(semesters_dict.keys()), key=semester_sort_key)
        available_years = sorted(list(years_dict.keys()), key=year_sort_key)

        # Build Semester Summaries (SGPA per semester)
        semester_summaries: Dict[str, dict] = {}
        cum_credits = 0.0
        cum_weighted_points = 0.0
        cum_passed = 0
        cum_backlog = 0

        for sem, sem_courses in semesters_dict.items():
            s_cred = 0.0
            s_pts = 0.0
            s_passed = 0
            s_backlog = 0
            for c_row in sem_courses:
                c = float(c_row.get("credits") or 0.0)
                g = str(c_row.get("grade") or "").strip().upper()
                pt = grade_points_map.get(g, 0.0)
                s_cred += c
                s_pts += (c * pt)
                if g == "F" or pt == 0.0:
                    s_backlog += 1
                else:
                    s_passed += 1

            sgpa = round(s_pts / s_cred, 2) if s_cred > 0 else 0.0
            corr_year = semester_number_to_year(semester_sort_key(sem))
            semester_summaries[sem] = {
                "semester": sem,
                "academic_year": corr_year,
                "year": corr_year,
                "total_credits": s_cred,
                "weighted_points": round(s_pts, 1),
                "sgpa": sgpa,
                "gpa": sgpa,
                "passed_count": s_passed,
                "backlog_count": s_backlog,
                "courses_count": len(sem_courses),
                "courses": sem_courses
            }

            cum_credits += s_cred
            cum_weighted_points += s_pts
            cum_passed += s_passed
            cum_backlog += s_backlog

        # Build Year Summaries (Aggregated GPA per academic year)
        year_summaries: Dict[str, dict] = {}
        for yr, yr_courses in years_dict.items():
            y_cred = 0.0
            y_pts = 0.0
            y_passed = 0
            y_backlog = 0
            y_sems = set()
            for c_row in yr_courses:
                c = float(c_row.get("credits") or 0.0)
                g = str(c_row.get("grade") or "").strip().upper()
                pt = grade_points_map.get(g, 0.0)
                y_cred += c
                y_pts += (c * pt)
                if g == "F" or pt == 0.0:
                    y_backlog += 1
                else:
                    y_passed += 1
                if c_row.get("semester"):
                    y_sems.add(c_row.get("semester"))

            ygpa = round(y_pts / y_cred, 2) if y_cred > 0 else 0.0
            year_summaries[yr] = {
                "academic_year": yr,
                "year": yr,
                "semesters": sorted(list(y_sems), key=semester_sort_key),
                "total_credits": y_cred,
                "weighted_points": round(y_pts, 1),
                "gpa": ygpa,
                "sgpa": ygpa,
                "passed_count": y_passed,
                "backlog_count": y_backlog,
                "courses_count": len(yr_courses),
                "courses": yr_courses
            }

        cgpa = round(cum_weighted_points / cum_credits, 2) if cum_credits > 0 else 0.0
        cumulative_metrics = {
            "total_credits": cum_credits,
            "weighted_points": round(cum_weighted_points, 1),
            "cgpa": cgpa,
            "gpa": cgpa,
            "sgpa": cgpa,
            "passed_count": cum_passed,
            "backlog_count": cum_backlog
        }

        # 5. Evaluate requested filter
        is_requested_present = True
        notice_message = None

        if requested_filter == "all":
            active_semester = available_semesters[-1] if available_semesters else "Semester 1"
            active_year = available_years[-1] if available_years else "1st Year"
            active_data = records
            active_metrics = cumulative_metrics
            is_requested_present = len(records) > 0
        elif requested_filter in semester_summaries:
            active_semester = requested_filter
            active_year = semester_summaries[requested_filter]["academic_year"]
            active_data = semesters_dict[requested_filter]
            active_metrics = semester_summaries[requested_filter]
            is_requested_present = True
        elif requested_filter in year_summaries:
            active_year = requested_filter
            active_semester = year_summaries[requested_filter]["semesters"][-1] if year_summaries[requested_filter]["semesters"] else "Semester 1"
            active_data = years_dict[requested_filter]
            active_metrics = year_summaries[requested_filter]
            is_requested_present = True
        else:
            is_requested_present = False
            active_semester = available_semesters[-1] if available_semesters else requested_filter
            active_year = available_years[-1] if available_years else normalize_year(requested_filter)
            active_data = records
            active_metrics = cumulative_metrics
            avail_str = f"Available: {', '.join(available_semesters)}" if available_semesters else "No semesters uploaded yet."
            notice_message = f"{requested_filter} results have not been uploaded yet. {avail_str}"

        return {
            "found": len(records) > 0,
            "registration_number": clean_reg,
            "total_records": len(records),
            "requested_filter": requested_filter,
            "requested_semester": requested_filter,
            "requested_year": normalize_year(requested_filter),
            "is_requested_present": is_requested_present,
            "is_requested_semester_present": is_requested_present,
            "is_requested_year_present": is_requested_present,
            "active_semester": active_semester,
            "active_year": active_year,
            "available_semesters": available_semesters,
            "available_years": available_years,
            "semesters_breakdown": semester_summaries,
            "years_breakdown": year_summaries,
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
            "requested_filter": requested_filter,
            "requested_semester": requested_filter,
            "requested_year": normalize_year(requested_filter),
            "is_requested_present": False,
            "is_requested_semester_present": False,
            "is_requested_year_present": False,
            "active_semester": "Semester 1",
            "active_year": "1st Year",
            "available_semesters": [],
            "available_years": [],
            "semesters_breakdown": {},
            "years_breakdown": {},
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
    target_semester: Optional[str] = Form(None, description="Target semester for upload (e.g. 'Semester 3')"),
    target_year: Optional[str] = Form(None, description="Target academic year for upload (e.g. '2nd Year')")
):
    """
    Class-Wide Document Pipeline:
    - Parses all subjects, semesters, academic years, and student records from uploaded files
    - Saves the raw file to Supabase Storage ('transcripts/shared/{filename}')
    - Records file metadata in 'uploaded_files'
    - Upserts ALL extracted student grades into 'student_grades' with dual Semester & Year support
    """
    all_parsed_courses = []
    uploaded_files_summary = []

    # Ensure storage bucket exists
    ensure_storage_bucket("transcripts")

    # Determine effective target semester/year
    chosen_target = target_semester or target_year
    norm_target_sem = normalize_semester(chosen_target) if chosen_target and chosen_target != "all" else None
    norm_target_yr = normalize_year(target_year or chosen_target) if (target_year or chosen_target) and (target_year or chosen_target) != "all" else None

    for file in files:
        filename = file.filename
        lower_name = filename.lower()
        if not (lower_name.endswith('.pdf') or lower_name.endswith('.txt') or lower_name.endswith('.csv')):
            continue

        file_bytes = await file.read()
        file_courses = parse_document_bytes(
            file_bytes=file_bytes,
            filename=filename,
            default_reg=registration_number,
            default_year=norm_target_yr,
            default_sem=norm_target_sem
        )
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
    if all_parsed_courses and supabase:
        save_courses_to_db(all_parsed_courses)

    detected_semesters = sorted(list(set(c.get("semester") for c in all_parsed_courses if c.get("semester"))), key=semester_sort_key)
    detected_years = sorted(list(set(c.get("academic_year") for c in all_parsed_courses if c.get("academic_year"))), key=year_sort_key)

    return {
        "total_records_found": len(all_parsed_courses),
        "detected_semesters": detected_semesters,
        "detected_years": detected_years,
        "data": all_parsed_courses,
        "uploaded_files": uploaded_files_summary
    }
