import asyncio
import motor.motor_asyncio
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

from config import settings
from replies.routes import _sync_user_replies
from leads.lead_db import Lead, SessionLocal

def is_meeting_confirmation(text):
    if not text: return False
    triggers = ["confirm the meeting", "scheduled for", "calendar invite", "See you on", "Great to confirm"]
    return any(phrase.lower() in text.lower() for phrase in triggers)

async def auto_create_meeting(db, user_id, reply_data):
    email = reply_data.get("from_email")
    
    # 🛑 Anti-duplicate check
    existing = await db.meetings.find_one({
        "user_id": str(user_id),
        "lead_snapshot.email": email,
        "status": "scheduled"
    })
    if existing: return None

    session = SessionLocal()
    lead = session.query(Lead).filter(Lead.email == email).first()
    
    lead_snapshot = {
        "lead_id": lead.id if lead else None,
        "company_name": lead.company_name if lead else "Prospect",
        "contact_name": lead.contact_name if lead else "Lead",
        "email": email,
        "city": lead.city if lead else "",
        "state": lead.state if lead else ""
    }
    session.close()

    # Default to 2 days out
    meeting_date = datetime.utcnow() + timedelta(days=2)
    meeting_date = meeting_date.replace(hour=10, minute=0, second=0, microsecond=0)

    doc = {
        "user_id": str(user_id),
        "lead_snapshot": lead_snapshot,
        "title": f"Bot Arranged: {lead_snapshot['company_name']}",
        "scheduled_at": meeting_date,
        "duration_minutes": 30,
        "notes": "Automated meeting confirmed by AI chatbot.",
        "status": "scheduled",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = await db.meetings.insert_one(doc)
    return result.inserted_id

async def main():
    client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB_NAME]
    
    # Acts on the first connected user found
    user = await db.users.find_one({"gmail_connected": True})
    if not user:
        print("❌ No user found.")
        return
        
    uid = str(user["_id"])
    seen_ids = set()

    print(f"🚀 Watcher active. Monitoring: {user['email']}")

    while True:
        try:
            await _sync_user_replies(user, db)
            replies = await db.replies.find({"user_id": uid}).to_list(50)
            
            for r in replies:
                mid = r.get("message_id")
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    if is_meeting_confirmation(r.get("sent_body")):
                        m_id = await auto_create_meeting(db, uid, r)
                        if m_id:
                            print(f"📅 SUCCESS: Meeting booked for {r['from_email']}")
        except Exception as e:
            print(f"⚠️ Error: {e}")
        await asyncio.sleep(30)
 
import asyncio, motor.motor_asyncio, sys, os
from datetime import datetime
sys.path.insert(0, os.getcwd())
from config import settings
from meetings.utils import auto_create_meeting
 
async def backfill_existing_meeting():
    """
    One-time script: finds replies already classified as meeting_request
    that don't yet have a meeting record, and creates them.
    Run once, then delete this file.
    """
    client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB_NAME]
 
    users = await db.users.find({"gmail_connected": True}).to_list(100)
    total = 0
 
    for user in users:
        uid = str(user["_id"])
        # Find all meeting_request replies that have no corresponding meeting
        meeting_replies = await db.replies.find({
            "user_id":        uid,
            "classification": "meeting_request",
        }).to_list(200)
 
        for r in meeting_replies:
            email = (r.get("from_email") or "").lower().strip()
            # Check if a meeting already exists
            existing = await db.meetings.find_one({
                "user_id":             uid,
                "lead_snapshot.email": email,
            })
            if existing:
                continue  # already has one
 
            mid = await auto_create_meeting(
                db=db,
                user_id=uid,
                reply_data={
                    "from_email": email,
                    "thread_id":  r.get("thread_id"),
                },
            )
            if mid:
                print(f"✅ Backfilled meeting {mid} for {email}")
                total += 1
 
    print(f"\nDone. {total} meetings backfilled.")
    client.close()        


        

if __name__ == "__main__":
    asyncio.run(backfill_existing_meeting())
