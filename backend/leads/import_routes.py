from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from typing import List, Dict, Optional
from auth_utils import get_current_user
from leads.lead_db import Lead, get_lead_db
from sqlalchemy.orm import Session
import pandas as pd
import io
import re
import sqlite3
import httpx
import base64
import json as _json
from config import settings


# PDF support — install with: pip install pdfplumber
try:
    import pdfplumber
    PDF_SUPPORTED = True
except ImportError:
    PDF_SUPPORTED = False

router = APIRouter()

# ── Field definitions ─────────────────────────────────────────────────────────

LEAD_FIELDS = [
    "company_name", "contact_name", "email", "mobile", "phone",
    "city", "state", "business_details", "website", "address",
]

_HINTS: Dict[str, List[str]] = {
    "company_name":     ["company", "company_name", "business name", "organization",
                         "organisation", "firm", "company name", "name"],
    "contact_name":     ["contact", "contact name", "contact_name", "person",
                         "owner", "proprietor", "full name", "full_name"],
    "email":            ["email", "email address", "e-mail", "email_address", "mail"],
    "mobile":           ["mobile", "mobile number", "mobile_number", "cell",
                         "whatsapp", "cell number"],
    "phone":            ["phone", "phone number", "landline", "telephone", "tel"],
    "city":             ["city", "town", "district"],
    "state":            ["state", "province", "region"],
    "business_details": ["business details", "business_details", "description",
                         "about", "products", "services", "business"],
    "website":          ["website", "url", "web", "site", "www"],
    "address":          ["address", "street", "locality", "addr"],
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auto_map(columns: List[str]) -> Dict[str, str]:
    lower_map = {c.lower().strip(): c for c in columns}
    result:        Dict[str, str] = {}
    already_mapped: set = set()

    for field, hints in _HINTS.items():
        if field in already_mapped:
            continue
        for hint in hints:
            if hint in lower_map:
                csv_col = lower_map[hint]
                if csv_col not in result:
                    result[csv_col] = field
                    already_mapped.add(field)
                    break
    return result


def _extract_emails_from_text(text: str) -> List[str]:
    """Return deduplicated list of emails found in arbitrary text."""
    found = _EMAIL_RE.findall(text)
    seen, out = set(), []
    for e in found:
        e = e.lower().strip()
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ── Pydantic models ───────────────────────────────────────────────────────────

class ImportConfirm(BaseModel):
    mappings: Dict[str, str]
    rows:     List[Dict]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/upload-preview")
async def upload_preview(
    file:         UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    """
    Step 1 — parse CSV or Excel file, return columns + preview rows.
    PDF is NOT supported here (PDFs don't have column mappings).
    Use /upload-pdf for PDFs instead.
    """
    content = await file.read()
    filename = (file.filename or "").lower()

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str, nrows=5000)
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str, nrows=5000)
        else:
            raise HTTPException(
                status_code=400,
                detail="Only CSV and Excel (.xlsx / .xls) files are supported for column mapping. "
                       "For PDFs use the Upload Leads button inside Campaigns.",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Could not parse file: {e}")

    df = df.fillna("")
    columns = list(df.columns)
    preview_rows = df.head(6).to_dict("records")
    all_rows = df.to_dict("records")

    return {
        "filename":    file.filename,
        "columns":     columns,
        "total_rows":  len(df),
        "preview_rows": preview_rows,
        "auto_map":    _auto_map(columns),
        "all_rows":    all_rows,
    }


@router.post("/import-confirm")
async def import_confirm(
    data:         ImportConfirm,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    """
    Step 2 — user confirmed column mapping.
    Insert all rows into the leads SQLite table.
    """
    if not data.rows:
        raise HTTPException(status_code=400, detail="No rows to import")
    if len(data.rows) > 5000:
        raise HTTPException(
            status_code=400, detail="Max 5 000 rows per import")

    imported = 0
    skipped = 0
    errors:  List[str] = []

    for i, row in enumerate(data.rows):
        try:
            mapped: Dict[str, str] = {}
            for csv_col, lead_field in data.mappings.items():
                if lead_field in LEAD_FIELDS:
                    mapped[lead_field] = str(
                        row.get(csv_col, "") or "").strip()

            if not mapped.get("company_name") and not mapped.get("email"):
                skipped += 1
                continue

            email = mapped.get("email", "")
            if email and "," in email:
                email = email.split(",")[0].strip()

            lead = Lead(
                company_name=mapped.get("company_name", ""),
                contact_name=mapped.get("contact_name", ""),
                email=email,
                mobile=mapped.get("mobile", ""),
                phone=mapped.get("phone", ""),
                city=mapped.get("city", ""),
                state=mapped.get("state", ""),
                business_details=mapped.get("business_details", ""),
                website=mapped.get("website", ""),
                address=mapped.get("address", ""),
                source="import",
                status="",
            )
            db.add(lead)
            imported += 1

            if imported % 200 == 0:
                db.commit()

        except Exception as e:
            errors.append(f"Row {i + 1}: {e}")
            skipped += 1

    db.commit()

    return {
        "imported": imported,
        "skipped":  skipped,
        "total":    len(data.rows),
        "errors":   errors[:10],
    }


# ── NEW: PDF upload endpoint ──────────────────────────────────────────────────

@router.post("/upload-pdf")
async def upload_pdf(
    file:         UploadFile = File(...),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    """
    Accept a PDF file.
    Extract all email addresses from the text content.
    Save each email as a Lead in SQLite (skip duplicates).
    Returns count of emails found and imported.
    """
    if not PDF_SUPPORTED:
        raise HTTPException(
            status_code=500,
            detail="PDF support not installed on this server. "
                   "Ask your admin to run: pip install pdfplumber"
        )

    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(
            status_code=400, detail="Only .pdf files are accepted here.")

    content = await file.read()

    # Extract text from all pages
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {e}")

    emails = _extract_emails_from_text(full_text)

    if not emails:
        raise HTTPException(
            status_code=400,
            detail="No email addresses found in this PDF. "
                   "Make sure the PDF contains readable text (not a scanned image)."
        )

    # Save to SQLite, skip duplicates
    imported = 0
    skipped = 0
    lead_ids = []

    for email in emails:
        existing = db.query(Lead).filter(Lead.email == email).first()
        if existing:
            lead_ids.append(existing.id)
            skipped += 1
            continue

        lead = Lead(
            email=email,
            source="pdf_upload",
            status="new",
        )
        db.add(lead)
        db.flush()
        lead_ids.append(lead.id)
        imported += 1

    db.commit()

    return {
        "imported":    imported,
        "skipped":     skipped,          # already existed in DB
        "total_found": len(emails),
        "lead_ids":    lead_ids,
        "emails":      emails[:20],      # preview first 20
    }


@router.get("/filters")
async def get_filters(current_user=Depends(get_current_user)):
    """Return common filter options for the sidebar dropdowns."""
    conn = sqlite3.connect("salesflow_leads.db")
    cursor = conn.cursor()

    # 1. Get unique states
    cursor.execute(
        "SELECT state, COUNT(*) as count FROM leads WHERE state != '' GROUP BY state ORDER BY count DESC LIMIT 20")
    states = [{"name": r[0], "count": r[1]} for r in cursor.fetchall()]

    # 2. Get unique cities
    cursor.execute(
        "SELECT city, COUNT(*) as count FROM leads WHERE city != '' GROUP BY city ORDER BY count DESC LIMIT 30")
    cities = [{"name": r[0], "count": r[1]} for r in cursor.fetchall()]

    conn.close()
    return {"states": states, "cities": cities}


@router.get("/search")
async def search_leads(
    q: Optional[str] = Query(None),
    group: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1),
    limit: int = Query(25),
    current_user=Depends(get_current_user)
):
    offset = (page - 1) * limit
    conn = sqlite3.connect("salesflow_leads.db")
    cursor = conn.cursor()

    conditions = []
    params = []

    # 1. INDUSTRY THEME (High Speed Index)
    if group:
        conditions.append("business_group = ?")
        params.append(group)

    # 2. TEXT SEARCH
    if q:
        conditions.append(
            "(company_name LIKE ? OR business_details LIKE ? OR contact_name LIKE ?)")
        search_val = f"%{q}%"
        params.extend([search_val, search_val, search_val])

    # 3. OTHER FILTERS
    if state:
        conditions.append("state = ?")
        params.append(state)
    if city:
        conditions.append("city = ?")
        params.append(city)
    if status:
        conditions.append("status = ?")
        params.append(status)
    else:
        # Crucial: Don't hide leads by default if we want to see the database!
        pass

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Execute with a LIMIT so we don't hang the browser
    sql = f"""
        SELECT id, company_name, contact_name, business_details, city, state, email, mobile, status 
        FROM leads 
        {where_clause} 
        LIMIT ? OFFSET ?
    """
    cursor.execute(sql, params + [limit, offset])
    rows = cursor.fetchall()

    leads = [dict(zip(["id", "company_name", "contact_name", "business_details",
                  "city", "state", "email", "mobile", "status"], r)) for r in rows]

    # Total count for pagination
    cursor.execute(f"SELECT COUNT(*) FROM leads {where_clause}", params)
    total = cursor.fetchone()[0]

    conn.close()
    return {
        "leads": leads,
        "total": total,
        "page": page,
        "pages": (total // limit) + (1 if total % limit > 0 else 0)
    }


import httpx
import base64
import json as _json
 
GROQ_VISION_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # best Groq vision model
 
 
@router.post("/scan-card")
async def scan_card(
    file:         UploadFile = File(...),
    current_user=Depends(get_current_user),
    db: Session   = Depends(get_lead_db),
):
    """
    Accept a JPEG/PNG image of a business card.
    Use Groq Vision (Llama 4 Scout) to extract all lead fields.
    Save to SQLite and return in upload-preview format so the
    existing mapping UI can optionally review before confirming.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file received")
 
    # ── Encode image as base64 for Groq Vision ────────────────────────────────
    mime = "image/jpeg"
    fname = (file.filename or "").lower()
    if fname.endswith(".png"):
        mime = "image/png"
    elif fname.endswith(".webp"):
        mime = "image/webp"
 
    b64_image = base64.b64encode(content).decode("utf-8")
 
    prompt = """You are a business card OCR expert. Extract ALL text from this business card image and return ONLY a valid JSON object with these exact keys (use empty string "" if a field is not present):
 
{
  "company_name": "",
  "contact_name": "",
  "email": "",
  "mobile": "",
  "phone": "",
  "city": "",
  "state": "",
  "website": "",
  "address": "",
  "business_details": ""
}
 
Rules:
- mobile: mobile/cell/WhatsApp numbers (10+ digits, often starts with +91 or 9/8/7/6 in India)
- phone: landline numbers (often has STD code like 0422-...)
- business_details: put job title, designation, tagline, or what the company does here
- address: full street address if present
- website: full URL including http/https if present, or add https:// if missing
- Return ONLY the JSON object, no explanation, no markdown, no backticks"""
 
    # ── Call Groq Vision ──────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROQ_VISION_URL,
            headers={
                "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":      GROQ_VISION_MODEL,
                "max_tokens": 500,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime};base64,{b64_image}"
                                }
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ]
            }
        )
 
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Groq Vision failed ({resp.status_code}): {resp.text[:200]}"
        )
 
    raw_content = resp.json()["choices"][0]["message"]["content"].strip()
 
    # ── Parse JSON from Groq response ─────────────────────────────────────────
    # Strip markdown fences if Groq added them despite instructions
    raw_content = re.sub(r"```(?:json)?", "", raw_content).strip().rstrip("`").strip()
 
    try:
        extracted = _json.loads(raw_content)
    except Exception:
        # Try to find JSON object within the response
        match = re.search(r'\{[^{}]+\}', raw_content, re.DOTALL)
        if match:
            try:
                extracted = _json.loads(match.group())
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail="Could not parse card details. Try a clearer photo with better lighting."
                )
        else:
            raise HTTPException(
                status_code=422,
                detail="Could not extract details from card. Try a clearer photo."
            )
 
    # ── Normalise fields ──────────────────────────────────────────────────────
    def _clean(val):
        return str(val or "").strip()
 
    company_name    = _clean(extracted.get("company_name"))
    contact_name    = _clean(extracted.get("contact_name"))
    email           = _clean(extracted.get("email")).lower()
    mobile          = _clean(extracted.get("mobile"))
    phone           = _clean(extracted.get("phone"))
    city            = _clean(extracted.get("city"))
    state           = _clean(extracted.get("state"))
    website         = _clean(extracted.get("website"))
    address         = _clean(extracted.get("address"))
    business_details = _clean(extracted.get("business_details"))
 
    if not company_name and not email:
        raise HTTPException(
            status_code=422,
            detail="Could not find a company name or email on the card. Try a clearer photo."
        )
 
    # ── Save to SQLite (upsert by email if present) ───────────────────────────
    lead = None
    if email:
        lead = db.query(Lead).filter(Lead.email == email).first()
 
    if lead:
        # Update existing lead with any new info
        if company_name:    lead.company_name    = company_name
        if contact_name:    lead.contact_name    = contact_name
        if mobile:          lead.mobile          = mobile
        if phone:           lead.phone           = phone
        if city:            lead.city            = city
        if state:           lead.state           = state
        if website:         lead.website         = website
        if address:         lead.address         = address
        if business_details: lead.business_details = business_details
        already_existed = True
    else:
        lead = Lead(
            company_name     = company_name,
            contact_name     = contact_name,
            email            = email,
            mobile           = mobile,
            phone            = phone,
            city             = city,
            state            = state,
            website          = website,
            address          = address,
            business_details = business_details,
            source           = "card_scan",
            status           = "new",
        )
        db.add(lead)
        already_existed = False
 
    db.commit()
    db.refresh(lead)
 
    # ── Return in upload-preview format so mapping UI works ───────────────────
    # Also return lead_ids + all_rows for direct campaign use (no mapping needed)
    row = {
        "company_name":     company_name,
        "contact_name":     contact_name,
        "email":            email,
        "mobile":           mobile,
        "phone":            phone,
        "city":             city,
        "state":            state,
        "website":          website,
        "address":          address,
        "business_details": business_details,
    }
 
    return {
        # upload-preview compatible (for upload.html mapping UI)
        "filename":    "business_card.jpg",
        "columns":     list(row.keys()),
        "total_rows":  1,
        "preview_rows": [row],
        "all_rows":    [row],
        "auto_map":    {k: k for k in row.keys()},   # all fields pre-mapped
        # campaign-direct compatible (for campaigns.html)
        "lead_ids":    [lead.id],
        "already_existed": already_existed,
        "extracted":   row,   # for success message
    }
 
