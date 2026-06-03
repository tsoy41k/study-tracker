/* Study Tracker - dashboard logic */

const api = {
  async get(url) {
    const r = await fetch(url);
    return r.json();
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  },
  async del(url) {
    const r = await fetch(url, { method: "DELETE" });
    return r.json();
  },
};

// ---------- Period banner ----------

async function loadPeriod() {
  const data = await api.get("/api/period");
  const banner = document.getElementById("period-banner");
  if (!data.ok || !data.period.configured) {
    banner.innerHTML = "";
    return;
  }
  const p = data.period;
  if (p.immediate) {
    banner.className = "period-banner finished";
    banner.innerHTML =
      `<strong>Immediate mode.</strong> ` +
      `Recommendations are generated from your data as soon as it is entered.`;
  } else if (p.finished) {
    banner.className = "period-banner finished";
    banner.innerHTML =
      `<strong>Analysis period finished.</strong> ` +
      `Recommendations below are based on ${p.period_days} day(s) of data ` +
      `(${p.started_at} → ${p.ends_at}).`;
  } else {
    banner.className = "period-banner active";
    banner.innerHTML =
      `<strong>Analysis period active.</strong> ` +
      `Started ${p.started_at}, ends ${p.ends_at} ` +
      `(${p.days_left} day(s) left). Recommendations are locked until the period ends.`;
  }
}

// ---------- Subjects ----------

async function loadSubjects() {
  const data = await api.get("/api/subjects");
  if (!data.ok) return;

  const list = document.getElementById("subject-list");
  const select = document.getElementById("timer-subject");
  const gradeSelect = document.getElementById("grade-subject");

  list.innerHTML = "";
  select.innerHTML = "";
  gradeSelect.innerHTML = "";

  if (data.subjects.length === 0) {
    list.innerHTML = '<li class="muted small">No subjects yet. Add one above.</li>';
    const opt = document.createElement("option");
    opt.textContent = "No subjects available";
    opt.disabled = true;
    select.appendChild(opt.cloneNode(true));
    gradeSelect.appendChild(opt);
    document.getElementById("btn-start").disabled = true;
    return;
  }

  document.getElementById("btn-start").disabled = false;

  for (const s of data.subjects) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(s.name)}</span>
                    <button class="del" data-id="${s.id}" title="Delete">×</button>`;
    list.appendChild(li);

    for (const sel of [select, gradeSelect]) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name;
      sel.appendChild(opt);
    }
  }

  list.querySelectorAll(".del").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!confirm("Delete this subject and ALL its records/grades?")) return;
      const res = await api.del(`/api/subjects/${b.dataset.id}`);
      if (res.ok) {
        await reloadAll();
      } else {
        alert(res.error || "Delete failed");
      }
    });
  });
}

document
  .getElementById("add-subject-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("new-subject");
    const name = input.value.trim();
    if (!name) return;
    const res = await api.post("/api/subjects", { name });
    if (res.ok) {
      input.value = "";
      await loadSubjects();
      await loadRecommendations();
    } else {
      alert(res.error || "Failed to add subject");
    }
  });

// ---------- Timer ----------

let timerInterval = null;
let timerStartTs = null;

function formatHMS(totalSec) {
  const h = String(Math.floor(totalSec / 3600)).padStart(2, "0");
  const m = String(Math.floor((totalSec % 3600) / 60)).padStart(2, "0");
  const s = String(totalSec % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function tick() {
  const elapsed = Math.floor((Date.now() - timerStartTs) / 1000);
  document.getElementById("timer-display").textContent = formatHMS(elapsed);
}

document.getElementById("btn-start").addEventListener("click", () => {
  const select = document.getElementById("timer-subject");
  if (!select.value) {
    alert("Choose a subject first.");
    return;
  }
  timerStartTs = Date.now();
  tick();
  timerInterval = setInterval(tick, 1000);

  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-stop").disabled = false;
  select.disabled = true;

  const subjName = select.options[select.selectedIndex].textContent;
  document.getElementById("timer-status").textContent = `Studying: ${subjName}`;
});

document.getElementById("btn-stop").addEventListener("click", async () => {
  if (!timerInterval) return;
  clearInterval(timerInterval);
  timerInterval = null;

  const endedTs = Date.now();
  const durationSec = Math.floor((endedTs - timerStartTs) / 1000);
  const subjectId = parseInt(document.getElementById("timer-subject").value, 10);

  const started = new Date(timerStartTs).toISOString();
  const ended = new Date(endedTs).toISOString();

  const res = await api.post("/api/records", {
    subject_id: subjectId,
    started_at: started,
    ended_at: ended,
    duration_sec: durationSec,
  });

  document.getElementById("btn-start").disabled = false;
  document.getElementById("btn-stop").disabled = true;
  document.getElementById("timer-subject").disabled = false;
  document.getElementById("timer-display").textContent = "00:00:00";

  if (res.ok) {
    document.getElementById("timer-status").textContent =
      `Saved! Session: ${formatHMS(durationSec)}`;
    await loadRecords();
    await loadRecommendations();
  } else {
    document.getElementById("timer-status").textContent =
      "Error: " + (res.error || "could not save");
  }
});

// ---------- Grades ----------

document
  .getElementById("add-grade-form")
  .addEventListener("submit", async (e) => {
    e.preventDefault();
    const subjectId = parseInt(document.getElementById("grade-subject").value, 10);
    const grade = parseFloat(document.getElementById("grade-value").value);
    const note = document.getElementById("grade-note").value.trim();

    if (isNaN(grade) || grade < 0 || grade > 100) {
      alert("Grade must be a number between 0 and 100.");
      return;
    }

    const res = await api.post("/api/grades", { subject_id: subjectId, grade, note });
    if (res.ok) {
      document.getElementById("grade-value").value = "";
      document.getElementById("grade-note").value = "";
      await loadGrades();
      await loadRecommendations();
    } else {
      alert(res.error || "Failed to save grade");
    }
  });

async function loadGrades() {
  const data = await api.get("/api/grades");
  const tbody = document.querySelector("#grades-table tbody");
  tbody.innerHTML = "";
  if (!data.ok || data.grades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No grades yet.</td></tr>';
    return;
  }
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

// ---------- Records ----------

async function loadRecords() {
  const data = await api.get("/api/records");
  const tbody = document.querySelector("#records-table tbody");
  tbody.innerHTML = "";
  if (!data.ok || data.records.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">No records yet.</td></tr>';
    return;
  }
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

// ---------- Recommendations + stats ----------

async function loadRecommendations() {
  const data = await api.get("/api/recommendations");
  const box = document.getElementById("recommendations");
  box.innerHTML = "";

  if (!data.ok) {
    box.textContent = "Failed to load recommendations.";
    return;
  }

  for (const rec of data.recommendations) {
    const div = document.createElement("div");
    div.className = `rec ${rec.type}`;
    div.textContent = rec.message;
    box.appendChild(div);
  }

  // Stats table + grade predictions
  const tbody = document.querySelector("#stats-table tbody");
  tbody.innerHTML = "";
  if (!data.subject_stats || data.subject_stats.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No statistics yet.</td></tr>';
    return;
  }

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

  const cells = tbody.querySelectorAll("td[data-pred]");
  await Promise.all(
    Array.from(cells).map(async (cell) => {
      const sid = cell.dataset.pred;
      const p = await api.get(`/api/predict/${sid}`);
      if (p.ok && p.predicted_grade !== null && p.predicted_grade !== undefined) {
        cell.textContent = `${p.predicted_grade} / 100  (${p.method})`;
      } else if (p.ok) {
        cell.textContent = `— (${p.method})`;
      } else {
        cell.textContent = "—";
      }
    })
  );
}

document
  .getElementById("btn-refresh-recs")
  .addEventListener("click", reloadAll);

// ---------- Utility ----------

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function reloadAll() {
  await loadPeriod();
  await loadSubjects();
  await loadRecords();
  await loadGrades();
  await loadRecommendations();
}

(async function init() {
  await reloadAll();
})();
