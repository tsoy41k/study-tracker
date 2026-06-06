"""
ML module for Study Tracker.

Everything here works ONLY on the study-time records produced by the timer
(no grades are involved). There are two scikit-learn models plus a small
rule-based recommender:

  1. analyze_patterns(user_id)            -> KMeans (unsupervised)
        Clusters the student's individual study SESSIONS by
        (hour_of_day, weekday, duration_minutes) to discover behaviour
        patterns:
          - the most productive time of day (cluster with the longest sessions),
          - typical session length,
          - which subjects are neglected (little or no study time).
        Returns cluster info + points for the scatter chart.

  2. forecast_study_time(user_id)         -> LinearRegression (supervised)
        Fits a line over (week_index -> total_minutes_that_week) to find the
        trend of the student's weekly study load and forecast next week.
        Returns the weekly series, the slope (trend) and the next-week forecast.

  3. generate_recommendations(user_id)    -> rule-based (NOT ML)
        Plain-English advice derived purely from the time records:
          - subjects never studied,
          - a large imbalance between the most/least studied subject,
          - the productive-time insight from analyze_patterns,
          - the trend insight from forecast_study_time.
        Gated behind the analysis period exactly like before.
"""

from __future__ import annotations          # allow modern type hints

from collections import defaultdict          # grouping helper
from datetime import datetime, timedelta     # date math for weeks
from statistics import mean                  # simple averages

import numpy as np                           # numeric arrays for the models
from sklearn.cluster import KMeans           # unsupervised model (patterns)
from sklearn.linear_model import LinearRegression  # supervised model (forecast)

from database import (
    analysis_status,   # analysis-period status (gate for recommendations)
    get_records,       # all study sessions for a user
    get_subjects,      # all subjects of a user
    get_user,          # the user row
)


# ---------- Helpers ----------

def _parse_dt(value: str) -> datetime:
    """Parse a stored timestamp string into a datetime (tolerates two formats)."""
    try:
        return datetime.fromisoformat(value)                    # "2025-05-27T10:00:00"
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")    # "2025-05-27 10:00:00"


def _hour_name(hour: int) -> str:
    """Human label for a time-of-day bucket given an hour (0..23)."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


# =====================================================================
# MODEL 1 — KMeans clustering of study sessions (patterns)
# =====================================================================

def analyze_patterns(user_id: int) -> dict:
    """
    Cluster the user's study sessions to surface behaviour patterns.

    Features per session: [hour_of_day, weekday, duration_minutes].
    """
    records = get_records(user_id, limit=10000)   # all sessions

    # Need a reasonable number of sessions for clustering to be meaningful.
    MIN_SESSIONS = 6
    if len(records) < MIN_SESSIONS:
        return {
            "ok": True,
            "enough_data": False,
            "message": (
                f"Need at least {MIN_SESSIONS} study sessions to detect patterns "
                f"(you have {len(records)}). Keep using the timer."
            ),
            "points": [],
            "clusters": [],
            "best_time": None,
        }

    # Build the feature matrix and keep parallel info for the scatter chart.
    feats = []     # numeric features for KMeans
    points = []    # raw points to draw (hour vs duration)
    for r in records:
        dt = _parse_dt(r["started_at"])
        dur_min = r["duration_sec"] / 60.0
        feats.append([dt.hour, dt.weekday(), dur_min])
        points.append({
            "hour": dt.hour,
            "weekday": dt.weekday(),
            "minutes": round(dur_min, 1),
            "subject": r["subject_name"],
        })

    X = np.array(feats, dtype=float)

    # Standardise features manually (mean 0, std 1) so one feature with a large
    # range (minutes) does not dominate the distance metric.
    means = X.mean(axis=0)
    stds = X.std(axis=0)
    stds[stds == 0] = 1.0                       # avoid division by zero
    X_scaled = (X - means) / stds

    # Choose a small number of clusters (at most 3, fewer if few sessions).
    k = min(3, len(records))
    model = KMeans(n_clusters=k, n_init=10, random_state=42)
    labels = model.fit_predict(X_scaled)        # cluster id per session

    # Attach the cluster id to each scatter point.
    for i, p in enumerate(points):
        p["cluster"] = int(labels[i])

    # Summarise each cluster: size, average hour, average duration.
    clusters = []
    for c in range(k):
        idx = [i for i, lab in enumerate(labels) if lab == c]
        if not idx:
            continue
        avg_hour = mean(feats[i][0] for i in idx)
        avg_dur = mean(feats[i][2] for i in idx)
        clusters.append({
            "cluster": c,
            "size": len(idx),
            "avg_hour": round(avg_hour, 1),
            "avg_minutes": round(avg_dur, 1),
            "time_of_day": _hour_name(int(round(avg_hour))),
        })

    # The "best time" = cluster whose average session is the LONGEST
    # (i.e. when this student tends to study most intensely).
    best = max(clusters, key=lambda c: c["avg_minutes"])
    best_time = {
        "time_of_day": best["time_of_day"],
        "around_hour": int(round(best["avg_hour"])),
        "avg_minutes": best["avg_minutes"],
    }

    return {
        "ok": True,
        "enough_data": True,
        "points": points,        # for the scatter chart (hour vs minutes, coloured)
        "clusters": clusters,    # cluster summaries
        "best_time": best_time,  # the headline insight
        "n_sessions": len(records),
    }


# =====================================================================
# MODEL 2 — Linear regression forecast of weekly study time
# =====================================================================

def forecast_study_time(user_id: int) -> dict:
    """
    Fit a line over (week_index -> total minutes that week) and forecast the
    next week's total study time.
    """
    records = get_records(user_id, limit=10000)
    if not records:
        return {
            "ok": True,
            "enough_data": False,
            "message": "No study sessions yet. Start the timer to build a forecast.",
            "weeks": [],
        }

    # Group total minutes by ISO calendar week.
    by_week: dict[tuple[int, int], float] = defaultdict(float)
    for r in records:
        dt = _parse_dt(r["started_at"])
        iso = dt.isocalendar()                       # (year, week, weekday)
        by_week[(iso[0], iso[1])] += r["duration_sec"] / 60.0

    # Sort the weeks chronologically and turn them into a numbered series.
    weeks_sorted = sorted(by_week.keys())
    series = []
    for i, wk in enumerate(weeks_sorted):
        series.append({
            "week_index": i,
            "label": f"{wk[0]}-W{wk[1]:02d}",
            "minutes": round(by_week[wk], 1),
        })

    # Need at least 2 weeks to fit a trend line.
    if len(series) < 2:
        return {
            "ok": True,
            "enough_data": False,
            "message": (
                "Only one week of data so far. A trend forecast needs at least "
                "two weeks of study history."
            ),
            "weeks": series,
        }

    # Train: X = week index, y = minutes that week.
    X = np.array([[s["week_index"]] for s in series], dtype=float)
    y = np.array([s["minutes"] for s in series], dtype=float)
    model = LinearRegression().fit(X, y)

    slope = float(model.coef_[0])                    # minutes change per week
    next_index = len(series)                         # the upcoming week
    forecast = float(model.predict(np.array([[next_index]], dtype=float))[0])
    forecast = max(0.0, forecast)                    # cannot study negative time

    # Describe the trend in words.
    if slope > 5:
        trend = "increasing"
    elif slope < -5:
        trend = "decreasing"
    else:
        trend = "stable"

    return {
        "ok": True,
        "enough_data": True,
        "weeks": series,                             # weekly series for the line chart
        "slope_per_week": round(slope, 1),           # trend strength (min/week)
        "trend": trend,                              # increasing/decreasing/stable
        "next_week_forecast": round(forecast, 1),    # predicted minutes next week
    }


# =====================================================================
# Per-subject statistics (used by charts and recommendations)
# =====================================================================

def _subject_stats(user_id: int) -> list[dict]:
    """Aggregate per-subject totals: study minutes and number of sessions."""
    subjects = get_subjects(user_id)
    records = get_records(user_id, limit=10000)

    by_subj: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        by_subj[r["subject_id"]].append(r)

    out = []
    for s in subjects:
        recs = by_subj.get(s["id"], [])
        total_min = sum(r["duration_sec"] for r in recs) / 60.0
        out.append({
            "subject_id": s["id"],
            "subject_name": s["name"],
            "sessions": len(recs),
            "total_minutes": round(total_min, 1),
        })
    return out


# =====================================================================
# Rule-based recommendations (NOT ML) — gated by the analysis period
# =====================================================================

def generate_recommendations(user_id: int) -> dict:
    """Produce time-based recommendations + per-subject stats."""
    user = get_user(user_id)
    if not user:
        return {"ok": False, "error": "User not found"}

    status = analysis_status(user)        # period info
    stats = _subject_stats(user_id)       # per-subject aggregates (also for charts)

    # Gate 1: no analysis period chosen yet.
    if not status["configured"]:
        return {
            "ok": True,
            "period": status,
            "subject_stats": stats,
            "recommendations": [{
                "type": "info",
                "message": "Set an analysis period to enable recommendations.",
            }],
        }

    # Gate 2: period still running → recommendations locked.
    if not status["finished"]:
        return {
            "ok": True,
            "period": status,
            "subject_stats": stats,
            "recommendations": [{
                "type": "info",
                "message": (
                    f"Collecting your study data. "
                    f"Recommendations will be generated in {status['days_left']} day(s), "
                    f"on {status['ends_at']}."
                ),
            }],
        }

    # Period finished (or immediate) → build advice from the time data.
    recommendations: list[dict] = []

    studied = [s for s in stats if s["sessions"] > 0]
    not_studied = [s for s in stats if s["sessions"] == 0]

    # Rule 1) Subjects never studied.
    for s in not_studied:
        recommendations.append({
            "type": "warning",
            "message": (
                f"You did not study '{s['subject_name']}' at all during the "
                f"analysis period. Schedule at least one session this week."
            ),
        })

    # Rule 2) Big imbalance between least- and most-studied subjects.
    if len(studied) >= 2:
        min_s = min(studied, key=lambda x: x["total_minutes"])
        max_s = max(studied, key=lambda x: x["total_minutes"])
        if min_s["total_minutes"] < max_s["total_minutes"] * 0.5:
            recommendations.append({
                "type": "focus",
                "message": (
                    f"You spent only {min_s['total_minutes']:.0f} min on "
                    f"'{min_s['subject_name']}' vs {max_s['total_minutes']:.0f} min on "
                    f"'{max_s['subject_name']}'. Consider rebalancing toward "
                    f"'{min_s['subject_name']}'."
                ),
            })

    # Rule 3) Insight from the KMeans pattern model (best time of day).
    patterns = analyze_patterns(user_id)
    if patterns.get("enough_data") and patterns.get("best_time"):
        bt = patterns["best_time"]
        recommendations.append({
            "type": "tip",
            "message": (
                f"Your longest study sessions happen in the {bt['time_of_day']} "
                f"(around {bt['around_hour']:02d}:00). Plan demanding subjects then."
            ),
        })

    # Rule 4) Insight from the regression forecast (weekly trend).
    forecast = forecast_study_time(user_id)
    if forecast.get("enough_data"):
        if forecast["trend"] == "decreasing":
            recommendations.append({
                "type": "warning",
                "message": (
                    f"Your weekly study time is decreasing "
                    f"({forecast['slope_per_week']:+.0f} min/week). "
                    f"Try to keep a steady schedule."
                ),
            })
        elif forecast["trend"] == "increasing":
            recommendations.append({
                "type": "success",
                "message": (
                    f"Great momentum — your weekly study time is increasing "
                    f"({forecast['slope_per_week']:+.0f} min/week). Keep it up!"
                ),
            })

    # Fallback if nothing fired.
    if not recommendations:
        recommendations.append({
            "type": "success",
            "message": "Your study habits during this period look balanced. Keep it up.",
        })

    return {
        "ok": True,
        "period": status,
        "subject_stats": stats,
        "recommendations": recommendations,
    }
