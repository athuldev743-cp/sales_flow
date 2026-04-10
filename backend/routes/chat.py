from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime
from bson import ObjectId
from auth_utils import get_current_user
from database import get_db
from rag.embedder import retrieve_context
from replies.classifier import groq_chat   # reuse shared groq_chat — no duplicate client
from config import settings
from typing import Optional

router = APIRouter()


class ChatMessage(BaseModel):
    message:      str
    session_id:   str = "default"
    reply_id:     Optional[str] = ""   # FIX: was "context_note: str" — now a real reply _id


SYSTEM_PROMPT = """You are a senior sales executive assistant built into SalesFlow. \
You work directly with the user, helping them close deals, handle replies, and run outreach campaigns.

Your personality:
- Direct and confident, like a seasoned sales pro talking to a colleague
- Brief but complete — no fluff, no filler phrases like "Great question!" or "Certainly!"
- When writing emails or messages, you write them ready-to-send
- You know the user's company, their product, their target audience, and their goals from their profile

USER PROFILE CONTEXT:
{context}

OPEN REPLY THREAD (if any):
{thread_context}

Rules:
- Never say "I'm just an AI" or apologise for being an AI
- Never use hollow openers — just get to the answer
- When drafting emails, use {{lead_name}} and {{lead_company}} as placeholders
- Keep replies under 200 words unless the user asks for something long
- If the user pastes a lead reply, help craft a response immediately
- If an open reply thread is shown above, your advice and drafts must be consistent \
with what has already been sent in that thread"""


def _build_thread_context(reply_doc: dict) -> str:
    """Turn a reply document into a plain-text thread summary for the system prompt."""
    if not reply_doc:
        return "No specific reply is open."

    lines = [
        f"Lead: {reply_doc.get('from_name', '')} <{reply_doc.get('from_email', '')}>",
        f"Company: {reply_doc.get('lead_company', 'unknown')}",
        f"Classification: {reply_doc.get('classification', 'unknown')}",
        f"Status: {reply_doc.get('status', 'pending')}",
        "",
        f"Lead's message:\n{reply_doc.get('body', '')[:600]}",
    ]

    if reply_doc.get("sent_body"):
        lines += ["", f"Last reply we sent:\n{reply_doc['sent_body'][:400]}"]
    elif reply_doc.get("draft_body"):
        lines += ["", f"Current AI draft (not yet sent):\n{reply_doc['draft_body'][:400]}"]

    return "\n".join(lines)


@router.post("/message")
async def chat(
    body:         ChatMessage,
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    user_id = str(current_user["_id"])

    # ── 1. RAG context from Chroma ────────────────────────────────────────────
    context = retrieve_context(user_id, body.message, top_k=3)

    # ── 2. Fetch open reply for thread context ────────────────────────────────
    # FIX: reply_id is now a real MongoDB _id; fetch the doc and build context
    reply_doc = None
    if body.reply_id:
        try:
            reply_doc = await db.replies.find_one({
                "_id":     ObjectId(body.reply_id),
                "user_id": user_id,
            })
        except Exception:
            pass   # invalid ObjectId — ignore silently

    thread_context = _build_thread_context(reply_doc)

    # ── 3. Load chat session history ──────────────────────────────────────────
    # FIX: if reply_id changed, start a fresh session scoped to that reply
    # so the chat doesn't bleed history across different leads
    effective_session = body.session_id
    if body.reply_id:
        effective_session = f"reply_{body.reply_id}"

    session = await db.chat_sessions.find_one({
        "user_id":    user_id,
        "session_id": effective_session,
    })
    history = (session["history"] if session else [])[-10:]

    # ── 4. If this thread has autopilot-sent messages, seed history from DB ───
    # FIX: when opening a reply for the first time, inject the real sent history
    # so the chat AI knows what was already said — even before the user typed anything
    if body.reply_id and not session and reply_doc:
        thread_replies = await db.replies.find(
            {"user_id": user_id, "thread_id": reply_doc.get("thread_id")}
        ).sort("created_at", 1).to_list(20)

        seeded = []
        for r in thread_replies:
            if r.get("body"):
                seeded.append({
                    "role":    "user",
                    "content": f"[Lead]: {r['body'][:400]}",
                })
            if r.get("status") == "sent" and r.get("sent_body"):
                seeded.append({
                    "role":    "assistant",
                    "content": f"[Sent by autopilot]: {r['sent_body'][:400]}",
                })
        history = seeded[-10:]   # cap at 10 turns

    # ── 5. Build messages and call GROQ ──────────────────────────────────────
    system = SYSTEM_PROMPT.format(
        context        = context or "No profile context found yet.",
        thread_context = thread_context,
    )

    messages = (
        [{"role": "system", "content": system}]
        + history
        + [{"role": "user", "content": body.message}]
    )

    # FIX: reuse groq_chat from classifier (shared retry logic + model fallback)
    reply_text = await groq_chat(messages, max_tokens=700)
    if not reply_text:
        raise HTTPException(status_code=500, detail="LLM call failed")

    # ── 6. Persist session ────────────────────────────────────────────────────
    new_history = history + [
        {"role": "user",      "content": body.message},
        {"role": "assistant", "content": reply_text},
    ]
    await db.chat_sessions.update_one(
        {"user_id": user_id, "session_id": effective_session},
        {"$set": {
            "history":    new_history,
            "reply_id":   body.reply_id or None,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    return {"reply": reply_text, "session_id": effective_session}


@router.delete("/session")
async def clear_session(
    session_id:   str = "default",
    current_user: dict = Depends(get_current_user),
    db            = Depends(get_db),
):
    """Clear a chat session (useful when switching to a different reply)."""
    await db.chat_sessions.delete_one({
        "user_id":    str(current_user["_id"]),
        "session_id": session_id,
    })
    return {"message": "Session cleared"}


@router.get("/ping")
async def ping():
    return {"ok": True}