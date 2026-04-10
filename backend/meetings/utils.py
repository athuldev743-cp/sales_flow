"""
meetings/utils.py

Auto-creates a meeting record when the AI chatbot arranges one.
Called from replies/routes.py → process_reply()
"""
import logging
from datetime import datetime, timedelta
from leads.lead_db import Lead, SessionLocal

logger = logging.getLogger(__name__)


async def auto_create_meeting(db, user_id: str, reply_data: dict) -> str | None:
    """
    Creates a meeting document in MongoDB when a chatbot-arranged meeting is detected.

    Triggers:
      - classification == "meeting_request"   (lead explicitly asked for a meeting)
      - decision["action"] == "confirm_and_close"  (lead confirmed a proposed time)

    Returns the inserted ObjectId as string, or None if skipped / duplicate.
    """
    email = (reply_data.get("from_email") or "").lower().strip()
    if not email:
        logger.warning("auto_create_meeting: no from_email — skipped")
        return None

    # ── Anti-duplicate: block ALL statuses, not just "scheduled" ─────────────
    existing = await db.meetings.find_one({
        "user_id":              user_id,
        "lead_snapshot.email":  email,
        "status": {"$in": ["scheduled", "confirmed", "pending", "rescheduled"]},
    })
    if existing:
        logger.info(f"auto_create_meeting: duplicate skipped for {email}")
        return None

    # ── Pull lead details from SQLite ─────────────────────────────────────────
    session = SessionLocal()
    try:
        lead = session.query(Lead).filter(Lead.email == email).first()
        lead_snapshot = {
            "lead_id":      lead.id           if lead else None,
            "company_name": lead.company_name if lead else "Prospect",
            "contact_name": lead.contact_name if lead else "Lead",
            "email":        email,
            "city":         lead.city         if lead else "",
            "state":        lead.state        if lead else "",
        }
    finally:
        session.close()

    # ── Default scheduled time: next business day at 10:00 UTC ───────────────
    meeting_date = datetime.utcnow() + timedelta(days=1)
    # Skip weekends
    while meeting_date.weekday() >= 5:           # 5 = Sat, 6 = Sun
        meeting_date += timedelta(days=1)
    meeting_date = meeting_date.replace(hour=10, minute=0, second=0, microsecond=0)

    doc = {
        "user_id":          user_id,
        "lead_snapshot":    lead_snapshot,
        "title":            f"Bot Arranged: {lead_snapshot['company_name']}",
        "scheduled_at":     meeting_date,
        "duration_minutes": 30,
        "notes":            "Automated meeting confirmed by AI chatbot.",
        "status":           "scheduled",
        "source":           "chatbot",          # lets you filter bot vs manual meetings
        "thread_id":        reply_data.get("thread_id"),
        "created_at":       datetime.utcnow(),
        "updated_at":       datetime.utcnow(),
    }

    result = await db.meetings.insert_one(doc)
    inserted_id = str(result.inserted_id)
    logger.info(
        f"auto_create_meeting: created meeting {inserted_id} for {email} "
        f"(scheduled {meeting_date.date()})"
    )
    return inserted_id