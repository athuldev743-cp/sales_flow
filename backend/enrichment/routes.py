from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List
from database import get_db
from auth_utils import get_current_user
from leads.lead_db import Lead, SessionLocal
from enrichment.enricher import enrich_lead_with_ai
from datetime import datetime
from bson import ObjectId
import asyncio

router = APIRouter()


# ── Request / response models ─────────────────────────────────────────────────

class EnrichRequest(BaseModel):
    lead_ids:  List[int]
    overwrite: bool = False   # overwrite existing business_details if True


# ── Background worker ─────────────────────────────────────────────────────────

async def _run_enrichment(job_id: str, lead_ids: List[int], overwrite: bool, db):
    await db.enrichment_jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {"status": "running", "started_at": datetime.utcnow()}},
    )

    completed = 0
    failed    = 0
    lead_db   = SessionLocal()

    try:
        for lead_id in lead_ids:
            try:
                lead = lead_db.query(Lead).filter(Lead.id == lead_id).first()
                if not lead:
                    failed += 1
                    continue

                # Skip if already has details and overwrite is off
                if lead.business_details and not overwrite:
                    completed += 1
                    await db.enrichment_jobs.update_one(
                        {"_id": ObjectId(job_id)},
                        {"$set": {"completed": completed, "failed": failed}},
                    )
                    continue

                result = await enrich_lead_with_ai(
                    company_name=     lead.company_name or "",
                    business_details= lead.business_details or "",
                    city=             lead.city or "",
                    state=            lead.state or "",
                    existing_email=   lead.email or "",
                    existing_website= lead.website or "",
                )

                # Write enriched description back to SQLite
                if result.get("enriched_description"):
                    lead.business_details = result["enriched_description"]
                    lead_db.commit()

                # Persist metadata in Mongo for future use
                await db.lead_enrichments.update_one(
                    {"lead_id": lead_id},
                    {"$set": {
                        "lead_id":           lead_id,
                        "industry":          result.get("industry"),
                        "company_size":      result.get("company_size"),
                        "pain_points":       result.get("pain_points", []),
                        "suggested_subject": result.get("suggested_subject"),
                        "confidence":        result.get("confidence", 0.0),
                        "enriched_at":       datetime.utcnow(),
                    }},
                    upsert=True,
                )

                completed += 1
                await db.enrichment_jobs.update_one(
                    {"_id": ObjectId(job_id)},
                    {"$set": {"completed": completed, "failed": failed}},
                )

                await asyncio.sleep(0.4)   # stay within Groq rate limit

            except Exception:
                failed += 1
                lead_db.rollback()
    finally:
        lead_db.close()

    await db.enrichment_jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {
            "status":      "completed",
            "completed":   completed,
            "failed":      failed,
            "finished_at": datetime.utcnow(),
        }},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/enrich")
async def start_enrichment(
    data:             EnrichRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    if not data.lead_ids:
        raise HTTPException(status_code=400, detail="No leads selected")
    if len(data.lead_ids) > 100:
        raise HTTPException(status_code=400, detail="Max 100 leads per enrichment job")

    job_doc = {
        "user_id":     str(current_user["_id"]),
        "lead_ids":    data.lead_ids,
        "total":       len(data.lead_ids),
        "completed":   0,
        "failed":      0,
        "status":      "queued",
        "created_at":  datetime.utcnow(),
        "started_at":  None,
        "finished_at": None,
    }

    result = await db.enrichment_jobs.insert_one(job_doc)
    job_id = str(result.inserted_id)

    background_tasks.add_task(_run_enrichment, job_id, data.lead_ids, data.overwrite, db)

    return {"job_id": job_id, "total": len(data.lead_ids), "status": "queued"}


@router.get("/status/{job_id}")
async def get_job_status(
    job_id:       str,
    current_user= Depends(get_current_user),
    db=           Depends(get_db),
):
    job = await db.enrichment_jobs.find_one({
        "_id":     ObjectId(job_id),
        "user_id": str(current_user["_id"]),
    })
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id":      job_id,
        "status":      job.get("status"),
        "total":       job.get("total", 0),
        "completed":   job.get("completed", 0),
        "failed":      job.get("failed", 0),
        "started_at":  job["started_at"].isoformat()  if job.get("started_at")  else None,
        "finished_at": job["finished_at"].isoformat() if job.get("finished_at") else None,
    }


@router.get("/lead/{lead_id}")
async def get_lead_enrichment(
    lead_id:      int,
    current_user= Depends(get_current_user),
    db=           Depends(get_db),
):
    e = await db.lead_enrichments.find_one({"lead_id": lead_id})
    if not e:
        return {"lead_id": lead_id, "enriched": False}

    return {
        "lead_id":           lead_id,
        "enriched":          True,
        "industry":          e.get("industry"),
        "company_size":      e.get("company_size"),
        "pain_points":       e.get("pain_points", []),
        "suggested_subject": e.get("suggested_subject"),
        "confidence":        e.get("confidence", 0.0),
        "enriched_at":       e["enriched_at"].isoformat() if e.get("enriched_at") else None,
    }