"""
parser.py — SmartCampus timetable PDF parser

Uses TABLE-based extraction so day columns are always preserved correctly.

Cell entry format (2–3 lines per lecture):
    BTCS708N(P)B1   ← subject_code + (P) = lab + optional batch suffix
    CS-PG           ← faculty abbreviation
    SEL             ← lab room code (ONLY present when subject has (P))

Without (P):
    BTCS601N        ← subject_code, regular lecture
    CS-AS           ← faculty abbreviation
    (no 3rd line)
"""

import pdfplumber
import re
import os
import sys
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(__file__))
from database import get_connection

# ── Constants ──────────────────────────────────────────────────────────────────

DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

TIME_SLOTS = {
    'L1': '10:15-11:15',
    'L2': '11:15-12:15',
    'L3': '12:45-13:45',
    'L4': '13:45-14:45',
    'L5': '14:45-15:45',
    'L6': '15:45-16:45',
}

# Lab room codes seen in the PDF (short ALL-CAPS identifiers after faculty abbr in lab slots)
LAB_CODES = {'SEL', 'BDH', 'OOT', 'DSL', 'MLL', 'DS', 'WDL', 'NCL', 'PL_IT'}

# Faculty abbreviation pattern
# Handles prefixed: CS-AS, AI-KB, IS-GS, HU-SJ, MBA-NSR, ISCC-SK
# Also bare suffixes from splits like CS-JD/TK → TK has no prefix
ABBR_RE = re.compile(r'^(CS|AI|IS|HU|MBA|ISCC)-[\w]+$|^[A-Z]{2,5}$')

# Slot label rows and their paired data rows in the 27-row table
SLOT_ROW_PAIRS = [
    ('L1',  5,  6),
    ('L2',  7,  8),
    ('L3', 10, 11),
    ('L4', 12, 13),
    ('L5', 14, 15),
    ('L6', 16, 17),
]


# ── Name normalisation ─────────────────────────────────────────────────────────

def normalize_name(raw: str) -> str:
    """Lowercase, strip honorifics, collapse whitespace."""
    name = re.sub(r'^(Dr\.|Mr\.|Ms\.|Mrs\.|Prof\.)\s*', '', raw.strip(), flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', name).strip().lower()


def _name_tokens(raw: str) -> list[str]:
    """Return normalized name tokens for duplicate detection."""
    return [token for token in re.sub(r'[^a-z0-9\s]+', ' ', normalize_name(raw)).split() if token]


def _token_based_duplicate_match(name_a: str, name_b: str) -> bool:
    """Detect deterministic initial-vs-full-surname matches."""
    tokens_a = _name_tokens(name_a)
    tokens_b = _name_tokens(name_b)

    if len(tokens_a) < 2 or len(tokens_b) < 2:
        return False

    if tokens_a[:-1] != tokens_b[:-1]:
        return False

    final_a = tokens_a[-1]
    final_b = tokens_b[-1]

    if final_a == final_b:
        return True

    return (len(final_a) == 1 and final_b.startswith(final_a)) or (
        len(final_b) == 1 and final_a.startswith(final_b)
    )


def split_faculty_names(raw: str) -> list[str]:
    """
    Split only explicit combined faculty values joined with " / ".
    Keeps all other name formats unchanged.
    """
    clean = (raw or '').strip()
    if not clean:
        return []
    if ' / ' not in clean:
        return [clean]
    return [name.strip() for name in clean.split(' / ') if name.strip()]


def faculty_similarity(name_a: str, name_b: str) -> float:
    """Return a conservative similarity score for two faculty names."""
    norm_a = normalize_name(name_a)
    norm_b = normalize_name(name_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    compact_a = re.sub(r'[^a-z0-9]+', '', norm_a)
    compact_b = re.sub(r'[^a-z0-9]+', '', norm_b)
    token_a = ' '.join(sorted(norm_a.split()))
    token_b = ' '.join(sorted(norm_b.split()))

    compact_score = SequenceMatcher(None, compact_a, compact_b).ratio() if compact_a and compact_b else 0.0
    token_score = SequenceMatcher(None, token_a, token_b).ratio() if token_a and token_b else 0.0
    return max(compact_score, token_score)


def find_best_duplicate_candidate(conn, faculty_name: str, current_faculty_id: int | None = None):
    """Find the closest active faculty that may be the same person."""
    c = conn.cursor()
    rows = c.execute(
        'SELECT id, full_name FROM faculties WHERE is_active=1'
    ).fetchall()

    best = None
    for row in rows:
        if current_faculty_id and row['id'] == current_faculty_id:
            continue

        if _token_based_duplicate_match(faculty_name, row['full_name']):
            score = 1.0
        else:
            score = faculty_similarity(faculty_name, row['full_name'])
            if score < 0.88:
                continue

        if best is None or score > best['score']:
            best = {
                'id': row['id'],
                'full_name': row['full_name'],
                'score': score,
            }

    return best


def record_duplicate_review(conn, existing_faculty_id: int, candidate_faculty_id: int,
                           existing_name: str, candidate_name: str,
                           similarity_score: float, branch: str, year: int):
    """Store a pending duplicate review if one is not already open."""
    pair_key = f"{min(existing_faculty_id, candidate_faculty_id)}:{max(existing_faculty_id, candidate_faculty_id)}"
    conn.execute(
        '''
        INSERT OR IGNORE INTO faculty_duplicate_reviews
            (existing_faculty_id, candidate_faculty_id, existing_name, candidate_name,
             similarity_score, branch, year, pair_key, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        ''',
        (
            existing_faculty_id,
            candidate_faculty_id,
            existing_name,
            candidate_name,
            similarity_score,
            branch,
            year,
            pair_key,
        )
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def parse_pdf(pdf_path: str, branch: str, year: int) -> list:
    """
    Parse every page of a timetable PDF.
    Returns list of section_data dicts, one per page.
    """
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        # ── Pre-pass: build the best-name map for the whole PDF ───────────────
        global_faculty_map = build_global_faculty_map(pdf)
        print(f"  🗺️  Global faculty map: {len(global_faculty_map)} abbreviations resolved")

        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if not tables:
                print(f"  ⚠️  Page {page_num+1}: no table found, skipping")
                continue
            table = tables[0]
            try:
                data = parse_page(table, branch, year, page_num + 1, global_faculty_map)
                if data:
                    results.append(data)
                    print(f"  ✅ {data['section_name']} — {len(data['lectures'])} lecture entries")
            except Exception as e:
                import traceback
                print(f"  ❌ Page {page_num+1} error: {e}")
                traceback.print_exc()
    return results


# ── Per-page parser ────────────────────────────────────────────────────────────

def parse_page(table: list, branch: str, year: int, page_num: int,
               global_faculty_map: dict = None) -> dict:
    """Parse one page's 27-row table into a structured section dict."""

    meta = extract_metadata(table)
    if not meta.get('section'):
        print(f"  ⚠️  Page {page_num}: could not detect section name")
        return None

    faculty_map = extract_faculty_table(table, global_faculty_map or {})
    if not faculty_map:
        print(f"  ⚠️  Page {page_num}: no faculty table found")
        return None

    # Fill subject_name into faculty_map entries using the course name column
    for row in table[19:26]:
        if not row or not row[0]:
            continue
        code = str(row[0]).strip()
        name = str(row[1]).strip() if len(row) > 1 and row[1] else ''
        if re.match(r'^BT\w+', code) and name and 'MOOC' not in name:
            for abbr, info in faculty_map.items():
                if info['subject_code'] == code and not info.get('subject_name'):
                    info['subject_name'] = name

    lectures = extract_timetable(table, faculty_map, meta['section'])

    return {
        'section_name':   meta['section'],
        'program':        meta.get('program', 'B.Tech.'),
        'semester':       meta.get('semester', ''),
        'room_no':        meta.get('room_no', ''),
        'class_teacher':  meta.get('class_teacher', ''),
        'effective_from': meta.get('effective_from', ''),
        'branch':         branch,
        'year':           year,
        'faculty_map':    faculty_map,
        'lectures':       lectures,
    }


# ── Metadata extractor ────────────────────────────────────────────────────────

def extract_metadata(table: list) -> dict:
    """Pull section name, room, semester etc. from header row (row index 2)."""
    meta = {}
    header_text = str(table[2][0]) if table[2] and table[2][0] else ''

    m = re.search(r'Class\s*:\s*(\S+)', header_text)
    if m:
        meta['section'] = m.group(1).strip()

    m = re.search(r'Program:\s*(.+?)\s+Block:', header_text)
    if m:
        meta['program'] = m.group(1).strip()

    m = re.search(r'Room\s*No\.?\s*[:\s]?\s*(\S+)', header_text)
    if m:
        meta['room_no'] = m.group(1).strip()

    m = re.search(r'Sem\s*[:\s]+\s*(\S+)', header_text)
    if m:
        meta['semester'] = m.group(1).strip()

    m = re.search(r'Class\s+Teacher:\s*(?:Prof\.|Dr\.)?\s*(.+)', header_text)
    if m:
        meta['class_teacher'] = m.group(1).strip()

    m = re.search(r'Effective\s+From[:\-\s]+(\d+/\d+/\d+)', header_text)
    if m:
        meta['effective_from'] = m.group(1).strip()

    return meta


# ── Faculty table extractor ────────────────────────────────────────────────────

def extract_faculty_table(table: list, global_faculty_map: dict = None) -> dict:
    """
    Parse faculty reference rows (rows 18 onward).
    Returns {abbr: {name, subject_code, subject_name}}

    Accepts an optional global_faculty_map (built from the entire PDF pre-pass).
    After per-page extraction, any name that is shorter than the globally-known
    best name for the same abbreviation is replaced with the global version.
    This transparently fixes truncated combined-faculty names like
    "Mr. Pritesh Kumar J" → "Mr. Pritesh Kumar Jain".

    Handles:
    - Dual abbreviations: CS-NM/VJ  → CS-NM entry + VJ entry
    - Dual names:         Mr. A / Mr. B  → paired by position
    - Multi-line rows:    BTCSH313 name wraps to next row (CSE-T)
    - 3-person entries:   HU-SJ → Dr. A / Dr. B / Dr. C (all kept under HU-SJ)
    - Casing noise:       MS. MEENAKSHI → normalised at DB level
    """
    faculty_map = {}
    buffer_code = buffer_abbr = buffer_name = None

    def flush():
        if buffer_code and buffer_abbr:
            _register_faculty(faculty_map, buffer_code, buffer_abbr, buffer_name or '')

    for row in table[18:]:
        if row is None:
            continue
        cells = [str(c).strip() if c else '' for c in row]
        full  = ' '.join(cells)

        if any(x in full for x in ['Nivedita Tiwari', 'Time-Table Coordinator', 'Dean Academic']):
            flush()
            break

        if 'Course Code' in full:
            continue

        if 'GEMOOC' in full or 'Generic MOOC' in full:
            continue

        # Raw (unstripped) cells so we can split on \n if needed
        raw_cells = [str(c) if c else '' for c in row]
        code     = raw_cells[0].split('\n')[0].strip()   # first line only for code
        abbr_col = raw_cells[4].strip() if len(raw_cells) > 4 else ''
        # Name may contain \n (e.g. "Dr. A/Dr. B/\nDr. C") — join all lines
        name_col = ' '.join(raw_cells[5].split('\n')).strip() if len(raw_cells) > 5 else ''

        # Continuation row: no code, no abbr, has name (fallback for split rows)
        if not code and not abbr_col and name_col:
            if buffer_abbr:
                buffer_name = (buffer_name.rstrip(' /') + ' / ' + name_col).strip()
            continue

        flush()

        if not code or not re.match(r'^BT\w+', code):
            buffer_code = buffer_abbr = buffer_name = None
            continue

        buffer_code = code.strip()
        buffer_abbr = abbr_col.strip()
        buffer_name = name_col.strip()

    flush()

    # ── Apply global corrections ───────────────────────────────────────────────
    # For each abbreviation in this page's map, if the global pre-pass found a
    # longer (less-truncated) name, replace the local one.
    #
    # Also handles bare-suffix abbreviations: when a combined "CS-SM/PKJ" entry
    # is split, the second part becomes bare "PKJ" (no prefix).  We look up
    # both the exact key AND try to find a matching global key that ends in
    # the same suffix (e.g. "CS-PKJ" matches bare "PKJ").
    if global_faculty_map:
        for abbr, info in faculty_map.items():
            # Direct match
            global_name = global_faculty_map.get(abbr)

            # Suffix fallback: bare "PKJ" → find "CS-PKJ" in global map
            if not global_name and '-' not in abbr:
                for gkey, gname in global_faculty_map.items():
                    if gkey.split('-')[-1] == abbr:
                        global_name = gname
                        break

            if global_name and len(global_name) > len(info['name']):
                info['name'] = global_name

    return faculty_map


def _register_faculty(faculty_map, subject_code, abbr_raw, name_raw):
    """
    Split dual abbreviation/name pairs and register in faculty_map.
    CS-NM/VJ + Mr. Neeraj / Mr. Vikas  →  CS-NM→Neeraj,  VJ→Vikas
    HU-SJ + Dr. A / Dr. B / Dr. C      →  HU-SJ → 'Dr. A / Dr. B / Dr. C'
    """
    abbrs = [a.strip() for a in abbr_raw.split('/') if a.strip()]
    names = [n.strip() for n in re.split(r'\s*/\s*', name_raw) if n.strip()]

    if not abbrs:
        return

    if len(abbrs) == 1:
        # Single abbreviation — may have multiple names (keep them all joined)
        faculty_map[abbrs[0]] = {
            'name':         ' / '.join(names),
            'subject_code': subject_code,
            'subject_name': '',
        }
    else:
        # Multiple abbreviations — pair each with corresponding name
        for i, abbr in enumerate(abbrs):
            name = names[i] if i < len(names) else names[0]
            faculty_map[abbr] = {
                'name':         name,
                'subject_code': subject_code,
                'subject_name': '',
            }


# ── Global faculty map (pre-pass over entire PDF) ────────────────────────────

def build_global_faculty_map(pdf) -> dict:
    """
    Pre-pass: scan ALL pages of the already-open PDF and collect every
    (abbreviation → name) pair seen in any faculty table.

    For each abbreviation, keep the LONGEST name found across all pages.
    This self-heals truncated names: if page 8 gives "Mr. Pritesh Kumar J"
    but page 6 already gave "Mr. Pritesh Kumar Jain", the longer version wins.

    Returns: {abbr: best_full_name}
    """
    seen: dict[str, list[str]] = {}  # abbr → all names seen

    for page in pdf.pages:
        tables = page.extract_tables()
        if not tables:
            continue
        table = tables[0]

        for row in table[18:]:
            if not row:
                continue
            cells = [str(c).strip() if c else '' for c in row]
            full  = ' '.join(cells)

            if any(x in full for x in ['Nivedita Tiwari', 'Time-Table Coordinator', 'Course Code', 'GEMOOC']):
                continue

            # Need at least 6 columns: code, name, None, batch, abbr, faculty_name
            if len(cells) < 6:
                continue

            code_cell = cells[0]
            abbr_cell = cells[4]
            name_cell = ' '.join(cells[5].split()) if cells[5] else ''

            if not re.match(r'^BT\w+', code_cell):
                continue
            if not abbr_cell or not name_cell:
                continue

            # Split combined entries like "CS-NM/VJ" and "Mr. A / Mr. B"
            abbrs = [a.strip() for a in abbr_cell.split('/') if a.strip()]
            names = [n.strip() for n in re.split(r'\s*/\s*', name_cell) if n.strip()]

            for idx, abbr in enumerate(abbrs):
                if not re.match(r'^[A-Z]{1,5}-[A-Z]', abbr):
                    continue
                name = names[idx] if idx < len(names) else names[-1]
                name = re.sub(r'\s+', ' ', name).strip()
                if not name:
                    continue
                seen.setdefault(abbr, []).append(name)

    # For each abbreviation, the longest name is the least-truncated version
    return {abbr: max(name_list, key=len) for abbr, name_list in seen.items()}


# ── Timetable grid extractor ──────────────────────────────────────────────────

def extract_timetable(table: list, faculty_map: dict, section: str) -> list:
    """
    Iterate over each slot's two rows (label + data) and all 6 day columns.
    Merge any label-row spillover into the data row, then parse the combined cell.
    """
    lectures = []

    for slot_name, label_row_idx, data_row_idx in SLOT_ROW_PAIRS:
        if label_row_idx >= len(table) or data_row_idx >= len(table):
            continue

        label_row = table[label_row_idx]
        data_row  = table[data_row_idx]

        for col_idx, day in enumerate(DAYS, start=1):
            label_cell = str(label_row[col_idx] or '').strip() if len(label_row) > col_idx else ''
            data_cell  = str(data_row[col_idx]  or '').strip() if len(data_row)  > col_idx else ''

            # Merge label-row spillover with data row
            combined = '\n'.join(filter(None, [label_cell, data_cell]))
            if not combined.strip():
                continue

            day_lectures = parse_cell(combined, faculty_map, slot_name, day, section)
            lectures.extend(day_lectures)

    return lectures


# ── Cell parser ────────────────────────────────────────────────────────────────

def parse_cell(cell_text: str, faculty_map: dict, slot: str, day: str, section: str) -> list:
    """
    Parse one merged cell string into a list of lecture dicts.

    Each logical entry is 2 or 3 lines:
        Line 1: subject  BTCS708N(P)B1  /  BTCS601N(B1)  /  B1-BTCS607N  /  B2+B3
        Line 2: abbr     CS-PG
        Line 3: lab code SEL  (only when (P) in subject line)

    All possible subject-line formats:
        BTCS601N            → lecture, batch=ALL
        BTCS601N(B1)        → lecture, batch=B1
        BTCS708N(P)         → lab,     batch=ALL
        BTCS708N(P)B1       → lab,     batch=B1
        B1-BTCS607N         → lecture, batch=B1 (B prefix style)
        B2+B3               → standalone batch marker for NEXT subject line
    """
    if not cell_text.strip():
        return []

    skip = ['LUNCH BREAK', 'PLACEMENT TRAINING', 'SHIFT', 'Hardware Lab']
    if any(k in cell_text for k in skip):
        return []

    lines  = [l.strip() for l in cell_text.split('\n') if l.strip()]
    raw_entries = _group_lines(lines)

    lectures = []
    for entry in raw_entries:
        abbr = entry.get('abbr', '')
        if not abbr:
            continue

        for single_abbr in [a.strip() for a in abbr.split('/') if a.strip()]:
            finfo = _lookup_faculty(single_abbr, faculty_map)
            if not finfo:
                continue

            lectures.append({
                'day':          day,
                'time_slot':    slot,
                'time_range':   TIME_SLOTS.get(slot, ''),
                'subject_code': entry['subject_code'],
                'subject_name': finfo.get('subject_name', ''),
                'faculty_name': finfo['name'],
                'faculty_abbr': single_abbr,
                'batch':        entry['batch'],
                'is_lab':       entry['is_lab'],
                'lab_code':     entry['lab_code'],
                'section':      section,
            })

    return lectures


def _group_lines(lines: list) -> list:
    """
    State machine that converts flat cell lines into structured entries.

    States:
        EXPECT_SUBJECT           → waiting for a subject code line
        EXPECT_ABBR              → got subject, waiting for faculty abbreviation
        EXPECT_LAB_OR_SUBJECT    → got abbr; next is either lab code (if lab) or new subject
    """
    entries        = []
    current        = {}
    state          = 'EXPECT_SUBJECT'
    pending_batch  = None   # set when "B2+B3" standalone line is seen

    def flush():
        if current.get('subject_code') and current.get('abbr'):
            entries.append(dict(current))
        current.clear()

    def try_parse_subject(line):
        """Try to parse line as a subject code. Returns True if matched."""
        nonlocal pending_batch

        # B1-BTCS607N
        m = re.match(r'^(B[1-3])\s*[-–]\s*(BT\w+)$', line)
        if m:
            current['batch']        = m.group(1)
            current['subject_code'] = m.group(2)
            current['is_lab']       = False
            current['lab_code']     = ''
            return True

        # BTCS708N(P)B1  or  BTCS708N(P)
        m = re.match(r'^(BT\w+)\(P\)(B[1-3])?$', line)
        if m:
            current['subject_code'] = m.group(1)
            current['is_lab']       = True
            current['lab_code']     = ''
            if pending_batch:
                current['batch'] = pending_batch
                pending_batch = None
            else:
                current['batch'] = m.group(2) if m.group(2) else 'ALL'
            return True

        # BTCS601N(B1)  — lecture with batch
        m = re.match(r'^(BT\w+)\((B[1-3])\)$', line)
        if m:
            current['subject_code'] = m.group(1)
            current['batch']        = m.group(2)
            current['is_lab']       = False
            current['lab_code']     = ''
            return True

        # Plain BTCS601N  — lecture, all batches
        m = re.match(r'^(BT\w+)$', line)
        if m:
            current['subject_code'] = m.group(1)
            current['is_lab']       = False
            current['lab_code']     = ''
            if pending_batch:
                current['batch'] = pending_batch
                pending_batch = None
            else:
                current['batch'] = 'ALL'
            return True

        return False

    for line in lines:

        # ── Standalone batch-combo line: B2+B3 ────────────────────────────────
        m_batchcombo = re.match(r'^(B[1-3](?:\+B[1-3])+)$', line)
        if m_batchcombo:
            pending_batch = m_batchcombo.group(1)   # stored, applied to next subject
            continue

        # ── EXPECT_SUBJECT ─────────────────────────────────────────────────────
        if state == 'EXPECT_SUBJECT':
            if try_parse_subject(line):
                state = 'EXPECT_ABBR'
            # else ignore (time line, stray text, etc.)
            continue

        # ── EXPECT_ABBR ────────────────────────────────────────────────────────
        if state == 'EXPECT_ABBR':
            if ABBR_RE.match(line):
                current['abbr'] = line
                state = 'EXPECT_LAB_OR_SUBJECT'
            else:
                # Unexpected line — discard current entry and retry as subject
                current.clear()
                state = 'EXPECT_SUBJECT'
                if try_parse_subject(line):
                    state = 'EXPECT_ABBR'
            continue

        # ── EXPECT_LAB_OR_SUBJECT ──────────────────────────────────────────────
        if state == 'EXPECT_LAB_OR_SUBJECT':
            # Is it a lab room code?
            is_lab_code = line in LAB_CODES or bool(
                re.match(r'^[A-Z_]{2,7}$', line) and
                not re.match(r'^(ALL|BT)', line)
            )

            if is_lab_code and current.get('is_lab'):
                current['lab_code'] = line
                flush()
                state = 'EXPECT_SUBJECT'
                continue

            # Not a lab code — flush current and treat this line as new subject
            flush()
            state = 'EXPECT_SUBJECT'
            if try_parse_subject(line):
                state = 'EXPECT_ABBR'
            continue

    flush()

    # ── Expand B2+B3 batch combos into separate rows ───────────────────────────
    expanded = []
    for e in entries:
        batch = e.get('batch', 'ALL')
        if isinstance(batch, str) and '+' in batch:
            for b in batch.split('+'):
                expanded.append({**e, 'batch': b.strip()})
        else:
            expanded.append(e)

    return expanded


def _lookup_faculty(abbr: str, faculty_map: dict):
    """Exact match, then suffix fallback (handles ISCC-SK ↔ IS-SK mismatches)."""
    if abbr in faculty_map:
        return faculty_map[abbr]
    suffix = abbr.split('-')[-1]
    for k, v in faculty_map.items():
        if k.split('-')[-1] == suffix:
            return v
    return None


# ── Database persistence ───────────────────────────────────────────────────────

def get_or_create_faculty(conn, full_name: str, abbr: str, branch: str, year: int) -> int:
    """Find/create faculty rows; map abbreviation to a stable primary faculty."""
    c = conn.cursor()

    # For entries like "Dr. A / Dr. B", create separate faculty rows.
    names = split_faculty_names(full_name)
    if not names:
        names = [full_name]

    primary_fid = None
    for idx, single_name in enumerate(names):
        norm = normalize_name(single_name)
        c.execute('SELECT id FROM faculties WHERE normalized_name=?', (norm,))
        row = c.fetchone()
        if row:
            fid = row[0]
        else:
            duplicate_candidate = find_best_duplicate_candidate(conn, single_name)
            c.execute(
                'INSERT INTO faculties (full_name, normalized_name) VALUES (?, ?)',
                (single_name, norm)
            )
            fid = c.lastrowid

            if duplicate_candidate:
                record_duplicate_review(
                    conn,
                    duplicate_candidate['id'],
                    fid,
                    duplicate_candidate['full_name'],
                    single_name,
                    duplicate_candidate['score'],
                    branch,
                    year,
                )

        if idx == 0:
            primary_fid = fid

    c.execute('''
        INSERT OR IGNORE INTO faculty_abbreviations
            (faculty_id, abbreviation, branch, year)
        VALUES (?, ?, ?, ?)
    ''', (primary_fid, abbr, branch, year))

    return primary_fid


def save_to_db(section_data_list: list, branch: str, year: int, pdf_filename: str):
    """
    Persist parsed section data.  Replaces at SECTION level only.
    """
    conn = get_connection()
    c    = conn.cursor()
    saved = []

    for sd in section_data_list:
        sname = sd['section_name']
        try:
            # Delete old data for this section only
            c.execute(
                'SELECT id FROM sections WHERE name=? AND branch=? AND year=?',
                (sname, branch, year)
            )
            existing = c.fetchone()
            if existing:
                c.execute('DELETE FROM timetable WHERE section_id=?', (existing[0],))
                c.execute('DELETE FROM sections   WHERE id=?',         (existing[0],))
                print(f"    🔄 Replaced existing {sname}")

            # Insert section
            c.execute('''
                INSERT INTO sections
                    (name, branch, year, semester, program, room_no,
                     class_teacher_id, effective_from)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ''', (
                sname,
                branch,
                year,
                sd.get('semester', ''),
                sd.get('program', ''),
                sd.get('room_no', ''),
                sd.get('effective_from', ''),
            ))
            section_id = c.lastrowid

            # Register all faculty
            abbr_to_id = {}
            for abbr, info in sd['faculty_map'].items():
                fid = get_or_create_faculty(conn, info['name'], abbr, branch, year)
                abbr_to_id[abbr] = fid

            # Insert timetable rows
            inserted = 0
            for lec in sd['lectures']:
                fid = abbr_to_id.get(lec['faculty_abbr'])
                if not fid:
                    norm = normalize_name(lec['faculty_name'])
                    c.execute('SELECT id FROM faculties WHERE normalized_name=?', (norm,))
                    r = c.fetchone()
                    fid = r[0] if r else None
                if not fid:
                    continue

                c.execute('''
                    INSERT INTO timetable
                        (section_id, faculty_id, day, slot, time_range,
                         subject_code, subject_name, batch,
                         is_lab, lab_code, room_no)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    section_id, fid,
                    lec['day'], lec['time_slot'], lec['time_range'],
                    lec['subject_code'], lec['subject_name'],
                    lec['batch'], lec['is_lab'], lec['lab_code'],
                    '' if lec['is_lab'] else sd.get('room_no', ''),
                ))
                inserted += 1

            c.execute('''
                INSERT INTO pdf_uploads (filename, branch, year, section, status)
                VALUES (?, ?, ?, ?, 'processed')
            ''', (pdf_filename, branch, year, sname))

            conn.commit()
            saved.append(sname)
            print(f"    💾 {sname}: {inserted} rows saved")

        except Exception as e:
            conn.rollback()
            import traceback; traceback.print_exc()
            print(f"    ❌ {sname}: {e}")

    conn.close()
    return saved


# ── Quick test ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from database import init_db
    init_db()

    uploads_dir = os.path.join(os.path.dirname(__file__), '..', 'uploads')

    # Common candidate filenames (handles spaces vs underscores)
    candidates = [
        'III_Year_CSE_Class_Time_Table.pdf',
        'III Year CSE_Class Time Table.pdf',
        'III Year CSE Class Time Table.pdf',
    ]

    pdf_path = None
    for name in candidates:
        p = os.path.join(uploads_dir, name)
        if os.path.exists(p):
            pdf_path = p
            break

    if not pdf_path:
        import glob
        for f in glob.glob(os.path.join(uploads_dir, '*.pdf')):
            if re.search(r'III.*Year.*CSE.*Time.*Table', os.path.basename(f), re.IGNORECASE):
                pdf_path = f
                break

    if not pdf_path:
        existing = os.listdir(uploads_dir) if os.path.isdir(uploads_dir) else []
        raise FileNotFoundError(
            f"No timetable PDF found in {uploads_dir!r}. Found: {existing}"
        )

    print(f"\n📄 Parsing: {pdf_path}\n{'='*60}")

    results = parse_pdf(pdf_path, branch='BTech', year=3)

    print(f"\n{'='*60}")
    print(f"Sections parsed: {len(results)}")

    for r in results:
        labs    = [l for l in r['lectures'] if l['is_lab']]
        nonlabs = [l for l in r['lectures'] if not l['is_lab']]
        print(f"\n📋 {r['section_name']}  Room:{r['room_no']}  "
              f"Entries:{len(r['lectures'])}  (labs:{len(labs)}, lectures:{len(nonlabs)})")
        if labs:
            l = labs[0]
            print(f"   Lab : {l['day']} {l['time_slot']} | {l['subject_code']} | "
                  f"{l['faculty_name']} | batch={l['batch']} | room={l['lab_code']}")
        if nonlabs:
            l = nonlabs[0]
            print(f"   Lec : {l['day']} {l['time_slot']} | {l['subject_code']} | "
                  f"{l['faculty_name']} | batch={l['batch']}")