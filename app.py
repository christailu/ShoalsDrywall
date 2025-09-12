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
    "Kevin": {"rate": 18, "password": "amigo1"},
    "Eddy": {"rate": 18, "password": "Ydd00!"},
    "Bocho": {"rate": 18, "password": "Hero299"},
    "Daniella": {"rate": 17, "password": "amiga2"},
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
# --- Home / Employee dashboard ---
@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # --- if admin, redirect to admin dashboard ---
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    # --- Employee dashboard logic below ---
    if "selected_week" not in session:
        now = datetime.datetime.now(CENTRAL_TZ)
        days_since_saturday = (now.weekday() - 5) % 7
        start_week = (now - datetime.timedelta(days=days_since_saturday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        session["selected_week"] = start_week.date().isoformat()
    else:
        try:
            _ = datetime.date.fromisoformat(session["selected_week"])
        except ValueError:
            try:
                dt = datetime.datetime.strptime(
                    session["selected_week"], "%a, %d %b %Y %H:%M:%S %Z"
                )
                session["selected_week"] = dt.date().isoformat()
            except Exception:
                now = datetime.datetime.now(CENTRAL_TZ)
                days_since_saturday = (now.weekday() - 5) % 7
                start_week = (now - datetime.timedelta(days=days_since_saturday)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                session["selected_week"] = start_week.date().isoformat()

    if request.method == "POST" and "week_nav" in request.form:
        current = datetime.date.fromisoformat(session["selected_week"])
        if request.form["week_nav"] == "prev":
            session["selected_week"] = (current - datetime.timedelta(days=7)).isoformat()
        elif request.form["week_nav"] == "next":
            session["selected_week"] = (current + datetime.timedelta(days=7)).isoformat()
        return redirect(url_for("index"))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT clock_in, clock_out, hours, wage FROM work_sessions WHERE user_id=%s ORDER BY clock_in ASC",
        (session["user_id"],)
    )
    raw_sessions = cur.fetchall()
    cur.close()
    conn.close()

    selected_start = datetime.datetime.combine(
        datetime.date.fromisoformat(session["selected_week"]),
        datetime.time.min
    ).astimezone(CENTRAL_TZ)
    selected_end = selected_start + datetime.timedelta(days=7)

    weekly_hours = 0
    weekly_gross = 0
    sessions = []
    for s in raw_sessions:
        clock_in, clock_out, hours, wage = s
        if not clock_in:
            continue
        in_dt = datetime.datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
        if selected_start <= in_dt < selected_end:
            sessions.append(format_session(clock_in, clock_out, hours, wage))
            weekly_hours += hours if hours else 0
            weekly_gross += wage if wage else 0

    weekly_net = max(weekly_gross - TAX_FLAT if weekly_gross > 0 else 0, 0)

    return render_template(
        "index.html",
        sessions=sessions,
        username=session["username"],
        weekly_net=round(weekly_net, 2),
        tax_info="Este dinero es lo que recibirá al final de la semana. El impuesto se resta solo al final de la semana, no cada día."
    )


# --- Admin dashboard ---
@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403

    # --- Handle week navigation ---
    if "selected_week" not in session:
        # Align to Saturday (previous Saturday as start of current week)
        now = datetime.datetime.now(CENTRAL_TZ)
        days_since_saturday = (now.weekday() - 5) % 7
        start_week = (now - datetime.timedelta(days=days_since_saturday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # store ISO date string in session
        session["selected_week"] = start_week.date().isoformat()

    if request.method == "POST" and "week_nav" in request.form:
        current = datetime.date.fromisoformat(session["selected_week"])
        if request.form["week_nav"] == "prev":
            session["selected_week"] = (current - datetime.timedelta(days=7)).isoformat()
        elif request.form["week_nav"] == "next":
            session["selected_week"] = (current + datetime.timedelta(days=7)).isoformat()
        return redirect(url_for("admin_dashboard"))

    # --- Load all logs ---
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                   FROM work_sessions w
                   JOIN users u ON w.user_id = u.id
                   ORDER BY w.clock_in ASC""")
    raw_logs = cur.fetchall()
    cur.close()
    conn.close()

    # --- Determine week start and end ---
    selected_start = datetime.datetime.combine(
        datetime.date.fromisoformat(session["selected_week"]),
        datetime.time.min
    ).astimezone(CENTRAL_TZ)
    selected_end = selected_start + datetime.timedelta(days=7)

    # Prepare container
    week_key = selected_start.strftime("%Y-%m-%d")
    weeks_data = {
        week_key: {
            emp: {d: [] for d in ["Saturday","Sunday","Monday","Tuesday","Wednesday","Thursday","Friday"]}
            | {"Total": {"hours": 0, "gross": 0, "tax": 0, "net": 0}}
            for emp in EMPLOYEES.keys()
        }
    }

    # --- Fill with sessions ---
    for username, clock_in, clock_out, hours, wage in raw_logs:
        if not clock_in:
            continue
        in_dt = datetime.datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
        if selected_start <= in_dt < selected_end:
            day = in_dt.strftime("%A")
            entry = {
                "time": f"{in_dt.strftime('%I:%M %p')} - {datetime.datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ).strftime('%I:%M %p') if clock_out else '-'}",
                "hours": round(hours, 2) if hours else 0,
                "gross": round(wage, 2) if wage else 0,
            }
            weeks_data[week_key][username][day].append(entry)
            weeks_data[week_key][username]["Total"]["hours"] += entry["hours"]
            weeks_data[week_key][username]["Total"]["gross"] += entry["gross"]

    # --- Compute totals ---
    for emp in weeks_data[week_key]:
        total_gross = weeks_data[week_key][emp]["Total"]["gross"]
        weeks_data[week_key][emp]["Total"]["hours"] = round(weeks_data[week_key][emp]["Total"]["hours"], 2)
        weeks_data[week_key][emp]["Total"]["gross"] = round(total_gross, 2)
        weeks_data[week_key][emp]["Total"]["tax"] = TAX_FLAT if total_gross > 0 else 0
        weeks_data[week_key][emp]["Total"]["net"] = round(max(total_gross - weeks_data[week_key][emp]["Total"]["tax"], 0), 2)

    return render_template(
        "admin.html",
        weeks_data=weeks_data,
        selected_week=week_key,
    )


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

