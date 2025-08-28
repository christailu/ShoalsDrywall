from flask import Flask, render_template, request, redirect, session, url_for, send_file
import sqlite3
import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
import io

app = Flask(__name__)
app.secret_key = "shoals_drywall"

# Fixed employees with hourly rates and passwords
EMPLOYEES = {
    "Alexander Merida": {"rate": 22, "password": "alex123"},
    "Kevin": {"rate": 18, "password": "kevin123"},
    "Eddy": {"rate": 18, "password": "eddy123"},
    "Bocho": {"rate": 18, "password": "bocho123"},
    "Marvin": {"rate": 18, "password": "marvin123"},
    "Zacarias": {"rate": 18, "password": "zacarias123"},
    "Daniella Del Valle": {"rate": 17, "password": "daniella123"},
}

# --- Database setup ---
def init_db():
    conn = sqlite3.connect("payroll.db")
    c = conn.cursor()
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY,
                  username TEXT UNIQUE,
                  password TEXT,
                  role TEXT DEFAULT "employee")''')
    # Work sessions table
    c.execute('''CREATE TABLE IF NOT EXISTS work_sessions
                 (id INTEGER PRIMARY KEY,
                  user_id INTEGER,
                  clock_in TEXT,
                  clock_out TEXT,
                  hours REAL,
                  wage REAL)''')
    conn.commit()
    conn.close()

# --- Seed admin and employees ---
def seed_users():
    conn = sqlite3.connect("payroll.db")
    c = conn.cursor()
    # Admin
    c.execute("SELECT id FROM users WHERE username=?", ("admin",))
    if not c.fetchone():
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                  ("admin", generate_password_hash("admin123"), "admin"))
    # Employees
    for username, info in EMPLOYEES.items():
        c.execute("SELECT id FROM users WHERE username=?", (username,))
        if not c.fetchone():
            c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                      (username, generate_password_hash(info["password"]), "employee"))
    conn.commit()
    conn.close()

# --- Format session ---
def format_session(clock_in, clock_out, hours, wage):
    TAX_FLAT = 20
    in_dt = datetime.datetime.fromisoformat(clock_in)
    clock_in_str = in_dt.strftime("%H:%M on %m/%d/%Y")
    day = in_dt.strftime("%A")
    if clock_out:
        out_dt = datetime.datetime.fromisoformat(clock_out)
        clock_out_str = out_dt.strftime("%H:%M on %m/%d/%Y")
    else:
        clock_out_str = "-"
    hours = round(hours, 2) if hours else 0
    gross_pay = round(wage, 2) if wage else 0
    net_pay = round(max(gross_pay - TAX_FLAT, 0), 2)
    return {
        "day": day,
        "clock_in": clock_in_str,
        "clock_out": clock_out_str,
        "hours": hours,
        "gross_pay": gross_pay,
        "tax": TAX_FLAT if gross_pay else 0,
        "net_pay": net_pay
    }

# --- Login ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        conn = sqlite3.connect("payroll.db")
        c = conn.cursor()
        c.execute("SELECT id, password, role FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user[1], password):
            session["user_id"] = user[0]
            session["username"] = username
            session["role"] = user[2]
            return redirect("/")
        else:
            return render_template("invalid.html")
    return render_template("login.html")

# --- Home ---
@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    role = session["role"]

    if role == "admin":
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        action = request.form["action"]
        conn = sqlite3.connect("payroll.db")
        c = conn.cursor()
        if action == "Clock In":
            c.execute("INSERT INTO work_sessions (user_id, clock_in) VALUES (?, ?)",
                      (user_id, datetime.datetime.now().isoformat()))
        elif action == "Clock Out":
            c.execute("SELECT id, clock_in FROM work_sessions WHERE user_id=? AND clock_out IS NULL",
                      (user_id,))
            row = c.fetchone()
            if row:
                start_time = datetime.datetime.fromisoformat(row[1])
                end_time = datetime.datetime.now()
                hours = (end_time - start_time).total_seconds() / 3600
                rate = EMPLOYEES.get(session["username"], {}).get("rate", 15)
                wage = hours * rate
                c.execute("UPDATE work_sessions SET clock_out=?, hours=?, wage=? WHERE id=?",
                          (end_time.isoformat(), hours, wage, row[0]))
        conn.commit()
        conn.close()
        return redirect("/")

    conn = sqlite3.connect("payroll.db")
    c = conn.cursor()
    c.execute("SELECT clock_in, clock_out, hours, wage FROM work_sessions WHERE user_id=?", (user_id,))
    raw_sessions = c.fetchall()
    conn.close()
    sessions = [format_session(*s) for s in raw_sessions]
    return render_template("index.html", sessions=sessions, username=session["username"])

# --- Admin dashboard ---
@app.route("/admin")
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403
    conn = sqlite3.connect("payroll.db")
    c = conn.cursor()
    c.execute("""SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                 FROM work_sessions w
                 JOIN users u ON w.user_id = u.id
                 ORDER BY w.clock_in DESC""")
    raw_logs = c.fetchall()
    conn.close()
    logs = []
    for r in raw_logs:
        log = format_session(r[1], r[2], r[3], r[4])
        log["username"] = r[0]
        logs.append(log)
    return render_template("admin.html", logs=logs)

# --- Export Excel ---
@app.route("/export")
def export_excel():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403
    conn = sqlite3.connect("payroll.db")
    c = conn.cursor()
    c.execute("""SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                 FROM work_sessions w
                 JOIN users u ON w.user_id = u.id
                 ORDER BY w.clock_in DESC""")
    raw_logs = c.fetchall()
    conn.close()
    data = []
    for r in raw_logs:
        log = format_session(r[1], r[2], r[3], r[4])
        log["username"] = r[0]
        data.append(log)
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Payroll')
    output.seek(0)
    return send_file(output, download_name="payroll.xlsx", as_attachment=True)

# --- Logout ---
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- Main ---
if __name__ == "__main__":
    init_db()
    seed_users()
    app.run(debug=True)
