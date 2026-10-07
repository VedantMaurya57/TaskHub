from flask import Flask, render_template, request, jsonify, session
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "taskhub-secret-key-change-later"

DATABASE = "taskhub.db"


# ================= DATABASE =================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # USERS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            mobile TEXT,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer'
        )
    """)

    # TASKS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            location TEXT NOT NULL,
            task_date TEXT,
            task_time TEXT,
            description TEXT,
            status TEXT DEFAULT 'Pending',
            provider_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (provider_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# ================= FRONTEND =================

@app.route("/")
def home():
    return render_template("index.html")


# ================= API: CURRENT USER =================

@app.route("/api/me", methods=["GET"])
def current_user():

    if "user_id" not in session:
        return jsonify({
            "logged_in": False
        })

    conn = get_db()

    user = conn.execute("""
        SELECT id, name, email, mobile, role
        FROM users
        WHERE id = ?
    """, (session["user_id"],)).fetchone()

    conn.close()

    if not user:
        session.clear()
        return jsonify({
            "logged_in": False
        })

    return jsonify({
        "logged_in": True,
        "user": dict(user)
    })


# ================= API: SIGNUP =================

@app.route("/api/signup", methods=["POST"])
def api_signup():

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "message": "No data received"
        }), 400

    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    mobile = data.get("mobile", "").strip()
    password = data.get("password", "")
    role = data.get("role", "customer")

    if not name or not email or not password:
        return jsonify({
            "success": False,
            "message": "Name, email and password are required."
        }), 400

    if role not in ["customer", "provider"]:
        role = "customer"

    hashed_password = generate_password_hash(password)

    conn = get_db()

    try:

        cursor = conn.execute("""
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

        user_id = cursor.lastrowid

    except sqlite3.IntegrityError:

        conn.close()

        return jsonify({
            "success": False,
            "message": "Email is already registered."
        }), 409

    conn.close()

    return jsonify({
        "success": True,
        "message": "Account created successfully.",
        "user_id": user_id
    })


# ================= API: LOGIN =================

@app.route("/api/login", methods=["POST"])
def api_login():

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "message": "No data received"
        }), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE email = ?
    """, (email,)).fetchone()

    conn.close()

    if not user:
        return jsonify({
            "success": False,
            "message": "Invalid email or password."
        }), 401

    if not check_password_hash(user["password"], password):
        return jsonify({
            "success": False,
            "message": "Invalid email or password."
        }), 401

    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["role"] = user["role"]

    return jsonify({
        "success": True,
        "message": "Login successful.",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "mobile": user["mobile"],
            "role": user["role"]
        }
    })


# ================= API: LOGOUT =================

@app.route("/api/logout", methods=["POST"])
def api_logout():

    session.clear()

    return jsonify({
        "success": True,
        "message": "Logged out successfully."
    })


# ================= API: GET TASKS =================

@app.route("/api/tasks", methods=["GET"])
def get_tasks():

    conn = get_db()

    tasks = conn.execute("""
        SELECT
            tasks.id,
            tasks.service,
            tasks.location,
            tasks.task_date,
            tasks.task_time,
            tasks.description,
            tasks.status,
            tasks.provider_id,
            users.name AS customer_name
        FROM tasks
        JOIN users
        ON tasks.user_id = users.id
        ORDER BY tasks.id DESC
    """).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "tasks": [dict(task) for task in tasks]
    })


# ================= API: GET MY TASKS =================

@app.route("/api/my-tasks", methods=["GET"])
def my_tasks():

    if "user_id" not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    conn = get_db()

    tasks = conn.execute("""
        SELECT
            tasks.*,
            users.name AS provider_name
        FROM tasks
        LEFT JOIN users
        ON tasks.provider_id = users.id
        WHERE tasks.user_id = ?
        ORDER BY tasks.id DESC
    """, (session["user_id"],)).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "tasks": [dict(task) for task in tasks]
    })


# ================= API: CREATE TASK =================

@app.route("/api/tasks", methods=["POST"])
def create_task():

    if "user_id" not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "message": "No task data received."
        }), 400

    service = data.get("service", "").strip()
    location = data.get("location", "").strip()
    task_date = data.get("task_date", "")
    task_time = data.get("task_time", "")
    description = data.get("description", "").strip()

    if not service or not location:
        return jsonify({
            "success": False,
            "message": "Service and location are required."
        }), 400

    conn = get_db()

    cursor = conn.execute("""
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

    task_id = cursor.lastrowid

    conn.close()

    return jsonify({
        "success": True,
        "message": "Task posted successfully.",
        "task_id": task_id
    })


# ================= API: ACCEPT TASK =================

@app.route("/api/tasks/<int:task_id>/accept", methods=["POST"])
def accept_task(task_id):

    if session.get("role") != "provider":
        return jsonify({
            "success": False,
            "message": "Provider account required."
        }), 403

    conn = get_db()

    cursor = conn.execute("""
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

    updated = cursor.rowcount

    conn.close()

    if updated == 0:
        return jsonify({
            "success": False,
            "message": "Task is no longer available."
        }), 409

    return jsonify({
        "success": True,
        "message": "Task accepted successfully."
    })


# ================= API: START TASK =================

@app.route("/api/tasks/<int:task_id>/start", methods=["POST"])
def start_task(task_id):

    if session.get("role") != "provider":
        return jsonify({
            "success": False,
            "message": "Provider account required."
        }), 403

    conn = get_db()

    cursor = conn.execute("""
        UPDATE tasks
        SET status = 'In Progress'
        WHERE id = ?
        AND provider_id = ?
        AND status = 'Accepted'
    """, (
        task_id,
        session["user_id"]
    ))

    conn.commit()

    updated = cursor.rowcount

    conn.close()

    return jsonify({
        "success": updated > 0,
        "message": "Task started." if updated else "Unable to start task."
    })


# ================= API: COMPLETE TASK =================

@app.route("/api/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id):

    if session.get("role") != "provider":
        return jsonify({
            "success": False,
            "message": "Provider account required."
        }), 403

    conn = get_db()

    cursor = conn.execute("""
        UPDATE tasks
        SET status = 'Completed'
        WHERE id = ?
        AND provider_id = ?
        AND status = 'In Progress'
    """, (
        task_id,
        session["user_id"]
    ))

    conn.commit()

    updated = cursor.rowcount

    conn.close()

    return jsonify({
        "success": updated > 0,
        "message": "Task completed." if updated else "Unable to complete task."
    })


# ================= API: CANCEL TASK =================

@app.route("/api/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_task(task_id):

    if "user_id" not in session:
        return jsonify({
            "success": False,
            "message": "Please login first."
        }), 401

    conn = get_db()

    cursor = conn.execute("""
        UPDATE tasks
        SET status = 'Cancelled'
        WHERE id = ?
        AND user_id = ?
        AND status IN ('Pending', 'Accepted')
    """, (
        task_id,
        session["user_id"]
    ))

    conn.commit()

    updated = cursor.rowcount

    conn.close()

    return jsonify({
        "success": updated > 0,
        "message": "Task cancelled." if updated else "Unable to cancel task."
    })


# ================= RUN =================

# Initialize database
init_db()

# ================= RUN =================

if __name__ == "__main__":

    print("\n================================")
    print("      TASKHUB SERVER")
    print("================================")
    print("Server: http://127.0.0.1:5000")
    print("================================\n")

    app.run(debug=True)