"""
app.py — SmartCampus Flask Backend
Serves all APIs for Student, Faculty, and Admin portals.
"""

from flask import Flask, request, jsonify, session
from flask_cors import CORS
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, date
from database import get_connection, init_db
from parser import parse_pdf, save_to_db

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
CORS(app, supports_credentials=True)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Helpers ────────────────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def today_str() -> str:
    return date.today().isoformat()

def now_day() -> str:
    """Return current day name e.g. 'Monday'"""
    return datetime.now().strftime('%A')

def current_slot() -> str | None:
    """Return current time slot label based on real time."""
    now = datetime.now().strftime('%H:%M')
    slots = {
        'L1': ('10:15', '11:15'),
        'L2': ('11:15', '12:15'),
        'L3': ('12:45', '13:45'),
        'L4': ('13:45', '14:45'),
        'L5': ('14:45', '15:45'),
        'L6': ('15:45', '16:45'),
    }
    for label, (start, end) in slots.items():
        if start <= now < end:
            return label
    return None

def row_to_dict(row) -> dict:
    return dict(row) if row else {}

def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]

def error(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code

def ok(data=None, **kwargs):
    resp = {'success': True}
    if data is not None:
        resp['data'] = data
    resp.update(kwargs)
    return jsonify(resp)

def require_admin():
    if not session.get('admin_logged_in'):
        return error('Admin login required', 401)
    return None

def require_faculty():
    if not session.get('faculty_id'):
        return error('Faculty login required', 401)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    conn = get_connection()
    row = conn.execute(
        'SELECT * FROM admins WHERE username=? AND password_hash=?',
        (username, hash_password(password))
    ).fetchone()
    conn.close()

    if not row:
        return error('Invalid username or password', 401)

    session['admin_logged_in'] = True
    session['admin_username']  = username
    return ok({'username': username})


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    return ok()


@app.route('/api/admin/setup', methods=['POST'])
def admin_setup():
    """
    One-time admin account creation.
    Only works if no admin exists yet.
    """
    conn = get_connection()
    existing = conn.execute('SELECT id FROM admins').fetchone()
    if existing:
        conn.close()
        return error('Admin already exists', 403)

    data = request.json or {}
    username = data.get('username', 'admin').strip()
    password = data.get('password', '').strip()
    if not password:
        conn.close()
        return error('Password required')

    conn.execute(
        'INSERT INTO admins (username, password_hash) VALUES (?, ?)',
        (username, hash_password(password))
    )
    conn.commit()
    conn.close()
    return ok({'message': 'Admin account created'})


@app.route('/api/admin/upload', methods=['POST'])
def admin_upload():
    """Upload and parse one or more timetable PDFs."""
    guard = require_admin()
    if guard: return guard

    branch = request.form.get('branch', '').strip()
    year   = request.form.get('year', '').strip()

    if not branch or not year:
        return error('Branch and year are required')

    try:
        year = int(year)
    except ValueError:
        return error('Year must be a number')

    files = request.files.getlist('pdfs')
    if not files:
        return error('No PDF files uploaded')

    all_saved = []
    for f in files:
        if not f.filename.lower().endswith('.pdf'):
            continue
        save_path = os.path.join(UPLOAD_FOLDER, f.filename)
        f.save(save_path)

        print(f"\n📄 Parsing: {f.filename}")
        results = parse_pdf(save_path, branch=branch, year=year)
        saved   = save_to_db(results, branch=branch, year=year, pdf_filename=f.filename)
        all_saved.extend(saved)

    return ok({'saved_sections': all_saved})


@app.route('/api/admin/uploads', methods=['GET'])
def admin_uploads():
    """List all uploaded PDFs with status."""
    guard = require_admin()
    if guard: return guard

    conn = get_connection()
    rows = conn.execute(
        'SELECT * FROM pdf_uploads ORDER BY uploaded_at DESC'
    ).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/admin/conflicts', methods=['GET'])
def admin_conflicts():
    """Return all unresolved conflicts."""
    guard = require_admin()
    if guard: return guard

    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM conflicts WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/admin/conflicts/<int:conflict_id>/resolve', methods=['POST'])
def resolve_conflict(conflict_id):
    """Resolve a conflict: merge or separate."""
    guard = require_admin()
    if guard: return guard

    data   = request.json or {}
    action = data.get('action')  # 'merged' or 'separated'

    if action not in ('merged', 'separated'):
        return error('action must be "merged" or "separated"')

    conn = get_connection()
    conn.execute(
        "UPDATE conflicts SET status=?, resolved_by=?, resolved_at=datetime('now') WHERE id=?",
        (action, session.get('admin_username'), conflict_id)
    )
    conn.commit()
    conn.close()
    return ok()


@app.route('/api/admin/faculty', methods=['GET'])
def admin_faculty_list():
    """List all faculty."""
    guard = require_admin()
    if guard: return guard

    conn = get_connection()
    rows = conn.execute(
        'SELECT id, full_name, normalized_name, department, email, is_active FROM faculties ORDER BY full_name'
    ).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/admin/faculty/<int:fid>/create-account', methods=['POST'])
def create_faculty_account(fid):
    """Set email + password for a faculty so they can log in."""
    guard = require_admin()
    if guard: return guard

    data  = request.json or {}
    email = data.get('email', '').strip()
    pw    = data.get('password', '').strip()

    if not email or not pw:
        return error('Email and password required')

    conn = get_connection()
    conn.execute(
        'UPDATE faculties SET email=?, password_hash=? WHERE id=?',
        (email, hash_password(pw), fid)
    )
    conn.commit()
    conn.close()
    return ok()


@app.route('/api/admin/sections', methods=['GET'])
def admin_sections():
    """List all uploaded sections."""
    guard = require_admin()
    if guard: return guard

    conn = get_connection()
    rows = conn.execute(
        'SELECT * FROM sections ORDER BY branch, year, name'
    ).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


def _merge_faculty_records(conn, keep_faculty_id: int, merge_faculty_id: int):
    """Move timetable and related references from one faculty row into another."""
    if keep_faculty_id == merge_faculty_id:
        return

    c = conn.cursor()
    c.execute('UPDATE timetable SET faculty_id=? WHERE faculty_id=?', (keep_faculty_id, merge_faculty_id))
    c.execute('UPDATE attendance_log SET faculty_id=? WHERE faculty_id=?', (keep_faculty_id, merge_faculty_id))
    c.execute('UPDATE sections SET class_teacher_id=? WHERE class_teacher_id=?', (keep_faculty_id, merge_faculty_id))

    abbr_rows = c.execute(
        'SELECT id, abbreviation, branch, year FROM faculty_abbreviations WHERE faculty_id=?',
        (merge_faculty_id,)
    ).fetchall()
    for row in abbr_rows:
        existing = c.execute(
            '''
            SELECT 1 FROM faculty_abbreviations
            WHERE faculty_id=? AND abbreviation=? AND branch=? AND year IS ?
            ''',
            (keep_faculty_id, row['abbreviation'], row['branch'], row['year'])
        ).fetchone()
        if existing:
            c.execute('DELETE FROM faculty_abbreviations WHERE id=?', (row['id'],))
        else:
            c.execute('UPDATE faculty_abbreviations SET faculty_id=? WHERE id=?', (keep_faculty_id, row['id']))

    c.execute('UPDATE faculties SET is_active=0 WHERE id=?', (merge_faculty_id,))


@app.route('/api/admin/faculty-duplicates', methods=['GET'])
def admin_faculty_duplicates():
    """List pending faculty duplicate reviews."""
    guard = require_admin()
    if guard: return guard

    conn = get_connection()
    rows = conn.execute(
        '''
        SELECT r.*, 
               f1.full_name AS existing_faculty_name,
               f2.full_name AS candidate_faculty_name
        FROM faculty_duplicate_reviews r
        LEFT JOIN faculties f1 ON f1.id = r.existing_faculty_id
        LEFT JOIN faculties f2 ON f2.id = r.candidate_faculty_id
        ORDER BY r.created_at DESC
        '''
    ).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/admin/faculty-duplicates/<int:review_id>/resolve', methods=['POST'])
def resolve_faculty_duplicate(review_id):
    """Merge duplicate faculty rows or mark them as different people."""
    guard = require_admin()
    if guard: return guard

    data = request.json or {}
    action = data.get('action')

    if action not in ('merge', 'different'):
        return error('action must be "merge" or "different"')

    conn = get_connection()
    review = conn.execute(
        'SELECT * FROM faculty_duplicate_reviews WHERE id=?',
        (review_id,)
    ).fetchone()

    if not review:
        conn.close()
        return error('Review not found', 404)

    try:
        if action == 'merge':
            _merge_faculty_records(conn, review['existing_faculty_id'], review['candidate_faculty_id'])
            new_status = 'merged'
        else:
            new_status = 'different'

        conn.execute(
            '''
            UPDATE faculty_duplicate_reviews
            SET status=?, reviewed_by=?, reviewed_at=datetime('now')
            WHERE id=?
            ''',
            (new_status, session.get('admin_username'), review_id)
        )
        conn.commit()
        conn.close()
        return ok()
    except Exception as e:
        conn.rollback()
        conn.close()
        return error(str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════════
# FACULTY ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/faculty/login', methods=['POST'])
def faculty_login():
    data  = request.json or {}
    email = data.get('email', '').strip()
    pw    = data.get('password', '').strip()

    conn = get_connection()
    row  = conn.execute(
        'SELECT * FROM faculties WHERE email=? AND password_hash=?',
        (email, hash_password(pw))
    ).fetchone()
    conn.close()

    if not row:
        return error('Invalid email or password', 401)

    session['faculty_id']   = row['id']
    session['faculty_name'] = row['full_name']
    return ok({'id': row['id'], 'name': row['full_name']})


@app.route('/api/faculty/logout', methods=['POST'])
def faculty_logout():
    session.pop('faculty_id', None)
    session.pop('faculty_name', None)
    return ok()


@app.route('/api/faculty/me', methods=['GET'])
def faculty_me():
    guard = require_faculty()
    if guard: return guard

    fid  = session['faculty_id']
    conn = get_connection()
    row  = conn.execute(
        'SELECT id, full_name, email, department FROM faculties WHERE id=?', (fid,)
    ).fetchone()
    conn.close()
    return ok(row_to_dict(row))


@app.route('/api/faculty/schedule', methods=['GET'])
def faculty_schedule():
    """Full week schedule for the logged-in faculty."""
    guard = require_faculty()
    if guard: return guard

    fid  = session['faculty_id']
    conn = get_connection()
    rows = conn.execute('''
        SELECT t.day, t.slot, t.time_range, t.subject_code, t.subject_name,
               t.batch, t.is_lab, t.lab_code, t.room_no,
               s.name as section_name, s.branch, s.year
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        WHERE t.faculty_id = ?
        ORDER BY
            CASE t.day
                WHEN 'Monday'    THEN 1
                WHEN 'Tuesday'   THEN 2
                WHEN 'Wednesday' THEN 3
                WHEN 'Thursday'  THEN 4
                WHEN 'Friday'    THEN 5
                WHEN 'Saturday'  THEN 6
            END,
            t.slot
    ''', (fid,)).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/faculty/today', methods=['GET'])
def faculty_today():
    """Today's schedule for logged-in faculty."""
    guard = require_faculty()
    if guard: return guard

    fid = session['faculty_id']
    day = now_day()

    conn = get_connection()
    rows = conn.execute('''
        SELECT t.slot, t.time_range, t.subject_code, t.subject_name,
               t.batch, t.is_lab, t.lab_code, t.room_no,
               s.name as section_name
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        WHERE t.faculty_id=? AND t.day=?
        ORDER BY t.slot
    ''', (fid, day)).fetchall()
    conn.close()
    return ok({'day': day, 'schedule': rows_to_list(rows)})


@app.route('/api/faculty/attendance', methods=['POST'])
def mark_attendance():
    """Faculty marks themselves present or absent for today."""
    guard = require_faculty()
    if guard: return guard

    data   = request.json or {}
    status = data.get('status')

    if status not in ('present', 'absent'):
        return error('status must be "present" or "absent"')

    fid  = session['faculty_id']
    today = today_str()

    conn = get_connection()
    # Upsert: replace if already marked
    conn.execute('''
        INSERT INTO attendance_log (faculty_id, date, status, marked_by)
        VALUES (?, ?, ?, 'self')
        ON CONFLICT(faculty_id, date) DO UPDATE SET status=excluded.status, marked_at=datetime('now')
    ''', (fid, today, status))
    conn.commit()
    conn.close()
    return ok({'date': today, 'status': status})


@app.route('/api/faculty/attendance/status', methods=['GET'])
def faculty_attendance_status():
    """Check if faculty has marked attendance today."""
    guard = require_faculty()
    if guard: return guard

    fid   = session['faculty_id']
    today = today_str()

    conn = get_connection()
    row  = conn.execute(
        'SELECT status, marked_at FROM attendance_log WHERE faculty_id=? AND date=?',
        (fid, today)
    ).fetchone()
    conn.close()

    if row:
        return ok({'marked': True, 'status': row['status'], 'marked_at': row['marked_at']})
    return ok({'marked': False, 'status': None})


# ═══════════════════════════════════════════════════════════════════════════════
# STUDENT ROUTES  (no login required)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/faculty/search', methods=['GET'])
def search_faculty():
    """Search faculty by name (partial match)."""
    q = request.args.get('q', '').strip().lower()
    if not q:
        return error('Search query required')

    conn = get_connection()
    rows = conn.execute('''
        SELECT id, full_name, department
        FROM faculties
        WHERE LOWER(full_name) LIKE ? AND is_active=1
        ORDER BY full_name
    ''', (f'%{q}%',)).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/faculty/<int:fid>/where-now', methods=['GET'])
def faculty_where_now(fid):
    """
    Where is this faculty RIGHT NOW?
    Checks current day + current time slot.
    Also checks attendance — if marked absent, says so.
    """
    today = today_str()
    day   = now_day()
    slot  = current_slot()

    conn = get_connection()

    # Check attendance
    att = conn.execute(
        'SELECT status FROM attendance_log WHERE faculty_id=? AND date=?',
        (fid, today)
    ).fetchone()

    if att and att['status'] == 'absent':
        fac = conn.execute('SELECT full_name FROM faculties WHERE id=?', (fid,)).fetchone()
        conn.close()
        return ok({
            'faculty_name': fac['full_name'] if fac else '',
            'status': 'absent',
            'message': 'Faculty has marked themselves absent today'
        })

    if not slot:
        fac = conn.execute('SELECT full_name FROM faculties WHERE id=?', (fid,)).fetchone()
        conn.close()
        return ok({
            'faculty_name': fac['full_name'] if fac else '',
            'status': 'no_class',
            'message': 'No ongoing class slot right now'
        })

    rows = conn.execute('''
        SELECT t.slot, t.time_range, t.subject_code, t.subject_name,
               t.batch, t.is_lab, t.lab_code, t.room_no,
               s.name as section_name, s.branch, s.year,
               f.full_name as faculty_name
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        JOIN faculties f ON f.id = t.faculty_id
        WHERE t.faculty_id=? AND t.day=? AND t.slot=?
    ''', (fid, day, slot)).fetchall()
    conn.close()

    if not rows:
        fac_row = get_connection().execute(
            'SELECT full_name FROM faculties WHERE id=?', (fid,)
        ).fetchone()
        return ok({
            'faculty_name': fac_row['full_name'] if fac_row else '',
            'status': 'free',
            'message': f'Faculty is free during {slot}'
        })

    return ok({
        'status': 'in_class',
        'slot': slot,
        'classes': rows_to_list(rows)
    })


@app.route('/api/faculty/<int:fid>/schedule', methods=['GET'])
def faculty_public_schedule(fid):
    """Full week schedule for any faculty (student view)."""
    conn = get_connection()
    fac  = conn.execute('SELECT full_name FROM faculties WHERE id=?', (fid,)).fetchone()
    if not fac:
        conn.close()
        return error('Faculty not found', 404)

    rows = conn.execute('''
        SELECT t.day, t.slot, t.time_range, t.subject_code, t.subject_name,
               t.batch, t.is_lab, t.lab_code, t.room_no,
               s.name as section_name
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        WHERE t.faculty_id=?
        ORDER BY
            CASE t.day
                WHEN 'Monday'    THEN 1 WHEN 'Tuesday' THEN 2
                WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4
                WHEN 'Friday'    THEN 5 WHEN 'Saturday' THEN 6
            END, t.slot
    ''', (fid,)).fetchall()
    conn.close()

    return ok({
        'faculty_name': fac['full_name'],
        'schedule': rows_to_list(rows)
    })


@app.route('/api/faculty/<int:fid>/free-slots', methods=['GET'])
def faculty_free_slots(fid):
    """When is this faculty free? (today or a specific day)"""
    day = request.args.get('day', now_day())

    ALL_SLOTS = ['L1', 'L2', 'L3', 'L4', 'L5', 'L6']

    conn = get_connection()
    fac  = conn.execute('SELECT full_name FROM faculties WHERE id=?', (fid,)).fetchone()
    if not fac:
        conn.close()
        return error('Faculty not found', 404)

    busy_rows = conn.execute('''
        SELECT DISTINCT t.slot FROM timetable t
        WHERE t.faculty_id=? AND t.day=?
    ''', (fid, day)).fetchall()
    conn.close()

    busy_slots = {r['slot'] for r in busy_rows}
    free_slots = [s for s in ALL_SLOTS if s not in busy_slots]

    return ok({
        'faculty_name': fac['full_name'],
        'day': day,
        'free_slots': free_slots,
        'busy_slots': list(busy_slots)
    })


@app.route('/api/rooms/free', methods=['GET'])
def free_rooms():
    """
    Which rooms/labs are free at a given day + slot?
    ?day=Monday&slot=L1
    """
    day  = request.args.get('day', now_day())
    slot = request.args.get('slot', current_slot())

    if not slot:
        return error('No slot specified and no class is running right now')

    conn = get_connection()

    # All known rooms from sections
    all_rooms = conn.execute(
        "SELECT DISTINCT room_no FROM sections WHERE room_no != ''"
    ).fetchall()
    all_rooms = {r['room_no'] for r in all_rooms}

    # All known lab codes
    all_labs = conn.execute(
        "SELECT DISTINCT lab_code FROM timetable WHERE lab_code != '' AND lab_code IS NOT NULL"
    ).fetchall()
    all_labs = {r['lab_code'] for r in all_labs}

    # Busy rooms (section rooms)
    busy_rooms = conn.execute('''
        SELECT DISTINCT s.room_no
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        WHERE t.day=? AND t.slot=? AND s.room_no != ''
    ''', (day, slot)).fetchall()
    busy_rooms = {r['room_no'] for r in busy_rooms}

    # Busy labs
    busy_labs = conn.execute('''
        SELECT DISTINCT lab_code
        FROM timetable
        WHERE day=? AND slot=? AND is_lab=1 AND lab_code != ''
    ''', (day, slot)).fetchall()
    busy_labs = {r['lab_code'] for r in busy_labs}

    conn.close()

    return ok({
        'day': day,
        'slot': slot,
        'free_classrooms': sorted(all_rooms - busy_rooms),
        'free_labs':       sorted(all_labs - busy_labs),
        'busy_classrooms': sorted(busy_rooms),
        'busy_labs':       sorted(busy_labs),
    })


@app.route('/api/sections', methods=['GET'])
def list_sections():
    """List all sections, optionally filtered by branch/year."""
    branch = request.args.get('branch')
    year   = request.args.get('year')

    conn = get_connection()
    q    = 'SELECT * FROM sections WHERE 1=1'
    params = []
    if branch:
        q += ' AND branch=?'; params.append(branch)
    if year:
        q += ' AND year=?';   params.append(int(year))
    q += ' ORDER BY branch, year, name'

    rows = conn.execute(q, params).fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/sections/<string:section_name>/timetable', methods=['GET'])
def section_timetable(section_name):
    """Full timetable for a section."""
    branch = request.args.get('branch', 'BTech')
    year   = request.args.get('year', 3, type=int)

    conn = get_connection()
    sec  = conn.execute(
        'SELECT id, name, room_no, semester FROM sections WHERE name=? AND branch=? AND year=?',
        (section_name, branch, year)
    ).fetchone()

    if not sec:
        conn.close()
        return error(f'Section {section_name} not found', 404)

    rows = conn.execute('''
        SELECT t.day, t.slot, t.time_range, t.subject_code, t.subject_name,
               t.batch, t.is_lab, t.lab_code, t.room_no,
               f.full_name as faculty_name, f.id as faculty_id
        FROM timetable t
        JOIN faculties f ON f.id = t.faculty_id
        WHERE t.section_id=?
        ORDER BY
            CASE t.day
                WHEN 'Monday'    THEN 1 WHEN 'Tuesday' THEN 2
                WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4
                WHEN 'Friday'    THEN 5 WHEN 'Saturday' THEN 6
            END, t.slot, t.batch
    ''', (sec['id'],)).fetchall()
    conn.close()

    return ok({
        'section': dict(sec),
        'timetable': rows_to_list(rows)
    })


@app.route('/api/faculty/free-at', methods=['GET'])
def faculty_free_at():
    """
    Which faculty are free at a given day + slot?
    ?day=Monday&slot=L2
    Useful for admin to find substitute teachers.
    """
    day  = request.args.get('day', now_day())
    slot = request.args.get('slot', current_slot())

    if not slot:
        return error('Slot is required')

    conn = get_connection()

    busy_ids = conn.execute('''
        SELECT DISTINCT faculty_id FROM timetable
        WHERE day=? AND slot=?
    ''', (day, slot)).fetchall()
    busy_ids = {r['faculty_id'] for r in busy_ids}

    all_faculty = conn.execute(
        'SELECT id, full_name, department FROM faculties WHERE is_active=1'
    ).fetchall()

    free_faculty = [
        row_to_dict(f) for f in all_faculty
        if f['id'] not in busy_ids
    ]
    conn.close()

    return ok({
        'day': day,
        'slot': slot,
        'free_faculty': free_faculty
    })


@app.route('/api/attendance/today', methods=['GET'])
def attendance_today():
    """
    Today's attendance status for all faculty.
    Faculty who haven't marked = unknown.
    """
    today = today_str()
    conn  = get_connection()

    rows = conn.execute('''
        SELECT f.id, f.full_name, f.department,
               COALESCE(a.status, 'unknown') as status,
               a.marked_at
        FROM faculties f
        LEFT JOIN attendance_log a
            ON a.faculty_id = f.id AND a.date = ?
        WHERE f.is_active = 1
        ORDER BY f.full_name
    ''', (today,)).fetchall()
    conn.close()

    return ok({'date': today, 'faculty': rows_to_list(rows)})


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYTICS  (for visualisations / DS angle)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/analytics/faculty-load', methods=['GET'])
def faculty_load():
    """How many lecture slots does each faculty teach per week?"""
    conn = get_connection()
    rows = conn.execute('''
        SELECT f.full_name, f.department,
               COUNT(*) as total_slots,
               SUM(t.is_lab) as lab_slots,
               COUNT(*) - SUM(t.is_lab) as lecture_slots
        FROM timetable t
        JOIN faculties f ON f.id = t.faculty_id
        GROUP BY t.faculty_id
        ORDER BY total_slots DESC
    ''').fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/analytics/room-usage', methods=['GET'])
def room_usage():
    """How many slots is each room occupied per week?"""
    conn = get_connection()
    rows = conn.execute('''
        SELECT s.room_no,
               COUNT(DISTINCT t.day || t.slot) as occupied_slots
        FROM timetable t
        JOIN sections s ON s.id = t.section_id
        WHERE s.room_no != ''
        GROUP BY s.room_no
        ORDER BY occupied_slots DESC
    ''').fetchall()
    conn.close()
    return ok(rows_to_list(rows))


@app.route('/api/analytics/lab-usage', methods=['GET'])
def lab_usage():
    """How many slots is each lab occupied per week?"""
    conn = get_connection()
    rows = conn.execute('''
        SELECT lab_code,
               COUNT(DISTINCT day || slot) as occupied_slots,
               COUNT(*) as total_bookings
        FROM timetable
        WHERE is_lab=1 AND lab_code != ''
        GROUP BY lab_code
        ORDER BY occupied_slots DESC
    ''').fetchall()
    conn.close()
    return ok(rows_to_list(rows))


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()

    # Create default admin if none exists
    conn = get_connection()
    existing = conn.execute('SELECT id FROM admins').fetchone()
    if not existing:
        conn.execute(
            'INSERT INTO admins (username, password_hash) VALUES (?, ?)',
            ('admin', hash_password('admin123'))
        )
        conn.commit()
        print("✅ Default admin created  →  username: admin  |  password: admin123")
    conn.close()

    print("🚀 SmartCampus API running at http://localhost:5000")
    app.run(debug=True, port=5000)