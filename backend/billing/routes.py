"""
backend/billing/routes.py
Billing & Razorpay payment routes for SalesFlow
"""

import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

import razorpay
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth_utils import get_current_user   # reuse existing JWT helper
from database import get_db               # reuse existing DB helper

logger = logging.getLogger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Config  (loaded from .env via config.py – add these keys there)
# ─────────────────────────────────────────────────────────────────────────────
from config import settings   # expects settings.RAZORPAY_KEY_ID / SECRET

rzp_client = razorpay.Client(
    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
)

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    plan: str          # "pro" | "premium"
    amount: int        # amount in paise (e.g. 99900 for ₹999)

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan: str

class TopupRequest(BaseModel):
    amount: int        # paise

class SaveCardRequest(BaseModel):
    payment_id: str

class CardSetupOrderResponse(BaseModel):
    order_id: str

# ─────────────────────────────────────────────────────────────────────────────
# Plan definitions (source of truth – mirrors frontend PLANS const)
# ─────────────────────────────────────────────────────────────────────────────

PLAN_CONFIG = {
    "free":    {"price": 0,    "leads": 50,   "emails_per_day": 10,  "campaigns": 1},
    "pro":     {"price": 999,  "leads": 5000, "emails_per_day": 500, "campaigns": 20},
    "premium": {"price": 1999, "leads": 0,    "emails_per_day": 0,   "campaigns": 0},
}

# ─────────────────────────────────────────────────────────────────────────────
# Helper: verify Razorpay signature
# ─────────────────────────────────────────────────────────────────────────────

def _verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    payload = f"{order_id}|{payment_id}"
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# ─────────────────────────────────────────────────────────────────────────────
# Helper: upsert billing row in SQLite
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_billing_row(db, user_id: int):
    db.execute(
        """
        INSERT OR IGNORE INTO user_billing
          (user_id, plan, credits, created_at)
        VALUES (?, 'free', 0, ?)
        """,
        (user_id, datetime.utcnow().isoformat()),
    )
    db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# GET /api/billing/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def billing_status(user=Depends(get_current_user), db=Depends(get_db)):
    _ensure_billing_row(db, user.id)

    row = db.execute(
        "SELECT * FROM user_billing WHERE user_id = ?", (user.id,)
    ).fetchone()

    # Usage counters
    usage = {
        "leads":     db.execute("SELECT COUNT(*) FROM leads WHERE user_id = ?", (user.id,)).fetchone()[0],
        "emails":    db.execute(
                         "SELECT COUNT(*) FROM email_logs WHERE user_id = ? AND DATE(sent_at) = DATE('now')",
                         (user.id,)
                     ).fetchone()[0],
        "campaigns": db.execute("SELECT COUNT(*) FROM campaigns WHERE user_id = ?", (user.id,)).fetchone()[0],
        "groq_calls": db.execute(
                          "SELECT COALESCE(SUM(call_count), 0) FROM groq_usage WHERE user_id = ?",
                          (user.id,)
                      ).fetchone()[0],
    }

    # Invoices
    invoices = [
        dict(i) for i in db.execute(
            "SELECT * FROM invoices WHERE user_id = ? ORDER BY date DESC LIMIT 20",
            (user.id,)
        ).fetchall()
    ]

    # Saved card (masked)
    card_row = db.execute(
        "SELECT * FROM saved_cards WHERE user_id = ?", (user.id,)
    ).fetchone()
    card = dict(card_row) if card_row else None

    return {
        "plan":         row["plan"],
        "plan_expires": row["plan_expires"],
        "credits":      float(row["credits"] or 0),
        "usage":        usage,
        "invoices":     invoices,
        "card":         card,
    }

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/create-order   (step 1 of checkout)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/create-order")
async def create_order(body: CreateOrderRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if body.plan not in ("pro", "premium"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    expected_amount = PLAN_CONFIG[body.plan]["price"] * 100
    if body.amount != expected_amount:
        raise HTTPException(status_code=400, detail="Amount mismatch")

    try:
        order = rzp_client.order.create({
            "amount":   body.amount,
            "currency": "INR",
            "receipt":  f"sf_{user.id}_{body.plan}_{int(datetime.utcnow().timestamp())}",
            "notes":    {"user_id": str(user.id), "plan": body.plan},
        })
        return {"id": order["id"], "amount": order["amount"], "currency": order["currency"]}
    except Exception as e:
        logger.error("Razorpay order creation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error")

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/verify-payment   (step 2 of checkout)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/verify-payment")
async def verify_payment(body: VerifyPaymentRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if not _verify_signature(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(status_code=400, detail="Signature verification failed")

    if body.plan not in ("pro", "premium"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    _ensure_billing_row(db, user.id)

    # Set plan + expiry (30 days)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
    db.execute(
        "UPDATE user_billing SET plan = ?, plan_expires = ? WHERE user_id = ?",
        (body.plan, expires, user.id),
    )

    # Record invoice
    plan_cfg = PLAN_CONFIG[body.plan]
    db.execute(
        """
        INSERT INTO invoices (user_id, date, description, amount, status, razorpay_payment_id)
        VALUES (?, ?, ?, ?, 'paid', ?)
        """,
        (
            user.id,
            datetime.utcnow().isoformat(),
            f"SalesFlow {body.plan.title()} plan – 1 month",
            plan_cfg["price"],
            body.razorpay_payment_id,
        ),
    )
    db.commit()

    return {"success": True, "plan": body.plan, "expires": expires}

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/topup   (Premium credit top-up)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/topup/create-order")
async def topup_create_order(body: TopupRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if body.amount not in (20000, 50000, 100000, 200000):   # ₹200/500/1000/2000
        raise HTTPException(status_code=400, detail="Invalid topup amount")

    try:
        order = rzp_client.order.create({
            "amount":   body.amount,
            "currency": "INR",
            "receipt":  f"sf_topup_{user.id}_{int(datetime.utcnow().timestamp())}",
            "notes":    {"user_id": str(user.id), "type": "credit_topup"},
        })
        return {"id": order["id"]}
    except Exception as e:
        logger.error("Razorpay topup order failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error")


@router.post("/topup/verify")
async def topup_verify(body: VerifyPaymentRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if not _verify_signature(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(status_code=400, detail="Signature verification failed")

    # Fetch payment amount from Razorpay to confirm
    try:
        payment = rzp_client.payment.fetch(body.razorpay_payment_id)
        credited_inr = payment["amount"] / 100          # convert paise → ₹
    except Exception:
        raise HTTPException(status_code=502, detail="Could not verify payment amount")

    _ensure_billing_row(db, user.id)
    db.execute(
        "UPDATE user_billing SET credits = credits + ? WHERE user_id = ?",
        (credited_inr, user.id),
    )
    db.execute(
        """
        INSERT INTO invoices (user_id, date, description, amount, status, razorpay_payment_id)
        VALUES (?, ?, ?, ?, 'paid', ?)
        """,
        (
            user.id,
            datetime.utcnow().isoformat(),
            f"SalesFlow credit top-up",
            int(credited_inr),
            body.razorpay_payment_id,
        ),
    )
    db.commit()
    return {"success": True, "credited": credited_inr}

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/card-setup-order   (₹1 auth charge to save card)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/card-setup-order")
async def card_setup_order(user=Depends(get_current_user), db=Depends(get_db)):
    try:
        order = rzp_client.order.create({
            "amount":   100,      # ₹1 authorisation
            "currency": "INR",
            "receipt":  f"sf_card_{user.id}_{int(datetime.utcnow().timestamp())}",
        })
        return {"order_id": order["id"]}
    except Exception as e:
        logger.error("Card setup order failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment gateway error")


@router.post("/save-card")
async def save_card(body: SaveCardRequest, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        payment = rzp_client.payment.fetch(body.payment_id)
        card_data = payment.get("card", {})
    except Exception:
        raise HTTPException(status_code=502, detail="Could not fetch payment details")

    db.execute("DELETE FROM saved_cards WHERE user_id = ?", (user.id,))
    db.execute(
        """
        INSERT INTO saved_cards (user_id, last4, network, name, exp_month, exp_year, razorpay_token)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            card_data.get("last4", "****"),
            card_data.get("network", "CARD"),
            card_data.get("name", ""),
            card_data.get("expiry_month", ""),
            card_data.get("expiry_year", ""),
            payment.get("token_id", ""),
        ),
    )
    db.commit()
    return {"success": True}

# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/billing/remove-card
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/remove-card")
async def remove_card(user=Depends(get_current_user), db=Depends(get_db)):
    db.execute("DELETE FROM saved_cards WHERE user_id = ?", (user.id,))
    db.commit()
    return {"success": True}

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/cancel
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(user=Depends(get_current_user), db=Depends(get_db)):
    _ensure_billing_row(db, user.id)
    # Keep plan active until expiry but mark as cancelled so it won't renew
    db.execute(
        "UPDATE user_billing SET cancelled_at = ? WHERE user_id = ?",
        (datetime.utcnow().isoformat(), user.id),
    )
    db.commit()
    return {"success": True}

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/billing/downgrade
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/downgrade")
async def downgrade_to_free(user=Depends(get_current_user), db=Depends(get_db)):
    _ensure_billing_row(db, user.id)
    db.execute(
        "UPDATE user_billing SET plan = 'free', plan_expires = NULL WHERE user_id = ?",
        (user.id,),
    )
    db.commit()
    return {"success": True}