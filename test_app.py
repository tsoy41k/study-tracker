"""End-to-end smoke test for the Flask app using the test client."""
import os
import tempfile
import importlib

import database as db

# Use a fresh temp DB
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
db.DB_PATH = tmp.name
db.init_db()

import app as app_module
importlib.reload(app_module)
from app import app


def must(resp, msg=""):
    assert resp.status_code == 200, f"{msg}: status {resp.status_code} body {resp.data!r}"


with app.test_client() as c:
    # ----- Register -----
    r = c.post("/register", data={
        "user_id": "12345", "password": "pass", "confirm": "pass"
    }, follow_redirects=False)
    assert r.status_code == 302 and "/setup" in r.headers["Location"], \
        f"register should redirect to /setup, got {r.status_code} {r.headers.get('Location')}"
    print("OK: register redirects to /setup")

    # ----- Dashboard should also redirect to /setup until period is configured -----
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/setup" in r.headers["Location"]
    print("OK: /dashboard blocked until setup is done")

    # ----- 6 default subjects auto-created -----
    r = c.get("/api/subjects")
    subs = r.get_json()["subjects"]
    names = [s["name"] for s in subs]
    expected = {
        "Internet of Things", "Digital Marketing",
        "System analysis and design",
        "Distributed systems and cloud computing",
        "Technology law", "Business information systems project",
    }
    assert expected.issubset(set(names)), f"missing defaults: {expected - set(names)}"
    print(f"OK: 6 default subjects auto-created ({len(subs)} total)")

    # ----- Recommendations before setup: just the "set period" info -----
    r = c.get("/api/recommendations")
    rec = r.get_json()
    assert rec["ok"]
    assert not rec["period"]["configured"]
    assert any("analysis period" in m["message"].lower() for m in rec["recommendations"])
    print("OK: no real recommendations before setup")

    # ----- Configure analysis period -----
    r = c.post("/setup", data={"days": "14"}, follow_redirects=False)
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]
    print("OK: analysis period saved")

    # ----- Recommendations during active period: still locked -----
    r = c.get("/api/recommendations")
    rec = r.get_json()
    assert rec["period"]["configured"] and not rec["period"]["finished"]
    assert len(rec["recommendations"]) == 1
    assert "will be generated" in rec["recommendations"][0]["message"].lower()
    print("OK: recommendations locked while period is active")

    # ----- Pick first default subject and add a session -----
    sid = subs[0]["id"]
    r = c.post("/api/records", json={
        "subject_id": sid,
        "started_at": "2025-05-27T10:00:00",
        "ended_at":   "2025-05-27T10:30:00",
        "duration_sec": 1800,
    })
    must(r, "add record")
    assert r.get_json()["ok"]
    print("OK: added study record")

    # ----- Patterns (KMeans) with too little data -> enough_data False -----
    r = c.get("/api/patterns")
    pat = r.get_json()
    assert pat["ok"] and pat["enough_data"] is False
    print("OK: patterns report 'not enough data' with 1 session")

    # ----- Add enough sessions across different hours/days for KMeans -----
    samples = [
        ("2025-05-01T09:00:00", "2025-05-01T09:40:00", 2400),
        ("2025-05-02T10:00:00", "2025-05-02T10:30:00", 1800),
        ("2025-05-05T19:00:00", "2025-05-05T20:30:00", 5400),
        ("2025-05-06T20:00:00", "2025-05-06T21:30:00", 5400),
        ("2025-05-08T14:00:00", "2025-05-08T14:20:00", 1200),
        ("2025-05-09T21:00:00", "2025-05-09T22:00:00", 3600),
    ]
    for st, en, dur in samples:
        c.post("/api/records", json={
            "subject_id": sid, "started_at": st, "ended_at": en, "duration_sec": dur,
        })

    r = c.get("/api/patterns")
    pat = r.get_json()
    assert pat["ok"] and pat["enough_data"] is True
    assert pat["best_time"] is not None
    assert len(pat["clusters"]) >= 1
    print(f"OK: KMeans patterns ({len(pat['clusters'])} clusters, "
          f"best time = {pat['best_time']['time_of_day']})")

    # ----- Forecast (linear regression) over multiple weeks -----
    r = c.get("/api/forecast")
    fc = r.get_json()
    assert fc["ok"] and fc["enough_data"] is True
    assert fc["next_week_forecast"] is not None
    assert fc["trend"] in ("increasing", "decreasing", "stable")
    print(f"OK: forecast trend = {fc['trend']}, "
          f"next week = {fc['next_week_forecast']} min")

    # ----- Login flow -----
    c.get("/logout")
    r = c.post("/login", data={"user_id": "12345", "password": "wrong"})
    assert b"Invalid" in r.data
    print("OK: bad login rejected")

    r = c.post("/login", data={"user_id": "12345", "password": "pass"},
               follow_redirects=False)
    # Already configured analysis period → straight to dashboard
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]
    print("OK: login goes straight to dashboard (period already set)")

# ----- Immediate-mode flow (separate user) -----
with app.test_client() as c:
    c.post("/register", data={
        "user_id": "99999", "password": "pass", "confirm": "pass"
    })
    r = c.post("/setup", data={"days": "0"}, follow_redirects=False)
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]
    print("OK: immediate mode (days=0) accepted")

    r = c.get("/api/period")
    period = r.get_json()["period"]
    assert period["configured"] and period["immediate"] and period["finished"]
    print("OK: period reports immediate + finished")

    # Recommendations should NOT be locked
    r = c.get("/api/recommendations")
    rec = r.get_json()
    assert rec["ok"]
    # No "will be generated in N days" lock message
    locked = any("will be generated" in m["message"].lower() for m in rec["recommendations"])
    assert not locked, f"recommendations unexpectedly locked: {rec['recommendations']}"
    print(f"OK: real recommendations available immediately ({len(rec['recommendations'])} items)")

os.unlink(tmp.name)
print("\nAll end-to-end tests passed.")
