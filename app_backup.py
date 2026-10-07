from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "taskhub-secret-key"

DATABASE = "taskhub.db"


# ================= DATABASE =================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # Users table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            mobile TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer'
        )
    """)

    # Tasks table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            location TEXT NOT NULL,
            task_date TEXT NOT NULL,
            task_time TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'Pending',
            provider_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (provider_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# ================= HOME =================

@app.route("/")
def home():
    return render_template("index.html")


# ================= SERVICES =================

@app.route("/services")
def services():
    return render_template("service.html")


# ================= CONTACT =================

@app.route("/contact")
def contact():
    return render_template("contact.html")


# ================= SIGNUP =================

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        mobile = request.form["mobile"]
        password = request.form["password"]

        role = request.form.get("role", "customer")

        hashed_password = generate_password_hash(password)

        conn = get_db()

        try:

            conn.execute("""
                INSERT INTO users
                (name, email, mobile, password, role)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                email,
                mobile,
                hashed_password,
                role
            ))

            conn.commit()

        except sqlite3.IntegrityError:

            conn.close()

            return "Email already registered."

        conn.close()

        return redirect(url_for("login"))

    return render_template("signup.html")


# ================= LOGIN =================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()

        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        conn.close()

        if user and check_password_hash(
            user["password"],
            password
        ):

            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["role"] = user["role"]

            if user["role"] == "provider":
                return redirect(url_for("provider_dashboard"))

            return redirect(url_for("dashboard"))

        return "Invalid email or password."

    return render_template("login.html")


# ================= LOGOUT =================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# ================= CUSTOMER DASHBOARD =================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()

    tasks = conn.execute("""
        SELECT tasks.*, users.name AS provider_name
        FROM tasks
        LEFT JOIN users
        ON tasks.provider_id = users.id
        WHERE tasks.user_id = ?
        ORDER BY tasks.id DESC
    """, (session["user_id"],)).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        name=session["user_name"],
        tasks=tasks
    )


# ================= BOOK SERVICE =================

@app.route("/book", methods=["GET", "POST"])
def book():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        service = request.form["service"]
        location = request.form["location"]
        task_date = request.form["task_date"]
        task_time = request.form["task_time"]
        description = request.form["description"]

        conn = get_db()

        conn.execute("""
            INSERT INTO tasks
            (
                user_id,
                service,
                location,
                task_date,
                task_time,
                description,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'Pending')
        """, (
            session["user_id"],
            service,
            location,
            task_date,
            task_time,
            description
        ))

        conn.commit()
        conn.close()

        return redirect(url_for("dashboard"))

    service = request.args.get("service", "")

    return render_template(
        "booking.html",
        service=service
    )


# ==================================================
#                 PROVIDER SYSTEM
# ==================================================


# ================= PROVIDER DASHBOARD =================

@app.route("/provider")
def provider_dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "provider":
        return "Access denied. Provider account required."

    conn = get_db()

    # Pending jobs
    pending_tasks = conn.execute("""
        SELECT tasks.*, users.name AS customer_name
        FROM tasks
        JOIN users
        ON tasks.user_id = users.id
        WHERE tasks.status = 'Pending'
        ORDER BY tasks.id DESC
    """).fetchall()

    # Provider's accepted/completed jobs
    my_tasks = conn.execute("""
        SELECT tasks.*, users.name AS customer_name
        FROM tasks
        JOIN users
        ON tasks.user_id = users.id
        WHERE tasks.provider_id = ?
        ORDER BY tasks.id DESC
    """, (session["user_id"],)).fetchall()

    conn.close()

    return render_template(
        "provider_dashboard.html",
        name=session["user_name"],
        pending_tasks=pending_tasks,
        my_tasks=my_tasks
    )


# ================= ACCEPT TASK =================

@app.route("/accept_task/<int:task_id>")
def accept_task(task_id):

    if session.get("role") != "provider":
        return redirect(url_for("login"))

    conn = get_db()

    conn.execute("""
        UPDATE tasks
        SET
            status = 'Accepted',
            provider_id = ?
        WHERE id = ?
        AND status = 'Pending'
    """, (
        session["user_id"],
        task_id
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("provider_dashboard"))


# ================= REJECT TASK =================

@app.route("/reject_task/<int:task_id>")
def reject_task(task_id):

    if session.get("role") != "provider":
        return redirect(url_for("login"))

    conn = get_db()

    conn.execute("""
        UPDATE tasks
        SET status = 'Rejected'
        WHERE id = ?
        AND status = 'Pending'
    """, (task_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("provider_dashboard"))


# ================= START TASK =================

@app.route("/start_task/<int:task_id>")
def start_task(task_id):

    if session.get("role") != "provider":
        return redirect(url_for("login"))

    conn = get_db()

    conn.execute("""
        UPDATE tasks
        SET status = 'In Progress'
        WHERE id = ?
        AND provider_id = ?
    """, (
        task_id,
        session["user_id"]
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("provider_dashboard"))


# ================= COMPLETE TASK =================

@app.route("/complete_task/<int:task_id>")
def complete_task(task_id):

    if session.get("role") != "provider":
        return redirect(url_for("login"))

    conn = get_db()

    conn.execute("""
        UPDATE tasks
        SET status = 'Completed'
        WHERE id = ?
        AND provider_id = ?
    """, (
        task_id,
        session["user_id"]
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("provider_dashboard"))


# ================= START APP =================

if __name__ == "__main__":

    init_db()

    app.run(debug=True)
    