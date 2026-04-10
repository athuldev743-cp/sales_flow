import re
import io
import asyncio
import secrets
import pandas as pd
from datetime import datetime
from typing import List, Optional

# FastAPI & Routing
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from bson import ObjectId

# Local Project Imports
from database import get_db
from auth_utils import get_current_user
from config import settings
from leads.lead_db import Lead, SessionLocal, get_lead_db
from campaigns.models import CampaignCreate, CampaignInDB, CampaignLead, PreviewRequest
from campaigns.gmail_sender import send_gmail, refresh_access_token
from campaigns.personaliser import personalise_email

# Optional PDF Support
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

router = APIRouter()

GMAIL_DAILY_LIMIT = 450


def get_lead_info(lead_id: int) -> dict:
    """Fetch lead from SQLite by ID."""
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return None
        email = lead.email
        if email and ',' in email:
            email = email.split(',')[0].strip()
        return {
            "lead_id":          lead.id,
            "company_name":     lead.company_name or "",
            "contact_name":     lead.contact_name or "",
            "email":            email or "",
            "business_details": lead.business_details or "",
        }
    finally:
        db.close()


def _get_sender_profile(user: dict) -> dict:
    """Extract sender name, company, description, brand_color from user dict."""
    profile = user.get("profile") or {}
    import logging
    logging.getLogger(__name__).warning(
        "PROFILE KEYS: %s", list(profile.keys()))
    logging.getLogger(__name__).warning(
        "DESCRIPTION VALUE: %s", profile.get("company_description"))
    return {
        "name":        user.get("full_name", ""),
        "email":       user.get("email", ""),
        "company":     profile.get("company_name", ""),
        "description": profile.get("company_description", ""),
        "brand_color": profile.get("brand_color", "#7c6dfa"),
    }


async def run_campaign(campaign_id: str, user: dict, db):
    """Background task — sends emails one by one respecting Gmail limits."""
    sent_today = 0
    access_token = user.get("google_access_token")
    refresh_token = user.get("google_refresh_token")
    sender = _get_sender_profile(user)

    campaign = await db.campaigns.find_one({"_id": ObjectId(campaign_id)})
    if not campaign:
        return

    unsubscribe_token = secrets.token_urlsafe(16)

    await db.campaigns.update_one(
        {"_id": ObjectId(campaign_id)},
        {"$set": {"status": "running", "updated_at": datetime.utcnow()}}
    )

    leads = campaign.get("leads", [])
    daily_limit = campaign.get("daily_limit", GMAIL_DAILY_LIMIT)

    for i, lead in enumerate(leads):
        if lead.get("status") != "pending":
            continue

        if sent_today >= daily_limit:
            await db.campaigns.update_one(
                {"_id": ObjectId(campaign_id)},
                {"$set": {"status": "paused", "updated_at": datetime.utcnow()}}
            )
            break

        fresh = await db.campaigns.find_one(
            {"_id": ObjectId(campaign_id)}, {"status": 1}
        )
        if fresh and fresh.get("status") == "paused":
            break

        to_email = lead.get("email", "")
        if not to_email:
            await db.campaigns.update_one(
                {"_id": ObjectId(campaign_id)},
                {
                    "$set": {f"leads.{i}.status": "failed",
                             f"leads.{i}.error":  "no_email"},
                    "$inc": {"failed": 1},
                }
            )
            continue

        contact_name = (lead.get("contact_name") or "").strip()
        lead_biz_name = (lead.get("company_name") or "").strip()
        if lead_biz_name.lower() in ("your company", "none", "unknown", ""):
            lead_biz_name = ""

        subject = lead.get(
            "personalised_subject") or campaign.get("subject", "")
        body = lead.get("personalised_body") or campaign.get("body",    "")

        # Personalise on-the-fly if not pre-generated
        if not lead.get("personalised_subject") and campaign.get("personalise"):
            try:
                result = await personalise_email(
                    subject=subject,
                    body=body,
                    lead_name=contact_name,
                    lead_company=lead_biz_name,
                    business_details=lead.get("business_details", ""),
                    sender_name=sender["name"],
                    sender_company=sender["company"],
                    # ← fixed: was missing
                    sender_company_description=sender["description"],
                    lead_email=to_email,                               # ← fixed: was missing
                )
                subject = result["subject"]
                body = result["body"]
            except Exception:
                pass

        result = await send_gmail(
            access_token=access_token,
            to_email=to_email,
            from_email=sender["email"],
            from_name=sender["name"],
            subject=subject,
            body=body,
            unsubscribe_token=unsubscribe_token,
            user_company=sender["company"],
            lead_company=lead_biz_name,
            brand_color=sender["brand_color"],
            lead_name=contact_name,
        )

        if result.get("error") == "token_expired":
            try:
                access_token = await refresh_access_token(
                    refresh_token,
                    settings.GOOGLE_CLIENT_ID,
                    settings.GOOGLE_CLIENT_SECRET,
                )
                result = await send_gmail(
                    access_token=access_token,
                    to_email=to_email,
                    from_email=sender["email"],
                    from_name=sender["name"],
                    subject=subject,
                    body=body,
                    unsubscribe_token=unsubscribe_token,
                    user_company=sender["company"],
                    lead_company=lead_biz_name,
                    brand_color=sender["brand_color"],
                    lead_name=contact_name,
                )
            except Exception:
                await db.campaigns.update_one(
                    {"_id": ObjectId(campaign_id)},
                    {
                        "$set": {f"leads.{i}.status": "failed",
                                 f"leads.{i}.error":  "token_expired"},
                        "$inc": {"failed": 1},
                    }
                )
                await asyncio.sleep(2)
                continue

        if result["success"]:
            sent_today += 1
            await db.campaigns.update_one(
                {"_id": ObjectId(campaign_id)},
                {
                    "$set": {
                        f"leads.{i}.status":           "sent",
                        f"leads.{i}.gmail_message_id": result.get("message_id"),
                        f"leads.{i}.thread_id":        result.get("thread_id"),
                        f"leads.{i}.sent_at":          datetime.utcnow(),
                    },
                    "$inc": {"sent": 1},
                }
            )
            lead_db = SessionLocal()
            try:
                sl = lead_db.query(Lead).filter(
                    Lead.id == lead.get("lead_id")).first()
                if sl:
                    sl.status = "contacted"
                    lead_db.commit()
            finally:
                lead_db.close()
        else:
            error_str = str(result.get("error", "")).lower()
            status = "bounced" if any(
                x in error_str for x in ["not found", "invalid", "does not exist", "mailbox"]
            ) else "failed"
            await db.campaigns.update_one(
                {"_id": ObjectId(campaign_id)},
                {
                    "$set": {f"leads.{i}.status": status,
                             f"leads.{i}.error":  result["error"]},
                    "$inc": {"failed": 1},
                }
            )

        await asyncio.sleep(2)

    final = await db.campaigns.find_one({"_id": ObjectId(campaign_id)}, {"status": 1})
    if final and final.get("status") not in ("paused",):
        await db.campaigns.update_one(
            {"_id": ObjectId(campaign_id)},
            {
                "$set": {
                    "status":       "completed",
                    "completed_at": datetime.utcnow(),
                    "updated_at":   datetime.utcnow(),
                }
            }
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/preview")
async def preview_email(
    data:         PreviewRequest,
    current_user=Depends(get_current_user),
):
    """Preview personalised email for a single lead before sending."""
    lead_info = get_lead_info(data.lead_id)
    if not lead_info:
        raise HTTPException(status_code=404, detail="Lead not found")

    sender = _get_sender_profile(current_user)

    if data.personalise:
        result = await personalise_email(
            subject=data.subject,
            body=data.body,
            lead_name=lead_info["contact_name"],
            lead_company=lead_info["company_name"],
            business_details=lead_info["business_details"],
            sender_name=sender["name"],
            sender_company=sender["company"],
            # ← fixed: was missing
            sender_company_description=sender["description"],
            # ← fixed: was missing
            lead_email=lead_info["email"],
        )
    else:
        from campaigns.personaliser import _extract_first_name, _resolve_company
        first_name = _extract_first_name(
            lead_info["contact_name"], lead_info["email"])
        company = _resolve_company(
            lead_info["company_name"], lead_info["email"])
        result = {
            "subject": data.subject
            .replace("{lead_name}",    first_name)
            .replace("{lead_company}", company)
            .replace("{sender_name}",  sender["name"]),
            "body": data.body
            .replace("{lead_name}",    first_name)
            .replace("{lead_company}", company)
            .replace("{sender_name}",  sender["name"]),
        }

    return {
        "lead":    lead_info,
        "subject": result["subject"],
        "body":    result["body"],
    }


@router.post("/create")
async def create_campaign(
    data:             CampaignCreate,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Create campaign and optionally start sending immediately."""
    if not current_user.get("gmail_connected"):
        raise HTTPException(status_code=400, detail="Gmail not connected")
    if not data.lead_ids:
        raise HTTPException(status_code=400, detail="No leads selected")
    if len(data.lead_ids) > 500:
        raise HTTPException(
            status_code=400, detail="Max 500 leads per campaign")

    sender = _get_sender_profile(current_user)

    campaign_leads = []
    for lead_id in data.lead_ids:
        info = get_lead_info(lead_id)
        if not info or not info["email"]:
            continue
        campaign_leads.append({
            "lead_id":              info["lead_id"],
            "company_name":         info["company_name"],
            "contact_name":         info["contact_name"],
            "email":                info["email"],
            "business_details":     info["business_details"],
            "status":               "pending",
            "personalised_subject": None,
            "personalised_body":    None,
            "sent_at":              None,
            "error":                None,
        })

    if not campaign_leads:
        raise HTTPException(
            status_code=400, detail="No leads with valid email addresses")

    # Pre-personalise first 10 leads immediately (rest done during send)
    if data.personalise:
        for i, lead in enumerate(campaign_leads[:10]):
            try:
                result = await personalise_email(
                    subject=data.subject,
                    body=data.body,
                    lead_name=lead["contact_name"],
                    lead_company=lead["company_name"],
                    business_details=lead.get("business_details", ""),
                    sender_name=sender["name"],
                    sender_company=sender["company"],
                    # ← fixed: was missing
                    sender_company_description=sender["description"],
                    # ← fixed: was missing
                    lead_email=lead["email"],
                )
                campaign_leads[i]["personalised_subject"] = result["subject"]
                campaign_leads[i]["personalised_body"] = result["body"]
            except Exception:
                pass

    campaign_doc = {
        "name":        data.name,
        "subject":     data.subject,
        "body":        data.body,
        "user_id":     str(current_user["_id"]),
        "user_email":  sender["email"],
        "user_name":   sender["name"],
        "total_leads": len(campaign_leads),
        "sent":        0,
        "failed":      0,
        "status":      "draft",
        "personalise": data.personalise,
        "leads":       campaign_leads,
        "created_at":  datetime.utcnow(),
        "updated_at":  datetime.utcnow(),
        "daily_limit": data.daily_limit,
        "completed_at": None,
    }

    result = await db.campaigns.insert_one(campaign_doc)
    campaign_id = str(result.inserted_id)

    background_tasks.add_task(run_campaign, campaign_id, current_user, db)

    return {
        "campaign_id": campaign_id,
        "name":        data.name,
        "total_leads": len(campaign_leads),
        "status":      "running",
        "message":     f"Campaign started — sending to {len(campaign_leads)} leads",
    }


@router.get("/list")
async def list_campaigns(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    campaigns = await db.campaigns.find(
        {"user_id": user_id},
        {"leads": 0}
    ).sort("created_at", -1).limit(20).to_list(20)

    return [{
        "id":           str(c["_id"]),
        "name":         c.get("name"),
        "status":       c.get("status"),
        "total_leads":  c.get("total_leads", 0),
        "sent":         c.get("sent", 0),
        "failed":       c.get("failed", 0),
        "created_at":   c.get("created_at").isoformat() if c.get("created_at") else None,
        "completed_at": c.get("completed_at").isoformat() if c.get("completed_at") else None,
    } for c in campaigns]


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id:  str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    campaign = await db.campaigns.find_one({
        "_id":     ObjectId(campaign_id),
        "user_id": str(current_user["_id"])
    })
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    leads = campaign.get("leads", [])

    fail_reasons = {}
    for lead in leads:
        if lead.get("status") in ("failed", "bounced"):
            reason = lead.get("error", "unknown") or "unknown"
            if "not found" in reason.lower() or "mailbox" in reason.lower():
                reason = "Invalid email / mailbox not found"
            elif "token" in reason.lower():
                reason = "Gmail auth expired"
            elif "no_email" in reason:
                reason = "No email address in database"
            elif "quota" in reason.lower():
                reason = "Gmail daily limit reached"
            fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

    return {
        "id":           str(campaign["_id"]),
        "name":         campaign.get("name"),
        "subject":      campaign.get("subject"),
        "status":       campaign.get("status"),
        "total_leads":  campaign.get("total_leads", 0),
        "sent":         campaign.get("sent", 0),
        "failed":       campaign.get("failed", 0),
        "pending":      sum(1 for l in leads if l.get("status") == "pending"),
        "bounced":      sum(1 for l in leads if l.get("status") == "bounced"),
        "fail_reasons": fail_reasons,
        "created_at":   campaign.get("created_at").isoformat() if campaign.get("created_at") else None,
        "completed_at": campaign.get("completed_at").isoformat() if campaign.get("completed_at") else None,
        "leads_preview": [{
            "lead_id":      l.get("lead_id"),
            "company_name": l.get("company_name"),
            "email":        l.get("email"),
            "status":       l.get("status"),
            "error":        l.get("error"),
            "sent_at":      l.get("sent_at").isoformat() if l.get("sent_at") else None,
        } for l in leads[:100]],
    }


@router.post("/{campaign_id}/pause")
async def pause_resume_campaign(
    campaign_id:  str,
    action:       str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    campaign = await db.campaigns.find_one({
        "_id":     ObjectId(campaign_id),
        "user_id": str(current_user["_id"])
    })
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    new_status = "paused" if action == "pause" else "running"
    await db.campaigns.update_one(
        {"_id": ObjectId(campaign_id)},
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
    )
    return {"message": f"Campaign {new_status}", "status": new_status}


# ── Email extraction helpers ──────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _extract_emails_from_text(text: str) -> List[str]:
    found = _EMAIL_RE.findall(text)
    seen, out = set(), []
    for e in found:
        e = e.lower().strip()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _save_emails_to_sqlite(emails: List[str], source: str, db) -> List[int]:
    from leads.lead_db import Lead
    generic_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "rediffmail.com", "yahoo.co.in", "live.com"
    }
    ids = []
    for email in emails:
        existing = db.query(Lead).filter(Lead.email == email).first()
        if existing:
            ids.append(existing.id)
            continue
        domain_part = email.split("@")[1] if "@" in email else ""
        auto_company = ""
        if domain_part and domain_part not in generic_domains:
            auto_company = domain_part.split(".")[0].title()
        lead = Lead(email=email, company_name=auto_company,
                    source=source, status="new")
        db.add(lead)
        db.flush()
        ids.append(lead.id)
    db.commit()
    return ids


# ── Models ────────────────────────────────────────────────────────────────────

class SingleEmailPayload(BaseModel):
    email:        str
    contact_name: str = ""
    company_name: str = ""
    business_details: str = "" 


class BulkEmailPayload(BaseModel):
    raw_text: str


# ── Routes: upload / single / bulk ───────────────────────────────────────────

@router.post("/upload-leads")
async def upload_leads_file(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    """
    Campaign file uploader — uses the same smart fuzzy column parser as
    /leads/upload-preview + /leads/import-confirm so that company_name,
    contact_name, and business_details are always saved to SQLite, not
    just the email address.
    """
    from leads.lead_db import Lead

    content = await file.read()
    filename = (file.filename or "").lower()
    print(f"UPLOAD: filename='{file.filename}' lowered='{filename}'")

    GENERIC_DOMAINS = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com",
        "yahoo.co.in", "live.com", "icloud.com", "protonmail.com", "aol.com",
        "mail.com", "yandex.com", "zoho.com",
    }

    # ── Fuzzy column scorer ───────────────────────────────────────────────────
    # Exact phrase match scores highest so "COMPANY NAME" and "CONTACT NAME"
    # don't collide on the shared word "name".
    EMAIL_HINTS = ["email", "mail", "e-mail",
                   "emailid", "email address", "email id"]
    COMPANY_HINTS = ["company name", "company", "organisation", "organization",
                     "firm", "business name", "companyname", "org", "brand",
                     "client", "account name", "account"]
    CONTACT_HINTS = ["contact name", "contact person", "contact", "person",
                     "full name", "fullname", "first name", "owner name",
                     "owner", "poc", "lead name", "rep", "name"]
    BIZ_HINTS = ["business details", "business description", "business info",
                 "business", "description", "details", "about", "info",
                 "profile", "overview", "summary", "notes", "industry",
                 "service", "product", "what they do", "bio", "background"]
    WEBSITE_HINTS = ["website", "web", "url",
                     "site", "domain", "link", "homepage"]

    def _score(col: str, hints: list) -> int:
        c = col.lower().strip().replace("_", " ").replace(".", " ")
        score = 0
        for h in hints:
            if c == h:
                score += 10       # exact match wins
            elif c.startswith(h) or c.endswith(h):
                score += 5
            elif h in c:
                score += 1
        return score

    def _best_col(columns: list, hints: list, exclude: set = None) -> Optional[str]:
        scored = []
        for c in columns:
            if exclude and c in exclude:
                continue
            s = _score(c, hints)
            if s > 0:
                scored.append((c, s))
        return max(scored, key=lambda x: x[1])[0] if scored else None

    _EMAIL_SCAN = re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

    def _first_email(text: str) -> str:
        m = _EMAIL_SCAN.search(str(text))
        return m.group(0).lower().strip() if m else ""

    # ── Save one lead to SQLite (upsert) ──────────────────────────────────────
    def _save_lead(email: str, company: str, contact: str,
                   biz: str, website: str) -> Optional[int]:
        email = email.lower().strip()
        if not email:
            return None

        if not company:
            domain = email.split("@")[1] if "@" in email else ""
            if domain and domain not in GENERIC_DOMAINS:
                company = domain.split(".")[0].replace("-", " ").title()

        existing = db.query(Lead).filter(Lead.email == email).first()
        if existing:
            changed = False
            if not existing.company_name and company:
                existing.company_name = company[:500]
                changed = True
            if not existing.contact_name and contact:
                existing.contact_name = contact[:300]
                changed = True
            if not existing.business_details and biz:
                existing.business_details = biz[:2000]
                changed = True
            if not existing.website and website:
                existing.website = website[:500]
                changed = True
            if changed:
                db.flush()
            return existing.id

        lead = Lead(
            email=email,
            company_name=(company or "")[:500],
            contact_name=(contact or "")[:300],
            business_details=(biz or "")[:2000],
            website=(website or "")[:500],
            source="campaign_upload",
            status="new",
        )
        db.add(lead)
        db.flush()
        return lead.id

    # ── Structured DataFrame parser ───────────────────────────────────────────
    def _parse_df(df: pd.DataFrame) -> List[int]:
        df = df.fillna("").astype(str)
        cols = df.columns.tolist()

        email_col = _best_col(cols, EMAIL_HINTS)
        company_col = _best_col(cols, COMPANY_HINTS, exclude={email_col})
        contact_col = _best_col(cols, CONTACT_HINTS, exclude={
                                email_col, company_col})
        biz_col = _best_col(cols, BIZ_HINTS,     exclude={
                            email_col, company_col, contact_col})
        website_col = _best_col(cols, WEBSITE_HINTS,  exclude={
                                email_col, company_col, contact_col, biz_col})
        print(f"PARSE_DF: cols={cols}")
        print(
            f"PARSE_DF: email={email_col} company={company_col} contact={contact_col} biz={biz_col}")
        print(f"PARSE_DF: total rows={len(df)}")

        # Log detected columns to terminal for debugging
        import logging
        logging.getLogger(__name__).info(
            "upload-leads columns → email=%s | company=%s | contact=%s | biz=%s",
            email_col, company_col, contact_col, biz_col
        )

        ids = []
        for _, row in df.iterrows():
            raw_email = row[email_col].strip() if email_col else ""
            email = _first_email(raw_email) or _first_email(
                " ".join(str(v) for v in row.values)
            )
            if not email:
                continue

            lid = _save_lead(
                email=email,
                company=row[company_col].strip() if company_col else "",
                contact=row[contact_col].strip() if contact_col else "",
                biz=row[biz_col].strip() if biz_col else "",
                website=row[website_col].strip() if website_col else "",
            )
            if lid:
                ids.append(lid)

        db.commit()
        return ids

    # ── Plain text fallback (PDF, TXT, JSON etc) ──────────────────────────────
   # ── Plain text fallback (PDF, TXT, JSON etc) ──────────────────────────────
    def _parse_text(text: str) -> List[int]:
        emails = list(dict.fromkeys(
            e.lower().strip() for e in _EMAIL_SCAN.findall(text)
        ))
        ids = []
        for email in emails:
            lid = _save_lead(email, "", "", "", "")
            if lid:
                ids.append(lid)
        db.commit()
        return ids

    # ── Route by file type ────────────────────────────────────────────────────
    lead_ids: List[int] = []
    parse_error = ""

    if filename.endswith((".xlsx", ".xls")):
        try:
            df = pd.read_excel(io.BytesIO(content), dtype=str)
            lead_ids = _parse_df(df)
        except Exception as e:
            parse_error = str(e)
        if not lead_ids:
            try:
                flat = " ".join(str(v) for v in pd.read_excel(
                    io.BytesIO(content), dtype=str).values.flatten())
                lead_ids = _parse_text(flat)
            except Exception:
                pass

    elif filename.endswith(".csv"):
        try:
            # Fixed Indentation Here
            df = pd.read_csv(io.BytesIO(content), dtype=str,
                             encoding="utf-8")
            print(f"CSV READ: rows={len(df)} cols={df.columns.tolist()}")
            lead_ids = _parse_df(df)
            print(f"CSV DONE: lead_ids={lead_ids}")
        except Exception as e:
            parse_error = str(e)
            print(f"CSV ERROR: {e}")

        print(
            f"CSV FALLBACK CHECK: lead_ids={lead_ids} parse_error={parse_error}")

        if not lead_ids:
            try:
                lead_ids = _parse_text(
                    content.decode("utf-8", errors="replace"))
            except Exception:
                pass

    elif filename.endswith(".pdf"):
        if pdfplumber is None:
            raise HTTPException(
                status_code=500, detail="PDF support not installed. Run: pip install pdfplumber")
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            lead_ids = _parse_text(text)
        except Exception as e:
            parse_error = str(e)

    elif filename.endswith(".json"):
        try:
            import json as _json
            lead_ids = _parse_text(
                str(_json.loads(content.decode("utf-8", errors="replace"))))
        except Exception as e:
            parse_error = str(e)

    else:  # .txt, .tsv, .vcf, anything else
        try:
            if filename.endswith(".tsv"):
                df = pd.read_csv(io.BytesIO(content), sep="\t",
                                 dtype=str, encoding="utf-8")
                lead_ids = _parse_df(df)
            if not lead_ids:
                lead_ids = _parse_text(
                    content.decode("utf-8", errors="replace"))
        except Exception as e:
            parse_error = str(e)

    if not lead_ids:
        detail = "No valid email addresses found in the uploaded file."
        if parse_error:
            detail += f" (Parse error: {parse_error})"
        raise HTTPException(status_code=400, detail=detail)

    return {
        "lead_ids":    lead_ids,
        "total_found": len(lead_ids),
    }


@router.post("/add-single-email")
async def add_single_email(
    data: SingleEmailPayload,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    from leads.lead_db import Lead
    email = data.email.strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    existing = db.query(Lead).filter(Lead.email == email).first()
    if existing:
     if not existing.business_details and data.business_details.strip():
        existing.business_details = data.business_details.strip()
        db.commit()
     return {"lead_ids": [existing.id], "already_existed": True}

    company = data.company_name.strip()
    if not company:
        domain_part = email.split("@")[1] if "@" in email else ""
        generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                   "rediffmail.com", "yahoo.co.in", "live.com", "icloud.com", "protonmail.com"}
        company = domain_part.split(".")[0].title(
        ) if domain_part and domain_part not in generic else ""

    lead = Lead(email=email, contact_name=data.contact_name.strip(),
                company_name=company,business_details=data.business_details.strip(), source="manual_entry", status="new")
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"lead_ids": [lead.id], "already_existed": False}


@router.post("/add-bulk-emails")
async def add_bulk_emails(
    data: BulkEmailPayload,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    emails = _extract_emails_from_text(data.raw_text)
    if not emails:
        raise HTTPException(
            status_code=400, detail="No valid email addresses found in the pasted text.")
    if len(emails) > 500:
        raise HTTPException(
            status_code=400, detail=f"Too many emails ({len(emails)}). Max 500 per paste.")
    lead_ids = _save_emails_to_sqlite(emails, source="bulk_paste", db=db)
    return {"lead_ids": lead_ids, "total_found": len(emails), "emails": emails[:20]}
