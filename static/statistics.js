/* =====================================================================
 * Study Tracker — Statistics page logic.
 * Loads aggregated stats from /api/statistics and renders headline cards
 * plus two Chart.js charts (time per subject, daily study time).
 * ===================================================================== */

// Format a number of minutes as a friendly "Xh Ym" (or "Ym").
function formatMinutes(min) {
  const m = Math.round(min);
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return h > 0 ? `${h}h ${rem}m` : `${rem}m`;
}

// Escape user text before inserting into HTML (subject names).
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

const PALETTE = [
  "#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2",
  "#db2777", "#65a30d",
];

let chartSubjects = null;   // bar chart: minutes per subject
let chartDaily = null;      // line chart: minutes per day

async function loadStatistics() {
  const r = await fetch("/api/statistics");
  const data = await r.json();
  if (!data.ok) return;

  // Empty state: hide cards/charts, show the message.
  if (!data.has_data) {
    document.getElementById("stat-empty").style.display = "block";
    return;
  }

  // ----- Fill the headline cards -----
  document.getElementById("stat-total").textContent = formatMinutes(data.total_minutes);
  document.getElementById("stat-sessions").textContent = data.total_sessions;
  document.getElementById("stat-avg-session").textContent =
    formatMinutes(data.avg_session_minutes);
  document.getElementById("stat-avg-day").textContent =
    formatMinutes(data.avg_per_active_day);
  document.getElementById("stat-streak").textContent =
    `${data.current_streak} day${data.current_streak === 1 ? "" : "s"}`;
  document.getElementById("stat-top").textContent =
    data.top_subject ? data.top_subject.name : "—";

  // ----- Charts (only if Chart.js loaded) -----
  if (typeof Chart === "undefined") return;

  // Bar chart: time per subject.
  const subjLabels = data.per_subject.map((s) => s.name);
  const subjMinutes = data.per_subject.map((s) => s.minutes);
  const colors = subjLabels.map((_, i) => PALETTE[i % PALETTE.length]);

  if (chartSubjects) chartSubjects.destroy();
  chartSubjects = new Chart(document.getElementById("chart-subjects"), {
    type: "bar",
    data: {
      labels: subjLabels,
      datasets: [{ label: "Minutes", data: subjMinutes, backgroundColor: colors }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });

  // Line chart: daily study time.
  const dayLabels = data.daily.map((d) => d.date);
  const dayMinutes = data.daily.map((d) => d.minutes);

  if (chartDaily) chartDaily.destroy();
  chartDaily = new Chart(document.getElementById("chart-daily"), {
    type: "line",
    data: {
      labels: dayLabels,
      datasets: [{
        label: "Minutes per day",
        data: dayMinutes,
        borderColor: "#2563eb",
        backgroundColor: "#2563eb",
        tension: 0.2,
        fill: false,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "Minutes" } } },
    },
  });
}

// Run on page load.
loadStatistics();
