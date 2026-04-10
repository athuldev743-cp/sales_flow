-- billing/migrations.sql
-- Run once against salesflow_leads.db to add billing tables

CREATE TABLE IF NOT EXISTS user_billing (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL UNIQUE,
    plan          TEXT    NOT NULL DEFAULT 'free',   -- free | pro | premium
    plan_expires  TEXT,                               -- ISO datetime
    credits       REAL    NOT NULL DEFAULT 0,         -- ₹ credits (premium)
    cancelled_at  TEXT,                               -- set when user cancels
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL UNIQUE,
    last4            TEXT,
    network          TEXT,
    name             TEXT,
    exp_month        TEXT,
    exp_year         TEXT,
    razorpay_token   TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL,
    date                 TEXT    NOT NULL,
    description          TEXT,
    amount               INTEGER NOT NULL,            -- ₹ (not paise)
    status               TEXT    NOT NULL DEFAULT 'paid',
    razorpay_payment_id  TEXT,
    pdf_url              TEXT
);

-- Optional: track per-user Groq API call counts for Premium billing
CREATE TABLE IF NOT EXISTS groq_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, date)
);

-- Optional: email send log (used for daily email usage counter)
CREATE TABLE IF NOT EXISTS email_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    sent_at  TEXT    NOT NULL
);