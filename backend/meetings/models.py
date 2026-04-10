from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime


class MeetingCreate(BaseModel):
    lead_id: int
    title: str
    scheduled_at: datetime
    duration_minutes: int = 30
    notes: Optional[str] = None
    meeting_link: Optional[str] = None


class MeetingUpdate(BaseModel):
    title: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    notes: Optional[str] = None
    meeting_link: Optional[str] = None
    status: Optional[Literal["scheduled", "completed", "no_show", "cancelled", "rescheduled"]] = None
    outcome: Optional[str] = None