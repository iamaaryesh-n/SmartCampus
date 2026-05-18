(function (global) {
  const state = {
    store: {},
    loaded: false,
    loading: false,
    error: null,
    request: null,
    fetchedAt: null,
    autoRefreshTimer: null,
    autoRefreshIntervalMs: 0,
    visibilityHandler: null,
  };

  function normalizeStatus(status) {
    if (status === 'present' || status === 'absent' || status === 'unmarked') {
      return status;
    }
    if (status === 'unknown') return 'unmarked';
    return 'unmarked';
  }

  function toRecord(entry) {
    return {
      status: normalizeStatus(entry?.status),
      marked_at: entry?.marked_at || null,
    };
  }

  function setStore(facultyList) {
    const next = {};
    for (const faculty of facultyList || []) {
      if (!faculty || faculty.id === undefined || faculty.id === null) continue;
      next[String(faculty.id)] = toRecord(faculty);
    }

    state.store = next;
    state.loaded = true;
    state.loading = false;
    state.error = null;
    state.request = null;
    state.fetchedAt = Date.now();
    return state.store;
  }

  async function fetchTodayAttendance(force = false) {
    if (state.loading && state.request) {
      return state.request;
    }

    if (state.loaded && !force) {
      return state.store;
    }

    state.loading = true;
    state.error = null;

    state.request = apiFetch(CONFIG.API.ATTENDANCE_TODAY)
      .then(({ ok, data }) => {
        if (!ok || !data || !data.success) {
          throw new Error((data && data.error) || 'Could not load attendance');
        }

        const faculty = Array.isArray(data.data?.faculty) ? data.data.faculty : [];
        return setStore(faculty);
      })
      .catch(error => {
        state.loaded = state.loaded || Object.keys(state.store).length > 0;
        state.loading = false;
        state.error = error;
        state.request = null;
        return state.store;
      });

    return state.request;
  }

  function getFacultyAttendanceStatus(facultyId) {
    if (facultyId === undefined || facultyId === null) return null;
    return state.store[String(facultyId)] || null;
  }

  function getStatusBadgeVariant(status) {
    const normalized = normalizeStatus(status);
    if (normalized === 'present') return 'green';
    if (normalized === 'absent') return 'red';
    return 'gray';
  }

  function getReadableAttendanceLabel(status) {
    const normalized = normalizeStatus(status);
    if (normalized === 'present') return 'Present Today';
    if (normalized === 'absent') return 'Absent Today';
    return 'Not Marked';
  }

  function isLoaded() {
    return state.loaded;
  }

  function isLoading() {
    return state.loading;
  }

  function getError() {
    return state.error;
  }

  function stopAutoRefresh() {
    if (state.autoRefreshTimer) {
      clearInterval(state.autoRefreshTimer);
      state.autoRefreshTimer = null;
    }

    if (state.visibilityHandler) {
      document.removeEventListener('visibilitychange', state.visibilityHandler);
      state.visibilityHandler = null;
    }

    state.autoRefreshIntervalMs = 0;
  }

  function startAutoRefresh(intervalMs = 4 * 60 * 1000) {
    const resolvedInterval = Math.max(3 * 60 * 1000, Math.min(intervalMs || 0, 5 * 60 * 1000));

    if (state.autoRefreshTimer) {
      if (state.autoRefreshIntervalMs === resolvedInterval) {
        return state.autoRefreshTimer;
      }
      stopAutoRefresh();
    }

    const tick = () => {
      fetchTodayAttendance(true).catch(() => {
        // Keep the previous cache visible on refresh failure.
      });
    };

    state.autoRefreshIntervalMs = resolvedInterval;
    state.autoRefreshTimer = setInterval(tick, resolvedInterval);

    state.visibilityHandler = () => {
      if (!document.hidden) {
        tick();
      }
    };
    document.addEventListener('visibilitychange', state.visibilityHandler);

    return state.autoRefreshTimer;
  }

  global.AttendanceStore = {
    fetchTodayAttendance,
    getFacultyAttendanceStatus,
    getStatusBadgeVariant,
    getReadableAttendanceLabel,
    isLoaded,
    isLoading,
    getError,
    startAutoRefresh,
    stopAutoRefresh,
  };
})(window);