from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from datetime import datetime
from bson import ObjectId
from database import get_db
from auth_utils import get_current_user
from leads.lead_db import Lead, SessionLocal
from replies.gmail_poller import fetch_replies, send_reply, mark_as_read
from replies.classifier import classify_reply, draft_reply, should_continue_conversation, draft_closing_message
from campaigns.gmail_sender import refresh_access_token
from config import settings
from pydantic import BaseModel
from typing import Optional
import asyncio
import logging
import httpx
from meetings.utils import auto_create_meeting

logger = logging.getLogger(__name__)
router = APIRouter()

UNSUBSCRIBE_KEYWORDS = [
    "unsubscribe", "remove me", "opt out", "opt-out",
    "don't contact", "do not contact", "please remove",
    "take me off", "remove from list",
]


class SendReplyRequest(BaseModel):
    reply_id: str
    body:     str
    subject:  Optional[str] = None


class UpdateReplyRequest(BaseModel):
    draft_body: str


def find_lead_by_email(email: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.email.like(f"%{email}%")).first()
        if lead:
            return {
                "id":               lead.id,
                "company_name":     lead.company_name,
                "contact_name":     lead.contact_name,
                "email":            lead.email,
                "business_details": lead.business_details,
            }
        return None
    finally:
        db.close()


def update_lead_status(lead_id: int, status: str):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if lead:
            lead.status = status
            db.commit()
    finally:
        db.close()


async def get_fresh_access_token(user: dict, db) -> str:
    access_token = user.get("google_access_token")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://www.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if r.status_code == 200:
                return access_token
    except Exception:
        pass

    new_token = await refresh_access_token(
        user.get("google_refresh_token"),
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_CLIENT_SECRET,
    )
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"google_access_token": new_token}}
    )
    return new_token


async def process_reply(reply: dict, user: dict, db) -> dict:
    body = reply.get("body", "")
    from_email = reply.get("from_email", "")
    thread_id = reply.get("thread_id", "")
    user_id = str(user["_id"])

    # ── 1. Unsubscribe detection ──────────────────────────────────────────────
    def _is_unsubscribe_intent(text: str) -> bool:
        import re
        clean_lines = [
            ln for ln in text.splitlines()
            if not ln.strip().startswith(">")
            and not ln.strip().startswith("On ")
            and "all rights reserved" not in ln.lower()
            and "© 2026" not in ln
            and ln.strip().lower() not in ("unsubscribe",)
        ]
        clean_top = " ".join(clean_lines).lower()[:400]
        return any(re.search(rf"\b{re.escape(kw)}\b", clean_top) for kw in UNSUBSCRIBE_KEYWORDS)

    is_unsubscribe = _is_unsubscribe_intent(body)

    # ── 2. Parallel: classify + fetch thread history + fetch campaign ─────────
    # All three are independent — fire together, collect once
    async def _fetch_thread_history():
        return await db.replies.find(
            {"user_id": user_id, "thread_id": thread_id}
        ).sort("created_at", 1).to_list(30)

    async def _fetch_campaign():
        return await db.campaigns.find_one({
            "user_id":     user_id,
            "leads.email": from_email,
        })

    if is_unsubscribe:
        # Skip classify LLM call — result is already known
        classification_data = {
            "classification": "unsubscribe",
            "confidence":     "high",
            "summary":        "Requested removal from list",
            "hot_lead":       False,
        }
        existing_replies, campaign = await asyncio.gather(
            _fetch_thread_history(),
            _fetch_campaign(),
        )
    else:
        # Run all three in parallel
        classification_data, existing_replies, campaign = await asyncio.gather(
            classify_reply(body),
            _fetch_thread_history(),
            _fetch_campaign(),
        )

    # ── 3. Lead info + status update ─────────────────────────────────────────
    lead_info = find_lead_by_email(from_email)
    if lead_info:
        new_status = "hot" if classification_data["hot_lead"] else "replied"
        update_lead_status(lead_info["id"], new_status)

    # ── 4. Build thread history for LLM context ───────────────────────────────
    original_body = campaign.get("body", "") if campaign else ""

    thread_history = []
    for prev in existing_replies:
        if prev.get("body"):
            thread_history.append({
                "role":    "user",
                "content": f"[{prev.get('from_name', 'Lead')}]: {prev['body'][:500]}"
            })
        if prev.get("status") == "sent" and prev.get("sent_body"):
            thread_history.append({
                "role":    "assistant",
                "content": prev["sent_body"]
            })

    # Append current incoming message
    thread_history.append({
        "role":    "user",
        "content": f"[{reply.get('from_name', 'Lead')}]: {body[:700]}"
    })

    # ── 5. Thread mode + duplicate-send guard (parallel) ─────────────────────
    async def _fetch_last_sent():
        return await db.replies.find_one(
            {"user_id": user_id, "thread_id": thread_id, "status": "sent"},
            sort=[("sent_at", -1)]
        )

    async def _fetch_user_settings():
        return await db.users.find_one({"_id": ObjectId(user_id)})

    # Run continue-check, last-sent lookup, and user settings in parallel
    decision, our_last_reply, user_settings = await asyncio.gather(
        should_continue_conversation(
            conversation_history=thread_history,
            classification=classification_data["classification"],
            last_lead_message=body,
        ),
        _fetch_last_sent(),
        _fetch_user_settings(),
    )

    # Duplicate send guard
    recent_sent = False
    if our_last_reply and our_last_reply.get("sent_at"):
        seconds_since = (datetime.utcnow() -
                         our_last_reply["sent_at"]).total_seconds()
        if seconds_since < 60:
            recent_sent = True
            logger.info(
                f"Thread {thread_id}: skipping — replied {int(seconds_since)}s ago")

    # Thread mode from latest reply
    thread_mode = "ai"
    if existing_replies:
        thread_mode = existing_replies[-1].get("mode", "ai")

    # Autopilot setting
    autopilot = user_settings.get(
        "auto_pilot", False) if user_settings else False

    # ── 6. Profile context ────────────────────────────────────────────────────
    profile = user.get("profile", {}) or {}
    sender_name = user.get("full_name", "")
    sender_co = profile.get("company_name", "")

    # ── 7. Draft the reply ────────────────────────────────────────────────────
    if not decision["continue"] and decision["action"] in ["confirm_and_close", "send_final_close"]:
        draft = await draft_closing_message(
            sender_name=sender_name,
            sender_company=sender_co,
            lead_name=reply.get("from_name", ""),
            lead_company=lead_info["company_name"] if lead_info else "",
            action=decision["action"],
            conversation_history=thread_history,
            user_profile=profile,
        )
    else:
        draft = await draft_reply(
            original_email_body=original_body,
            reply_body=body,
            classification=classification_data["classification"],
            sender_name=sender_name,
            sender_company=sender_co,
            lead_name=lead_info["contact_name"] if lead_info else reply.get(
                "from_name", ""),
            lead_company=lead_info["company_name"] if lead_info else "",
            user_profile=profile,
            conversation_history=thread_history,
        )

    # ── 8. Autopilot send ─────────────────────────────────────────────────────
    safe_classifications = ["interested",
                            "question", "meeting_request", "other"]
    reply_status = "pending"
    sent_body = None

    should_auto_send = (
        autopilot
        and not recent_sent
        and not is_unsubscribe
        and thread_mode != "manual"
        and classification_data["classification"] in safe_classifications
        and (decision["continue"] or decision["action"] in ["confirm_and_close", "send_final_close"])
        and draft
    )

    if should_auto_send:
        try:
            access_token = await get_fresh_access_token(user, db)
            auto_result = await send_reply(
                access_token=access_token,
                thread_id=thread_id,
                to_email=from_email,
                from_email=user.get("email"),
                from_name=sender_name,
                subject=reply.get("subject", ""),
                body=draft,
                in_reply_to_message_id=reply.get("message_id"),
            )
            if auto_result.get("success"):
                reply_status = "sent"
                sent_body = draft
                logger.info(
                    f"Auto-pilot sent reply to {from_email} in thread {thread_id}")

                # Sync to chat session
                try:
                    chat_session_id = f"reply_{thread_id}"
                    chat_session = await db.chat_sessions.find_one({
                        "user_id":    user_id,
                        "session_id": chat_session_id,
                    })
                    existing_chat = chat_session["history"] if chat_session else [
                    ]
                    updated_chat = existing_chat + [
                        {"role": "user",
                            "content": f"[Lead replied]: {body[:400]}"},
                        {"role": "assistant",
                            "content": f"[Autopilot sent]: {draft[:400]}"},
                    ]
                    await db.chat_sessions.update_one(
                        {"user_id": user_id, "session_id": chat_session_id},
                        {"$set": {
                            "history": updated_chat[-20:], "updated_at": datetime.utcnow()}},
                        upsert=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not sync autopilot send to chat session: {e}")
            else:
                logger.warning(
                    f"Auto-pilot send failed: {auto_result.get('error')}")
        except Exception as e:
            logger.error(f"Auto-pilot error: {e}")

    # ── 9. Escalation for hot meeting requests (once, no duplicate) ───────────
    if classification_data["hot_lead"] and classification_data["classification"] == "meeting_request":
        await db.escalations.insert_one({
            "user_id":      user_id,
            "from_email":   from_email,
            "from_name":    reply.get("from_name"),
            "lead_company": lead_info["company_name"] if lead_info else "",
            "lead_id":      lead_info["id"] if lead_info else None,
            "summary":      classification_data["summary"],
            "channel":      "whatsapp",
            "status":       "pending",
            "created_at":   datetime.utcnow(),
        })

    # ── 10. Auto-create meeting + send calendar invite email ──────────────────
    #
    # Triggers:
    #   a) Lead explicitly requests a meeting  → classification == "meeting_request"
    #   b) Lead confirms a proposed time       → decision["action"] == "confirm_and_close"
    #      (only when conversation was meeting-related, not every close)
    #
    meeting_id = None
    should_create_meeting = (
        classification_data["classification"] == "meeting_request"
        or (
            decision.get("action") == "confirm_and_close"
            # ← only meeting_request, not "interested"
            and classification_data["classification"] == "meeting_request"
        )
    )

    if should_create_meeting:
        try:
            meeting_id = await auto_create_meeting(
                db=db,
                user_id=user_id,
                reply_data={
                    "from_email": from_email,
                    "thread_id":  thread_id,
                },
            )

            if meeting_id:
                logger.info(
                    f"Meeting auto-created: {meeting_id} for {from_email}")

                # ── Send the actual calendar invite email to the lead ─────────
                try:
                    meeting_doc = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
                    scheduled_at = meeting_doc.get("scheduled_at")
                    meeting_link = meeting_doc.get("meeting_link", "")
                    lead_name = reply.get("from_name", "there")

                    # Format the datetime nicely, e.g. "Wednesday, 16 April 2026 at 10:00 UTC"
                    time_str = (
                        scheduled_at.strftime("%A, %d %B %Y at %H:%M UTC")
                        if scheduled_at else "a time we discussed"
                    )

                    invite_lines = [
                        f"Hi {lead_name},",
                        "",
                        "Great news — your meeting has been confirmed! Here are the details:",
                        "",
                        f"  📅  Date & Time : {time_str}",
                        f"  ⏱  Duration    : {meeting_doc.get('duration_minutes', 30)} minutes",
                    ]
                    if meeting_link:
                        invite_lines.append(
                            f"  🔗  Join Link   : {meeting_link}")
                    invite_lines += [
                        "",
                        "Please add this to your calendar. "
                        "If you need to reschedule, just reply to this email.",
                        "",
                        f"Looking forward to speaking with you!",
                        "",
                        f"Best,",
                        sender_name,
                        sender_co,
                    ]

                    invite_body = "\n".join(invite_lines)

                    access_token_cal = await get_fresh_access_token(user, db)
                    cal_result = await send_reply(
                        access_token=access_token_cal,
                        thread_id=thread_id,
                        to_email=from_email,
                        from_email=user.get("email"),
                        from_name=sender_name,
                        subject=f"📅 Meeting Confirmed: {time_str}",
                        body=invite_body,
                        in_reply_to_message_id=reply.get("message_id"),
                    )

                    if cal_result.get("success"):
                        logger.info(
                            f"Calendar invite email sent to {from_email}")
                        # Record that the invite was dispatched on the meeting doc
                        await db.meetings.update_one(
                            {"_id": ObjectId(meeting_id)},
                            {"$set": {
                                "invite_sent":    True,
                                "invite_sent_at": datetime.utcnow(),
                            }}
                        )
                    else:
                        logger.warning(
                            f"Calendar invite send failed: {cal_result.get('error')}")

                except Exception as e:
                    logger.error(f"Calendar invite email error: {e}")

        except Exception as e:
            logger.error(f"auto_create_meeting failed: {e}")

    # ── Return ────────────────────────────────────────────────────────────────
    return {
        "message_id":        reply.get("message_id"),
        "thread_id":         thread_id,
        "from_email":        from_email,
        "from_name":         reply.get("from_name", ""),
        "subject":           reply.get("subject", ""),
        "body":              body,
        "timestamp":         reply.get("timestamp"),
        "classification":    classification_data["classification"],
        "confidence":        classification_data["confidence"],
        "summary":           classification_data["summary"],
        "hot_lead":          classification_data["hot_lead"],
        "draft_body":        draft,
        "sent_body":         sent_body,
        "lead_id":           lead_info["id"] if lead_info else None,
        "lead_company":      lead_info["company_name"] if lead_info else "",
        "status":            reply_status,
        "mode":              thread_mode,
        "conversation_turn": len(thread_history),
        "should_continue":   decision["continue"],
        "close_reason":      decision.get("reason") if not decision["continue"] else None,
        "auto_pilot":        autopilot,
        "meeting_id":        meeting_id,
        "created_at":        datetime.utcnow(),
        "updated_at":        datetime.utcnow(),
        "sent_at":           datetime.utcnow() if reply_status == "sent" else None,
    }

# ── Background auto-sync ──────────────────────────────────────────────────────


async def background_sync_all_users(db):
    while True:
        try:
            users = await db.users.find(
                {"gmail_connected": True, "google_refresh_token": {"$exists": True}}
            ).to_list(200)

            for user in users:
                try:
                    await _sync_user_replies(user, db)
                except Exception as e:
                    logger.error(
                        f"Auto-sync failed for {user.get('email')}: {e}")

        except Exception as e:
            logger.error(f"Background sync loop error: {e}")

        await asyncio.sleep(300)


async def _sync_user_replies(user: dict, db):
    user_id = str(user["_id"])
    logger.info(f"SYNC START | user_id={user_id} | db={db}")

    # Fetch campaigns for this user
    campaigns = await db.campaigns.find(
        {"user_id": user_id},
        {"leads": 1, "body": 1}
    ).to_list(100)

    # --- DEBUG: Initial Check ---
    logger.info(f"SYNC | user_id: {user_id}")
    logger.info(f"SYNC | campaigns found in DB: {len(campaigns)}")

    sent_to_emails = set()
    sent_message_ids = []

    for campaign in campaigns:
        leads = campaign.get("leads", [])
        # DEBUG: Check what leads look like for this campaign
        if leads:
            sample = [(l.get('email'), l.get('status')) for l in leads[:3]]
            logger.info(
                f"SYNC | Campaign {campaign.get('_id')} sample leads: {sample}")

        for lead in leads:
            if lead.get("status") == "sent" and lead.get("email"):
                sent_to_emails.add(lead["email"].lower().strip())
            if lead.get("gmail_message_id"):
                sent_message_ids.append(lead["gmail_message_id"])

    # --- DEBUG: Post-Collection ---
    logger.info(f"SYNC | Final sent_to_emails count: {len(sent_to_emails)}")
    logger.info(
        f"SYNC | Final sent_message_ids count: {len(sent_message_ids)}")

    if not sent_to_emails and not sent_message_ids:
        logger.info(
            "SYNC | Early exit: No 'sent' leads found. Check lead status cases.")
        return 0

    # Handle metadata and timestamp
    meta = await db.reply_meta.find_one({"user_id": user_id})
    since = meta.get("last_sync_ts") if meta else None

    # Token Refresh
    try:
        new_token = await refresh_access_token(
            user.get("google_refresh_token"),
            settings.GOOGLE_CLIENT_ID,
            settings.GOOGLE_CLIENT_SECRET,
        )
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"google_access_token": new_token}}
        )
        access_token = new_token
    except Exception as e:
        logger.error(f"SYNC | Token refresh failed: {e}")
        access_token = user.get("google_access_token")

    # Fetching from Gmail
    raw_replies = await fetch_replies(
        access_token=access_token,
        campaign_emails=list(sent_to_emails),
        sent_message_ids=sent_message_ids,
        our_email=user.get("email", ""),
        since_timestamp=since,
    )
    logger.info(f"SYNC | raw_replies found from Gmail: {len(raw_replies)}")

    new_count = 0
    for reply in raw_replies:
        msg_id = reply.get("message_id")
        from_email = reply.get("from_email", "").lower().strip()

        # Filter out non-campaign emails or self-replies
        if from_email not in sent_to_emails:
            continue
        if user.get("email", "").lower() in from_email:
            continue

        # Avoid duplicates
        exists = await db.replies.find_one({"message_id": msg_id, "user_id": user_id})
        if exists:
            continue

        # Process and save the reply
        processed = await process_reply(reply, user, db)
        processed["user_id"] = user_id
        await db.replies.insert_one(processed)

        # Mark as read in Gmail
        try:
            await mark_as_read(access_token, msg_id)
        except Exception as e:
            logger.warning(f"SYNC | Could not mark {msg_id} as read: {e}")

        new_count += 1

    # Update Sync Metadata
    await db.reply_meta.update_one(
        {"user_id": user_id},
        {"$set": {
            "last_sync_ts": int(datetime.utcnow().timestamp() * 1000),
            "last_sync": datetime.utcnow(),
        }},
        upsert=True
    )

    logger.info(f"SYNC | Finished. New replies added: {new_count}")
    return new_count


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/sync")
async def sync_replies(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    if not current_user.get("gmail_connected"):
        raise HTTPException(status_code=400, detail="Gmail not connected")
    try:
        new_count = await _sync_user_replies(current_user, db)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Gmail sync failed: {str(e)}")
    return {"synced": new_count, "message": f"Found {new_count} new replies"}


@router.get("/list")
async def list_replies(
    classification: Optional[str] = None,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    query = {"user_id": user_id}
    if classification:
        query["classification"] = classification

    replies = await db.replies.find(query).sort("created_at", -1).limit(50).to_list(50)

    return [{
        "id":             str(r["_id"]),
        "from_email":     r.get("from_email"),
        "from_name":      r.get("from_name"),
        "subject":        r.get("subject"),
        "body":           r.get("body", "")[:300],
        "classification": r.get("classification"),
        "confidence":     r.get("confidence"),
        "summary":        r.get("summary"),
        "hot_lead":       r.get("hot_lead", False),
        "draft_body":     r.get("draft_body"),
        "lead_company":   r.get("lead_company"),
        "status":         r.get("status", "pending"),
        "mode":           r.get("mode", "ai"),
        "sent_body":      r.get("sent_body"),
        "created_at":     r.get("created_at").isoformat() if r.get("created_at") else None,
    } for r in replies]


@router.get("/stats")
async def reply_stats(
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    total = await db.replies.count_documents({"user_id": user_id})
    interested = await db.replies.count_documents({"user_id": user_id, "classification": "interested"})
    meetings = await db.replies.count_documents({"user_id": user_id, "classification": "meeting_request"})
    hot_leads = await db.replies.count_documents({"user_id": user_id, "hot_lead": True})
    unsubscribes = await db.replies.count_documents({"user_id": user_id, "classification": "unsubscribe"})

    return {
        "total":        total,
        "interested":   interested,
        "meetings":     meetings,
        "hot_leads":    hot_leads,
        "unsubscribes": unsubscribes,
        "auto_pilot":   current_user.get("auto_pilot", False),
    }


@router.put("/{reply_id}/draft")
async def update_draft(
    reply_id:    str,
    data:        UpdateReplyRequest,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    await db.replies.update_one(
        {"_id": ObjectId(reply_id), "user_id": str(current_user["_id"])},
        {"$set": {"draft_body": data.draft_body, "updated_at": datetime.utcnow()}}
    )
    return {"message": "Draft updated"}


@router.post("/{reply_id}/send")
async def send_reply_route(
    reply_id:    str,
    data:        SendReplyRequest,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    reply = await db.replies.find_one({
        "_id":     ObjectId(reply_id),
        "user_id": str(current_user["_id"])
    })
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")

    try:
        access_token = await get_fresh_access_token(current_user, db)
    except Exception:
        access_token = current_user.get("google_access_token")

    result = await send_reply(
        access_token=access_token,
        thread_id=reply.get("thread_id"),
        to_email=reply.get("from_email"),
        from_email=current_user.get("email"),
        from_name=current_user.get("full_name", ""),
        subject=reply.get("subject", ""),
        body=data.body,
        in_reply_to_message_id=reply.get("message_id"),
    )

    if result["success"]:
        await db.replies.update_one(
            {"_id": ObjectId(reply_id)},
            {"$set": {
                "status":     "sent",
                "sent_at":    datetime.utcnow(),
                "sent_body":  data.body,
                "updated_at": datetime.utcnow(),
            }}
        )

        # FIX: sync manual send into chat session too
        try:
            user_id = str(current_user["_id"])
            chat_session_id = f"reply_{reply.get('thread_id')}"
            chat_session = await db.chat_sessions.find_one({
                "user_id":    user_id,
                "session_id": chat_session_id,
            })
            existing = chat_session["history"] if chat_session else []
            updated = existing + [
                {"role": "user",
                    "content": f"[Lead]: {reply.get('body', '')[:400]}"},
                {"role": "assistant",
                    "content": f"[Manually sent]: {data.body[:400]}"},
            ]
            await db.chat_sessions.update_one(
                {"user_id": user_id, "session_id": chat_session_id},
                {"$set": {"history": updated[-20:],
                          "updated_at": datetime.utcnow()}},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"Could not sync manual send to chat session: {e}")

        return {"message": "Reply sent successfully"}
    else:
        raise HTTPException(
            status_code=500, detail=f"Failed to send: {result.get('error')}")


@router.post("/{reply_id}/discard")
async def discard_reply(
    reply_id:    str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    await db.replies.update_one(
        {"_id": ObjectId(reply_id), "user_id": str(current_user["_id"])},
        {"$set": {"status": "discarded", "updated_at": datetime.utcnow()}}
    )
    return {"message": "Reply discarded"}


@router.post("/autopilot")
async def toggle_autopilot(
    enabled:     bool,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"auto_pilot": enabled, "updated_at": datetime.utcnow()}}
    )
    return {"message": f"Auto-pilot {'enabled' if enabled else 'disabled'}", "enabled": enabled}


class RegenerateRequest(BaseModel):
    instruction: Optional[str] = ""


@router.post("/{reply_id}/regenerate")
async def regenerate_draft(
    reply_id: str,
    data:     RegenerateRequest,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    reply = await db.replies.find_one({"_id": ObjectId(reply_id), "user_id": user_id})
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")

    profile = current_user.get("profile", {}) or {}
    sender_name = current_user.get("full_name", "")
    sender_co = profile.get("company_name", "")

    original_body = ""
    campaign = await db.campaigns.find_one({
        "user_id":     user_id,
        "leads.email": reply.get("from_email"),
    })
    if campaign:
        original_body = campaign.get("body", "")

    # FIX: load thread history so regenerate has full conversation context
    thread_history = []
    existing_replies = await db.replies.find(
        {"user_id": user_id, "thread_id": reply.get("thread_id")}
    ).sort("created_at", 1).to_list(30)

    for prev in existing_replies:
        if prev.get("body"):
            thread_history.append({
                "role":    "user",
                "content": f"[{prev.get('from_name', 'Lead')}]: {prev['body'][:500]}"
            })
        if prev.get("status") == "sent" and prev.get("sent_body"):
            thread_history.append({
                "role":    "assistant",
                "content": prev["sent_body"]
            })

    patched_profile = dict(profile)
    if data.instruction and data.instruction.strip():
        existing_desc = profile.get("company_description", "")
        patched_profile["company_description"] = (
            existing_desc +
            f"\n\nIMPORTANT instruction from the sender: {data.instruction.strip()}"
        )

    new_draft = await draft_reply(
        original_email_body=original_body,
        reply_body=reply.get("body", ""),
        classification=reply.get("classification", "other"),
        sender_name=sender_name,
        sender_company=sender_co,
        lead_name=reply.get("from_name", ""),
        lead_company=reply.get("lead_company", ""),
        user_profile=patched_profile,
        # FIX: was missing — AI had no memory of thread
        conversation_history=thread_history,
    )

    await db.replies.update_one(
        {"_id": ObjectId(reply_id)},
        {"$set": {"draft_body": new_draft, "updated_at": datetime.utcnow()}}
    )
    return {"draft_body": new_draft}


@router.post("/{reply_id}/takeover")
async def takeover_reply(
    reply_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    result = await db.replies.update_one(
        {"_id": ObjectId(reply_id), "user_id": str(current_user["_id"])},
        {"$set": {"mode": "manual", "updated_at": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Reply not found")
    return {"message": "Manual mode enabled"}


@router.post("/{reply_id}/resume-ai")
async def resume_ai(
    reply_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    await db.replies.update_one(
        {"_id": ObjectId(reply_id), "user_id": str(current_user["_id"])},
        {"$set": {"mode": "ai", "updated_at": datetime.utcnow()}}
    )
    return {"message": "AI mode resumed"}


@router.get("/{reply_id}")
async def get_reply(
    reply_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    r = await db.replies.find_one({
        "_id":     ObjectId(reply_id),
        "user_id": str(current_user["_id"])
    })
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "id":             str(r["_id"]),
        "from_email":     r.get("from_email"),
        "from_name":      r.get("from_name"),
        "subject":        r.get("subject"),
        "body":           r.get("body", ""),
        "classification": r.get("classification"),
        "summary":        r.get("summary"),
        "hot_lead":       r.get("hot_lead", False),
        "draft_body":     r.get("draft_body"),
        "sent_body":      r.get("sent_body"),
        "lead_company":   r.get("lead_company"),
        "status":         r.get("status", "pending"),
        "mode":           r.get("mode", "ai"),
        # FIX: expose thread_id so frontend can scope chat
        "thread_id":      r.get("thread_id"),
        "created_at":     r.get("created_at").isoformat() if r.get("created_at") else None,
    }
