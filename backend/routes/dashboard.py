from fastapi import APIRouter, Depends
from database import get_db
from auth_utils import get_current_user
from models import DashboardStats
from bson import ObjectId

router = APIRouter()


@router.get("/stats", response_model=DashboardStats)
async def get_stats(current_user=Depends(get_current_user), db=Depends(get_db)):
    user_id = str(current_user["_id"])

    # Campaign count
    campaigns = await db.campaigns.count_documents({"user_id": user_id})

    # ✅ Optimized emails_sent using MongoDB aggregation
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "total": {"$sum": "$sent"}}}
    ]
    result = await db.campaigns.aggregate(pipeline).to_list(1)
    emails_sent = result[0]["total"] if result else 0

    # Other stats
    replies = await db.emails.count_documents({"user_id": user_id, "replied": True})
    hot_leads = await db.leads.count_documents({"user_id": user_id, "status": "hot"})
    meetings = await db.meetings.count_documents({"user_id": user_id})
    calls = await db.calls.count_documents({"user_id": user_id})
    total_leads = await db.leads.count_documents({"user_id": user_id})

    return DashboardStats(
        leads_total=total_leads,
        emails_sent=emails_sent,
        replies_received=replies,
        meetings_booked=meetings,
        hot_leads=hot_leads,
        calls_made=calls,
    )