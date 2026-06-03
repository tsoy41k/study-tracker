"""
Flask backend for the Study Tracker web application.

Page routes:
  GET  /                       -> redirect to dashboard or login
  GET/POST /register           -> create account (numeric ID + password).
                                  6 default subjects are auto-created.
  GET/POST /login              -> authenticate
  GET/POST /setup              -> first-time setup: choose analysis period (days)
  GET  /logout                 -> clear session
  GET  /dashboard              -> main page

JSON API:
  GET    /api/subjects
  POST   /api/subjects                 {name}
  DELETE /api/subjects/<id>

  POST   /api/records                  {subject_id, started_at, ended_at, duration_sec}
  GET    /api/records[?subject_id=]

  POST   /api/grades                   {subject_id, grade, note?}
  GET    /api/grades[?subject_id=]

  GET    /api/predict/<subject_id>     -> predicted grade
  GET    /api/recommendations          -> recommendations + per-subject stats
                                           (gated by analysis period)
  GET    /api/period                   -> analysis period status
"""

import os
from datetime import datetime
from functools import wraps

from flask import (
    Flask, jsonify, redirect, render_template,
    request, session, url_for, flash,
)
from werkzeug.security import check_password_hash, generate_password_hash

import database as db
from ml_model import generate_recommendations, predict_grade


app = Flask(__name__)
# In production set SECRET_KEY env var. The fallback is fine for local dev only.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")

# Make sure the DB schema exists on startup (matters for gunicorn workers).
db.init_db()


# ---------- Auth helpers ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def setup_required(view):
    """Block dashboard until the analysis period is configured."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = db.get_user(int(session["user_id"]))
        if user is None:
            # Stale session referring to a deleted/missing user.
            session.clear()
            return redirect(url_for("login"))
        if user.get("analysis_period_days") is None:
            return redirect(url_for("setup"))
        return view(*args, **kwargs)
    return wrapped


def current_user_id() -> int:
    return int(session["user_id"])


# ---------- Page routes ----------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        user_id_raw = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not user_id_raw.isdigit():
            flash("Student ID must contain only digits.", "error")
            return render_template("register.html")
        if len(password) < 4:
            flash("Password must be at least 4 characters.", "error")
            return render_template("register.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        user_id = int(user_id_raw)
        if db.get_user(user_id):
            flash("This Student ID is already registered. Please log in.", "error")
            return render_template("register.html")

        db.create_user(user_id, generate_password_hash(password))
        session["user_id"] = user_id
        flash("Account created. Now choose your analysis period.", "success")
        return redirect(url_for("setup"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user_id_raw = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")

        if not user_id_raw.isdigit():
            flash("Student ID must contain only digits.", "error")
            return render_template("login.html")

        user = db.get_user(int(user_id_raw))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid Student ID or password.", "error")
            return render_template("login.html")

        session["user_id"] = int(user_id_raw)
        if user.get("analysis_period_days") is None:
            return redirect(url_for("setup"))
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
@login_required
def setup():
    user = db.get_user(current_user_id())
    if user is None:
        session.clear()
        return redirect(url_for("login"))
    if user.get("analysis_period_days") is not None:
        # Already configured — go to dashboard
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        days_raw = request.form.get("days", "").strip()
        if not days_raw.isdigit():
            flash("Please choose a valid number of days.", "error")
            return render_template("setup.html")
        days = int(days_raw)
        if not (0 <= days <= 365):
            flash("Analysis period must be between 0 and 365 days.", "error")
            return render_template("setup.html")

        db.set_analysis_period(current_user_id(), days)
        if days == 0:
            flash(
                "Immediate mode enabled. Recommendations are available right away.",
                "success",
            )
        else:
            flash(
                f"Analysis period set to {days} day(s). "
                f"Recommendations will appear after the period ends.",
                "success",
            )
        return redirect(url_for("dashboard"))

    return render_template("setup.html")


@app.route("/dashboard")
@login_required
@setup_required
def dashboard():
    return render_template("dashboard.html", user_id=current_user_id())


# ---------- API: subjects ----------

@app.get("/api/subjects")
@login_required
def api_list_subjects():
    return jsonify({"ok": True, "subjects": db.get_subjects(current_user_id())})


@app.post("/api/subjects")
@login_required
def api_add_subject():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Subject name is required"}), 400
    if len(name) > 80:
        return jsonify({"ok": False, "error": "Subject name is too long"}), 400
    try:
        sid = db.add_subject(current_user_id(), name)
    except Exception:
        return jsonify({"ok": False, "error": "Subject already exists"}), 400
    return jsonify({"ok": True, "id": sid, "name": name})


@app.delete("/api/subjects/<int:subject_id>")
@login_required
def api_delete_subject(subject_id: int):
    ok = db.delete_subject(current_user_id(), subject_id)
    if not ok:
        return jsonify({"ok": False, "error": "Subject not found"}), 404
    return jsonify({"ok": True})


# ---------- API: records ----------

@app.post("/api/records")
@login_required
def api_add_record():
    data = request.get_json(silent=True) or {}
    try:
        subject_id = int(data["subject_id"])
        started_at = str(data["started_at"])
        ended_at = str(data["ended_at"])
        duration_sec = int(data["duration_sec"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    if duration_sec <= 0:
        return jsonify({"ok": False, "error": "Duration must be positive"}), 400

    if not db.get_subject(current_user_id(), subject_id):
        return jsonify({"ok": False, "error": "Subject not found"}), 404

    try:
        started_at = datetime.fromisoformat(started_at).isoformat(sep=" ", timespec="seconds")
        ended_at   = datetime.fromisoformat(ended_at).isoformat(sep=" ", timespec="seconds")
    except ValueError:
        return jsonify({"ok": False, "error": "Bad datetime format"}), 400

    rid = db.add_record(current_user_id(), subject_id, started_at, ended_at, duration_sec)
    return jsonify({"ok": True, "id": rid})


@app.get("/api/records")
@login_required
def api_list_records():
    subject_id = request.args.get("subject_id", type=int)
    records = db.get_records(current_user_id(), subject_id=subject_id)
    return jsonify({"ok": True, "records": records})


# ---------- API: grades ----------

@app.post("/api/grades")
@login_required
def api_add_grade():
    data = request.get_json(silent=True) or {}
    try:
        subject_id = int(data["subject_id"])
        grade = float(data["grade"])
        note = str(data.get("note", "")).strip()
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    if not (0.0 <= grade <= 100.0):
        return jsonify({"ok": False, "error": "Grade must be between 0 and 100"}), 400
    if not db.get_subject(current_user_id(), subject_id):
        return jsonify({"ok": False, "error": "Subject not found"}), 404
    if len(note) > 120:
        return jsonify({"ok": False, "error": "Note is too long"}), 400

    gid = db.add_grade(current_user_id(), subject_id, grade, note)
    return jsonify({"ok": True, "id": gid})


@app.get("/api/grades")
@login_required
def api_list_grades():
    subject_id = request.args.get("subject_id", type=int)
    return jsonify({"ok": True, "grades": db.get_grades(current_user_id(), subject_id)})


# ---------- API: ML & period ----------

@app.get("/api/predict/<int:subject_id>")
@login_required
def api_predict(subject_id: int):
    return jsonify(predict_grade(current_user_id(), subject_id))


@app.get("/api/recommendations")
@login_required
def api_recommendations():
    return jsonify(generate_recommendations(current_user_id()))


@app.get("/api/period")
@login_required
def api_period():
    user = db.get_user(current_user_id())
    if user is None:
        session.clear()
        return jsonify({"ok": False, "error": "auth required"}), 401
    return jsonify({"ok": True, "period": db.analysis_status(user)})


# ---------- Entry point ----------

if __name__ == "__main__":
    db.init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
