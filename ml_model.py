"""
ML module for Study Tracker.

The model answers two questions for the student:

  1. predict_grade(user_id, subject_id)
        Predicts the next grade (0..100) the student is likely to get for a
        subject, based on the relationship between total study time on that
        subject and the grades the student has already entered.

        Algorithm:
          - Build training examples (X, y) from the grades table:
              X[i] = cumulative_minutes_studied up to the moment grade[i] was added
              y[i] = grade[i]
          - Fit a LinearRegression (sklearn) over those points.
          - Predict y* for X* = current cumulative study minutes on that subject.
          - If the user has < 2 grades, fall back to the simple average grade
            (or to "no prediction yet" if there are no grades at all).

  2. generate_recommendations(user_id)
        Produces a list of actionable, plain-English study recommendations.
        IMPORTANT: recommendations are gated behind the user's analysis period
        (set during initial setup). Until the analysis period elapses, this
        function returns a single "still collecting data" message; only after
        the period ends does it analyze the records and return real advice.

        Heuristics applied after the period:
          - "Under-studied subject":     subject with the lowest total study time.
          - "Most time spent":           subject with the highest total time (info).
          - "Low average grade":         subject whose average grade < 60.
          - "Time vs grade mismatch":    a lot of time studied but low grade,
                                         or very little time but a good grade
                                         (suggests review of method or priorities).
          - "No data for subject":       no records at all in this period.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean

import numpy as np
from sklearn.linear_model import LinearRegression

from database import (
    analysis_status,
    get_grades,
    get_records,
    get_subject,
    get_subjects,
    get_user,
)


# ---------- Helpers ----------

def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _cumulative_minutes_at(records: list[dict], moment: datetime) -> float:
    """Sum (in minutes) of all study records strictly before `moment`."""
    total = 0
    for r in records:
        if _parse_dt(r["started_at"]) <= moment:
            total += r["duration_sec"]
    return total / 60.0


# ---------- Grade prediction ----------

def predict_grade(user_id: int, subject_id: int) -> dict:
    """Predict the next grade (0..100) for a subject."""
    subject = get_subject(user_id, subject_id)
    if not subject:
        return {"ok": False, "error": "Subject not found"}

    records = get_records(user_id, subject_id=subject_id, limit=10000)
    records.sort(key=lambda r: _parse_dt(r["started_at"]))
    grades = get_grades(user_id, subject_id=subject_id)
    grades.sort(key=lambda g: _parse_dt(g["created_at"]))

    if not grades:
        return {
            "ok": True,
            "subject": subject["name"],
            "predicted_grade": None,
            "method": "no grades yet — add at least one grade to enable prediction",
            "samples": 0,
        }

    # Build (X, y): cumulative study minutes BEFORE the grade was given -> grade
    X, y = [], []
    for g in grades:
        moment = _parse_dt(g["created_at"])
        cum_min = _cumulative_minutes_at(records, moment)
        X.append([cum_min])
        y.append(g["grade"])

    current_total_min = sum(r["duration_sec"] for r in records) / 60.0

    if len(grades) < 2:
        avg = float(mean(y))
        return {
            "ok": True,
            "subject": subject["name"],
            "predicted_grade": round(avg, 1),
            "method": "average (need 2+ grades for regression)",
            "samples": len(grades),
            "current_study_minutes": round(current_total_min, 1),
        }

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=float)

    # Guard against zero variance in X (all grades entered at the same total time)
    if float(np.std(X_arr)) < 1e-6:
        avg = float(np.mean(y_arr))
        return {
            "ok": True,
            "subject": subject["name"],
            "predicted_grade": round(avg, 1),
            "method": "average (study time was identical at every grade)",
            "samples": len(grades),
            "current_study_minutes": round(current_total_min, 1),
        }

    model = LinearRegression().fit(X_arr, y_arr)
    pred = float(model.predict(np.array([[current_total_min]], dtype=float))[0])
    pred = max(0.0, min(pred, 100.0))  # clamp to 0..100

    return {
        "ok": True,
        "subject": subject["name"],
        "predicted_grade": round(pred, 1),
        "method": "linear regression (study time → grade)",
        "samples": len(grades),
        "current_study_minutes": round(current_total_min, 1),
        "slope_per_hour": round(float(model.coef_[0]) * 60.0, 2),
    }


# ---------- Recommendations ----------

def _subject_stats(user_id: int) -> list[dict]:
    """Aggregate per-subject totals: study minutes, sessions, avg grade."""
    subjects = get_subjects(user_id)
    records  = get_records(user_id, limit=10000)
    grades   = get_grades(user_id)

    by_subj_records: dict[int, list[dict]] = defaultdict(list)
    by_subj_grades:  dict[int, list[float]] = defaultdict(list)
    for r in records:
        by_subj_records[r["subject_id"]].append(r)
    for g in grades:
        by_subj_grades[g["subject_id"]].append(float(g["grade"]))

    out = []
    for s in subjects:
        recs = by_subj_records.get(s["id"], [])
        gs   = by_subj_grades.get(s["id"], [])
        total_min = sum(r["duration_sec"] for r in recs) / 60.0
        avg_grade = float(mean(gs)) if gs else None
        out.append({
            "subject_id": s["id"],
            "subject_name": s["name"],
            "sessions": len(recs),
            "total_minutes": round(total_min, 1),
            "grades_count": len(gs),
            "avg_grade": round(avg_grade, 1) if avg_grade is not None else None,
        })
    return out


def generate_recommendations(user_id: int) -> dict:
    """
    Produce recommendations + per-subject stats.
    Gated behind the user's analysis period: if the period has not yet ended,
    return a single 'still collecting data' message and no advice.
    """
    user = get_user(user_id)
    if not user:
        return {"ok": False, "error": "User not found"}

    status = analysis_status(user)
    stats = _subject_stats(user_id)

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

    # Period finished → produce real recommendations
    recommendations: list[dict] = []

    studied_stats = [s for s in stats if s["sessions"] > 0]
    not_studied   = [s for s in stats if s["sessions"] == 0]

    # 1) Subjects with no study at all during the period
    for s in not_studied:
        recommendations.append({
            "type": "warning",
            "message": (
                f"You did not study '{s['subject_name']}' at all during the "
                f"analysis period. Schedule at least one session this week."
            ),
        })

    if studied_stats:
        # 2) Under-studied vs most-studied
        if len(studied_stats) >= 2:
            min_s = min(studied_stats, key=lambda x: x["total_minutes"])
            max_s = max(studied_stats, key=lambda x: x["total_minutes"])
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

        # 3) Low average grades
        for s in studied_stats:
            if s["avg_grade"] is not None and s["avg_grade"] < 60:
                recommendations.append({
                    "type": "warning",
                    "message": (
                        f"Average grade for '{s['subject_name']}' is "
                        f"{s['avg_grade']:.0f}/100 — below 60. "
                        f"Increase study time or change study method."
                    ),
                })

        # 4) Time vs grade mismatch
        for s in studied_stats:
            if s["avg_grade"] is None:
                continue
            if s["total_minutes"] >= 180 and s["avg_grade"] < 70:
                recommendations.append({
                    "type": "tip",
                    "message": (
                        f"You spent a lot of time ({s['total_minutes']:.0f} min) on "
                        f"'{s['subject_name']}' but the grade is {s['avg_grade']:.0f}. "
                        f"The study method may need revision (practice tests, summaries, peers)."
                    ),
                })
            if s["total_minutes"] <= 30 and s["avg_grade"] >= 80:
                recommendations.append({
                    "type": "success",
                    "message": (
                        f"'{s['subject_name']}': great result ({s['avg_grade']:.0f}) "
                        f"with only {s['total_minutes']:.0f} min of tracked study. "
                        f"Keep the current approach."
                    ),
                })

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
