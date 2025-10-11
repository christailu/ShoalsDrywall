from flask import Flask, render_template, request, redirect, session, url_for, send_file, send_from_directory, jsonify
import psycopg2
import datetime
import pytz
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
import io
import os

from werkzeug.utils import secure_filename


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

    # Users table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'employee'
        )
    ''')

    # Work sessions table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS work_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            clock_in TEXT,
            clock_out TEXT,
            hours REAL,
            wage REAL
        )
    ''')

    # --- NEW: Documents table ---
    cur.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            filename TEXT NOT NULL,
            folder TEXT NOT NULL,
            content BYTEA NOT NULL
        )
    ''')

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

    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    user_id = session["user_id"]
    username = session["username"]

    # --- Employee dashboard week selection ---
    if "selected_week" not in session:
        now = datetime.datetime.now(CENTRAL_TZ)
        days_since_saturday = (now.weekday() - 5) % 7
        start_week = (now - datetime.timedelta(days=days_since_saturday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        session["selected_week"] = start_week.date().isoformat()

    # --- Handle POST actions ---
    if request.method == "POST":
        if "week_nav" in request.form:
            current = datetime.date.fromisoformat(session["selected_week"])
            if request.form["week_nav"] == "prev":
                session["selected_week"] = (current - datetime.timedelta(days=7)).isoformat()
            elif request.form["week_nav"] == "next":
                session["selected_week"] = (current + datetime.timedelta(days=7)).isoformat()
            return redirect(url_for("index"))

        elif "action" in request.form:
            action = request.form["action"]
            conn = get_db_connection()
            cur = conn.cursor()

            if action == "Clock In":
                # Only allow clock in if not already clocked in
                cur.execute("SELECT clock_out FROM work_sessions WHERE user_id=%s ORDER BY clock_in DESC LIMIT 1", (user_id,))
                last = cur.fetchone()
                if not last or last[0] is not None:
                    now_iso = datetime.datetime.now(CENTRAL_TZ).isoformat()
                    cur.execute(
                        "INSERT INTO work_sessions (user_id, clock_in) VALUES (%s, %s)",
                        (user_id, now_iso)
                    )

            elif action == "Clock Out":
                # Only allow clock out if currently clocked in
                cur.execute("SELECT id, clock_in, clock_out FROM work_sessions WHERE user_id=%s ORDER BY clock_in DESC LIMIT 1", (user_id,))
                last = cur.fetchone()
                if last and last[2] is None:
                    now_dt = datetime.datetime.now(CENTRAL_TZ)
                    clock_in_dt = datetime.datetime.fromisoformat(last[1])
                    hours = (now_dt - clock_in_dt).total_seconds() / 3600
                    wage = hours * EMPLOYEES[username]["rate"]
                    cur.execute(
                        "UPDATE work_sessions SET clock_out=%s, hours=%s, wage=%s WHERE id=%s",
                        (now_dt.isoformat(), hours, wage, last[0])
                    )
            conn.commit()
            cur.close()
            conn.close()
            return redirect(url_for("index"))

    # --- Fetch sessions ---
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT clock_in, clock_out, hours, wage FROM work_sessions WHERE user_id=%s ORDER BY clock_in ASC",
        (user_id,)
    )
    raw_sessions = cur.fetchall()

    cur.execute(
        "SELECT clock_in, clock_out FROM work_sessions WHERE user_id=%s ORDER BY clock_in DESC LIMIT 1",
        (user_id,)
    )
    last_session = cur.fetchone()
    cur.close()
    conn.close()

    if last_session and last_session[1] is None:
        clock_in_dt = datetime.datetime.fromisoformat(last_session[0]).astimezone(CENTRAL_TZ)
        status = f"Clocked in since {clock_in_dt.strftime('%I:%M %p on %m/%d/%Y')}"
    else:
        status = "Currently not clocked in."

    # --- Filter sessions for selected week ---
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
            sessions.append({
                "day": in_dt.strftime("%A"),
                "clock_in": in_dt.strftime("%I:%M %p on %m/%d/%Y"),
                "clock_out": datetime.datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ).strftime("%I:%M %p on %m/%d/%Y") if clock_out else "-",
                "hours": round(hours, 2) if hours else 0,
                "wage": round(wage, 2) if wage else 0
            })
            weekly_hours += hours if hours else 0
            weekly_gross += wage if wage else 0

    weekly_net = max(weekly_gross - TAX_FLAT if weekly_gross > 0 else 0, 0)

    return render_template(
        "index.html",
        sessions=sessions,
        username=username,
        weekly_net=round(weekly_net, 2),
        tax_info="Este dinero es lo que recibirá al final de la semana. El impuesto se resta solo al final de la semana, no cada día.",
        status=status
    )



# --- Admin dashboard ---
@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403

    # --- Handle week navigation ---
    if "selected_week" not in session:
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
        return redirect(url_for("admin_dashboard"))

    week_key = datetime.date.fromisoformat(session["selected_week"]).strftime("%Y-%m-%d")

    weeks_data = {
        week_key: {
            emp: {d: [] for d in ["Saturday","Sunday","Monday","Tuesday","Wednesday","Thursday","Friday"]}
            | {"Total": {"hours": 0, "gross": 0, "tax": 0, "net": 0}}
            for emp in EMPLOYEES.keys()
        }
    }

    # --- Handle uploads and fetch data safely ---
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Handle file upload
            if request.method == "POST" and "upload" in request.form:
                folder = request.form['folder']
                file = request.files['file']
                content = file.read()
                cur.execute(
                    "INSERT INTO documents (filename, folder, content) VALUES (%s,%s,%s)",
                    (file.filename, folder, psycopg2.Binary(content))
                )
                conn.commit()

            # Fetch all work sessions
            cur.execute("""
                SELECT u.username, w.clock_in, w.clock_out, w.hours, w.wage
                FROM work_sessions w
                JOIN users u ON w.user_id = u.id
                ORDER BY w.clock_in ASC
            """)
            raw_logs = cur.fetchall()

            # Fetch documents
            cur.execute("SELECT id, filename, folder FROM documents ORDER BY id DESC")
            docs = cur.fetchall()

    # --- Fill week data ---
    selected_start = datetime.datetime.combine(
        datetime.date.fromisoformat(session["selected_week"]),
        datetime.time.min
    ).astimezone(CENTRAL_TZ)
    selected_end = selected_start + datetime.timedelta(days=7)

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
    week_total = {"hours": 0, "gross": 0, "tax": 0, "net": 0}
    for emp in weeks_data[week_key]:
        total_gross = weeks_data[week_key][emp]["Total"]["gross"]
        total_hours = weeks_data[week_key][emp]["Total"]["hours"]

        weeks_data[week_key][emp]["Total"]["hours"] = round(total_hours, 2)
        weeks_data[week_key][emp]["Total"]["gross"] = round(total_gross, 2)
        weeks_data[week_key][emp]["Total"]["tax"] = TAX_FLAT if total_gross > 0 else 0
        weeks_data[week_key][emp]["Total"]["net"] = round(
            max(total_gross - weeks_data[week_key][emp]["Total"]["tax"], 0), 2
        )

        week_total["hours"] += weeks_data[week_key][emp]["Total"]["hours"]
        week_total["gross"] += weeks_data[week_key][emp]["Total"]["gross"]
        week_total["tax"] += weeks_data[week_key][emp]["Total"]["tax"]
        week_total["net"] += weeks_data[week_key][emp]["Total"]["net"]

    # --- Organize documents by folder ---
    folders = {}
    for doc_id, filename, folder in docs:
        if folder not in folders:
            folders[folder] = []
        folders[folder].append({"id": doc_id, "filename": filename})

    return render_template(
        "admin.html",
        weeks_data=weeks_data,
        selected_week=week_key,
        week_total=week_total,
        folders=folders,
        os=os
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
RESET_PIN = "2025"
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

@app.route("/safety")
def safety():
    return render_template("safety.html")

# --- File Upload Config ---


UPLOAD_FOLDER = os.path.join('static', 'uploads')

@app.route("/upload_document", methods=["GET", "POST"])
def upload_document():
    # Only admin can upload
    if "user_id" not in session or session.get("role") != "admin":
        return "Access denied. Admins only.", 403

    if request.method == "POST":
        file = request.files.get("file")
        folder = request.form.get("folder", "General")  # Default folder if none provided

        if not file:
            return "No file selected", 400

        # Save file content in DB
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (filename, folder, content) VALUES (%s, %s, %s)",
            (file.filename, folder, psycopg2.Binary(file.read()))
        )
        conn.commit()
        cur.close()
        conn.close()

        return redirect(url_for("admin_dashboard"))

    # GET request: show a simple upload form
    return """
    <h2>Upload Document</h2>
    <form method="POST" enctype="multipart/form-data">
        <input type="file" name="file" required><br><br>
        <input type="text" name="folder" placeholder="Folder name" required><br><br>
        <button type="submit">Upload</button>
    </form>
    <p><a href='/admin'>Back to Admin Dashboard</a></p>
    """


# --- View document ---
@app.route("/view_document/<int:doc_id>")
def view_document(doc_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT filename, content FROM documents WHERE id=%s", (doc_id,))
    doc = cur.fetchone()
    cur.close()
    conn.close()

    if not doc:
        return "Document not found", 404

    # Auto-detect mimetype
    return send_file(
        io.BytesIO(doc[1]),
        download_name=doc[0]
    )


# --- Delete document ---
@app.route("/delete_document/<int:doc_id>", methods=["POST"])
def delete_document(doc_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "success"})



# --- Main ---
if __name__ == "__main__":
    init_db()
    seed_users()
    app.run(debug=True)

