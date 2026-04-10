from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime


class CampaignCreate(BaseModel):
    name:          str
    subject:       str
    body:          str
    lead_ids:      List[int]
    schedule_at:   Optional[str] = None
    personalise:   bool = True
    daily_limit:   int = 50
    send_order:    str = "as_selected"


class CampaignLead(BaseModel):
    lead_id:       int
    company_name:  Optional[str] = None
    contact_name:  Optional[str] = None
    email:         Optional[str] = None
    business_details: Optional[str] = None
    status:        Literal["pending", "sent", "failed", "bounced"] = "pending"
    personalised_subject: Optional[str] = None
    personalised_body:    Optional[str] = None
    sent_at:       Optional[datetime] = None
    error:         Optional[str] = None


class CampaignInDB(BaseModel):
    name:          str
    subject:       str
    body:          str
    user_id:       str
    user_email:    str
    user_name:     str
    total_leads:   int = 0
    sent:          int = 0
    failed:        int = 0
    status:        Literal["draft", "running", "paused", "completed"] = "draft"
    personalise:   bool = True
    leads:         List[CampaignLead] = []
    created_at:    datetime = Field(default_factory=datetime.utcnow)
    updated_at:    datetime = Field(default_factory=datetime.utcnow)
    completed_at:  Optional[datetime] = None


class PreviewRequest(BaseModel):
    subject:          str
    body:             str
    lead_id:          int
    personalise:      bool = True


class CampaignPauseResume(BaseModel):
    action: Literal["pause", "resume"]



class CampaignCreate(BaseModel):
    name:          str
    subject:       str
    body:          str
    lead_ids:      List[int]
    schedule_at:   Optional[str] = None
    personalise:   bool = True
    daily_limit:   int = 50        # ← add this

