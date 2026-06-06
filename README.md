# Study Tracker

A Flask web application that lets a student log study sessions per subject
with a built-in timer. Two scikit-learn models then analyse the resulting
**study-time records** to surface behaviour patterns and forecast the
student's study load. No grades are involved — everything is derived from
the time data the timer produces.

## How the workflow looks for the student

1. **Register** with a numeric Student ID + password.
2. On first entry the student is sent to the **Setup** screen and chooses
   an *analysis period* (Immediate / 7 / 14 / 30 / 60 / 90 days).
3. The dashboard automatically contains 6 default subjects:
   - Internet of Things
   - Digital Marketing
   - System analysis and design
   - Distributed systems and cloud computing
   - Technology law
   - Business information systems project

   The student can add their own subjects or delete any of them.
4. The student picks a subject, starts the **timer**, presses **Stop & Save**
   at the end — a study record (start, end, duration) is written to the DB.
5. **Recommendations are locked** until the analysis period ends (unless
   Immediate mode was chosen). The dashboard banner shows the days left and
   a notification appears once the period is over.
6. The dashboard shows two ML analyses (below) plus overview charts.

## The two ML models (both work only on study-time records)

### 1. Study patterns — KMeans (unsupervised)

Lives in `analyze_patterns()` in [`ml_model.py`](ml_model.py).

- **Features per session:** `[hour_of_day, weekday, duration_minutes]`,
  standardised (mean 0, std 1) so minutes don't dominate the distance.
- **Model:** `sklearn.cluster.KMeans` (up to 3 clusters).
- **Output:** clusters of similar sessions, the **most productive time of
  day** (cluster with the longest average sessions), and a scatter chart of
  *hour of day vs session length* coloured by cluster.
- Needs at least 6 sessions; otherwise it reports "not enough data".

### 2. Study forecast — Linear Regression (supervised)

Lives in `forecast_study_time()` in [`ml_model.py`](ml_model.py).

- **Training data:** `X = week_index`, `y = total_minutes_that_week`.
- **Model:** `sklearn.linear_model.LinearRegression`.
- **Output:** the weekly trend (increasing / decreasing / stable, in
  min/week) and a **forecast of next week's study minutes**, shown as a line
  chart with the forecast point highlighted.
- Needs at least 2 weeks of history; otherwise it reports "not enough data".

## How recommendations work (rule-based, NOT ML)

The recommender lives in `generate_recommendations()` and runs on every call
to `/api/recommendations`.

**Step 1 — analysis-period gate.** If the period has not finished yet, it
returns a single info message and stops. No advice is produced.

**Step 2 — per-subject aggregation.** Once the period has ended it computes,
per subject: number of sessions and total minutes studied.

**Step 3 — rule application.** Plain-English advice from a small rule set:

| Rule | Trigger | Message type |
| ---- | ------- | ------------ |
| *No study* | A subject has 0 sessions during the period | `warning` |
| *Under-studied subject* | One subject has < 50% of the time of the most-studied one | `focus` |
| *Best time of day* | Insight taken from the KMeans pattern model | `tip` |
| *Decreasing trend* | Weekly study time is falling (from the regression) | `warning` |
| *Increasing trend* | Weekly study time is rising (from the regression) | `success` |
| *Balanced* | None of the above fires | `success` |

Recommendations are intentionally rule-based and explainable; the ML output
(patterns + forecast) feeds two of the rules.

## Project layout

```
.
├── app.py                # Flask routes (pages + JSON API)
├── database.py           # SQLite schema + CRUD
├── ml_model.py           # KMeans patterns + regression forecast + recommendations
├── test_app.py           # End-to-end smoke test (Flask test client)
├── requirements.txt
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   ├── setup.html        # Choose analysis period (first-time)
│   └── dashboard.html
└── static/
    ├── style.css
    └── app.js
```

## Database schema

`study_tracker.db` (SQLite, auto-created) has three tables:

| Table          | Key columns |
| -------------- | ----------- |
| `users`        | `id` (numeric Student ID, PK), `password_hash`, `created_at`, `analysis_period_days`, `analysis_started_at` |
| `subjects`     | `id` (PK), `user_id` (FK), `name`, `created_at` — unique per user |
| `study_records`| `id` (PK), `user_id`, `subject_id`, `started_at`, `ended_at`, `duration_sec` |

Foreign keys cascade on delete.

## API reference (JSON; all require login)

| Method | Path                          | Description                          |
| ------ | ----------------------------- | ------------------------------------ |
| GET    | `/api/subjects`               | List user's subjects                 |
| POST   | `/api/subjects`               | Add subject `{name}`                 |
| DELETE | `/api/subjects/<id>`          | Delete subject (and its records)     |
| POST   | `/api/records`                | Add session record                   |
| GET    | `/api/records[?subject_id=]`  | List records                         |
| GET    | `/api/patterns`               | KMeans study-session patterns        |
| GET    | `/api/forecast`               | Linear-regression weekly forecast    |
| GET    | `/api/recommendations`        | Recommendations + per-subject stats  |
| GET    | `/api/period`                 | Analysis period status               |

## Setup & run

Requires Python 3.10+ (tested on Python 3.14).

```powershell
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 — register an account, choose an analysis
period, then start tracking.

## Tests

```powershell
python test_app.py
```

## Notes & limitations

- The Flask `secret_key` in `app.py` is a placeholder; change it for any
  real deployment.
- Authentication is intentionally minimal (Student ID + password). No
  password reset, no rate limiting.
- Both ML models work only on the study-time records produced by the timer;
  no grades are stored or used. KMeans needs ≥ 6 sessions and the forecast
  needs ≥ 2 weeks of history before they produce results.
