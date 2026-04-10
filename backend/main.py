from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from database import connect_db, close_db, get_db
from leads.lead_db import init_db as init_lead_db
from routes import auth, profile, dashboard
from routes import leads
from campaigns import routes as campaign_routes
from replies import routes as reply_routes
from replies.routes import background_sync_all_users
from meetings import routes as meeting_routes
from enrichment import routes as enrichment_routes
from routes.chat import router as chat_router
from leads.import_routes import router as import_router
from billing import routes as billing_routes          # ← NEW
import asyncio
import logging

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    init_lead_db()

    db = get_db()
    sync_task = asyncio.create_task(background_sync_all_users(db))
    logger.info("Background Gmail reply sync started")

    yield

    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
    await close_db()

app = FastAPI(title="SalesFlow API", version="4.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────

app.include_router(auth.router,                  prefix="/api/auth",       tags=["auth"])
app.include_router(profile.router,               prefix="/api/profile",    tags=["profile"])
app.include_router(dashboard.router,             prefix="/api/dashboard",  tags=["dashboard"])

# 🚨 PRIORITY: Optimised import router must come BEFORE standard leads router
app.include_router(import_router,                prefix="/api/leads",      tags=["leads-import"])
app.include_router(leads.router,                 prefix="/api/leads",      tags=["leads"])

app.include_router(campaign_routes.router,       prefix="/api/campaigns",  tags=["campaigns"])
app.include_router(reply_routes.router,          prefix="/api/replies",    tags=["replies"])
app.include_router(meeting_routes.router,        prefix="/api/meetings",   tags=["meetings"])
app.include_router(enrichment_routes.router,     prefix="/api/enrichment", tags=["enrichment"])
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(billing_routes.router,        prefix="/api/billing",    tags=["billing"])   # ← NEW

@app.get("/api/debug/groq")
async def debug_groq():
    from replies.classifier import test_groq_connection
    return await test_groq_connection()

@app.get("/health")
async def health():
    return {"status": "ok", "service": "SalesFlow API", "version": "4.3.0"}