// ─── SmartCampus Frontend Configuration ──────────────────────
// Change BASE_URL here if you deploy to a different server.
// All HTML files import this file — never hardcode URLs elsewhere.

const CONFIG = {
    BASE_URL: 'http://127.0.0.1:5000',

    API: {
        // ── Admin ──────────────────────────────────────────
        ADMIN_LOGIN:            '/api/admin/login',
        ADMIN_LOGOUT:           '/api/admin/logout',
        ADMIN_UPLOAD:           '/api/admin/upload',
        ADMIN_UPLOADS_LIST:     '/api/admin/uploads',
        ADMIN_CONFLICTS:        '/api/admin/conflicts',
        ADMIN_RESOLVE_CONFLICT: '/api/admin/conflicts',   // + /{id}/resolve
        ADMIN_FACULTY_LIST:     '/api/admin/faculty',
        ADMIN_CREATE_ACCOUNT:   '/api/admin/faculty',     // + /{id}/create-account
        ADMIN_SECTIONS:         '/api/admin/sections',

        // ── Faculty ────────────────────────────────────────
        FACULTY_LOGIN:          '/api/faculty/login',
        FACULTY_LOGOUT:         '/api/faculty/logout',
        FACULTY_ME:             '/api/faculty/me',
        FACULTY_SCHEDULE:       '/api/faculty/schedule',
        FACULTY_TODAY:          '/api/faculty/today',
        FACULTY_MARK_ATTENDANCE:'/api/faculty/attendance',
        FACULTY_ATT_STATUS:     '/api/faculty/attendance/status',

        // ── Shared attendance snapshot ─────────────────────
        ATTENDANCE_TODAY:       '/api/attendance/today',

        // ── Student (public) ───────────────────────────────
        FACULTY_SEARCH:         '/api/faculty/search',
        FACULTY_WHERE_NOW:      '/api/faculty',           // + /{id}/where-now
        FACULTY_PUBLIC_SCHEDULE:'/api/faculty',           // + /{id}/schedule
        FACULTY_FREE_SLOTS:     '/api/faculty',           // + /{id}/free-slots
        FACULTY_FREE_AT:        '/api/faculty/free-at',
        ROOMS_FREE:             '/api/rooms/free',
        SECTIONS_LIST:          '/api/sections',
        SECTION_TIMETABLE:      '/api/sections',          // + /{name}/timetable

        // ── Analytics ──────────────────────────────────────
        ANALYTICS_FACULTY_LOAD: '/api/analytics/faculty-load',
        ANALYTICS_ROOM_USAGE:   '/api/analytics/room-usage',
        ANALYTICS_LAB_USAGE:    '/api/analytics/lab-usage',
    },

    // ── Time slot labels for display ──────────────────────
    TIME_SLOTS: {
        L1: '10:15 - 11:15',
        L2: '11:15 - 12:15',
        L3: '12:45 - 01:45',
        L4: '01:45 - 02:45',
        L5: '02:45 - 03:45',
        L6: '03:45 - 04:45',
    },

    DAYS: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
};

// ─── Helper: build full URL ───────────────────────────────────
function apiUrl(path) {
    return CONFIG.BASE_URL + path;
}

// ─── Helper: standard fetch wrapper ──────────────────────────
async function apiFetch(path, options = {}) {
    const url = apiUrl(path);
    const defaults = {
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
    };
    const merged = { ...defaults, ...options };
    // Remove JSON header for FormData uploads
    if (merged.body instanceof FormData) {
        delete merged.headers['Content-Type'];
    }
    if (
        merged.body &&
        typeof merged.body !== 'string' &&
        !(merged.body instanceof FormData)
    ) {
        merged.body = JSON.stringify(merged.body);
    }
    const res  = await fetch(url, merged);
    const data = await res.json();
    // Handle 401 Unauthorized — session lost
    if (res.status === 401) {
        if (typeof window.handleAuthError === 'function') {
            window.handleAuthError();
        }
    }
    return { ok: res.ok, status: res.status, data };
}