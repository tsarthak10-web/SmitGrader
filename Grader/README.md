# GradeFlow 🎓

> **Automated Academic Transcript Parsing, Cloud Archival & Real-Time GPA Intelligence Engine**

GradeFlow is a high-performance web platform designed to streamline student grade verification, transcript analysis, and GPA projections. It replaces slow manual entry with automated document parsing (PDF and TXT), an industry-standard 3-layer cloud storage architecture, and an interactive "What-If" GPA simulator.

---

## 🌟 Key Features

- **Automated Transcript Extraction**: Parses complex university grade sheets (both structured `.txt` rosters and visual `.pdf` transcripts) using regular expressions and `pdfplumber`.
- **Instant Server Search ($O(\log N)$)**: Queries student records in sub-millisecond time via PostgreSQL B-Tree indexing on registration numbers—no need to select branches or departments.
- **3-Layer Storage Architecture**:
  - **Layer A (Cloud Raw Storage)**: Retains original transcript files in Supabase Storage under `transcripts/{registration_number}/{filename}`.
  - **Layer B (Metadata Tracking)**: Logs upload events in `uploaded_files` to provide audit history and instant document links.
  - **Layer C (Structured Grade Database)**: Records parsed course codes, credits, and marks in `student_grades` with idempotent upserting to prevent duplicate record spam.
- **Interactive "What-If" CGPA Simulator**: Allows students to simulate future semester scenarios with dynamic grade point recalculation.
- **Verification & Review Pipeline**: Lets students and administrators review, edit, or adjust courses before generating official grade records.
- **Official Export & Grievance Tickets**: Generates printable grade summaries and digital grievance dispute tickets (`#DISP-XXXXXX`).

---

## 🏛️ System Architecture

```text
[ User Browser ]
       │
       ├── (1) Query Student ────► FastAPI (GET /api/student/{reg})
       │                                  │
       │                                  ▼
       │                          [ PostgreSQL Index ] ──► Instant Match? ──► [ verify.html ]
       │                                  │
       │                                  ▼ No Records
       │                          Prompt Upload ──► [ upload.html ]
       │                                  │
       ├── (2) Upload Transcript(s) ──────┘
       │            │
       │            ▼
       │     FastAPI (POST /api/upload)
       │            │
       │            ├── Layer A: Store Original PDF/TXT ──► [ Supabase Storage: 'transcripts' ]
       │            ├── Layer B: Log Upload Event ───────► [ PostgreSQL: 'uploaded_files' ]
       │            └── Layer C: Parse & Idempotent Upsert ─► [ PostgreSQL: 'student_grades' ]
       │
       ▼
[ verify.html ] ──► Adjust Credits/Grades ──► [ results.html ] (Honors, PDF Export)
```

---

## 📁 Repository Structure

```text
Grader/
├── index.html            # Main dashboard, registration query & What-If simulator
├── upload.html           # Multi-file drag-and-drop transcript ingestion
├── verify.html           # Live course verification, credit adjustment & document preview
├── results.html          # Performance metrics, honors badges, transcript PDF export
└── Backend/
    ├── main.py           # FastAPI application & Supabase integration pipeline
    ├── schema.sql        # Database schema, B-tree indexes, and storage policies
    ├── requirements.txt  # Python package dependencies
    ├── .env.example      # Template for environment credentials
    └── .gitignore        # Prevents secret and temporary file leaks
```

---

## 🚀 Getting Started

### 1. Prerequisites
- **Python 3.10+**
- A free **[Supabase](https://supabase.com)** project

### 2. Configure Environment Variables
Inside `Backend/`, create a `.env` file from the example template:
```bash
cd Backend
cp .env.example .env
```
Fill in your Supabase credentials:
```env
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your-supabase-service-role-or-anon-key
```

### 3. Run Database Schema & Storage Setup
Open your **Supabase Dashboard** $\rightarrow$ **SQL Editor** $\rightarrow$ **New Query**, paste the contents of [`Backend/schema.sql`](Backend/schema.sql), and click **Run**. This will:
- Create `student_grades` and `uploaded_files` tables.
- Add B-Tree indexes on `registration_number`.
- Create a composite unique index on `(registration_number, course_code)` for idempotent upserts.
- Provision the public `transcripts` Supabase Storage bucket and access policies.

### 4. Install Dependencies
```bash
cd Backend
pip install -r requirements.txt
```

### 5. Start the Backend Server
```bash
python -m uvicorn main:app --reload
```
The API documentation will be accessible at:
- **Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

### 6. Launch the Frontend
Open `index.html` in any modern web browser or run with a local static server:
```bash
python -m http.server 3000
```
Navigate to `http://localhost:3000`.

---

## 📡 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/wakeup` | Health check endpoint for server pre-warming. |
| `GET` | `/api/student/{registration_number}` | Queries indexed grades, upload history, and computes instant performance metrics. |
| `POST` | `/api/upload` | Ingests batch transcripts (PDF/TXT), uploads raw files to Supabase Storage, and upserts courses. |

---

## 🔒 Security
- `.env` and sensitive API keys are ignored by `.gitignore` and must never be committed to source control.
- Supabase Row Level Security (RLS) policies govern table access.

---

## 📄 License
MIT License. Created by Sarthak Tiwari.

