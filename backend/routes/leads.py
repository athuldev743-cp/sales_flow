from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from typing import Optional
from leads.lead_db import get_lead_db, Lead
from auth_utils import get_current_user

router = APIRouter()

def lead_to_dict(lead) -> dict:
    email = lead.email
    if email and ',' in email:
        email = email.split(',')[0].strip()
    return {
        "id":               lead.id,
        "company_name":     lead.company_name,
        "contact_name":     lead.contact_name,
        "city":             lead.city,
        "state":            lead.state,
        "mobile":           lead.mobile,
        "phone":            lead.phone,
        "email":            email,
        "website":          lead.website,
        "business_details": lead.business_details,
        "address":          lead.address,
        "status":           lead.status,
        "source":           lead.source,
    }

@router.get("/search")
async def search_leads(
    q:         Optional[str] = Query(None),
    city:      Optional[str] = Query(None),
    state:     Optional[str] = Query(None),
    status:    Optional[str] = Query(None),
    page:      int = Query(1,  ge=1),
    limit:     int = Query(25, ge=1, le=100),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    offset = (page - 1) * limit
    leads = []
    total = 0

    # 1. FULL-TEXT SEARCH (FTS) LOGIC
    if q and q.strip():
        search_term = " ".join(f"{w}*" for w in q.strip().split())
        where_parts = ["l.id IN (SELECT rowid FROM leads_fts WHERE leads_fts MATCH :query)"]
        params = {"query": search_term, "limit": limit, "offset": offset}

        if city:
            where_parts.append("LOWER(l.city) LIKE :city_like")
            params["city_like"] = f"%{city.lower()}%"
        if state:
            where_parts.append("LOWER(l.state) LIKE :state_like")
            params["state_like"] = f"%{state.lower()}%"
        if status:
            where_parts.append("l.status = :status")
            params["status"] = status
        else:
            # Default for search: hide contacted leads if no status is specified
            where_parts.append("(l.status IS NULL OR l.status = '')")

        where_clause = " AND ".join(where_parts)
        
        fts_sql = text(f"""
            SELECT l.id, l.company_name, l.contact_name, l.address,
                   l.city, l.state, l.mobile, l.phone, l.email,
                   l.website, l.business_details, l.status, l.source
            FROM leads l
            WHERE {where_clause}
            LIMIT :limit OFFSET :offset
        """)
        
        count_sql = text(f"SELECT COUNT(*) FROM leads l WHERE {where_clause}")
        count_params = {k: v for k, v in params.items() if k not in ('limit', 'offset')}
        
        rows = db.execute(fts_sql, params).fetchall()
        total = db.execute(count_sql, count_params).scalar() or 0

        for row in rows:
            email = row[8]
            if email and ',' in email:
                email = email.split(',')[0].strip()
            leads.append({
                "id": row[0], "company_name": row[1], "contact_name": row[2],
                "address": row[3], "city": row[4], "state": row[5],
                "mobile": row[6], "phone": row[7], "email": email,
                "website": row[9], "business_details": row[10],
                "status": row[11], "source": row[12],
            })

    # 2. STANDARD FILTERING LOGIC (With Optimized Count)
    else:
        query = db.query(Lead)
        if city:   query = query.filter(Lead.city.ilike(f"%{city}%"))
        if state:  query = query.filter(Lead.state.ilike(f"%{state}%"))
        if status: query = query.filter(Lead.status == status)
        
        # DEFAULT: If no status filter is applied, only show new leads
        if not status:
            query = query.filter((Lead.status == None) | (Lead.status == ""))

        # Optimization: Fast approximate count for the base table
        # We only use this when no specific filters are applied
        if not city and not state and not status:
            # For SQLite, even SELECT COUNT(*) can be slow on 1M+ rows.
            # If you have a 'stats' table, pull it from there. Otherwise, this is the safest direct way:
            total = db.execute(text("SELECT COUNT(*) FROM leads")).scalar() or 0
        else:
            total = query.count()

        results = query.offset(offset).limit(limit).all()
        leads = [lead_to_dict(l) for l in results]

    return {
        "leads": leads,
        "total": total,
        "page":  page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total > 0 else 0
    }

@router.get("/stats")
async def lead_stats(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    total = db.query(func.count(Lead.id)).scalar()
    cities = db.query(Lead.city, func.count(Lead.id)).group_by(
        Lead.city).order_by(func.count(Lead.id).desc()).limit(10).all()
    states = db.query(Lead.state, func.count(Lead.id)).group_by(
        Lead.state).order_by(func.count(Lead.id).desc()).limit(10).all()
    return {
        "total_leads": total,
        "top_cities":  [{"city": c,  "count": n} for c, n in cities if c],
        "top_states":  [{"state": s, "count": n} for s, n in states if s],
    }

@router.get("/filters")
async def get_filter_options(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    cities = db.execute(text("""
        SELECT city, COUNT(*) as cnt FROM leads
        WHERE city IS NOT NULL AND city != ''
        GROUP BY city ORDER BY cnt DESC LIMIT 100
    """)).fetchall()

    states = db.execute(text("""
        SELECT state, COUNT(*) as cnt FROM leads
        WHERE state IS NOT NULL AND state != ''
        GROUP BY state ORDER BY cnt DESC LIMIT 50
    """)).fetchall()

    return {
        "cities": [{"name": r[0], "count": r[1]} for r in cities],
        "states": [{"name": r[0], "count": r[1]} for r in states],
    }

@router.get("/{lead_id}")
async def get_lead(
    lead_id:     int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_lead_db),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead_to_dict(lead)

@router.patch("/{lead_id}/status")
async def update_lead_status(
    lead_id:     int,
    status:      str,
    current_user=Depends(get_current_user),
    db: Session  = Depends(get_lead_db),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead.status = status
    db.commit()
    return {"message": "Status updated", "new_status": status}

@router.delete("/{lead_id}")
async def delete_lead(
    lead_id:     int,
    current_user=Depends(get_current_user),
    db: Session  = Depends(get_lead_db),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    db.delete(lead)
    db.commit()
    return {"message": "Lead removed"}