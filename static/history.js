/* =====================================================================
 * Study Tracker — History page logic.
 * Loads the subject list (to fill the filter dropdown) and the study
 * history (from /api/history) with optional subject + date-range filters.
 * ===================================================================== */

// Format seconds as HH:MM:SS.
function formatHMS(totalSec) {
  const h = String(Math.floor(totalSec / 3600)).padStart(2, "0");
  const m = String(Math.floor((totalSec % 3600) / 60)).padStart(2, "0");
  const s = String(totalSec % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

// Escape user text before inserting into HTML.
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

// Fill the subject filter dropdown from /api/subjects.
async function loadSubjectsFilter() {
  const r = await fetch("/api/subjects");
  const data = await r.json();
  if (!data.ok) return;
  const sel = document.getElementById("filter-subject");
  for (const s of data.subjects) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name;
    sel.appendChild(opt);
  }
}

// Load history applying the current filter values, then render the table.
async function loadHistory() {
  // Build the query string from the filter inputs.
  const params = new URLSearchParams();
  const subject = document.getElementById("filter-subject").value;
  const from = document.getElementById("filter-from").value;
  const to = document.getElementById("filter-to").value;
  if (subject) params.set("subject_id", subject);
  if (from) params.set("from", from);
  if (to) params.set("to", to);

  const r = await fetch("/api/history?" + params.toString());
  const data = await r.json();
  const tbody = document.querySelector("#history-table tbody");
  const summary = document.getElementById("history-summary");
  tbody.innerHTML = "";

  if (!data.ok) {
    summary.textContent = "Failed to load history.";
    return;
  }

  // Summary line: count + total minutes for the filtered set.
  summary.textContent =
    `${data.count} session(s), ${data.total_minutes} minutes total.`;

  if (data.records.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No sessions match the filters.</td></tr>';
    return;
  }

  // One row per session.
  for (const rec of data.records) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(rec.subject_name)}</td>
      <td>${escapeHtml(rec.started_at)}</td>
      <td>${escapeHtml(rec.ended_at)}</td>
      <td>${formatHMS(rec.duration_sec)}</td>`;
    tbody.appendChild(tr);
  }
}

// Apply button: re-load with the chosen filters.
document.getElementById("history-filters").addEventListener("submit", (e) => {
  e.preventDefault();
  loadHistory();
});

// Reset button: clear filters and reload.
document.getElementById("filter-reset").addEventListener("click", () => {
  document.getElementById("filter-subject").value = "";
  document.getElementById("filter-from").value = "";
  document.getElementById("filter-to").value = "";
  loadHistory();
});

// Initial load.
(async function init() {
  await loadSubjectsFilter();
  await loadHistory();
})();
