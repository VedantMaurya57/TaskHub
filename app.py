"""TaskHub - Flask backend.

The single-page frontend (templates/index.html) talks to this server through
the JSON API under /api/. All data lives in SQLite and every rule (who can
apply, accept, review, moderate, etc.) is enforced here, not in the browser.

Configuration (environment variables):
    SECRET_KEY        required in production, long random string
    DATABASE_PATH     path to the SQLite file (default: taskhub.db)
    FLASK_DEBUG       "1" to enable debug mode (never on a public server)
    SECURE_COOKIES    "1" when served over HTTPS (default "1" unless debug)
    ADMIN_EMAIL       optional, creates/updates an admin account on startup
    ADMIN_PASSWORD    optional, password for ADMIN_EMAIL (8+ chars)
    SEED_DEMO         "1" to load demo users and tasks into an empty database
"""

import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date
from functools import wraps

from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

# ================= CONFIG =================

app = Flask(__name__)

DEBUG = os.environ.get("FLASK_DEBUG") == "1"
DATABASE = os.environ.get("DATABASE_PATH", "taskhub.db")
SEED_DEMO = os.environ.get("SEED_DEMO") == "1"

secret = os.environ.get("SECRET_KEY")
if not secret:
    if not DEBUG:
        print("WARNING: SECRET_KEY is not set. Using a random key; "
              "users will be logged out on every restart.")
    secret = secrets.token_hex(32)

app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "0" if DEBUG else "1") == "1",
    MAX_CONTENT_LENGTH=1024 * 1024,  # 1 MB per request (images are capped at 300 KB)
)

AREAS = ["Andheri", "Bandra", "Borivali", "Powai", "Dadar", "Thane", "Navi Mumbai"]
CATS = ["Home Help", "Moving & Transport", "Delivery", "Cleaning", "Computer Help",
        "Repair", "Errands", "Event Help", "Tutoring", "Other"]
URGENCY = ["Normal", "Urgent", "Flexible"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IMG_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")
MAX_IMG_CHARS = 410_000  # ~300 KB of binary data once base64 encoded


def now_ms():
    return int(time.time() * 1000)


# ================= DATABASE =================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    roles TEXT NOT NULL DEFAULT 'customer',
    mode TEXT NOT NULL DEFAULT 'customer',
    skills TEXT NOT NULL DEFAULT '[]',
    cats TEXT NOT NULL DEFAULT '[]',
    exp TEXT NOT NULL DEFAULT '',
    avail TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    ver TEXT NOT NULL DEFAULT 'Profile not yet verified',
    susp INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    cat TEXT NOT NULL,
    sub TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL,
    task_date TEXT NOT NULL,
    task_time TEXT NOT NULL DEFAULT '',
    budget INTEGER NOT NULL,
    urg TEXT NOT NULL DEFAULT 'Normal',
    req TEXT NOT NULL DEFAULT '',
    img TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Open',
    helper_id INTEGER REFERENCES users(id),
    removed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    status TEXT NOT NULL,
    by_id INTEGER REFERENCES users(id),
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    msg TEXT NOT NULL,
    price INTEGER NOT NULL,
    eta TEXT NOT NULL,
    exp TEXT NOT NULL DEFAULT '',
    st TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL,
    UNIQUE (task_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    from_id INTEGER NOT NULL REFERENCES users(id),
    to_id INTEGER NOT NULL REFERENCES users(id),
    txt TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    txt TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '#/',
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    from_id INTEGER NOT NULL REFERENCES users(id),
    to_id INTEGER NOT NULL REFERENCES users(id),
    n INTEGER NOT NULL,
    txt TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    target TEXT NOT NULL,
    why TEXT NOT NULL,
    by_id INTEGER NOT NULL REFERENCES users(id),
    done INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    by_id INTEGER NOT NULL REFERENCES users(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (by_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_apps_task ON applications(task_id);
CREATE INDEX IF NOT EXISTS idx_msgs_to ON messages(to_id);
CREATE INDEX IF NOT EXISTS idx_notifs_user ON notifications(user_id);
"""


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")

    # A database created by the old version of app.py has a different schema.
    # Keep it as *_old tables instead of crashing.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if cols and "roles" not in cols:
        conn.execute("ALTER TABLE users RENAME TO users_old")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
            conn.execute("ALTER TABLE tasks RENAME TO tasks_old")

    conn.executescript(SCHEMA)

    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    if admin_email and len(admin_pw) >= 8:
        row = conn.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET roles='admin', mode='admin', password=? WHERE id=?",
                         (generate_password_hash(admin_pw), row["id"]))
        else:
            conn.execute("""INSERT INTO users (name,email,password,area,roles,mode,ver,created_at)
                            VALUES ('Platform Admin',?,?,'Dadar','admin','admin','Staff',?)""",
                         (admin_email, generate_password_hash(admin_pw), now_ms()))

    if SEED_DEMO and not conn.execute("SELECT 1 FROM users WHERE email LIKE '%@demo.in'").fetchone():
        seed_demo(conn)

    conn.commit()
    conn.close()


def seed_demo(conn):
    t0 = now_ms()
    pw = generate_password_hash("demo1234")
    users = [
        ("Aarav Mehta", "aarav@demo.in", "Andheri", "customer,helper", [], [], "", "", ""),
        ("Meera Kulkarni", "meera@demo.in", "Bandra", "helper,customer",
         ["moving", "boxes", "furniture", "packing"], ["Moving & Transport", "Home Help"],
         "4 years of local moving and packing help", "Weekdays 9-7, Sat mornings",
         "Careful with fragile items. Based near Bandra."),
        ("Rohan Desai", "rohan@demo.in", "Powai", "helper",
         ["laptop", "windows", "wifi", "software", "printer"], ["Computer Help", "Repair"],
         "IT support for 6 years", "Evenings and weekends",
         "I set up laptops, Wi-Fi and printers at home."),
        ("Isha Nair", "isha@demo.in", "Thane", "helper", ["cleaning", "kitchen", "bathroom"],
         ["Cleaning", "Home Help"], "3 years in home cleaning", "Mon-Sat 8-4",
         "Thorough, quiet, on time."),
    ]
    ids = []
    for name, email, area, roles, skills, cats, exp, avail, bio in users:
        cur = conn.execute("""INSERT INTO users (name,email,password,area,roles,mode,skills,cats,exp,
                              avail,bio,ver,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (name, email, pw, area, roles, roles.split(",")[0], json.dumps(skills),
                            json.dumps(cats), exp, avail, bio, "Phone verified (demo)",
                            int(t0 - 90 * 864e5)))
        ids.append(cur.lastrowid)
    today = date.today().toordinal()
    tasks = [("Need help shifting boxes", "Moving & Transport", "Andheri", 1, 800, "Normal"),
             ("Computer setup at home", "Computer Help", "Powai", 2, 1200, "Normal"),
             ("Urgent document delivery", "Delivery", "Bandra", 0, 350, "Urgent"),
             ("Help with moving furniture", "Moving & Transport", "Thane", 3, 1500, "Normal"),
             ("Basic home cleaning", "Cleaning", "Borivali", 2, 900, "Normal"),
             ("Event setup assistance", "Event Help", "Dadar", 5, 2000, "Normal"),
             ("Laptop software installation", "Computer Help", "Navi Mumbai", 1, 500, "Normal")]
    for title, cat, area, days, budget, urg in tasks:
        cur = conn.execute("""INSERT INTO tasks (user_id,title,description,cat,area,task_date,task_time,
                              budget,urg,created_at) VALUES (?,?,?,?,?,?,'10:00',?,?,?)""",
                           (ids[0], title, title + ". Please be on time and bring basic tools if "
                            "needed. Message me for details.", cat, area,
                            date.fromordinal(today + days).isoformat(), budget, urg, t0))
        conn.execute("INSERT INTO task_history (task_id,status,by_id,at) VALUES (?, 'Open', ?, ?)",
                     (cur.lastrowid, ids[0], t0))
    conn.execute("INSERT INTO reviews (from_id,to_id,n,txt,created_at) VALUES (?,?,5,?,?)",
                 (ids[0], ids[1], "Careful and punctual.", int(t0 - 864e6)))
    conn.execute("INSERT INTO reviews (from_id,to_id,n,txt,created_at) VALUES (?,?,4,?,?)",
                 (ids[0], ids[2], "Fixed my Wi-Fi in an hour.", int(t0 - 864e6)))


# ================= HELPERS =================

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"ok": False, "error": err.message}), err.status


@app.errorhandler(413)
def too_large(_err):
    return jsonify({"ok": False, "error": "Upload is too large."}), 413


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Invalid request.")
    return data


def s(data, key, max_len=2000):
    """Read a string field safely (never crashes on numbers, lists or null)."""
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ApiError(f"Invalid value for {key}.")
    return str(value).strip()[:max_len]


def num(data, key):
    try:
        value = float(data.get(key))
    except (TypeError, ValueError):
        return 0
    if value != value or value in (float("inf"), float("-inf")):
        return 0
    return int(round(value))


def pid(value, prefix):
    """'u12' -> 12, 'T-1012' -> 12."""
    value = str(value or "")
    if not value.startswith(prefix):
        raise ApiError("Not found.", 404)
    try:
        n = int(value[len(prefix):])
    except ValueError:
        raise ApiError("Not found.", 404)
    return n - 1000 if prefix == "T-" else n


def uid(n):
    return f"u{n}" if n is not None else None


def tid(n):
    return f"T-{1000 + n}"


def roles_of(user):
    return [r for r in user["roles"].split(",") if r]


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["susp"]:
        session.clear()
        return None
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            raise ApiError("Please log in first.", 401)
        g.user = user
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if "admin" not in roles_of(g.user):
            raise ApiError("Admin access only.", 403)
        return fn(*args, **kwargs)
    return wrapper


def notify(db, user_id, txt, link):
    if user_id:
        db.execute("INSERT INTO notifications (user_id,txt,link,created_at) VALUES (?,?,?,?)",
                   (user_id, txt[:300], link, now_ms()))


def history(db, task_id, status, by_id):
    db.execute("INSERT INTO task_history (task_id,status,by_id,at) VALUES (?,?,?,?)",
               (task_id, status, by_id, now_ms()))


def get_task(db, task_ref):
    task = db.execute("SELECT * FROM tasks WHERE id=? AND removed=0",
                      (pid(task_ref, "T-"),)).fetchone()
    if not task:
        raise ApiError("Task not found.", 404)
    return task


def rating(db, user_id):
    row = db.execute("SELECT AVG(n) a, COUNT(*) c FROM reviews WHERE to_id=?", (user_id,)).fetchone()
    return (row["a"] or 0, row["c"])


def match_score(db, helper, task):
    words = f"{task['title']} {task['description']} {task['cat']} {task['sub']}".lower()
    score = 0
    if task["cat"] in json.loads(helper["cats"]):
        score += 40
    score += min(20, sum(10 for k in json.loads(helper["skills"]) if k.lower() in words))
    if helper["area"] == task["area"]:
        score += 25
    avg, count = rating(db, helper["id"])
    score += round(avg * 2) if count else 6
    if helper["avail"]:
        score += 5
    return min(99, score)


def ok(**extra):
    return jsonify({"ok": True, **extra})


# ================= FRONTEND =================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/healthz")
def healthz():
    return "ok"


# ================= STATE =================

@app.route("/api/state")
def state():
    """Everything the current visitor is allowed to see, in the shape the frontend uses."""
    db = get_db()
    me = current_user()
    me_id = me["id"] if me else None
    is_admin = bool(me and "admin" in roles_of(me))

    users = []
    for u in db.execute("SELECT * FROM users"):
        item = {"id": uid(u["id"]), "name": u["name"], "area": u["area"], "roles": roles_of(u),
                "mode": u["mode"], "at": u["created_at"], "skills": json.loads(u["skills"]),
                "cats": json.loads(u["cats"]), "exp": u["exp"], "avail": u["avail"],
                "bio": u["bio"], "ver": u["ver"], "susp": bool(u["susp"])}
        if u["id"] == me_id:
            item["email"] = u["email"]
        users.append(item)

    hist = {}
    for h in db.execute("SELECT * FROM task_history ORDER BY id"):
        hist.setdefault(h["task_id"], []).append({"s": h["status"], "t": h["at"], "by": uid(h["by_id"])})
    napps = {r["task_id"]: r["c"] for r in
             db.execute("SELECT task_id, COUNT(*) c FROM applications GROUP BY task_id")}

    tasks = [{"id": tid(t["id"]), "by": uid(t["user_id"]), "title": t["title"],
              "desc": t["description"], "cat": t["cat"], "sub": t["sub"], "area": t["area"],
              "date": t["task_date"], "time": t["task_time"], "budget": t["budget"],
              "urg": t["urg"], "req": t["req"], "img": t["img"], "status": t["status"],
              "helper": uid(t["helper_id"]), "at": t["created_at"],
              "hist": hist.get(t["id"], []), "napps": napps.get(t["id"], 0)}
             for t in db.execute("SELECT * FROM tasks WHERE removed=0 ORDER BY created_at DESC")]

    reviews = [{"id": f"r{r['id']}", "task": tid(r["task_id"]) if r["task_id"] else None,
                "from": uid(r["from_id"]), "to": uid(r["to_id"]), "n": r["n"], "txt": r["txt"],
                "at": r["created_at"]} for r in db.execute("SELECT * FROM reviews")]

    apps, msgs, notifs, blocks, reports = [], [], [], [], []
    if me:
        apps = [{"id": f"a{a['id']}", "task": tid(a["task_id"]), "by": uid(a["user_id"]),
                 "msg": a["msg"], "price": a["price"], "eta": a["eta"], "exp": a["exp"],
                 "st": a["st"], "at": a["created_at"]}
                for a in db.execute("""SELECT a.* FROM applications a JOIN tasks t ON t.id=a.task_id
                                       WHERE a.user_id=? OR t.user_id=?""", (me_id, me_id))]
        msgs = [{"id": f"m{m['id']}", "task": tid(m["task_id"]), "from": uid(m["from_id"]),
                 "to": uid(m["to_id"]), "txt": m["txt"], "at": m["created_at"],
                 "read": bool(m["is_read"])}
                for m in db.execute("SELECT * FROM messages WHERE from_id=? OR to_id=? ORDER BY id",
                                    (me_id, me_id))]
        notifs = [{"id": f"n{n['id']}", "to": uid(n["user_id"]), "txt": n["txt"], "link": n["link"],
                   "read": bool(n["is_read"]), "at": n["created_at"]}
                  for n in db.execute("""SELECT * FROM notifications WHERE user_id=?
                                         ORDER BY id DESC LIMIT 200""", (me_id,))]
        blocks = [{"by": uid(b["by_id"]), "u": uid(b["user_id"])}
                  for b in db.execute("SELECT * FROM blocks WHERE by_id=?", (me_id,))]
    if is_admin:
        reports = [{"id": f"p{r['id']}", "type": r["type"], "target": r["target"], "why": r["why"],
                    "by": uid(r["by_id"]), "at": r["created_at"], "done": bool(r["done"])}
                   for r in db.execute("SELECT * FROM reports ORDER BY id DESC")]

    return jsonify({"ok": True, "me": uid(me_id), "demo": SEED_DEMO, "cats": CATS,
                    "users": users, "tasks": tasks, "apps": apps, "msgs": msgs,
                    "notifs": notifs, "reviews": reviews, "reports": reports, "blocks": blocks})


# ================= AUTH =================

@app.route("/api/register", methods=["POST"])
def register():
    data = body()
    name = s(data, "name", 80)
    email = s(data, "email", 200).lower()
    password = data.get("pw") if isinstance(data.get("pw"), str) else ""
    role = s(data, "role")
    area = s(data, "area")

    if len(name) < 2:
        raise ApiError("Please enter your name.")
    if not EMAIL_RE.match(email):
        raise ApiError("Please enter a valid email address.")
    if len(password) < 8 or not re.search(r"\d", password) or not re.search(r"[a-zA-Z]", password):
        raise ApiError("Password needs 8+ characters with letters and a number.")
    if len(password) > 200:
        raise ApiError("Password is too long.")
    roles = {"customer": ["customer"], "helper": ["helper"],
             "both": ["customer", "helper"]}.get(role)
    if not roles:
        raise ApiError("Choose how you want to use TaskHub.")
    if area not in AREAS:
        raise ApiError("Choose your area.")

    db = get_db()
    try:
        cur = db.execute("""INSERT INTO users (name,email,password,area,roles,mode,created_at)
                            VALUES (?,?,?,?,?,?,?)""",
                         (name, email, generate_password_hash(password), area, ",".join(roles),
                          roles[0], now_ms()))
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError("An account with this email already exists.", 409)

    session.clear()
    session["user_id"] = cur.lastrowid
    session.permanent = True
    return ok(me=uid(cur.lastrowid))


@app.route("/api/login", methods=["POST"])
def login():
    data = body()
    email = s(data, "email", 200).lower()
    password = data.get("pw") if isinstance(data.get("pw"), str) else ""
    user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password"], password):
        raise ApiError("Invalid email or password.", 401)
    if user["susp"]:
        raise ApiError("This account is suspended.", 403)
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return ok(me=uid(user["id"]), admin="admin" in roles_of(user))


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return ok()


# ================= PROFILE =================

@app.route("/api/mode", methods=["POST"])
@login_required
def switch_mode():
    roles = roles_of(g.user)
    if "customer" in roles and "helper" in roles:
        new_mode = "customer" if g.user["mode"] == "helper" else "helper"
        db = get_db()
        db.execute("UPDATE users SET mode=? WHERE id=?", (new_mode, g.user["id"]))
        db.commit()
    return ok()


@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    data = body()
    user = g.user
    name = s(data, "name", 80) or user["name"]
    area = s(data, "area")
    if area not in AREAS:
        raise ApiError("Choose your area.")
    skills_raw = s(data, "skills", 500)
    skills = [x.strip()[:40] for x in skills_raw.split(",") if x.strip()][:20]
    cats = data.get("cats") if isinstance(data.get("cats"), list) else []
    cats = [c for c in cats if c in CATS]

    roles = []
    if data.get("rc"):
        roles.append("customer")
    if data.get("rh"):
        roles.append("helper")
    if "admin" in roles_of(user):
        roles.append("admin")
    if not roles:
        raise ApiError("Pick at least one role.")
    mode = user["mode"] if user["mode"] in roles else roles[0]

    db = get_db()
    db.execute("""UPDATE users SET name=?, area=?, bio=?, exp=?, avail=?, skills=?, cats=?,
                  roles=?, mode=? WHERE id=?""",
               (name, area, s(data, "bio", 1000), s(data, "exp", 300), s(data, "avail", 200),
                json.dumps(skills), json.dumps(cats), ",".join(roles), mode, user["id"]))
    db.commit()
    return ok()


# ================= TASKS =================

@app.route("/api/tasks", methods=["POST"])
@login_required
def create_task():
    data = body()
    title = s(data, "title", 80)
    desc = s(data, "desc", 3000)
    cat = s(data, "cat")
    area = s(data, "area")
    task_date = s(data, "date", 10)
    task_time = s(data, "time", 5)
    budget = num(data, "budget")
    urg = s(data, "urg") or "Normal"
    img = data.get("img") if isinstance(data.get("img"), str) else ""

    if len(title) < 5:
        raise ApiError("Please add a clear title (5+ characters).")
    if len(desc) < 15:
        raise ApiError("Please describe the task in a little more detail.")
    if cat not in CATS:
        raise ApiError("Choose a category.")
    if area not in AREAS:
        raise ApiError("Choose a location.")
    try:
        if date.fromisoformat(task_date) < date.today():
            raise ValueError
    except ValueError:
        raise ApiError("Choose a preferred date from today onward.")
    if task_time and not re.match(r"^\d{2}:\d{2}$", task_time):
        raise ApiError("Enter a valid time.")
    if not 0 < budget <= 10_000_000:
        raise ApiError("Enter a budget greater than zero.")
    if urg not in URGENCY:
        urg = "Normal"
    if img and (len(img) > MAX_IMG_CHARS or not IMG_RE.match(img)):
        raise ApiError("Image must be a PNG, JPEG, GIF or WebP under 300KB.")

    db = get_db()
    user = g.user
    roles = roles_of(user)
    if "customer" not in roles:
        db.execute("UPDATE users SET roles=? WHERE id=?",
                   (",".join(roles + ["customer"]), user["id"]))

    cur = db.execute("""INSERT INTO tasks (user_id,title,description,cat,sub,area,task_date,task_time,
                        budget,urg,req,img,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (user["id"], title, desc, cat, s(data, "sub", 80), area, task_date, task_time,
                      budget, urg, s(data, "req", 300), img, now_ms()))
    task_id = cur.lastrowid
    history(db, task_id, "Open", user["id"])

    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    for helper in db.execute("SELECT * FROM users WHERE susp=0 AND id<>?", (user["id"],)).fetchall():
        if "helper" in roles_of(helper):
            score = match_score(db, helper, task)
            if score >= 55:
                notify(db, helper["id"], f"New task matches you ({score}%): {title}",
                       "#/task/" + tid(task_id))
    db.commit()
    return ok(id=tid(task_id))


@app.route("/api/tasks/<task_ref>/apply", methods=["POST"])
@login_required
def apply_task(task_ref):
    data = body()
    db = get_db()
    task = get_task(db, task_ref)
    me = g.user
    if "helper" not in roles_of(me):
        raise ApiError("Turn on the Helper role in your profile to apply.", 403)
    if task["status"] != "Open":
        raise ApiError("This task is no longer available.", 409)
    if task["user_id"] == me["id"]:
        raise ApiError("You cannot apply to your own task.")
    msg = s(data, "msg", 1000)
    price = num(data, "price")
    eta = s(data, "eta", 80)
    if len(msg) < 10:
        raise ApiError("Write a short proposal (10+ characters).")
    if not 0 < price <= 10_000_000:
        raise ApiError("Enter a proposed price.")
    if not eta:
        raise ApiError("Add an estimated completion time.")
    try:
        db.execute("""INSERT INTO applications (task_id,user_id,msg,price,eta,exp,created_at)
                      VALUES (?,?,?,?,?,?,?)""",
                   (task["id"], me["id"], msg, price, eta, s(data, "exp", 300), now_ms()))
    except sqlite3.IntegrityError:
        raise ApiError("You have already applied to this task.", 409)
    notify(db, task["user_id"], f'{me["name"]} applied to "{task["title"]}"',
           "#/task/" + tid(task["id"]))
    if db.execute("SELECT COUNT(*) c FROM task_history WHERE task_id=?", (task["id"],)).fetchone()["c"] == 1:
        history(db, task["id"], "Applications Received", me["id"])
    db.commit()
    return ok()


def get_app_for_owner(db, app_ref):
    application = db.execute("SELECT * FROM applications WHERE id=?",
                             (pid(app_ref, "a"),)).fetchone()
    if not application:
        raise ApiError("Application not found.", 404)
    task = db.execute("SELECT * FROM tasks WHERE id=? AND removed=0",
                      (application["task_id"],)).fetchone()
    if not task or task["user_id"] != g.user["id"]:
        raise ApiError("Not allowed.", 403)
    return application, task


@app.route("/api/apps/<app_ref>/accept", methods=["POST"])
@login_required
def accept_application(app_ref):
    db = get_db()
    application, task = get_app_for_owner(db, app_ref)
    if task["status"] != "Open":
        raise ApiError("Task no longer open.", 409)
    link = "#/task/" + tid(task["id"])

    others = db.execute("SELECT user_id FROM applications WHERE task_id=? AND id<>?",
                        (task["id"], application["id"])).fetchall()
    db.execute("UPDATE applications SET st='Rejected' WHERE task_id=? AND id<>?",
               (task["id"], application["id"]))
    db.execute("UPDATE applications SET st='Accepted' WHERE id=?", (application["id"],))
    cur = db.execute("""UPDATE tasks SET helper_id=?, status='Assigned', budget=?
                        WHERE id=? AND status='Open'""",
                     (application["user_id"], application["price"], task["id"]))
    if cur.rowcount == 0:
        db.rollback()
        raise ApiError("Task no longer open.", 409)
    history(db, task["id"], "Assigned", g.user["id"])
    for other in others:
        notify(db, other["user_id"], f'Another helper was chosen for "{task["title"]}"', link)
    notify(db, application["user_id"], f'Your application was accepted: "{task["title"]}"', link)
    db.execute("INSERT INTO messages (task_id,from_id,to_id,txt,created_at) VALUES (?,?,?,?,?)",
               (task["id"], g.user["id"], application["user_id"],
                f'Hi! I accepted your application for "{task["title"]}". '
                "Let's confirm the details here.", now_ms()))
    db.commit()
    return ok()


@app.route("/api/apps/<app_ref>/reject", methods=["POST"])
@login_required
def reject_application(app_ref):
    db = get_db()
    application, task = get_app_for_owner(db, app_ref)
    if application["st"] != "Pending":
        raise ApiError("This application was already handled.", 409)
    db.execute("UPDATE applications SET st='Rejected' WHERE id=?", (application["id"],))
    notify(db, application["user_id"], f'Your application was not selected: "{task["title"]}"',
           "#/task/" + tid(task["id"]))
    db.commit()
    return ok()


@app.route("/api/tasks/<task_ref>/status", methods=["POST"])
@login_required
def change_status(task_ref):
    data = body()
    new_status = s(data, "s")
    db = get_db()
    task = get_task(db, task_ref)
    me_id = g.user["id"]
    allowed = (
        (new_status == "In Progress" and task["status"] == "Assigned" and task["helper_id"] == me_id)
        or (new_status == "Completed" and task["status"] == "In Progress"
            and me_id in (task["user_id"], task["helper_id"]))
    )
    if not allowed:
        raise ApiError("That change is not allowed right now.", 409)
    db.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task["id"]))
    history(db, task["id"], new_status, me_id)
    other = task["helper_id"] if task["user_id"] == me_id else task["user_id"]
    text = (f'"{task["title"]}" was marked completed. Please leave a rating.'
            if new_status == "Completed" else f'"{task["title"]}" is now in progress')
    notify(db, other, text, "#/task/" + tid(task["id"]))
    db.commit()
    return ok()


@app.route("/api/tasks/<task_ref>/cancel", methods=["POST"])
@login_required
def cancel_task(task_ref):
    db = get_db()
    task = get_task(db, task_ref)
    if task["user_id"] != g.user["id"] or task["status"] not in ("Open", "Assigned"):
        raise ApiError("This task cannot be cancelled.", 409)
    db.execute("UPDATE tasks SET status='Cancelled' WHERE id=?", (task["id"],))
    history(db, task["id"], "Cancelled", g.user["id"])
    notify(db, task["helper_id"], f'"{task["title"]}" was cancelled', "#/task/" + tid(task["id"]))
    db.commit()
    return ok()


@app.route("/api/tasks/<task_ref>/review", methods=["POST"])
@login_required
def review_task(task_ref):
    data = body()
    db = get_db()
    task = get_task(db, task_ref)
    me_id = g.user["id"]
    if task["status"] not in ("Completed", "Reviewed") or me_id not in (task["user_id"], task["helper_id"]):
        raise ApiError("Not allowed.", 403)
    if db.execute("SELECT 1 FROM reviews WHERE task_id=? AND from_id=?", (task["id"], me_id)).fetchone():
        raise ApiError("You already reviewed this task.", 409)
    stars = min(5, max(1, num(data, "n")))
    to_id = task["helper_id"] if task["user_id"] == me_id else task["user_id"]
    db.execute("INSERT INTO reviews (task_id,from_id,to_id,n,txt,created_at) VALUES (?,?,?,?,?,?)",
               (task["id"], me_id, to_id, stars, s(data, "txt", 1000), now_ms()))
    if task["user_id"] == me_id:
        db.execute("UPDATE tasks SET status='Reviewed' WHERE id=?", (task["id"],))
        history(db, task["id"], "Reviewed", me_id)
    notify(db, to_id, f"You received a new {stars}-star rating", "#/profile/" + uid(to_id))
    db.commit()
    return ok()


# ================= MESSAGES & NOTIFICATIONS =================

@app.route("/api/messages", methods=["POST"])
@login_required
def send_message():
    data = body()
    txt = s(data, "txt", 2000)
    if not txt:
        raise ApiError("Message is empty.")
    db = get_db()
    task = get_task(db, s(data, "task"))
    to_id = pid(s(data, "to"), "u")
    me_id = g.user["id"]
    if to_id == me_id:
        raise ApiError("Not allowed.", 403)

    # Only the task owner and a helper who applied / was assigned may talk.
    owner = task["user_id"]
    helper_side = to_id if me_id == owner else me_id
    if owner not in (me_id, to_id):
        raise ApiError("Not allowed.", 403)
    involved = task["helper_id"] == helper_side or db.execute(
        "SELECT 1 FROM applications WHERE task_id=? AND user_id=?", (task["id"], helper_side)).fetchone()
    if not involved:
        raise ApiError("Not allowed.", 403)
    if db.execute("SELECT 1 FROM blocks WHERE by_id=? AND user_id=?", (to_id, me_id)).fetchone():
        raise ApiError("You cannot message this user.", 403)

    db.execute("INSERT INTO messages (task_id,from_id,to_id,txt,created_at) VALUES (?,?,?,?,?)",
               (task["id"], me_id, to_id, txt, now_ms()))
    notify(db, to_id, f'New message from {g.user["name"]}',
           f"#/msgs/{tid(task['id'])}/{uid(helper_side)}")
    db.commit()
    return ok()


@app.route("/api/messages/read", methods=["POST"])
@login_required
def read_messages():
    data = body()
    db = get_db()
    db.execute("UPDATE messages SET is_read=1 WHERE task_id=? AND to_id=?",
               (pid(s(data, "task"), "T-"), g.user["id"]))
    db.commit()
    return ok()


@app.route("/api/notifs/<notif_ref>/read", methods=["POST"])
@login_required
def read_notification(notif_ref):
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
               (pid(notif_ref, "n"), g.user["id"]))
    db.commit()
    return ok()


@app.route("/api/notifs/read-all", methods=["POST"])
@login_required
def read_all_notifications():
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
    db.commit()
    return ok()


# ================= SAFETY =================

@app.route("/api/reports", methods=["POST"])
@login_required
def create_report():
    data = body()
    kind = s(data, "type")
    target = s(data, "target", 40)
    why = s(data, "why", 1000)
    if kind not in ("task", "user") or not target or not why:
        raise ApiError("Please describe the problem.")
    db = get_db()
    db.execute("INSERT INTO reports (type,target,why,by_id,created_at) VALUES (?,?,?,?,?)",
               (kind, target, why, g.user["id"], now_ms()))
    db.commit()
    return ok()


@app.route("/api/blocks", methods=["POST"])
@login_required
def block_user():
    data = body()
    other = pid(s(data, "u"), "u")
    if other == g.user["id"]:
        raise ApiError("You cannot block yourself.")
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=?", (other,)).fetchone():
        raise ApiError("User not found.", 404)
    db.execute("INSERT OR IGNORE INTO blocks (by_id,user_id) VALUES (?,?)", (g.user["id"], other))
    db.commit()
    return ok()


# ================= ADMIN =================

@app.route("/api/admin/users/<user_ref>/suspend", methods=["POST"])
@admin_required
def toggle_suspend(user_ref):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (pid(user_ref, "u"),)).fetchone()
    if not user:
        raise ApiError("User not found.", 404)
    if "admin" in roles_of(user):
        raise ApiError("Admins cannot be suspended.", 403)
    db.execute("UPDATE users SET susp=? WHERE id=?", (0 if user["susp"] else 1, user["id"]))
    db.commit()
    return ok()


@app.route("/api/admin/tasks/<task_ref>/remove", methods=["POST"])
@admin_required
def remove_task(task_ref):
    db = get_db()
    task = get_task(db, task_ref)
    db.execute("UPDATE tasks SET removed=1 WHERE id=?", (task["id"],))
    db.execute("UPDATE reports SET done=1 WHERE type='task' AND target=?", (tid(task["id"]),))
    db.commit()
    return ok()


@app.route("/api/admin/reports/<report_ref>/done", methods=["POST"])
@admin_required
def report_done(report_ref):
    db = get_db()
    db.execute("UPDATE reports SET done=1 WHERE id=?", (pid(report_ref, "p"),))
    db.commit()
    return ok()


# ================= RUN =================

init_db()

if __name__ == "__main__":
    print("\nTaskHub running at http://127.0.0.1:5000\n")
    app.run(debug=DEBUG)
