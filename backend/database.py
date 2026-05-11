import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "timetable.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    c = conn.cursor()

    # ── 1. FACULTIES ──────────────────────────────────────────────────────────
    # One row per real person. normalized_name is lowercase, title-stripped.
    c.execute("""
        CREATE TABLE IF NOT EXISTS faculties (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name       TEXT NOT NULL,          -- as printed in PDF
            normalized_name TEXT NOT NULL UNIQUE,   -- lowercase, no Dr./Mr./Ms.
            email           TEXT,
            password_hash   TEXT,
            department      TEXT,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── 2. FACULTY ABBREVIATIONS ──────────────────────────────────────────────
    # Maps abbreviation + branch to a faculty.
    # Same abbreviation in two branches can point to DIFFERENT faculty rows.
    c.execute("""
        CREATE TABLE IF NOT EXISTS faculty_abbreviations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id  INTEGER NOT NULL REFERENCES faculties(id),
            abbreviation TEXT NOT NULL,
            branch      TEXT NOT NULL,   -- e.g. BTech, BCA, MCA
            year        INTEGER,         -- e.g. 3
            UNIQUE(abbreviation, branch, year)
        )
    """)

    # ── 3. SECTIONS ───────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS sections (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,    -- CSE-A
            branch           TEXT NOT NULL,    -- BTech
            year             INTEGER NOT NULL, -- 3
            semester         TEXT,             -- VI
            room_no          TEXT,
            program          TEXT,             -- B.Tech.
            class_teacher_id INTEGER REFERENCES faculties(id),
            effective_from   TEXT,
            UNIQUE(name, branch, year)
        )
    """)

    # ── 4. TIMETABLE ──────────────────────────────────────────────────────────
    # One row per (section × day × slot × batch).
    # batch = 'ALL', 'B1', 'B2', 'B3', 'B1+B2', 'B2+B3', etc.
    c.execute("""
        CREATE TABLE IF NOT EXISTS timetable (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id   INTEGER NOT NULL REFERENCES sections(id),
            faculty_id   INTEGER NOT NULL REFERENCES faculties(id),
            day          TEXT NOT NULL,   -- Monday … Saturday
            slot         TEXT NOT NULL,   -- L1 … L6
            time_range   TEXT,            -- 10:15-11:15
            subject_code TEXT,
            subject_name TEXT,
            batch        TEXT DEFAULT 'ALL',
            is_lab       INTEGER DEFAULT 0,  -- 1 if (P) suffix
            lab_code     TEXT,               -- e.g. SEL, OOT, DSL …
            room_no      TEXT                -- inherits from section unless overridden
        )
    """)

    # ── 5. ATTENDANCE LOG ─────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id  INTEGER NOT NULL REFERENCES faculties(id),
            date        TEXT NOT NULL,   -- YYYY-MM-DD
            status      TEXT NOT NULL CHECK(status IN ('present','absent')),
            marked_at   TEXT DEFAULT (datetime('now')),
            marked_by   TEXT DEFAULT 'self',   -- 'self' or 'admin'
            source      TEXT DEFAULT 'manual', -- 'manual' / 'biometric' (future)
            UNIQUE(faculty_id, date)
        )
    """)

    # ── 6. PDF UPLOADS ────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS pdf_uploads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT NOT NULL,
            branch      TEXT NOT NULL,
            year        INTEGER NOT NULL,
            section     TEXT,            -- auto-detected from PDF
            uploaded_at TEXT DEFAULT (datetime('now')),
            uploaded_by TEXT,
            status      TEXT DEFAULT 'pending'
                        CHECK(status IN ('pending','processed','conflict','done'))
        )
    """)

    # ── 7. CONFLICTS ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS conflicts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_name TEXT NOT NULL,
            branch_1     TEXT,
            abbr_1       TEXT,
            branch_2     TEXT,
            abbr_2       TEXT,
            section_1    TEXT,
            section_2    TEXT,
            conflict_type TEXT,   -- 'same_abbr_diff_name' | 'same_name_diff_abbr'
            status       TEXT DEFAULT 'pending'
                         CHECK(status IN ('pending','merged','separated')),
            resolved_by  TEXT,
            resolved_at  TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── 8. FACULTY DUPLICATE REVIEWS ────────────────────────────────────────
    # Stores possible duplicate faculty pairs for later admin review.
    c.execute("""
        CREATE TABLE IF NOT EXISTS faculty_duplicate_reviews (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            existing_faculty_id INTEGER NOT NULL REFERENCES faculties(id),
            candidate_faculty_id INTEGER NOT NULL REFERENCES faculties(id),
            existing_name       TEXT NOT NULL,
            candidate_name      TEXT NOT NULL,
            similarity_score    REAL NOT NULL,
            branch              TEXT,
            year                INTEGER,
            pair_key            TEXT NOT NULL UNIQUE,
            status              TEXT DEFAULT 'pending'
                                CHECK(status IN ('pending','merged','different')),
            reviewed_by         TEXT,
            reviewed_at         TEXT,
            created_at          TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── 9. ADMINS ─────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    print("✅ All 8 tables created successfully.")


if __name__ == "__main__":
    init_db()