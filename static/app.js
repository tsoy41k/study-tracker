/* =====================================================================
 * Study Tracker — front-end logic for the dashboard page.
 *
 * Responsibilities:
 *   - Talk to the Flask JSON API (fetch wrapper `api`).
 *   - Manage the subjects list (add / delete / fill dropdowns).
 *   - Run the study timer (start / stop / save a record).
 *   - Save grades entered by the student.
 *   - Show recommendations + per-subject statistics + grade predictions.
 *   - Draw three Chart.js charts (study time bar, distribution pie, grades bar).
 *   - Show the analysis-period banner and the "period finished" notification.
 *
 * The whole file is plain browser JavaScript (no framework). It is loaded
 * at the bottom of dashboard.html, after Chart.js.
 * ===================================================================== */


/* ---------------------------------------------------------------------
 * `api` — a tiny wrapper around fetch() so we don't repeat boilerplate.
 * Every method returns the parsed JSON body of the response.
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
 * redraw them every time the data changes (Chart.js does not auto-update
 * from new arrays unless we update or recreate the chart).
 * ------------------------------------------------------------------- */
let chartTime = null;          // bar chart: study minutes per subject
let chartDistribution = null;  // pie chart: share of total study time
let chartGrades = null;        // bar chart: average grade per subject


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
 * We use localStorage so the notification is not shown again after the user
 * dismisses it (keyed by the period end date, so a NEW period shows it again).
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
 * Subjects: load list, fill the two dropdowns, handle add / delete.
 * ------------------------------------------------------------------- */

async function loadSubjects() {
  const data = await api.get("/api/subjects");  // fetch the subjects
  if (!data.ok) return;                         // bail out on error

  // Grab the three places that show subjects.
  const list = document.getElementById("subject-list");      // the visible list
  const select = document.getElementById("timer-subject");   // timer dropdown
  const gradeSelect = document.getElementById("grade-subject"); // grade dropdown

  // Clear all three before re-filling them.
  list.innerHTML = "";
  select.innerHTML = "";
  gradeSelect.innerHTML = "";

  // Special case: the user has no subjects.
  if (data.subjects.length === 0) {
    list.innerHTML = '<li class="muted small">No subjects yet. Add one above.</li>';
    const opt = document.createElement("option");
    opt.textContent = "No subjects available";
    opt.disabled = true;
    select.appendChild(opt.cloneNode(true));  // put a placeholder in both selects
    gradeSelect.appendChild(opt);
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

    // Add the subject as an option in BOTH dropdowns.
    for (const sel of [select, gradeSelect]) {
      const opt = document.createElement("option");
      opt.value = s.id;             // option value = subject id
      opt.textContent = s.name;     // option label = subject name
      sel.appendChild(opt);
    }
  }

  // Attach click handlers to every delete (×) button.
  list.querySelectorAll(".del").forEach((b) => {
    b.addEventListener("click", async () => {
      // Confirm because deleting a subject cascades to its records/grades.
      if (!confirm("Delete this subject and ALL its records/grades?")) return;
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
      await loadSubjects();                           // refresh the list/dropdowns
      await loadRecommendations();                    // stats may change
    } else {
      alert(res.error || "Failed to add subject");
    }
  });


/* ---------------------------------------------------------------------
 * Study timer: start counting, stop and save the elapsed session.
 * ------------------------------------------------------------------- */

let timerInterval = null;   // holds the setInterval id while the timer runs
let timerStartTs = null;    // timestamp (ms) when the timer was started

// Format a number of seconds as HH:MM:SS.
function formatHMS(totalSec) {
  const h = String(Math.floor(totalSec / 3600)).padStart(2, "0");
  const m = String(Math.floor((totalSec % 3600) / 60)).padStart(2, "0");
  const s = String(totalSec % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

// Called every second to update the on-screen timer display.
function tick() {
  const elapsed = Math.floor((Date.now() - timerStartTs) / 1000);  // seconds so far
  document.getElementById("timer-display").textContent = formatHMS(elapsed);
}

// "Start" button: begin the timer.
document.getElementById("btn-start").addEventListener("click", () => {
  const select = document.getElementById("timer-subject");
  if (!select.value) {                       // no subject chosen
    alert("Choose a subject first.");
    return;
  }
  timerStartTs = Date.now();                 // record the start time
  tick();                                    // show 00:00:00 immediately
  timerInterval = setInterval(tick, 1000);   // update every second

  // Toggle button states: disable Start, enable Stop, lock the subject.
  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-stop").disabled = false;
  select.disabled = true;

  // Show which subject is being studied.
  const subjName = select.options[select.selectedIndex].textContent;
  document.getElementById("timer-status").textContent = `Studying: ${subjName}`;
});

// "Stop & Save" button: stop the timer and POST the session to the server.
document.getElementById("btn-stop").addEventListener("click", async () => {
  if (!timerInterval) return;            // nothing running
  clearInterval(timerInterval);          // stop the per-second updates
  timerInterval = null;

  const endedTs = Date.now();            // end timestamp
  const durationSec = Math.floor((endedTs - timerStartTs) / 1000);  // total seconds
  const subjectId = parseInt(document.getElementById("timer-subject").value, 10);

  // Convert timestamps to ISO strings for the API.
  const started = new Date(timerStartTs).toISOString();
  const ended = new Date(endedTs).toISOString();

  // Send the completed session to the backend.
  const res = await api.post("/api/records", {
    subject_id: subjectId,
    started_at: started,
    ended_at: ended,
    duration_sec: durationSec,
  });

  // Reset the UI back to the idle state.
  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
  document.getElementById("timer-subject").disabled = false;
  document.getElementById("timer-display").textContent = "00:00:00";

  if (res.ok) {
    document.getElementById("timer-status").textContent =
      `Saved! Session: ${formatHMS(durationSec)}`;
    await loadRecords();          // refresh the records table
    await loadRecommendations();  // refresh stats + charts
  } else {
    document.getElementById("timer-status").textContent =
      "Error: " + (res.error || "could not save");
  }
});


/* ---------------------------------------------------------------------
 * Grades: save a grade (optional field) and list recent grades.
 * ------------------------------------------------------------------- */

document
  .getElementById("add-grade-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();  // don't reload the page

    const subjectSel = document.getElementById("grade-subject");
    const subjectId = parseInt(subjectSel.value, 10);
    const gradeRaw = document.getElementById("grade-value").value;
    const note = document.getElementById("grade-note").value.trim();

    // The grade field is OPTIONAL — but if the form is submitted, a subject
    // and a valid grade must be present.
    if (!subjectSel.value) {
      alert("Choose a subject for this grade.");
      return;
    }
    const grade = parseFloat(gradeRaw);
    if (gradeRaw === "" || isNaN(grade) || grade < 0 || grade > 100) {
      alert("Enter a grade between 0 and 100 (or leave the grade form empty).");
      return;
    }

    const res = await api.post("/api/grades", { subject_id: subjectId, grade, note });
    if (res.ok) {
      document.getElementById("grade-value").value = "";  // clear inputs
      document.getElementById("grade-note").value = "";
      await loadGrades();           // refresh grades table
      await loadRecommendations();  // grade affects stats/predictions/charts
    } else {
      alert(res.error || "Failed to save grade");
    }
  });

// Load and render the recent grades table.
async function loadGrades() {
  const data = await api.get("/api/grades");
  const tbody = document.querySelector("#grades-table tbody");
  tbody.innerHTML = "";
  if (!data.ok || data.grades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No grades yet.</td></tr>';
    return;
  }
  // Show up to 30 most recent grades.
  for (const g of data.grades.slice(0, 30)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(g.subject_name)}</td>
      <td>${Number(g.grade).toFixed(1)}</td>
      <td>${escapeHtml(g.note || "")}</td>
      <td>${escapeHtml(g.created_at)}</td>`;
    tbody.appendChild(tr);
  }
}


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
 * Recommendations + statistics table + grade predictions + charts.
 * This is the central "refresh everything ML-related" function.
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

  // ----- Statistics table -----
  const tbody = document.querySelector("#stats-table tbody");
  tbody.innerHTML = "";
  if (!data.subject_stats || data.subject_stats.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No statistics yet.</td></tr>';
    drawCharts([]);   // clear charts too
    return;
  }

  // One row per subject; the prediction cell is filled in asynchronously.
  for (const s of data.subject_stats) {
    const tr = document.createElement("tr");
    const avg = s.avg_grade !== null && s.avg_grade !== undefined
      ? Number(s.avg_grade).toFixed(1)
      : "—";
    tr.innerHTML = `
      <td>${escapeHtml(s.subject_name)}</td>
      <td>${s.sessions}</td>
      <td>${s.total_minutes}</td>
      <td>${s.grades_count}</td>
      <td>${avg}</td>
      <td data-pred="${s.subject_id}">loading…</td>`;
    tbody.appendChild(tr);
  }

  // Draw the charts FIRST, from data we already have, so they always appear
  // even if a prediction request below is slow or fails.
  drawCharts(data.subject_stats);

  // Fetch a grade prediction for every subject in parallel, then fill cells.
  // Each fetch is wrapped in try/catch so one failure cannot break the others.
  const cells = tbody.querySelectorAll("td[data-pred]");
  await Promise.all(
    Array.from(cells).map(async (cell) => {
      const sid = cell.dataset.pred;                       // subject id
      try {
        const p = await api.get(`/api/predict/${sid}`);    // call ML endpoint
        if (p.ok && p.predicted_grade !== null && p.predicted_grade !== undefined) {
          cell.textContent = `${p.predicted_grade} / 100  (${p.method})`;
        } else if (p.ok) {
          cell.textContent = `— (${p.method})`;            // no grade yet
        } else {
          cell.textContent = "—";
        }
      } catch (e) {
        cell.textContent = "—";                            // network/parse error
      }
    })
  );
}

// Refresh button next to recommendations re-loads the whole dashboard.
document
  .getElementById("btn-refresh-recs")
  .addEventListener("click", reloadAll);


/* ---------------------------------------------------------------------
 * Charts: draw three Chart.js charts from the per-subject statistics.
 * ------------------------------------------------------------------- */

function drawCharts(stats) {
  // Defensive guard: if the Chart.js library did not load (e.g. the CDN is
  // blocked by an ad-blocker or there is no internet), skip drawing instead
  // of throwing a "Chart is not defined" error that would break the page.
  if (typeof Chart === "undefined") {
    const note = document.getElementById("charts-empty");
    note.style.display = "block";
    note.textContent = "Charts could not load (Chart.js library is unavailable).";
    return;
  }

  // Build parallel arrays from the stats objects.
  const labels = stats.map((s) => s.subject_name);          // x-axis labels
  const minutes = stats.map((s) => s.total_minutes);        // study minutes
  // For grades, use null where there is no grade so the bar is simply absent.
  const grades = stats.map((s) =>
    s.avg_grade !== null && s.avg_grade !== undefined ? s.avg_grade : null
  );

  // If there is no study time at all and no grades, show the "no data" note.
  const hasTime = minutes.some((m) => m > 0);
  const hasGrades = grades.some((g) => g !== null);
  document.getElementById("charts-empty").style.display =
    hasTime || hasGrades ? "none" : "block";

  // A fixed palette so every subject keeps a consistent colour.
  const palette = [
    "#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2",
    "#db2777", "#65a30d",
  ];
  const colors = labels.map((_, i) => palette[i % palette.length]);

  // Destroy existing chart instances before recreating (avoids ghosting).
  if (chartTime) chartTime.destroy();
  if (chartDistribution) chartDistribution.destroy();
  if (chartGrades) chartGrades.destroy();

  // ----- Chart 1: bar chart of study minutes per subject -----
  chartTime = new Chart(document.getElementById("chart-time"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Minutes",
        data: minutes,
        backgroundColor: colors,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },   // single dataset, no legend
      scales: { y: { beginAtZero: true } },
    },
  });

  // ----- Chart 2: pie chart of how total study time is distributed -----
  chartDistribution = new Chart(document.getElementById("chart-distribution"), {
    type: "pie",
    data: {
      labels,
      datasets: [{
        data: minutes,
        backgroundColor: colors,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom" } },
    },
  });

  // ----- Chart 3: bar chart of average grade per subject -----
  chartGrades = new Chart(document.getElementById("chart-grades"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Average grade",
        data: grades,
        backgroundColor: colors,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, max: 100 } },  // grades are 0..100
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
  await loadSubjects();         // subjects list + dropdowns
  await loadRecords();          // study records table
  await loadGrades();           // grades table
  await loadRecommendations();  // recommendations + stats + predictions + charts
}


/* ---------------------------------------------------------------------
 * Entry point — runs once when the page has loaded.
 * ------------------------------------------------------------------- */
(async function init() {
  await reloadAll();
})();
