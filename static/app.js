/* =====================================================================
 * Study Tracker — front-end logic for the dashboard page.
 *
 * Responsibilities:
 *   - Talk to the Flask JSON API (fetch wrapper `api`).
 *   - Manage the subjects list (add / delete / fill the timer dropdown).
 *   - Run the study timer (start / stop / save a record).
 *   - Show rule-based recommendations + per-subject statistics.
 *   - Render the two ML models:
 *       * Study patterns  (KMeans)  -> scatter chart + cluster summary
 *       * Study forecast  (Linear regression) -> weekly line + trend
 *   - Draw the overview charts (study time bar + distribution pie).
 *   - Show the analysis-period banner and the "period finished" notification.
 *
 * Plain browser JavaScript (no framework). Loaded after Chart.js.
 * ===================================================================== */


/* ---------------------------------------------------------------------
 * `api` — a tiny wrapper around fetch() so we don't repeat boilerplate.
 * ------------------------------------------------------------------- */
const api = {
  // GET request → returns parsed JSON.
  async get(url) {
    const r = await fetch(url);          // perform the HTTP GET
    return r.json();                     // parse and return the JSON body
  },
  // POST request with a JSON body → returns parsed JSON.
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST",                                  // HTTP method
      headers: { "Content-Type": "application/json" }, // tell server we send JSON
      body: JSON.stringify(body),                      // serialise the JS object
    });
    return r.json();
  },
  // DELETE request → returns parsed JSON.
  async del(url) {
    const r = await fetch(url, { method: "DELETE" });
    return r.json();
  },
};


/* ---------------------------------------------------------------------
 * Chart.js chart instances. We keep references so we can destroy and
 * recreate each chart whenever the data changes.
 * ------------------------------------------------------------------- */
let chartTime = null;          // bar chart: study minutes per subject
let chartDistribution = null;  // pie chart: share of total study time
let chartPatterns = null;      // scatter chart: KMeans clusters (hour vs minutes)
let chartForecast = null;      // line chart: weekly minutes + regression forecast


/* ---------------------------------------------------------------------
 * Period banner + "period finished" notification.
 * ------------------------------------------------------------------- */

async function loadPeriod() {
  // Ask the server about the analysis-period status of the current user.
  const data = await api.get("/api/period");
  const banner = document.getElementById("period-banner");

  // If the request failed or the period is not configured, clear the banner.
  if (!data.ok || !data.period.configured) {
    banner.innerHTML = "";
    return;
  }

  const p = data.period;  // shorthand for the period object

  if (p.immediate) {
    // Immediate mode: recommendations are always available.
    banner.className = "period-banner finished";
    banner.innerHTML =
      `<strong>Immediate mode.</strong> ` +
      `Recommendations are generated from your data as soon as it is entered.`;
  } else if (p.finished) {
    // The analysis period has ended → recommendations are unlocked.
    banner.className = "period-banner finished";
    banner.innerHTML =
      `<strong>Analysis period finished.</strong> ` +
      `Recommendations below are based on ${p.period_days} day(s) of data ` +
      `(${p.started_at} → ${p.ends_at}).`;
    // Also show the one-time "period finished" notification (see below).
    maybeShowFinishedNotification(p);
  } else {
    // The period is still running → recommendations are locked.
    banner.className = "period-banner active";
    banner.innerHTML =
      `<strong>Analysis period active.</strong> ` +
      `Started ${p.started_at}, ends ${p.ends_at} ` +
      `(${p.days_left} day(s) left). Recommendations are locked until the period ends.`;
  }
}

/*
 * Show the big "your period is over" notification once per finished period.
 * We use localStorage so it is not shown again after the user dismisses it
 * (keyed by the period end date, so a NEW period shows it again).
 */
function maybeShowFinishedNotification(period) {
  if (period.immediate) return;                 // never for immediate mode
  const key = "period_notif_dismissed_" + period.ends_at;  // unique per period
  if (localStorage.getItem(key) === "1") return;           // already dismissed

  const box = document.getElementById("period-notification");
  box.style.display = "flex";                   // reveal the notification

  // Wire the "Got it" button to remember the dismissal and hide the banner.
  document.getElementById("btn-dismiss-notification").onclick = () => {
    localStorage.setItem(key, "1");             // remember for this period
    box.style.display = "none";                 // hide it
  };
}


/* ---------------------------------------------------------------------
 * Subjects: load list, fill the timer dropdown, handle add / delete.
 * ------------------------------------------------------------------- */

async function loadSubjects() {
  const data = await api.get("/api/subjects");  // fetch the subjects
  if (!data.ok) return;                         // bail out on error

  const list = document.getElementById("subject-list");      // the visible list
  const select = document.getElementById("timer-subject");   // timer dropdown

  // Clear both before re-filling them.
  list.innerHTML = "";
  select.innerHTML = "";

  // Special case: the user has no subjects.
  if (data.subjects.length === 0) {
    list.innerHTML = '<li class="muted small">No subjects yet. Add one above.</li>';
    const opt = document.createElement("option");
    opt.textContent = "No subjects available";
    opt.disabled = true;
    select.appendChild(opt);
    document.getElementById("btn-start").disabled = true;  // can't start a timer
    return;
  }

  document.getElementById("btn-start").disabled = false;  // enable the timer

  // For each subject: add a list item with a delete button, and an <option>.
  for (const s of data.subjects) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(s.name)}</span>
                    <button class="del" data-id="${s.id}" title="Delete">×</button>`;
    list.appendChild(li);

    const opt = document.createElement("option");
    opt.value = s.id;             // option value = subject id
    opt.textContent = s.name;     // option label = subject name
    select.appendChild(opt);
  }

  // Attach click handlers to every delete (×) button.
  list.querySelectorAll(".del").forEach((b) => {
    b.addEventListener("click", async () => {
      // Confirm because deleting a subject cascades to its records.
      if (!confirm("Delete this subject and ALL its records?")) return;
      const res = await api.del(`/api/subjects/${b.dataset.id}`);
      if (res.ok) {
        await reloadAll();          // refresh the whole dashboard
      } else {
        alert(res.error || "Delete failed");
      }
    });
  });
}

// Handle the "Add subject" form submission.
document
  .getElementById("add-subject-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();                               // don't reload the page
    const input = document.getElementById("new-subject");
    const name = input.value.trim();                  // trimmed subject name
    if (!name) return;                                // ignore empty input
    const res = await api.post("/api/subjects", { name });
    if (res.ok) {
      input.value = "";                               // clear the input
      await loadSubjects();                           // refresh the list/dropdown
      await loadRecommendations();                    // stats may change
    } else {
      alert(res.error || "Failed to add subject");
    }
  });


/* ---------------------------------------------------------------------
 * Study timer: start counting, stop and save the elapsed session.
 * ------------------------------------------------------------------- */

let timerInterval = null;   // will hold the id of the per-second ticker (empty = timer not running)
let timerStartTs = null;    // will hold the start moment in milliseconds (empty for now)


// ===== TIME FORMAT: seconds -> "HH:MM:SS" =====

function formatHMS(totalSec) {                                             // takes a number of seconds
  const h = String(Math.floor(totalSec / 3600)).padStart(2, "0");         // hours: divide by 3600, round down, to text, pad with leading zero
  const m = String(Math.floor((totalSec % 3600) / 60)).padStart(2, "0");  // minutes: remainder after hours, divide by 60, round down, pad zero
  const s = String(totalSec % 60).padStart(2, "0");                       // seconds: remainder after dividing by 60, pad zero
  return `${h}:${m}:${s}`;                                                 // join into "HH:MM:SS" and return
}


// ===== TICK: updates the display (called once per second) =====

function tick() {                                                         // display-update function
  const elapsed = Math.floor((Date.now() - timerStartTs) / 1000);        // elapsed seconds = (now - start) in ms, divide by 1000, round down
  document.getElementById("timer-display").textContent = formatHMS(elapsed); // write the formatted time into the on-screen display
}


// ===== START BUTTON =====

document.getElementById("btn-start").addEventListener("click", () => {    // find the Start button and listen for a click
  const select = document.getElementById("timer-subject");                // find the subject dropdown
  if (!select.value) {                                                    // if no subject is selected...
    alert("Choose a subject first.");                                     // ...show a warning
    return;                                                               // ...and exit, do not start the timer
  }
  timerStartTs = Date.now();                                              // REMEMBER the start moment (current time in ms)
  tick();                                                                 // show 00:00:00 immediately, don't wait a second
  timerInterval = setInterval(tick, 1000);                                // start calling tick every 1000 ms (once per second), keep its id

  document.getElementById("btn-start").disabled = true;                   // disable Start (can't start twice)
  document.getElementById("btn-stop").disabled = false;                   // enable Stop (now it can be stopped)
  select.disabled = true;                                                 // lock the subject dropdown (can't switch mid-session)

  const subjName = select.options[select.selectedIndex].textContent;      // get the selected subject's name (text, not id)
  document.getElementById("timer-status").textContent = `Studying: ${subjName}`; // show "Studying: <subject>"
});


// ===== STOP & SAVE BUTTON =====

document.getElementById("btn-stop").addEventListener("click", async () => { // find Stop, listen for click (async = it will wait for the server)
  if (!timerInterval) return;                                             // if the timer isn't running, exit, nothing to stop
  clearInterval(timerInterval);                                           // STOP the per-second updates (using the saved id)
  timerInterval = null;                                                   // reset the variable — timer no longer running

  const endedTs = Date.now();                                             // record the end moment (current time in ms)
  const durationSec = Math.floor((endedTs - timerStartTs) / 1000);        // FINAL duration = (end - start) in ms / 1000, round down
  const subjectId = parseInt(document.getElementById("timer-subject").value, 10); // selected subject id, parseInt turns text into a number (10 = decimal)

  const started = new Date(timerStartTs).toISOString();                   // start moment -> standard time text (ISO, e.g. "2025-05-27T19:00:00Z")
  const ended = new Date(endedTs).toISOString();                          // end moment -> same ISO text

  const res = await api.post("/api/records", {                            // SEND the session to the server (POST), wait for the response
    subject_id: subjectId,                                                // which subject
    started_at: started,                                                  // when it started
    ended_at: ended,                                                      // when it ended
    duration_sec: durationSec,                                            // how long it lasted (seconds)
  });

  document.getElementById("btn-start").disabled = false;                  // enable Start again
  document.getElementById("btn-stop").disabled = true;                    // disable Stop again
  document.getElementById("timer-subject").disabled = false;             // unlock the subject dropdown
  document.getElementById("timer-display").textContent = "00:00:00";      // reset the display to zero

  if (res.ok) {                                                           // if the server saved it successfully...
    document.getElementById("timer-status").textContent =                 // ...show a success message
      `Saved! Session: ${formatHMS(durationSec)}`;                        //    with the duration of the saved session
    await loadRecords();                                                  // refresh the records table
    await loadRecommendations();                                         // refresh recommendations + stats + overview charts
    await loadPatterns();                                                 // recompute KMeans (new data arrived)
    await loadForecast();                                                 // recompute the regression forecast
  } else {                                                                // if the server returned an error...
    document.getElementById("timer-status").textContent =                 // ...show an error message
      "Error: " + (res.error || "could not save");
  }
});


/* ---------------------------------------------------------------------
 * Records table: list recent study sessions.
 * ------------------------------------------------------------------- */

async function loadRecords() {
  const data = await api.get("/api/records");
  const tbody = document.querySelector("#records-table tbody");
  tbody.innerHTML = "";
  if (!data.ok || data.records.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No records yet.</td></tr>';
    return;
  }
  // Show up to 30 most recent sessions.
  for (const r of data.records.slice(0, 30)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.subject_name)}</td>
      <td>${escapeHtml(r.started_at)}</td>
      <td>${escapeHtml(r.ended_at)}</td>
      <td>${formatHMS(r.duration_sec)}</td>`;
    tbody.appendChild(tr);
  }
}


/* ---------------------------------------------------------------------
 * Recommendations + per-subject statistics table + overview charts.
 * ------------------------------------------------------------------- */

async function loadRecommendations() {
  const data = await api.get("/api/recommendations");
  const box = document.getElementById("recommendations");
  box.innerHTML = "";

  if (!data.ok) {
    box.textContent = "Failed to load recommendations.";
    return;
  }

  // Render each recommendation as a coloured card (class depends on type).
  for (const rec of data.recommendations) {
    const div = document.createElement("div");
    div.className = `rec ${rec.type}`;   // e.g. "rec warning"
    div.textContent = rec.message;
    box.appendChild(div);
  }

  // ----- Statistics table (Subject / Sessions / Total minutes) -----
  const tbody = document.querySelector("#stats-table tbody");
  tbody.innerHTML = "";
  if (!data.subject_stats || data.subject_stats.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">No statistics yet.</td></tr>';
    drawOverviewCharts([]);   // clear the overview charts too
    return;
  }

  for (const s of data.subject_stats) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(s.subject_name)}</td>
      <td>${s.sessions}</td>
      <td>${s.total_minutes}</td>`;
    tbody.appendChild(tr);
  }

  // Redraw the overview charts from the same per-subject statistics.
  drawOverviewCharts(data.subject_stats);
}

// Refresh button next to recommendations re-loads the whole dashboard.
document
  .getElementById("btn-refresh-recs")
  .addEventListener("click", reloadAll);


/* ---------------------------------------------------------------------
 * Overview charts: study time per subject (bar) + distribution (pie).
 * ------------------------------------------------------------------- */

// A fixed palette so every subject keeps a consistent colour across charts.
const PALETTE = [
  "#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2",
  "#db2777", "#65a30d",
];

function drawOverviewCharts(stats) {
  // Defensive guard: if Chart.js failed to load (CDN blocked / offline),
  // skip drawing instead of throwing "Chart is not defined".
  if (typeof Chart === "undefined") {
    const note = document.getElementById("charts-empty");
    note.style.display = "block";
    note.textContent = "Charts could not load (Chart.js library is unavailable).";
    return;
  }

  // Build parallel arrays from the stats objects.
  const labels = stats.map((s) => s.subject_name);     // x-axis labels
  const minutes = stats.map((s) => s.total_minutes);   // study minutes
  const colors = labels.map((_, i) => PALETTE[i % PALETTE.length]);

  // Show the "no data" note if there is no study time at all.
  const hasTime = minutes.some((m) => m > 0);
  document.getElementById("charts-empty").style.display = hasTime ? "none" : "block";

  // Destroy existing chart instances before recreating (avoids ghosting).
  if (chartTime) chartTime.destroy();
  if (chartDistribution) chartDistribution.destroy();

  // ----- Bar chart: study minutes per subject -----
  chartTime = new Chart(document.getElementById("chart-time"), {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: "Minutes", data: minutes, backgroundColor: colors }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });

  // ----- Pie chart: distribution of total study time -----
  chartDistribution = new Chart(document.getElementById("chart-distribution"), {
    type: "pie",
    data: {
      labels,
      datasets: [{ data: minutes, backgroundColor: colors }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom" } },
    },
  });
}


/* ---------------------------------------------------------------------
 * ML model 1 — Study patterns (KMeans).
 * Renders a scatter chart of sessions (hour of day vs minutes) coloured
 * by cluster, a one-line headline insight, and a list of cluster summaries.
 * ------------------------------------------------------------------- */

async function loadPatterns() {
  const data = await api.get("/api/patterns");
  const insight = document.getElementById("patterns-insight");
  const clusterBox = document.getElementById("patterns-clusters");
  insight.innerHTML = "";
  clusterBox.innerHTML = "";

  // Not enough sessions yet → show the explanatory message, clear the chart.
  if (!data.ok || !data.enough_data) {
    insight.innerHTML =
      `<div class="rec info">${escapeHtml(data.message || "Not enough data yet.")}</div>`;
    if (chartPatterns) { chartPatterns.destroy(); chartPatterns = null; }
    return;
  }

  // Headline insight: the most productive time of day.
  const bt = data.best_time;
  if (bt) {
    insight.innerHTML =
      `<div class="rec success">` +
      `Your longest study sessions happen in the <strong>${escapeHtml(bt.time_of_day)}</strong> ` +
      `(around ${String(bt.around_hour).padStart(2, "0")}:00), ` +
      `averaging ${bt.avg_minutes} min. Plan demanding subjects then.` +
      `</div>`;
  }

  // List each detected cluster as a small summary card.
  for (const c of data.clusters) {
    const div = document.createElement("div");
    div.className = "cluster-item";
    div.innerHTML =
      `<span class="cluster-dot" style="background:${PALETTE[c.cluster % PALETTE.length]}"></span>` +
      `<span><strong>${escapeHtml(c.time_of_day)}</strong> ` +
      `(~${String(Math.round(c.avg_hour)).padStart(2, "0")}:00) — ` +
      `${c.size} session(s), avg ${c.avg_minutes} min</span>`;
    clusterBox.appendChild(div);
  }

  // Draw the scatter chart (only if Chart.js is available).
  if (typeof Chart === "undefined") return;

  // Group the points by cluster so each cluster is its own coloured dataset.
  const byCluster = {};
  for (const pt of data.points) {
    (byCluster[pt.cluster] = byCluster[pt.cluster] || []).push({
      x: pt.hour,            // x-axis: hour of day (0..23)
      y: pt.minutes,         // y-axis: session length in minutes
      subject: pt.subject,   // kept for the tooltip
    });
  }

  const datasets = Object.keys(byCluster).map((cid) => ({
    label: `Cluster ${Number(cid) + 1}`,
    data: byCluster[cid],
    backgroundColor: PALETTE[Number(cid) % PALETTE.length],
    pointRadius: 5,
  }));

  if (chartPatterns) chartPatterns.destroy();
  chartPatterns = new Chart(document.getElementById("chart-patterns"), {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      scales: {
        x: {
          title: { display: true, text: "Hour of day" },
          min: 0, max: 24, ticks: { stepSize: 3 },
        },
        y: {
          title: { display: true, text: "Session length (min)" },
          beginAtZero: true,
        },
      },
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: {
            // Show subject + time + duration in the tooltip.
            label: (ctx) => {
              const p = ctx.raw;
              return `${p.subject}: ${p.y} min at ${String(p.x).padStart(2, "0")}:00`;
            },
          },
        },
      },
    },
  });
}


/* ---------------------------------------------------------------------
 * ML model 2 — Study forecast (Linear regression).
 * Renders the weekly study-minutes series as a line plus a forecast point
 * for next week, and a one-line trend insight.
 * ------------------------------------------------------------------- */

async function loadForecast() {
  const data = await api.get("/api/forecast");
  const insight = document.getElementById("forecast-insight");
  insight.innerHTML = "";

  // Not enough weekly data yet → show the message, clear the chart.
  if (!data.ok || !data.enough_data) {
    insight.innerHTML =
      `<div class="rec info">${escapeHtml(data.message || "Not enough data yet.")}</div>`;
    if (chartForecast) { chartForecast.destroy(); chartForecast = null; }
    return;
  }

  // Trend insight sentence with a colour matching the direction.
  const trendClass =
    data.trend === "increasing" ? "success" :
    data.trend === "decreasing" ? "warning" : "info";
  insight.innerHTML =
    `<div class="rec ${trendClass}">` +
    `Weekly study time is <strong>${escapeHtml(data.trend)}</strong> ` +
    `(${data.slope_per_week >= 0 ? "+" : ""}${data.slope_per_week} min/week). ` +
    `Forecast for next week: <strong>${data.next_week_forecast} min</strong>.` +
    `</div>`;

  if (typeof Chart === "undefined") return;

  // Build the labels (week labels + a "Next" label for the forecast point).
  const labels = data.weeks.map((w) => w.label);
  labels.push("Next");

  // Actual minutes per week; the forecast slot stays null on this dataset.
  const actual = data.weeks.map((w) => w.minutes);
  actual.push(null);

  // Forecast dataset: only the last point is set (so it appears as a marker).
  const forecastSeries = data.weeks.map(() => null);
  forecastSeries.push(data.next_week_forecast);

  if (chartForecast) chartForecast.destroy();
  chartForecast = new Chart(document.getElementById("chart-forecast"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Actual minutes",
          data: actual,
          borderColor: "#2563eb",
          backgroundColor: "#2563eb",
          tension: 0.2,
        },
        {
          label: "Forecast (next week)",
          data: forecastSeries,
          borderColor: "#16a34a",
          backgroundColor: "#16a34a",
          pointRadius: 6,
          pointStyle: "rectRot",
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "Minutes" } } },
    },
  });
}


/* ---------------------------------------------------------------------
 * Utilities.
 * ------------------------------------------------------------------- */

// Escape user-provided text before inserting into HTML, to prevent XSS.
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Reload every section of the dashboard in a sensible order.
async function reloadAll() {
  await loadPeriod();           // banner + finished notification
  await loadSubjects();         // subjects list + dropdown
  await loadRecords();          // study records table
  await loadRecommendations();  // recommendations + stats + overview charts
  await loadPatterns();         // ML 1: KMeans patterns
  await loadForecast();         // ML 2: regression forecast
}


/* ---------------------------------------------------------------------
 * Entry point — runs once when the page has loaded.
 * ------------------------------------------------------------------- */
(async function init() {
  await reloadAll();
})();
