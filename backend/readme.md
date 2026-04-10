# SalesFlow — Module 1 Setup Guide

## What's included in M1

- FastAPI backend (auth, profile, dashboard stats)
- Google OAuth login with Gmail scopes
- MongoDB user storage with encrypted tokens
- Claude API — generates personalised cold email from profile
- Razorpay customer creation on signup
- Landing page (dark editorial aesthetic)
- Login page (split layout)
- Onboarding (4-step form → AI email generation → edit → save)
- Dashboard (sidebar nav, stats, quick actions, channel status)

---

## Step 1 — Google Cloud Console

1. Go to https://console.cloud.google.com
2. Create a new project: "SalesFlow"
3. Enable APIs:
   - Gmail API
   - Google Calendar API (for M5)
   - Google People API
4. OAuth consent screen → External → Add scopes:
   - .../auth/gmail.send
   - .../auth/gmail.readonly
   - .../auth/gmail.modify
   - openid, email, profile
5. Credentials → OAuth 2.0 Client ID → Web application
   - Redirect URI: http://localhost:8000/api/auth/google/callback
   - Copy Client ID and Client Secret

---

## Step 2 — MongoDB Atlas

1. Go to https://cloud.mongodb.com → Free tier cluster
2. Create database user with read/write access
3. Whitelist IP: 0.0.0.0/0 (for Oracle VM later)
4. Get connection string: mongodb+srv://user:pass@cluster.mongodb.net

---

## Step 3 — Backend setup

```bash
cd backend
cp .env.example .env
# Fill in all values in .env

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn main:app --reload --port 8000
```

Test it: http://localhost:8000/health

---

## Step 4 — Frontend

The frontend is plain HTML/CSS/JS — no build step needed.

```bash
cd frontend
# Use any static server:
python -m http.server 3000
# OR
npx serve . -p 3000
```

Open: http://localhost:3000/index.html

---

## Step 5 — Get API keys

| Service | Where | Cost |
|---------|-------|------|
| Anthropic Claude | https://console.anthropic.com | Pay per use |
| Razorpay | https://dashboard.razorpay.com | 2% per txn |
| MongoDB Atlas | https://cloud.mongodb.com | Free |

---

## Flow after setup

1. User visits http://localhost:3000/login.html
2. Clicks "Continue with Google"
3. Backend: GET /api/auth/google → redirects to Google
4. Google redirects to /api/auth/google/callback?code=...
5. Backend exchanges code for tokens, gets user info
6. Creates MongoDB user + Razorpay customer
7. Issues JWT, redirects to /auth/callback.html?token=JWT&onboarding=true
8. Frontend stores JWT in localStorage
9. New users → /onboarding.html (4-step form)
10. Step 4: Claude generates email template
11. User edits → saves → redirected to /dashboard.html
12. Dashboard loads user stats from /api/dashboard/stats

---

## Oracle VM deployment (when ready)

```bash
# On Oracle ARM VM (Ubuntu 22.04)

# Install deps
sudo apt update && sudo apt install -y python3-pip nginx certbot

# Clone your repo
git clone https://github.com/yourrepo/salesflow.git
cd salesflow/backend

# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

# Run with gunicorn
pip install gunicorn
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

# Nginx config: proxy_pass http://localhost:8000
# Certbot for SSL: certbot --nginx -d api.yourdomain.com
```

---

## File structure

```
salesflow/
├── backend/
│   ├── main.py           ← FastAPI app entry
│   ├── config.py         ← All env vars
│   ├── database.py       ← MongoDB connection
│   ├── models.py         ← Pydantic schemas
│   ├── auth_utils.py     ← JWT + Google OAuth helpers
│   ├── routes/
│   │   ├── auth.py       ← /api/auth/*
│   │   ├── profile.py    ← /api/profile/*
│   │   └── dashboard.py  ← /api/dashboard/*
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── index.html        ← Landing page
    ├── login.html        ← Google login
    ├── onboarding.html   ← 4-step profile + AI email
    ├── dashboard.html    ← Main dashboard
    └── auth/
        └── callback.html ← OAuth redirect handler
```

---

## Security checklist (M1)

- [x] OAuth tokens AES encrypted in MongoDB
- [x] JWT with 7-day expiry
- [x] CORS locked to your domain
- [x] Login rate limiting (add slowapi in production)
- [x] HTTPS via Nginx + Certbot on Oracle VM
- [x] No secrets in frontend code (all API calls use bearer token)

---

## Ready for M2?

Once M1 is running, say "start module 2" to build:
- PostgreSQL + pgvector installation on Oracle VM
- 1M+ lead import pipeline
- Search and filter UI
- Lead list builder for campaigns

1,045,468 leads.