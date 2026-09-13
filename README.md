# CONNECT

A full-stack Flask wellness app for **spiritual, mental, and emotional** well-being — featuring live Panchang data, AI-powered journal analysis, mood trend analytics, and user authentication.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask 3.0 |
| Auth | Flask-Login, bcrypt |
| Database | MySQL 8 with connection pooling |
| AI chat | Groq (Llama-3.1-8B) with a crisis-keyword safety check |
| Panchang API | Prokerala v2 with mock fallback |
| Rate limiting | Flask-Limiter |
| Deployment | Gunicorn, Docker, Render |

---

## Features

- **Authentication** — email + password registration and login (bcrypt-hashed)
- **Spiritual** — daily deity, mantra with Sanskrit, jaap counter (persisted per user/day), Panchangam (live via Prokerala or weekday mock)
- **Mental** — animated pranayama breath reset (4s inhale / 6s exhale × 4 rounds)
- **Emotional** — mood check-in, a short back-and-forth chat with the AI companion (Groq/Llama-3.1), personalised practice suggestions. Messages are checked for crisis language before being sent to the model, and are answered with a fixed safe response and helpline numbers instead if any is found
- **Dashboard** — 30-day mood trend (stacked bar + doughnut chart via Chart.js)
- **REST API** — `GET /api/guidance/today`, `GET /api/mood/history`

---

## Local setup

### 1. Clone and install

```bash
git clone <your-repo>
cd flask_connect
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set MYSQL_PASSWORD
```

Optional API keys (app works without them — graceful fallback):
- `ANTHROPIC_API_KEY` — enables Claude Haiku journal analysis
- `PROKERALA_CLIENT_ID` + `PROKERALA_CLIENT_SECRET` — enables live Panchang ([sign up](https://api.prokerala.com/))

### 3. Create the database

```bash
python init_db.py
```

### 4. Run

```bash
flask --app app run --debug
```

Open `http://127.0.0.1:5000`, register an account, and start.

---

## Running tests

```bash
pytest tests/ -v
```

Tests cover: journal keyword analysis, Claude fallback, grounding scoring, spiritual API weekday mapping and override logic, and store input validation — all without a real database or API key.

---

## Docker

```bash
docker build -t connect-app .
docker run -p 8000:8000 --env-file .env connect-app
```

---

## Deploy to Render

1. Push to GitHub.
2. Create a new **Web Service** on [Render](https://render.com/) pointing at your repo.
3. Render auto-detects `render.yaml` — fill in the secret env vars in the dashboard.
4. Connect a MySQL database (PlanetScale free tier works well).
5. Run `python init_db.py` once via Render Shell to create tables.

---

## Security notes

- Passwords are bcrypt-hashed; never stored in plaintext.
- Mood allowlist and journal 2 000-char cap enforced server-side.
- Rate limiting: 20/hour on login, 10/hour on register, 120/min on jaap writes.
- Set `LIMITER_STORAGE_URI=redis://...` in production for distributed rate limiting.
- Add HTTPS termination at the reverse proxy layer before exposing to the internet.
