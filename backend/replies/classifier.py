"""
replies/classifier.py  — no logic changes needed.

groq_chat is already defined here and is now imported by routes/chat.py
to avoid duplicating the GROQ client + model-fallback logic.

All functions exported:
  groq_call               — single-turn prompt
  groq_chat               — multi-turn messages list  ← imported by routes/chat.py
  classify_reply          — classify an incoming email
  draft_reply             — write the AI reply
  should_continue_conversation
  draft_closing_message
"""
import httpx
import logging
from config import settings
from typing import Literal, List
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "moonshotai/kimi-k2-instruct",
]


async def groq_call(prompt: str, max_tokens: int = 500, system: str = None) -> str:
    key = (settings.GROQ_API_KEY or "").strip()
    if not key or len(key) < 10:
        logger.error("GROQ_API_KEY missing")
        return ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=25.0) as client:
        for model in GROQ_MODELS:
            try:
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "messages": messages}
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip()
                    logger.info(f"Groq OK model={model}")
                    return content
                logger.warning(f"Groq {model} HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"Groq {model} error: {e}")
    return ""


async def groq_chat(messages: list, max_tokens: int = 400) -> str:
    key = (settings.GROQ_API_KEY or "").strip()
    if not key:
        return ""
    async with httpx.AsyncClient(timeout=25.0) as client:
        for model in GROQ_MODELS:
            try:
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "messages": messages}
                )
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.error(f"groq_chat {model}: {e}")
    return ""


async def classify_reply(reply_body: str) -> dict:
    prompt = f"""Classify this email reply into exactly ONE category.

EMAIL:
{reply_body[:800]}

CATEGORIES:
- interested: Shows curiosity or positive interest
- meeting_request: Explicitly wants to schedule a call or meeting
- question: Has a specific question about the product/service
- not_interested: Clearly declines or says not relevant
- out_of_office: Auto-reply about being away
- unsubscribe: Wants to be removed from list
- other: Doesn't fit any category

Respond in EXACTLY this format:
CLASSIFICATION: [category]
CONFIDENCE: [high/medium/low]
SUMMARY: [one sentence]
HOT_LEAD: [yes/no]"""

    result = await groq_call(prompt, max_tokens=120)

    classification = "other"
    confidence     = "low"
    summary        = "Could not classify"
    hot_lead       = False

    for line in result.split("\n"):
        line = line.strip()
        if line.startswith("CLASSIFICATION:"):
            val = line.split(":", 1)[1].strip().lower()
            if val in ["interested", "meeting_request", "question",
                       "not_interested", "out_of_office", "unsubscribe", "other"]:
                classification = val
        elif line.startswith("CONFIDENCE:"):
            confidence = line.split(":", 1)[1].strip().lower()
        elif line.startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("HOT_LEAD:"):
            hot_lead = line.split(":", 1)[1].strip().lower() == "yes"

    return {"classification": classification, "confidence": confidence,
            "summary": summary, "hot_lead": hot_lead}


def _build_system_prompt(sender_name: str, sender_company: str, user_profile: dict) -> str:
    description  = user_profile.get("company_description", "")
    target       = user_profile.get("target_audience", "")
    goal         = user_profile.get("goal", "book discovery calls")
    purpose      = user_profile.get("purpose", "")
    website      = user_profile.get("website", "")
    company_name = user_profile.get("company_name", sender_company)

    return f"""You are {sender_name}, a senior sales executive at {company_name}.

COMPANY: {description}
TARGET CUSTOMERS: {target}
GOAL: {goal}
PURPOSE: {purpose}
{f"WEBSITE: {website}" if website else ""}

YOUR PERSONA — strict rules:
1. You are a real human — never sound like a bot or template
2. Warm, confident, direct — like a top B2B sales executive
3. NEVER use: "I hope this email finds you well", "As per my last email", "Circling back", "Just following up"
4. Keep replies SHORT — under 100 words unless answering a technical question
5. Always end with ONE clear next step
6. Never invent statistics or fake case studies
7. When warm — offer exactly 2 specific time slots (e.g. "Tuesday 3pm or Thursday 11am")
8. Match the lead's energy and tone
9. Never repeat the same thing twice across turns — always move forward
10. You remember everything said earlier in this conversation

CRITICAL CLOSING RULES:
- If lead says meeting is confirmed / "sounds good" / "let's do it" → send ONE warm confirmation, mention you're sending a calendar invite, then STOP. Do not ask anything else.
- If lead clearly says not interested / "not now" / "no thanks" → acknowledge graciously in ONE message, leave door open, then STOP contacting.
- After 3 of YOUR replies with no booking → send ONE final "closing the loop" message, then STOP.
- Unsubscribe request → brief apology, confirm removal in ONE message, never contact again.
- You NEVER send the same message twice. Each reply must move the conversation forward."""


async def draft_reply(
    original_email_body:  str,
    reply_body:           str,
    classification:       str,
    sender_name:          str,
    sender_company:       str,
    lead_name:            str,
    lead_company:         str,
    user_profile:         dict,
    conversation_history: list = None,
) -> str:
    first = lead_name.split()[0] if lead_name else "there"

    # 1. Hard-coded no-AI cases — fast and consistent
    if classification == "unsubscribe":
        return (f"Hi {first},\n\nUnderstood — I'll remove you right away. "
                f"Sorry for the interruption.\n\nBest,\n{sender_name}")
    if classification == "not_interested":
        return (f"Hi {first},\n\nTotally understand — I won't follow up again. "
                f"If things change, feel free to reach out.\n\n{sender_name}")
    if classification == "out_of_office":
        return (f"Hi {first},\n\nThanks for the heads up — I'll follow up when "
                f"you're back.\n\nBest,\n{sender_name}")

    # 2. Build Prompt Context
    system   = _build_system_prompt(sender_name, sender_company, user_profile)
    messages = [{"role": "system", "content": system}]

    if original_email_body:
        messages.append({
            "role":    "assistant",
            "content": f"[My original cold email to {first} at {lead_company}]\n\n{original_email_body[:500]}"
        })

    if conversation_history:
        for turn in conversation_history:
            role    = turn.get("role", "user")
            content = turn.get("content", "").strip()
            if content:
                messages.append({"role": role, "content": content})
    else:
        messages.append({
            "role":    "user",
            "content": f"[{first} at {lead_company} replied]\n\n{reply_body[:700]}"
        })

    # 3. Define Task
    task_map = {
        "interested":      f"They showed genuine interest. Acknowledge warmly and offer 2 specific time slots this week. Under 80 words.",
        "meeting_request": f"They want to meet. Be enthusiastic. Give 2 specific time slots. Make it easy to say yes. Under 70 words.",
        "question":        f"Answer their question clearly and concisely. Then suggest a quick call to cover more. Under 100 words.",
        "other":           f"Respond naturally. Be warm. Move toward a discovery call. Under 80 words.",
    }
    task = task_map.get(classification, task_map["other"])

    messages.append({
        "role":    "user",
        "content": (f"Write your next reply as {sender_name}.\n"
                    f"Task: {task}\n"
                    f"Write ONLY the email body — no subject line, no sign-off block. "
                    f"Start with 'Hi {first},'")
    })

    # 4. AI Generation
    draft = await groq_chat(messages, max_tokens=350)

    # 5. Hallucination Guard
    # If the AI starts looping (low unique word ratio), we clear the draft to trigger fallback
    if draft:
        words = draft.split()
        if len(words) > 8:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.4:
                draft = "" 

    # 6. Fallbacks (Triggers if Groq fails OR if guard cleared the draft)
    if not draft:
        if classification == "meeting_request":
            return (f"Hi {first},\n\nLet's lock it in.\n\n"
                    f"Tuesday 3pm or Wednesday 11am — which works?\n\n{sender_name}")
        if classification == "interested":
            return (f"Hi {first},\n\nGreat to hear — I think we can really help {lead_company}.\n\n"
                    f"Tuesday 3pm or Thursday 2pm for a quick 20-min call?\n\n{sender_name}")
        if classification == "question":
            return (f"Hi {first},\n\nHappy to answer that properly on a quick call.\n\n"
                    f"Do you have 20 minutes this week?\n\n{sender_name}")
        
        return (f"Hi {first},\n\nThanks for getting back to me.\n\n"
                f"Would a 20-minute call this week make sense? "
                f"Tuesday 3pm or Wednesday 11am works for me.\n\n{sender_name}")

    return draft


async def should_continue_conversation(
    conversation_history: list,
    classification:       str,
    last_lead_message:    str = "",
) -> dict:
    # Hard stops — never reply to these
    if classification in ["unsubscribe", "not_interested"]:
        return {"continue": False, "reason": "opted out", "action": "close"}

    # Count only our sent replies (assistant turns)
    our_turns       = [m for m in conversation_history if m.get("role") == "assistant"]
    our_reply_count = len(our_turns)

    # Meeting booked signals
    booked_signals = [
        "sounds good", "let's do it", "confirmed", "booked", "works for me",
        "see you then", "calendar", "accepted", "perfect", "done", "great",
        "talk then", "see you", "looking forward", "i'll be there", "that works",
        "absolutely", "yes, let's", "yes let's",
    ]
    last_lower = last_lead_message.lower()
    is_booked  = any(s in last_lower for s in booked_signals)

    if is_booked:
        return {"continue": False, "reason": "meeting booked", "action": "confirm_and_close"}

    if our_reply_count >= 3:
        return {"continue": False, "reason": "3 replies without booking", "action": "send_final_close"}

    return {"continue": True, "reason": "active", "action": "reply"}


async def draft_closing_message(
    sender_name:          str,
    sender_company:       str,
    lead_name:            str,
    lead_company:         str,
    action:               str,
    conversation_history: list = None,
    user_profile:         dict = None,
) -> str:
    first = lead_name.split()[0] if lead_name else "there"

    if action == "confirm_and_close":
        system   = _build_system_prompt(sender_name, sender_company, user_profile or {})
        messages = [{"role": "system", "content": system}]

        if conversation_history:
            for turn in conversation_history[-6:]:
                role    = turn.get("role", "user")
                content = turn.get("content", "").strip()
                if content:
                    messages.append({"role": role, "content": content})

        messages.append({
            "role":    "user",
            "content": (
                f"The lead just confirmed the meeting. Write a warm, brief confirmation reply as {sender_name}.\n"
                f"Rules:\n"
                f"- Confirm the meeting warmly\n"
                f"- Say you're sending a calendar invite now\n"
                f"- Express genuine excitement about speaking\n"
                f"- Under 60 words\n"
                f"- Start with 'Hi {first},'\n"
                f"- Do NOT ask any more questions\n"
                f"- This is the LAST message in this thread"
            )
        })

        draft = await groq_chat(messages, max_tokens=150)
        if draft:
            return draft

        return (f"Hi {first},\n\nPerfect — really looking forward to it.\n\n"
                f"Sending a calendar invite across now. "
                f"Speak soon!\n\nBest,\n{sender_name}")

    if action == "send_final_close":
        system   = _build_system_prompt(sender_name, sender_company, user_profile or {})
        messages = [{"role": "system", "content": system}]

        if conversation_history:
            for turn in conversation_history[-6:]:
                role    = turn.get("role", "user")
                content = turn.get("content", "").strip()
                if content:
                    messages.append({"role": role, "content": content})

        messages.append({
            "role":    "user",
            "content": (
                f"We've had 3+ exchanges without booking a call. Write a professional, warm closing message as {sender_name}.\n"
                f"Rules:\n"
                f"- Acknowledge that the timing probably isn't right\n"
                f"- Leave the door genuinely open for the future — no pressure\n"
                f"- Sound like a real human, not a template\n"
                f"- Under 70 words\n"
                f"- Start with 'Hi {first},'\n"
                f"- This is the FINAL message — do not suggest another call or follow-up"
            )
        })

        draft = await groq_chat(messages, max_tokens=150)
        if draft:
            return draft

        return (f"Hi {first},\n\nI'll leave it here — the timing clearly isn't right "
                f"and I don't want to keep filling your inbox.\n\n"
                f"If things change and {lead_company} ever wants to explore this, "
                f"my door's always open. Wishing you and the team all the best.\n\n"
                f"{sender_name}")

    return ""

def extract_meeting_from_text(text: str):
    """
    Scans the chatbot response for meeting confirmation and returns 
    basic details if found.
    """
    # Look for common confirmation phrases
    confirmation_patterns = [
        r"confirm the meeting on (\w+)",
        r"scheduled for (\w+)",
        r"See you on (\w+)"
    ]
    
    is_confirmed = False
    day_str = "Upcoming"
    
    for pattern in confirmation_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            is_confirmed = True
            day_str = match.group(1)
            break
            
    if is_confirmed:
        return {
            "title": f"Confirmed Meeting ({day_str})",
            "day": day_str
        }
    return None