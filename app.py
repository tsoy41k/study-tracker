"""
Flask backend for the Study Tracker web application.

This file wires together everything the browser talks to:
  * HTML page routes (login, register, setup, dashboard).
  * A small JSON API used by the dashboard's JavaScript (subjects, study
    records, ML patterns/forecast, recommendations, analysis period).

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

  GET    /api/patterns                 -> KMeans study-session patterns
  GET    /api/forecast                 -> linear-regression weekly time forecast
  GET    /api/recommendations          -> recommendations + per-subject stats
                                           (gated by analysis period)
  GET    /api/period                   -> analysis period status
"""

import os                                  # read environment variables (SECRET_KEY)
from datetime import datetime, timedelta   # parse/normalise ISO datetimes; streak math
from functools import wraps                # preserve function metadata in decorators

# Flask components we use throughout the app.
from flask import (
    Flask,            # the application object
    jsonify,          # turn a dict into a JSON HTTP response
    redirect,         # send an HTTP redirect
    render_template,  # render a Jinja2 HTML template
    request,          # access the incoming request (form data, JSON, query args)
    session,          # signed cookie storage for the logged-in user id
    url_for,          # build URLs from view function names
    flash,            # one-time messages shown on the next page
)
# Password helpers: hash on registration, verify on login (never store plaintext).
from werkzeug.security import check_password_hash, generate_password_hash

import database as db                                   # our SQLite data-access layer
from ml_model import (                                    # the ML / advice functions
    analyze_patterns,         # KMeans clustering of study sessions
    forecast_study_time,      # linear-regression weekly forecast
    generate_recommendations, # rule-based time recommendations
)


# Create the Flask application instance.
app = Flask(__name__)
# Secret key signs the session cookie. In production it is supplied via the
# SECRET_KEY environment variable (set on Render); the fallback is dev-only.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")

# Ensure all database tables exist as soon as the module is imported. This is
# important under gunicorn, where each worker imports this module on startup.
db.init_db()


# ---------- Auth helpers ----------

def login_required(view):
    """Decorator: only allow the wrapped view if a user is logged in."""
    @wraps(view)                                  # keep the original function's name/docstring
    def wrapped(*args, **kwargs):
        if "user_id" not in session:             # no logged-in user in the session
            if request.path.startswith("/api/"): # API calls get a JSON 401...
                return jsonify({"ok": False, "error": "auth required"}), 401
            return redirect(url_for("login"))    # ...page requests are redirected to login
        return view(*args, **kwargs)             # authorised → run the real view
    return wrapped


def setup_required(view):
    """Decorator: block the dashboard until the analysis period is configured."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = db.get_user(int(session["user_id"]))   # load the current user row
        if user is None:
            # The session points to a user that no longer exists (e.g. DB reset).
            session.clear()                            # drop the stale session
            return redirect(url_for("login"))
        if user.get("analysis_period_days") is None:   # period not chosen yet
            return redirect(url_for("setup"))          # force the setup screen first
        return view(*args, **kwargs)                    # all good → show the dashboard
    return wrapped


def current_user_id() -> int:
    """Return the logged-in user's numeric id from the session."""
    return int(session["user_id"])


# ---------- Page routes ----------

@app.route("/")
def index():
    """Root URL: send logged-in users to the dashboard, others to login."""
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Create a new account from a numeric Student ID + password."""
    if request.method == "POST":                          # the form was submitted
        user_id_raw = request.form.get("user_id", "").strip()  # raw ID text
        password = request.form.get("password", "")            # chosen password
        confirm = request.form.get("confirm", "")              # password confirmation

        # Validation 1: the Student ID must be digits only.
        if not user_id_raw.isdigit():
            flash("Student ID must contain only digits.", "error")
            return render_template("register.html")
        # Validation 2: minimum password length.
        if len(password) < 4:
            flash("Password must be at least 4 characters.", "error")
            return render_template("register.html")
        # Validation 3: both password fields must match.
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        user_id = int(user_id_raw)                        # safe: validated as digits
        # Validation 4: the ID must not already be registered.
        if db.get_user(user_id):
            flash("This Student ID is already registered. Please log in.", "error")
            return render_template("register.html")

        # Create the user (also auto-creates the 6 default subjects) and log in.
        db.create_user(user_id, generate_password_hash(password))
        session["user_id"] = user_id                      # mark the user as logged in
        flash("Account created. Now choose your analysis period.", "success")
        return redirect(url_for("setup"))                 # go to first-time setup

    # GET request: just show the empty registration form.
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate an existing user by Student ID + password."""
    if request.method == "POST":                          # the login form was submitted
        user_id_raw = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")

        # The ID must be numeric; if not, reject immediately.
        if not user_id_raw.isdigit():
            flash("Student ID must contain only digits.", "error")
            return render_template("login.html")

        user = db.get_user(int(user_id_raw))              # look up the user row
        # Reject if no such user OR the password hash does not match.
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid Student ID or password.", "error")
            return render_template("login.html")

        session["user_id"] = int(user_id_raw)             # log the user in
        # If they never set an analysis period, send them to setup first.
        if user.get("analysis_period_days") is None:
            return redirect(url_for("setup"))
        return redirect(url_for("dashboard"))             # otherwise straight to dashboard

    # GET request: show the empty login form.
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    """Log out by clearing the session, then go back to the login page."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
@login_required
def setup():
    """First-time setup: the user picks how long the analysis period lasts."""
    user = db.get_user(current_user_id())                 # load current user
    if user is None:
        session.clear()                                   # stale session → log out
        return redirect(url_for("login"))
    if user.get("analysis_period_days") is not None:
        # Already configured before → no need to set it again.
        return redirect(url_for("dashboard"))

    if request.method == "POST":                          # the setup form was submitted
        days_raw = request.form.get("days", "").strip()
        if not days_raw.isdigit():                        # must be a number
            flash("Please choose a valid number of days.", "error")
            return render_template("setup.html")
        days = int(days_raw)
        # 0 = immediate mode; up to 365 days for a normal analysis window.
        if not (0 <= days <= 365):
            flash("Analysis period must be between 0 and 365 days.", "error")
            return render_template("setup.html")

        db.set_analysis_period(current_user_id(), days)   # store the choice + start time
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

    # GET request: show the period-selection form.
    return render_template("setup.html")


@app.route("/dashboard")
@login_required      # must be logged in...
@setup_required      # ...and must have configured the analysis period
def dashboard():
    """Render the main dashboard page (all data is loaded via the JSON API)."""
    return render_template("dashboard.html", user_id=current_user_id(),
                           active_page="dashboard")


@app.route("/statistics")
@login_required
@setup_required
def statistics():
    """Render the statistics page (data loaded via /api/statistics)."""
    return render_template("statistics.html", active_page="statistics")


@app.route("/history")
@login_required
@setup_required
def history():
    """Render the full study-history page (data loaded via /api/history)."""
    return render_template("history.html", active_page="history")


# ---------- API: subjects ----------

@app.get("/api/subjects")
@login_required
def api_list_subjects():
    """Return the current user's subjects as JSON."""
    return jsonify({"ok": True, "subjects": db.get_subjects(current_user_id())})


@app.post("/api/subjects")
@login_required
def api_add_subject():
    """Add a new subject for the current user."""
    data = request.get_json(silent=True) or {}            # parse JSON body (or {})
    name = (data.get("name") or "").strip()               # subject name, trimmed
    if not name:                                          # name is required
        return jsonify({"ok": False, "error": "Subject name is required"}), 400
    if len(name) > 80:                                    # keep names reasonable
        return jsonify({"ok": False, "error": "Subject name is too long"}), 400
    try:
        sid = db.add_subject(current_user_id(), name)     # insert into DB
    except Exception:
        # The (user_id, name) UNIQUE constraint failed → duplicate subject.
        return jsonify({"ok": False, "error": "Subject already exists"}), 400
    return jsonify({"ok": True, "id": sid, "name": name})


@app.delete("/api/subjects/<int:subject_id>")
@login_required
def api_delete_subject(subject_id: int):
    """Delete a subject (and, via cascade, its study records)."""
    ok = db.delete_subject(current_user_id(), subject_id)
    if not ok:                                            # nothing was deleted
        return jsonify({"ok": False, "error": "Subject not found"}), 404
    return jsonify({"ok": True})


# ---------- API: records ----------

@app.post("/api/records")
@login_required
def api_add_record():
    """Store one completed study session (the result of the timer)."""
    data = request.get_json(silent=True) or {}
    try:
        # Pull and type-cast the required fields; missing/invalid → except.
        subject_id = int(data["subject_id"])
        started_at = str(data["started_at"])
        ended_at = str(data["ended_at"])
        duration_sec = int(data["duration_sec"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    if duration_sec <= 0:                                 # a session must have a length
        return jsonify({"ok": False, "error": "Duration must be positive"}), 400

    # Make sure the subject exists and belongs to this user.
    if not db.get_subject(current_user_id(), subject_id):
        return jsonify({"ok": False, "error": "Subject not found"}), 404

    try:
        # Normalise both timestamps to "YYYY-MM-DD HH:MM:SS" for storage.
        started_at = datetime.fromisoformat(started_at).isoformat(sep=" ", timespec="seconds")
        ended_at   = datetime.fromisoformat(ended_at).isoformat(sep=" ", timespec="seconds")
    except ValueError:
        return jsonify({"ok": False, "error": "Bad datetime format"}), 400

    # Insert and return the new record id.
    rid = db.add_record(current_user_id(), subject_id, started_at, ended_at, duration_sec)
    return jsonify({"ok": True, "id": rid})


@app.get("/api/records")
@login_required
def api_list_records():
    """List study records, optionally filtered to a single subject."""
    subject_id = request.args.get("subject_id", type=int)        # optional ?subject_id=
    records = db.get_records(current_user_id(), subject_id=subject_id)
    return jsonify({"ok": True, "records": records})


# ---------- API: ML & period ----------

@app.get("/api/patterns")
@login_required
def api_patterns():
    """Return KMeans study-session patterns (productive time, clusters)."""
    return jsonify(analyze_patterns(current_user_id()))


@app.get("/api/forecast")
@login_required
def api_forecast():
    """Return the linear-regression weekly study-time forecast."""
    return jsonify(forecast_study_time(current_user_id()))


@app.get("/api/recommendations")
@login_required
def api_recommendations():
    """Return recommendations + per-subject statistics (gated by the period)."""
    return jsonify(generate_recommendations(current_user_id()))


@app.get("/api/period")
@login_required
def api_period():
    """Return the analysis-period status used by the dashboard banner."""
    user = db.get_user(current_user_id())
    if user is None:                                     # stale session safety check
        session.clear()
        return jsonify({"ok": False, "error": "auth required"}), 401
    return jsonify({"ok": True, "period": db.analysis_status(user)})


# ---------- API: statistics & history ----------

def _parse_record_dt(value: str) -> datetime:
    """Parse a stored timestamp (tolerates both 'T' and space separators)."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


@app.get("/api/statistics")
@login_required
def api_statistics():
    """
    Aggregate overall study statistics for the Statistics page:
      total time, session count, averages, top subject, current streak,
      per-subject breakdown and a daily time series for the chart.
    """
    records = db.get_records(current_user_id(), limit=100000)

    # Empty state: nothing studied yet.
    if not records:
        return jsonify({
            "ok": True,
            "has_data": False,
            "total_minutes": 0,
            "total_sessions": 0,
            "avg_session_minutes": 0,
            "active_days": 0,
            "avg_per_active_day": 0,
            "current_streak": 0,
            "top_subject": None,
            "per_subject": [],
            "daily": [],
        })

    total_sec = sum(r["duration_sec"] for r in records)         # all time studied
    total_min = total_sec / 60.0
    total_sessions = len(records)

    # Group minutes by calendar day and by subject.
    by_day = {}        # "YYYY-MM-DD" -> minutes
    by_subject = {}    # subject name -> minutes
    for r in records:
        day = _parse_record_dt(r["started_at"]).date().isoformat()
        by_day[day] = by_day.get(day, 0) + r["duration_sec"] / 60.0
        name = r["subject_name"]
        by_subject[name] = by_subject.get(name, 0) + r["duration_sec"] / 60.0

    active_days = len(by_day)                                   # distinct study days
    avg_session = total_min / total_sessions
    avg_per_day = total_min / active_days if active_days else 0

    # Top subject by total time.
    top_name = max(by_subject, key=by_subject.get)
    top_subject = {"name": top_name, "minutes": round(by_subject[top_name], 1)}

    # Current streak: consecutive days (ending today or yesterday) with study.
    studied_days = set(by_day.keys())
    streak = 0
    cursor = datetime.now().date()
    if cursor.isoformat() not in studied_days:
        cursor = cursor - timedelta(days=1)                    # allow "ends yesterday"
    while cursor.isoformat() in studied_days:
        streak += 1
        cursor = cursor - timedelta(days=1)

    # Per-subject breakdown (sorted by time, descending).
    per_subject = [
        {"name": n, "minutes": round(m, 1)}
        for n, m in sorted(by_subject.items(), key=lambda kv: kv[1], reverse=True)
    ]

    # Daily series (sorted by date) for the line chart.
    daily = [
        {"date": d, "minutes": round(by_day[d], 1)}
        for d in sorted(by_day.keys())
    ]

    return jsonify({
        "ok": True,
        "has_data": True,
        "total_minutes": round(total_min, 1),
        "total_sessions": total_sessions,
        "avg_session_minutes": round(avg_session, 1),
        "active_days": active_days,
        "avg_per_active_day": round(avg_per_day, 1),
        "current_streak": streak,
        "top_subject": top_subject,
        "per_subject": per_subject,
        "daily": daily,
    })


@app.get("/api/history")
@login_required
def api_history():
    """
    Return study records for the History page, with optional filters:
      ?subject_id=  -> only that subject
      ?from=YYYY-MM-DD, ?to=YYYY-MM-DD -> only sessions whose start date is in range
    Also returns a summary (count + total minutes) for the filtered set.
    """
    subject_id = request.args.get("subject_id", type=int)        # optional subject filter
    date_from = request.args.get("from", "").strip()             # optional start date
    date_to = request.args.get("to", "").strip()                 # optional end date

    # Start from all records (optionally narrowed to one subject by the DB).
    records = db.get_records(current_user_id(), subject_id=subject_id, limit=100000)

    # Apply the date filters in Python (start date inclusive on both ends).
    def in_range(rec):
        d = _parse_record_dt(rec["started_at"]).date()
        if date_from:
            try:
                if d < datetime.fromisoformat(date_from).date():
                    return False
            except ValueError:
                pass
        if date_to:
            try:
                if d > datetime.fromisoformat(date_to).date():
                    return False
            except ValueError:
                pass
        return True

    filtered = [r for r in records if in_range(r)]

    total_min = sum(r["duration_sec"] for r in filtered) / 60.0
    return jsonify({
        "ok": True,
        "records": filtered,
        "count": len(filtered),
        "total_minutes": round(total_min, 1),
    })


# ---------- Entry point ----------

if __name__ == "__main__":
    # Only runs for local development (`python app.py`). On Render the app is
    # started by gunicorn instead, which imports `app` without running this block.
    db.init_db()                                         # make sure tables exist
    app.run(host="127.0.0.1", port=5000, debug=True)    # dev server with auto-reload
