from flask import Flask, render_template, request, redirect, session, url_for, send_file
import psycopg2
import datetime
import pytz
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
import io

app = Flask(__name__)
app.secret_key = "shoals_drywalls"

# PostgreSQL connection
DB_URL = "postgresql://sddata_user:cojDN21iqaIpkEsmrGvN68QTpWwh5v3L@dpg-d2rdf0gdl3ps73d1etbg-a.oregon-postgres.render.com/sddata"

def get_db_connection():
    return psycopg2.connect(DB_URL)

# Fixed employees with hourly rates and passwords
EMPLOYEES = {
    "Alex": {"rate": 22, "password": "Merida23"},
    "Kevin": {"rate": 18, "password": "Kelos45"},
    "Eddy": {"rate": 18, "password": "Ydd00!"},
    "Bocho": {"rate": 18, "password": "Hero299"},
    "Daniella": {"rate": 17, "password": "Valle1999"},
}

CENTRAL_TZ = pytz.timezone("America/Chicago")
TAX_FLAT = 20  # Flat tax per week

# --- Database setup ---
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT DEFAULT 'employee'
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS work_sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        clock_in TEXT,
        clock_out TEXT,
        hours REAL,
        wage REAL
    )''')
    conn.commit()
    cur.close()
    conn.close()

# --- Seed users and update passwords if changed ---
def seed_users():
    conn = get_db_connection()
    cur = conn.cursor()

    # Admin
    cur.execute("SELECT id FROM users WHERE username=%s", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
            ("admin", generate_password_hash("admin123"), "admin")
        )

    # Employees
    for username, info in EMPLOYEES.items():
        cur.execute("SELECT id, password FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        hashed_pw = generate_password_hash(info["password"])
        if not row:
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
                (username, hashed_pw, "employee")
            )
        else:
            # Update password if changed
            if not check_password_hash(row[1], info["password"]):
                cur.execute("UPDATE users SET password=%s WHERE username=%s", (hashed_pw, username))

    conn.commit()
    cur.close()
    conn.close()

# --- Format session ---
def format_session(clock_in, clock_out, hours, wage):
    in_dt = datetime.datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
    clock_in_str = in_dt.strftime("%I:%M %p on %m/%d/%Y")
    day = in_dt.strftime("%A")
    clock_out_str = "-"
    if clock_out:
        out_dt = datetime.datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
        clock_out_str = out_dt.strftime("%I:%M %p on %m/%d/%Y")
    return {
        "day": day,
        "clock_in": clock_in_str,
        "clock_out": clock_out_str,
        "hours": round(hours, 2) if hours else 0,
        "wage": round(wage, 2) if wage else 0
    }

# --- Login ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, password, role FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and check_password_hash(user[1], password):
            session["user_id"] = user[0]
            session["username"] = username
            session["role"] = user[2]
            return redirect("/")
        return render_template("invalid.html")
    return render_template("login.html")

# --- Home / Employee dashboard ---
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
        conn = get_db_connection()
        cur = conn.cursor()
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if action == "Clock In":
            cur.execute(
                "INSERT INTO work_sessions (user_id, clock_in) VALUES (%s, %s)",
                (user_id, now_utc)
            )
        elif action == "Clock Out":
            cur.execute(
                "SELECT id, clock_in FROM work_sessions WHERE user_id=%s AND clock_out IS NULL",
                (user_id,)
            )
            row = cur.fetchone()
            if row:
                start_time = datetime.datetime.fromisoformat(row[1])
                end_time = datetime.datetime.now(datetime.timezone.utc)
                hours = (end_time - start_time).total_seconds() / 3600
                rate = EMPLOYEES.get(session["username"], {}).get("rate", 15)
                wage = hours * rate
                cur.execute(
                    "UPDATE work_sessions SET clock_out=%s, hours=%s, wage=%s WHERE id=%s",
                    (end_time.isoformat(), hours, wage, row[0])
                )
        conn.commit()
        cur.close()
        conn.close()
        return redirect("/")

    # Fetch all sessions
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT clock_in, clock_out, hours, wage FROM work_sessions WHERE user_id=%s",
        (user_id,)
    )
    raw_sessions = cur.fetchall()
    cur.close()
    conn.close()
    sessions = [format_session(*s) for s in raw_sessions]

    # Compute current week's net pay
    now = datetime.datetime.now(CENTRAL_TZ)
    start_week = now - datetime.timedelta(days=now.weekday())
    start_week = start_week.replace(hour=0, minute=0, second=0, microsecond=0)
    weekly_hours = 0
    weekly_gross = 0
    for s in raw_sessions:
        in_dt = datetime.datetime.fromisoformat(s[0]).astimezone(CENTRAL_TZ)
        if start_week <= in_dt <= now:
            weekly_hours += s[2] if s[2] else 0
            weekly_gross += s[3] if s[3] else 0
    weekly_net = max(weekly_gross - TAX_FLAT if weekly_gross > 0 else 0, 0)

    return render_template("index.html",
                           sessions=sessions,
                           username=session["username"],
                           weekly_net=round(weekly_net,2),
                           tax_info="Este dinero es lo que recibirá al final de la semana. El impuesto se resta solo al final de la semana, no cada día.")

# --- Admin dashboard ---
@app.route("/admin")
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                   FROM work_sessions w
                   JOIN users u ON w.user_id = u.id
                   ORDER BY w.clock_in ASC""")
    raw_logs = cur.fetchall()
    cur.close()
    conn.close()

    now_central = datetime.datetime.now(datetime.timezone.utc).astimezone(CENTRAL_TZ)
    start_of_week = now_central - datetime.timedelta(days=now_central.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    week_starts = [start_of_week + datetime.timedelta(weeks=i) for i in range(4)]

    weeks_data = {}
    for ws in week_starts:
        week_key = ws.strftime("%Y-%m-%d")
        weeks_data[week_key] = {}
        for emp in EMPLOYEES.keys():
            weeks_data[week_key][emp] = {d: [] for d in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]}
            weeks_data[week_key][emp]["Total"] = {"hours": 0, "gross": 0, "tax": 0, "net": 0}

    for username, clock_in, clock_out, hours, wage in raw_logs:
        if not clock_in:
            continue
        in_dt = datetime.datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
        for ws in week_starts:
            we = ws + datetime.timedelta(days=7)
            if ws <= in_dt < we:
                week_key = ws.strftime("%Y-%m-%d")
                day = in_dt.strftime("%A")
                entry = {
                    "time": f"{in_dt.strftime('%I:%M %p')} - {datetime.datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ).strftime('%I:%M %p') if clock_out else '-'}",
                    "hours": round(hours, 2) if hours else 0,
                    "gross": round(wage, 2) if wage else 0
                }
                weeks_data[week_key][username][day].append(entry)
                weeks_data[week_key][username]["Total"]["hours"] += entry["hours"]
                weeks_data[week_key][username]["Total"]["gross"] += entry["gross"]
                break

    for wk, employees in weeks_data.items():
        for emp in employees:
            total_gross = employees[emp]["Total"]["gross"]
            employees[emp]["Total"]["hours"] = round(employees[emp]["Total"]["hours"], 2)
            employees[emp]["Total"]["gross"] = round(total_gross, 2)
            employees[emp]["Total"]["tax"] = TAX_FLAT if total_gross > 0 else 0
            employees[emp]["Total"]["net"] = round(max(total_gross - employees[emp]["Total"]["tax"],0),2)

    return render_template("admin.html", weeks_data=weeks_data,
                           days=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])

# --- Export Excel (net pay only, weekly) ---
@app.route("/export")
def export_excel():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                   FROM work_sessions w
                   JOIN users u ON w.user_id = u.id
                   ORDER BY w.clock_in ASC""")
    raw_logs = cur.fetchall()
    cur.close()
    conn.close()

    # Weekly net pay calculation
    CENTRAL_TZ = pytz.timezone("America/Chicago")
    weekly_data = {}
    for username, clock_in, clock_out, hours, wage in raw_logs:
        if not clock_in:
            continue
        in_dt = datetime.datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
        start_week = in_dt - datetime.timedelta(days=in_dt.weekday())
        start_week = start_week.replace(hour=0, minute=0, second=0, microsecond=0)
        week_key = start_week.strftime("%Y-%m-%d")
        if username not in weekly_data:
            weekly_data[username] = {}
        if week_key not in weekly_data[username]:
            weekly_data[username][week_key] = {"hours":0, "gross":0}
        weekly_data[username][week_key]["hours"] += hours if hours else 0
        weekly_data[username][week_key]["gross"] += wage if wage else 0

    # Prepare Excel
    data = []
    for username, weeks in weekly_data.items():
        for wk, info in weeks.items():
            net = max(info["gross"] - TAX_FLAT if info["gross"]>0 else 0, 0)
            data.append({
                "Username": username,
                "Week Start": wk,
                "Hours Worked": round(info["hours"],2),
                "Net Pay": round(net,2)
            })

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

# --- Reset DB ---
RESET_PIN = "2003"
@app.route("/reset", methods=["POST"])
def reset_db():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403
    pin = request.form.get("pin")
    if pin != RESET_PIN:
        return "Invalid PIN. Reset denied.", 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM work_sessions")
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for("admin_dashboard"))

# --- Main ---
if __name__ == "__main__":
    init_db()
    seed_users()
    app.run(debug=True)

