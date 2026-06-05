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

from __future__ import annotations          # allow "list[dict]" type hints on older Pythons

from collections import defaultdict          # dict that creates default values on access
from datetime import datetime                # parse timestamps stored as text
from statistics import mean                  # simple average for the fallback path

import numpy as np                           # numeric arrays for the regression input
from sklearn.linear_model import LinearRegression  # the actual ML model

# Data-access helpers from our database layer (no raw SQL in this file).
from database import (
    analysis_status,   # period status (configured/finished/days_left)
    get_grades,        # grades for a user/subject
    get_records,       # study sessions for a user/subject
    get_subject,       # one subject (with ownership check)
    get_subjects,      # all subjects of a user
    get_user,          # the user row (for the period gate)
)


# ---------- Helpers ----------

def _parse_dt(value: str) -> datetime:
    """Parse a stored timestamp string into a datetime (tolerates two formats)."""
    try:
        return datetime.fromisoformat(value)                    # e.g. "2025-05-27T10:00:00"
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")    # e.g. "2025-05-27 10:00:00"


def _cumulative_minutes_at(records: list[dict], moment: datetime) -> float:
    """
    Total minutes studied up to (and including) `moment`.
    Used to know how much study time existed when a grade was recorded.
    """
    total = 0                                          # running total in seconds
    for r in records:                                  # walk every study session
        if _parse_dt(r["started_at"]) <= moment:       # only sessions before the grade
            total += r["duration_sec"]                 # add its length
    return total / 60.0                                # convert seconds → minutes


# ---------- Grade prediction ----------

def predict_grade(user_id: int, subject_id: int) -> dict:
    """Predict the next grade (0..100) for a subject using linear regression."""
    # Verify the subject exists and belongs to this user.
    subject = get_subject(user_id, subject_id)
    if not subject:
        return {"ok": False, "error": "Subject not found"}

    # Load this subject's study sessions, sorted oldest → newest.
    records = get_records(user_id, subject_id=subject_id, limit=10000)
    records.sort(key=lambda r: _parse_dt(r["started_at"]))
    # Load this subject's grades, sorted oldest → newest.
    grades = get_grades(user_id, subject_id=subject_id)
    grades.sort(key=lambda g: _parse_dt(g["created_at"]))

    # Fallback 0: no grades → we cannot predict anything yet.
    if not grades:
        return {
            "ok": True,
            "subject": subject["name"],
            "predicted_grade": None,
            "method": "no grades yet — add at least one grade to enable prediction",
            "samples": 0,
        }

    # Build the training data:
    #   X[i] = cumulative study minutes that existed when grade[i] was recorded
    #   y[i] = the grade value itself
    X, y = [], []
    for g in grades:
        moment = _parse_dt(g["created_at"])                 # when the grade was entered
        cum_min = _cumulative_minutes_at(records, moment)   # study time up to that point
        X.append([cum_min])                                 # one feature per sample
        y.append(g["grade"])                                # the target value

    # The student's CURRENT total study time on this subject (the value we
    # feed into the trained model to get the next-grade prediction).
    current_total_min = sum(r["duration_sec"] for r in records) / 60.0

    # Fallback 1: only one grade → regression needs at least two points.
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

    # Convert the Python lists to numpy arrays (what sklearn expects).
    X_arr = np.array(X, dtype=float)        # shape (n_samples, 1)
    y_arr = np.array(y, dtype=float)        # shape (n_samples,)

    # Fallback 2: if every grade was recorded at the same study time, the X
    # values have no variance and a line cannot be fitted → use the average.
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

    # The real ML step: fit a straight line grade = slope*minutes + intercept.
    model = LinearRegression().fit(X_arr, y_arr)
    # Predict the grade for the student's current total study time.
    pred = float(model.predict(np.array([[current_total_min]], dtype=float))[0])
    # Clamp into the valid 0..100 range (regression can extrapolate outside it).
    pred = max(0.0, min(pred, 100.0))

    return {
        "ok": True,
        "subject": subject["name"],
        "predicted_grade": round(pred, 1),
        "method": "linear regression (study time → grade)",
        "samples": len(grades),
        "current_study_minutes": round(current_total_min, 1),
        # How many points the grade rises per extra hour of study (explainable!).
        "slope_per_hour": round(float(model.coef_[0]) * 60.0, 2),
    }


# ---------- Recommendations ----------

def _subject_stats(user_id: int) -> list[dict]:
    """Aggregate per-subject totals: study minutes, session count, average grade."""
    # Load everything once, then group in memory (fewer DB round-trips).
    subjects = get_subjects(user_id)
    records  = get_records(user_id, limit=10000)
    grades   = get_grades(user_id)

    # Group records and grades by subject id.
    by_subj_records: dict[int, list[dict]] = defaultdict(list)
    by_subj_grades:  dict[int, list[float]] = defaultdict(list)
    for r in records:
        by_subj_records[r["subject_id"]].append(r)
    for g in grades:
        by_subj_grades[g["subject_id"]].append(float(g["grade"]))

    out = []
    for s in subjects:                                    # one stats row per subject
        recs = by_subj_records.get(s["id"], [])           # this subject's sessions
        gs   = by_subj_grades.get(s["id"], [])            # this subject's grades
        total_min = sum(r["duration_sec"] for r in recs) / 60.0   # total study minutes
        avg_grade = float(mean(gs)) if gs else None       # average grade (or None)
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
    # Load the user (needed to read the analysis-period gate).
    user = get_user(user_id)
    if not user:
        return {"ok": False, "error": "User not found"}

    status = analysis_status(user)        # period info (configured/finished/...)
    stats = _subject_stats(user_id)       # per-subject aggregates (also used by charts)

    # Gate 1: the user never chose a period → ask them to.
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

    # Gate 2: the period is still running → recommendations stay locked.
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

    # Period finished (or immediate mode) → run the rule set and build advice.
    recommendations: list[dict] = []

    # Split subjects into "studied" and "not studied at all".
    studied_stats = [s for s in stats if s["sessions"] > 0]
    not_studied   = [s for s in stats if s["sessions"] == 0]

    # Rule 1) Warn about every subject that got no study time at all.
    for s in not_studied:
        recommendations.append({
            "type": "warning",
            "message": (
                f"You did not study '{s['subject_name']}' at all during the "
                f"analysis period. Schedule at least one session this week."
            ),
        })

    if studied_stats:
        # Rule 2) Flag a big imbalance between the least- and most-studied subjects.
        if len(studied_stats) >= 2:
            min_s = min(studied_stats, key=lambda x: x["total_minutes"])   # least time
            max_s = max(studied_stats, key=lambda x: x["total_minutes"])   # most time
            if min_s["total_minutes"] < max_s["total_minutes"] * 0.5:      # under half
                recommendations.append({
                    "type": "focus",
                    "message": (
                        f"You spent only {min_s['total_minutes']:.0f} min on "
                        f"'{min_s['subject_name']}' vs {max_s['total_minutes']:.0f} min on "
                        f"'{max_s['subject_name']}'. Consider rebalancing toward "
                        f"'{min_s['subject_name']}'."
                    ),
                })

        # Rule 3) Warn about subjects whose average grade is below 60.
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

        # Rule 4) Detect a mismatch between time spent and the grade achieved.
        for s in studied_stats:
            if s["avg_grade"] is None:               # skip subjects with no grades
                continue
            # 4a) Lots of time but a low grade → the study method may be the issue.
            if s["total_minutes"] >= 180 and s["avg_grade"] < 70:
                recommendations.append({
                    "type": "tip",
                    "message": (
                        f"You spent a lot of time ({s['total_minutes']:.0f} min) on "
                        f"'{s['subject_name']}' but the grade is {s['avg_grade']:.0f}. "
                        f"The study method may need revision (practice tests, summaries, peers)."
                    ),
                })
            # 4b) Little time but a good grade → the current approach works well.
            if s["total_minutes"] <= 30 and s["avg_grade"] >= 80:
                recommendations.append({
                    "type": "success",
                    "message": (
                        f"'{s['subject_name']}': great result ({s['avg_grade']:.0f}) "
                        f"with only {s['total_minutes']:.0f} min of tracked study. "
                        f"Keep the current approach."
                    ),
                })

    # If no rule fired, give an encouraging "everything looks balanced" message.
    if not recommendations:
        recommendations.append({
            "type": "success",
            "message": "Your study habits during this period look balanced. Keep it up.",
        })

    # Return advice + the stats (the front end reuses stats for the charts).
    return {
        "ok": True,
        "period": status,
        "subject_stats": stats,
        "recommendations": recommendations,
    }
