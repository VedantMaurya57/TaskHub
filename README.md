# TaskHub

A local task marketplace for Mumbai. Customers post tasks, helpers apply, the
customer picks one, and both sides can message and rate each other. Admins can
moderate users, tasks and reports.

- Backend: Flask + SQLite (`app.py`), JSON API under `/api/`
- Frontend: single page app in `templates/index.html` that talks to the API

## Run locally

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
FLASK_DEBUG=1 SEED_DEMO=1 python app.py
```

Open http://127.0.0.1:5000. With `SEED_DEMO=1` you can log in as
`aarav@demo.in` (customer) or `meera@demo.in` (helper), password `demo1234`.

## Deploy to a server

1. Set these environment variables in your host (see `.env.example`):

   | Variable | What it does |
   |---|---|
   | `SECRET_KEY` | Required. Long random string: `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `DATABASE_PATH` | Where the SQLite file lives. Must be on a persistent disk, e.g. `/data/taskhub.db` |
   | `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Creates your admin account on startup (password 8+ chars) |
   | `SECURE_COOKIES` | `1` when the site is served over HTTPS (default). Set `0` only for plain HTTP testing |
   | `SEED_DEMO` | `1` to add demo users and tasks. Leave `0` for the real site |
   | `FLASK_DEBUG` | Leave unset or `0` on a server |

2. Start command (already in `Procfile`):

   ```bash
   gunicorn app:app --preload --workers 2 --bind 0.0.0.0:$PORT
   ```

3. Health check URL: `/healthz`

Notes:
- SQLite works well for a small site on one server. Platforms that wipe the
  disk on every deploy (for example free tiers without a volume) will lose the
  database, so attach a persistent disk or move to PostgreSQL later.
- Put the app behind HTTPS (most hosts do this for you).
