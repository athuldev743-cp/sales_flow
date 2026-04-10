from pydantic import BaseModel, EmailStr, Field, HttpUrl
from typing import Optional, Literal, List
from datetime import datetime

# ─── User ────────────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    # --- Step 1: Personal & Localization ---
    full_name: str
    sender_role: Optional[str] = None
    website: Optional[str] = None
    language_region: str = "English, India"
    brand_color: str = "#7c6dfa"

    # --- Step 2: Company & Value ---
    company_name: str
    company_description: str
    industries_served: Optional[str] = None # Using str to match ProfileSubmit
    value_proposition: Optional[str] = None

    # --- Step 3: ICP & Goals ---
    target_audience: str
    company_size: Optional[str] = None
    job_titles: Optional[str] = None
    purpose: str
    goal: str
    desired_cta: str = "Book a 20-minute discovery call"

    # --- Step 4: Sales Intelligence ---
    proof_points: Optional[str] = None
    pricing_model: Optional[str] = None       # Aligned with ProfileSubmit
    contract_type: Optional[str] = None
    objection_handling: Optional[str] = None  # Aligned with ProfileSubmit
    competitors: Optional[str] = None
    differentiators: Optional[str] = None
    tone_preference: str = "Professional and helpful"
    never_say: Optional[str] = None
    custom_rules: Optional[str] = None

class UserCreate(BaseModel):
    email: EmailStr
    google_id: str
    full_name: str
    avatar_url: Optional[str] = None
    google_access_token: str
    google_refresh_token: Optional[str] = None

class UserInDB(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    email: str
    google_id: str
    full_name: str
    avatar_url: Optional[str] = None
    google_access_token: str
    google_refresh_token: Optional[str] = None
    profile: Optional[UserProfile] = None
    ai_email_template: Optional[str] = None   # Claude-generated email
    plan: Literal["free", "starter", "growth", "pro"] = "free"
    razorpay_customer_id: Optional[str] = None
    gmail_connected: bool = False
    whatsapp_connected: bool = False
    onboarding_complete: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True

class UserPublic(BaseModel):
    id: str
    email: str
    full_name: str
    avatar_url: Optional[str] = None
    profile: Optional[UserProfile] = None
    ai_email_template: Optional[str] = None
    plan: str
    gmail_connected: bool
    whatsapp_connected: bool
    onboarding_complete: bool

# ─── Auth ────────────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic

# ─── Profile ─────────────────────────────────────────────────────────────────

# backend/models.py

class ProfileSubmit(BaseModel):
    # ── Step 1: Identity ──────────────────────────────────────────────────────
    full_name:           str
    sender_role:         Optional[str] = None   # "Founder", "Head of Sales"
    website:             Optional[str] = None
    language_region:     Optional[str] = "English, India"
    brand_color:         Optional[str] = "#7c6dfa" # Keep this to prevent UI errors

    # ── Step 2: Company ───────────────────────────────────────────────────────
    company_name:        str
    company_description: str
    industries_served:   Optional[str] = None
    value_proposition:   str                    # CRITICAL: AI uses this for the 'hook'

    # ── Step 3: Audience & Goal ───────────────────────────────────────────────
    target_audience:     str
    company_size:        Optional[str] = None
    job_titles:          Optional[str] = None
    purpose:             str
    goal:                str
    desired_cta:         Optional[str] = "Book a 20-minute discovery call"

    # ── Step 4: Sales Intelligence ────────────────────────────────────────────
    proof_points:        Optional[str] = None   # "Reduced churn by 20% for 50+ clients"
    pricing_model:       Optional[str] = None   # renamed from pricing_info for clarity
    contract_type:       Optional[str] = None
    objection_handling:  Optional[str] = None   # renamed from objection_responses
    competitors:         Optional[str] = None
    differentiators:     Optional[str] = None
    tone_preference:     Optional[str] = "Professional and helpful"
    never_say:           Optional[str] = None   # The 'Guardrails'
    custom_rules:        Optional[str] = None   # Any other specific instructions
    custom_faq:          Optional[str] = None

class EmailTemplateUpdate(BaseModel):
    ai_email_template: str

# ─── Dashboard ───────────────────────────────────────────────────────────────

class DashboardStats(BaseModel):
    leads_total: int = 0
    emails_sent: int = 0
    replies_received: int = 0
    meetings_booked: int = 0
    hot_leads: int = 0
    calls_made: int = 0



