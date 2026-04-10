from fastapi import APIRouter, Depends, HTTPException
from database import get_db
from auth_utils import get_current_user
from datetime import datetime, timezone

router = APIRouter()

def _fmt(m: dict) -> dict:
    """Formats MongoDB document for the Frontend JS."""
    return {
        "id": str(m["_id"]),
        "lead_snapshot": m.get("lead_snapshot", {}),
        "title": m.get("title", "Untitled"),
        "scheduled_at": m["scheduled_at"].isoformat() if m.get("scheduled_at") else None,
        "duration_minutes": m.get("duration_minutes", 30),
        "status": m.get("status", "scheduled"),
        "notes": m.get("notes", ""),
        "meeting_link": m.get("meeting_link")
    }

@router.get("/")
async def list_meetings(current_user=Depends(get_current_user), db=Depends(get_db)):
    uid = str(current_user["_id"])
    meetings = await db.meetings.find({"user_id": uid}).sort("scheduled_at", 1).to_list(100)
    return [_fmt(m) for m in meetings]

@router.get("/stats")
async def meeting_stats(current_user=Depends(get_current_user), db=Depends(get_db)):
    uid = str(current_user["_id"])
    return {
        "total": await db.meetings.count_documents({"user_id": uid}),
        "upcoming": await db.meetings.count_documents({"user_id": uid, "status": "scheduled"}),
        "completed": await db.meetings.count_documents({"user_id": uid, "status": "completed"}),
        "no_show": await db.meetings.count_documents({"user_id": uid, "status": "no_show"})
    }