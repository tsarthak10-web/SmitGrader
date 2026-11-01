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


ROMAN_TO_SEMESTER = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
    "1ST": 1, "2ND": 2, "3RD": 3, "4TH": 4, "5TH": 5, "6TH": 6, "7TH": 7, "8TH": 8,
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8
}

def normalize_semester(val: Optional[str]) -> str:
    """
    Normalizes any semester string to a standard format: 'Semester X' or 'all'.
    Handles roman numerals (III -> Semester 3), ordinal words (Third -> Semester 3),
    numbers (3 -> Semester 3), and term names (fall-2024 -> Semester 5).
    """
    if not val:
        return "Semester 1"

    s = str(val).strip()
    if s.lower() in ("all", "all semesters", "all-semesters", "*", "cumulative"):
        return "all"

    # Direct match for digits 1-8 (e.g. 'Semester 3', 'Sem 3', 'Term 3', '3rd Sem')
    m_num = re.search(r"(?:semester|sem|term)?[_\-\s]*([1-8])(?:st|nd|rd|th)?", s, re.IGNORECASE)
    if m_num and m_num.group(1):
        return f"Semester {m_num.group(1)}"

    # Match for roman numerals I-VIII
    m_rom = re.search(r"\b(VIII|VII|VI|IV|V|III|II|I)\b", s, re.IGNORECASE)
    if m_rom:
        rom_val = m_rom.group(1).upper()
        if rom_val in ROMAN_TO_SEMESTER:
            return f"Semester {ROMAN_TO_SEMESTER[rom_val]}"

    # Match ordinal words (e.g. Third Semester)
    for word, num in ROMAN_TO_SEMESTER.items():
        if len(word) > 2 and word in s.upper():
            return f"Semester {num}"

    # Handle term names like Fall 2024 -> Semester 5 etc.
    s_lower = s.lower()
    if "term 5" in s_lower or "fall-2024" in s_lower or "fall 2024" in s_lower:
        return "Semester 5"
    elif "term 4" in s_lower or "spring-2024" in s_lower or "spring 2024" in s_lower:
        return "Semester 4"
    elif "term 3" in s_lower or "fall-2023" in s_lower or "fall 2023" in s_lower:
        return "Semester 3"
    elif "term 2" in s_lower or "spring-2023" in s_lower or "spring 2023" in s_lower:
        return "Semester 2"
    elif "term 1" in s_lower or "fall-2022" in s_lower or "fall 2022" in s_lower:
        return "Semester 1"

    return "Semester 1"


def semester_sort_key(sem_str: str) -> int:
    """Sort helper so Semester 1 comes before Semester 2, etc."""
    if not sem_str:
        return 999
    m = re.search(r"(\d+)", str(sem_str))
    return int(m.group(1)) if m else 999


def infer_semester_from_course_code(course_code: Optional[str]) -> str:
    """
    Infers semester based on course level digits:
    e.g. CS101 -> Semester 1, MA206 -> Semester 3, CS302 -> Semester 5, CS410 -> Semester 7
    """
    if not course_code:
        return "Semester 1"
    m = re.search(r"[A-Z]{2,4}(\d)", course_code.strip().upper())
    if m:
        digit = int(m.group(1))
        if digit == 1:
            return "Semester 1"
        elif digit == 2:
            return "Semester 3"
        elif digit == 3:
            return "Semester 5"
        elif digit == 4:
            return "Semester 7"
        elif 5 <= digit <= 8:
            return f"Semester {digit}"
    return "Semester 1"


def extract_semester_from_text(text: str) -> Optional[str]:
    """
    Extracts semester identifier from document text (headers, footers, titles).
    """
    if not text:
        return None
    # 1. Regex for: Semester : 3 / Sem - 5 / Term: 3 / Semester III
    p1 = r"(?:Semester|Sem|Term)\s*[:\-]?\s*([0-9IVX]+(?:st|nd|rd|th)?)"
    m1 = re.search(p1, text, re.IGNORECASE)
    if m1:
        return normalize_semester(m1.group(1))

    # 2. Regex for: 3rd Semester / III SEMESTER / 5th Sem
    p2 = r"([0-9IVX]+(?:st|nd|rd|th)?)\s+(?:Semester|Sem|Term)"
    m2 = re.search(p2, text, re.IGNORECASE)
    if m2:
        return normalize_semester(m2.group(1))

    # 3. Regex for word forms: THIRD SEMESTER / FIFTH SEMESTER
    p3 = r"\b(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH)\s+(?:SEMESTER|SEM)\b"
    m3 = re.search(p3, text, re.IGNORECASE)
    if m3:
        return normalize_semester(m3.group(1))

    return None


def parse_page_text_for_metadata(page_text: str):
    """
    Extracts Subject Code, Subject Title, Credits, and Semester from page header text.
    Handles flexible spacing, casing, and separators.
    """
    meta_pattern = r"Subject Code\s*[:\-]?\s*([A-Z0-9]+).*?(?:Subject Title\s*[:\-]?\s*(.*?))?(?:Subject Credit\s*[:\-]?\s*([\d\.]+)|Credit\s*[:\-]?\s*([\d\.]+))"
    match = re.search(meta_pattern, page_text, re.IGNORECASE | re.DOTALL)
    c_sem = extract_semester_from_text(page_text)

    if not match:
        # Fallback simpler regex
        code_m = re.search(r"Subject Code\s*[:\-]?\s*([A-Z0-9]+)", page_text, re.IGNORECASE)
        title_m = re.search(r"Subject Title\s*[:\-]?\s*([^\n\r]+)", page_text, re.IGNORECASE)
        cred_m = re.search(r"(?:Subject Credit|Credit)\s*[:\-]?\s*([\d\.]+)", page_text, re.IGNORECASE)
        c_code = code_m.group(1).strip() if code_m else None
        c_name = title_m.group(1).strip() if title_m else None
        c_cred = float(cred_m.group(1)) if cred_m else None
        return c_code, c_name, c_cred, c_sem

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
    return c_code, c_name, c_cred, c_sem


def parse_document_bytes(
    file_bytes: bytes,
    filename: str,
    default_reg: Optional[str] = None,
    default_sem: Optional[str] = None
) -> List[dict]:
    """
    Comprehensive document parser:
    1. Per-page / per-section Subject Code and Semester tracking
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
    file_sem = extract_semester_from_text(filename)
    current_sem = file_sem or (normalize_semester(default_sem) if default_sem and default_sem != 'all' else None)

    # Flexible row pattern for class grade sheet rows:
    # Match: RegNo (8-11 digits), Int marks/status, Ext marks/status, Total marks, Grade (S, A, B, C, D, E, F, etc.)
    row_pattern = r"(\d{8,11})\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+|AB|DT|MP|NE|NA)\s+([\d\.]+)\s+([A-Z\+]{1,3})"

    for p_idx, text in enumerate(pages_text):
        c_code, c_name, c_cred, c_sem = parse_page_text_for_metadata(text)
        if c_code:
            current_code = c_code
        if c_name:
            current_name = c_name
        if c_cred is not None:
            current_credits = c_cred
        if c_sem:
            current_sem = c_sem

        effective_sem = current_sem or infer_semester_from_course_code(current_code)

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
                    "semester": effective_sem
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
                    "semester": effective_sem or infer_semester_from_course_code(code)
                })

    return parsed_courses


def save_courses_to_db(courses: List[dict]):
    """
    Deduplicates courses by (registration_number, course_code, semester) and
    safely upserts/inserts them into the Supabase 'student_grades' table.
    Gracefully handles schemas with or without a 'semester' column.
    """
    if not courses or not supabase:
        return

    # In-memory deduplication: keep cleanest/latest record per (reg, course, semester)
    deduped: Dict[tuple, dict] = {}
    for c in courses:
        r = str(c.get("registration_number", "")).strip()
        code = str(c.get("course_code", "")).strip()
        sem = str(c.get("semester", "")).strip()
        if r and code:
            deduped[(r, code, sem)] = c
    clean_list = list(deduped.values())

    # Chunked upsert/insert
    chunk_size = 50
    for i in range(0, len(clean_list), chunk_size):
        chunk = clean_list[i:i + chunk_size]
        try:
            # 1. Attempt upsert including 'semester'
            supabase.table("student_grades").upsert(
                chunk,
                on_conflict="registration_number,course_code"
            ).execute()
        except Exception as upsert_err:
            err_str = str(upsert_err)
            # If the semester column does not exist in the Supabase schema yet,
            # strip 'semester' and upsert so the database write succeeds without crashing!
            if "semester" in err_str.lower() and "column" in err_str.lower():
                print("[Supabase Notice] 'semester' column not yet in student_grades schema. Stripping column for upsert.")
                stripped_chunk = [{k: v for k, v in row.items() if k != "semester"} for row in chunk]
                try:
                    supabase.table("student_grades").upsert(
                        stripped_chunk,
                        on_conflict="registration_number,course_code"
                    ).execute()
                    continue
                except Exception:
                    pass

            # Row-by-row fallback
            for row in chunk:
                try:
                    supabase.table("student_grades").upsert(
                        row,
                        on_conflict="registration_number,course_code"
                    ).execute()
                except Exception:
                    try:
                        stripped_row = {k: v for k, v in row.items() if k != "semester"}
                        supabase.table("student_grades").upsert(
                            stripped_row,
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
def get_student_records(registration_number: str, semester: Optional[str] = Query(None)):
    """
    1. Queries Supabase 'student_grades' for this student.
    2. If not found, references previously uploaded class files to find and recover grades.
    3. Categorizes courses by semester and computes both per-semester SGPA and cumulative CGPA.
    4. Identifies whether requested semester is present or missing.
    """
    clean_reg = registration_number.strip()
    if not supabase:
        return {
            "found": False,
            "registration_number": clean_reg,
            "total_records": 0,
            "requested_semester": normalize_semester(semester) if semester else "all",
            "is_requested_semester_present": False,
            "active_semester": "Semester 1",
            "available_semesters": [],
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
                    # Deduplicate by course_code and semester
                    d_map = {}
                    for nr in newly_recovered:
                        d_map[(nr.get("course_code"), nr.get("semester"))] = nr
                    records = list(d_map.values())
            except Exception as ref_err:
                print(f"[Reference Upload Notice]: {ref_err}")

        # Ensure every record has a well-defined 'semester'
        for r in records:
            sem = r.get("semester")
            if not sem or str(sem).strip() in ("Unknown", "None", ""):
                sem = infer_semester_from_course_code(r.get("course_code"))
            else:
                sem = normalize_semester(sem)
            r["semester"] = sem

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

        # 4. Group courses by semester & calculate per-semester and cumulative metrics
        grade_points_map = {
            "S": 10.0, "A": 9.0, "B": 8.0, "C": 7.0, "D": 6.0, "E": 5.0, "F": 0.0
        }
        semesters_dict: Dict[str, List[dict]] = {}
        for r in records:
            sem = r.get("semester", "Semester 1")
            if sem not in semesters_dict:
                semesters_dict[sem] = []
            semesters_dict[sem].append(r)

        # Sort available semesters logically (Semester 1, Semester 2, ...)
        available_semesters = sorted(list(semesters_dict.keys()), key=semester_sort_key)

        semester_summaries: Dict[str, dict] = {}
        cum_credits = 0.0
        cum_weighted_points = 0.0
        cum_passed = 0
        cum_backlog = 0

        for sem, sem_courses in semesters_dict.items():
            sem_cred = 0.0
            sem_pts = 0.0
            sem_passed = 0
            sem_backlog = 0
            for c_row in sem_courses:
                c = float(c_row.get("credits") or 0.0)
                g = str(c_row.get("grade") or "").strip().upper()
                pt = grade_points_map.get(g, 0.0)
                sem_cred += c
                sem_pts += (c * pt)
                if g == "F" or pt == 0.0:
                    sem_backlog += 1
                else:
                    sem_passed += 1

            sem_sgpa = round(sem_pts / sem_cred, 2) if sem_cred > 0 else 0.0
            semester_summaries[sem] = {
                "semester": sem,
                "total_credits": sem_cred,
                "weighted_points": round(sem_pts, 1),
                "sgpa": sem_sgpa,
                "passed_count": sem_passed,
                "backlog_count": sem_backlog,
                "courses_count": len(sem_courses),
                "courses": sem_courses
            }

            cum_credits += sem_cred
            cum_weighted_points += sem_pts
            cum_passed += sem_passed
            cum_backlog += sem_backlog

        cgpa = round(cum_weighted_points / cum_credits, 2) if cum_credits > 0 else 0.0
        cumulative_metrics = {
            "total_credits": cum_credits,
            "weighted_points": round(cum_weighted_points, 1),
            "cgpa": cgpa,
            "sgpa": cgpa,  # for backwards compatibility
            "passed_count": cum_passed,
            "backlog_count": cum_backlog
        }

        # 5. Evaluate requested semester vs available semesters
        is_requested_semester_present = True
        notice_message = None
        requested_semester_clean = "all"

        if not semester or str(semester).strip().lower() in ["all", "", "none"]:
            requested_semester_clean = "all"
            active_semester = available_semesters[-1] if available_semesters else "Semester 1"
            active_data = records
            active_metrics = cumulative_metrics
            is_requested_semester_present = len(records) > 0
        else:
            requested_semester_clean = normalize_semester(semester)
            if requested_semester_clean in semester_summaries:
                is_requested_semester_present = True
                active_semester = requested_semester_clean
                active_data = semesters_dict[requested_semester_clean]
                active_metrics = semester_summaries[requested_semester_clean]
            else:
                is_requested_semester_present = False
                active_semester = available_semesters[-1] if available_semesters else requested_semester_clean
                active_data = records
                active_metrics = cumulative_metrics
                notice_message = (
                    f"{requested_semester_clean} results have not been uploaded yet. "
                    f"Available semesters: {', '.join(available_semesters) if available_semesters else 'None'}."
                )

        return {
            "found": len(records) > 0,
            "registration_number": clean_reg,
            "total_records": len(records),
            "requested_semester": requested_semester_clean,
            "is_requested_semester_present": is_requested_semester_present,
            "active_semester": active_semester,
            "available_semesters": available_semesters,
            "semesters_breakdown": semester_summaries,
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
            "requested_semester": normalize_semester(semester) if semester else "all",
            "is_requested_semester_present": False,
            "active_semester": "Semester 1",
            "available_semesters": [],
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
    target_semester: Optional[str] = Form(None, description="Optional target semester for the upload")
):
    """
    Class-Wide Document Pipeline:
    - Parses all subjects, semesters, and student records from uploaded files
    - Saves the raw file to Supabase Storage ('transcripts/shared/{filename}')
    - Records file metadata in 'uploaded_files'
    - Upserts ALL extracted student grades into 'student_grades' with multi-semester support!
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
        file_courses = parse_document_bytes(file_bytes, filename, registration_number, target_semester)
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

    detected_semesters = sorted(list(set(c.get("semester") for c in all_parsed_courses if c.get("semester"))), key=semester_sort_key)

    return {
        "total_records_found": len(all_parsed_courses),
        "detected_semesters": detected_semesters,
        "data": all_parsed_courses,
        "uploaded_files": uploaded_files_summary
    }

